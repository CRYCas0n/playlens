"""HTTP transport: retries, backoff, circuit breaker, instrumentation.

Everything the adapters share about *talking to the outside world* lives here, so a
service never sees an ``httpx`` object and a change of policy happens in one place.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.domain.errors import CircuitOpen, PermanentError, ProviderError, RetryableError
from app.logging import get_logger

log = get_logger(__name__)


class RateLimiterProtocol(Protocol):
    def acquire(self, tokens: float = 1.0) -> float: ...


@dataclass
class CircuitBreaker:
    """Fail fast while a dependency is down, instead of burning retries on every job."""

    fail_threshold: int = 5
    reset_timeout_s: float = 300.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _clock: Callable[[], float] = field(default=time.monotonic)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_timeout_s:
            return "half_open"
        return "open"

    def before_request(self) -> None:
        if self.state == "open":
            raise CircuitOpen(
                f"circuit open after {self._failures} consecutive failures",
                retry_after_s=self.reset_timeout_s,
            )

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.fail_threshold:
            self._opened_at = self._clock()


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes
    elapsed_ms: int

    def json(self) -> Any:
        import json

        if not self.content:
            # An empty 200 is a provider failure, not an empty result. This is the exact
            # shape of YouTube's timedtext response without a proof-of-origin token, and
            # treating it as "no data" is the classic way to get that wrong (ADR-016).
            raise ProviderError(f"empty body with HTTP {self.status_code} from {self.url}")
        return json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass
class TransportConfig:
    user_agent: str
    timeout_s: float = 20.0
    connect_timeout_s: float = 5.0
    max_retries: int = 5
    backoff_base_s: float = 1.0
    backoff_max_s: float = 32.0
    circuit_fail_threshold: int = 5
    circuit_reset_timeout_s: float = 300.0


RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})


class HttpTransport:
    """A small, deliberately boring HTTP client.

    Boring is the point: every external call in the project goes through the same
    rate limiter, the same retry policy and the same breaker, and every one of them is
    counted, so "what is the source doing to us" is answerable from one place.
    """

    def __init__(
        self,
        config: TransportConfig,
        *,
        rate_limiter: RateLimiterProtocol | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.rate_limiter = rate_limiter
        self.breaker = CircuitBreaker(
            fail_threshold=config.circuit_fail_threshold,
            reset_timeout_s=config.circuit_reset_timeout_s,
        )
        self._sleep = sleep
        self._on_event = on_event
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
            headers={"User-Agent": config.user_agent, "Accept": "application/json, text/*"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_status: frozenset[int] = frozenset(),
    ) -> HttpResponse:
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.config.max_retries:
            attempt += 1
            self.breaker.before_request()
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()

            started = time.monotonic()
            try:
                raw = self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = RetryableError(f"timeout: {exc}")
                self._after_failure(url, attempt, last_error)
                continue
            except httpx.HTTPError as exc:
                last_error = RetryableError(f"network error: {exc}")
                self._after_failure(url, attempt, last_error)
                continue

            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._emit(
                "http.response",
                {"url": str(raw.url), "status": raw.status_code, "elapsed_ms": elapsed_ms},
            )

            if raw.status_code in allow_status or 200 <= raw.status_code < 300:
                self.breaker.on_success()
                return HttpResponse(
                    status_code=raw.status_code,
                    url=str(raw.url),
                    headers=dict(raw.headers),
                    content=raw.content,
                    elapsed_ms=elapsed_ms,
                )

            if raw.status_code in RETRYABLE_STATUS:
                retry_after = _parse_retry_after(raw.headers.get("Retry-After"))
                last_error = RetryableError(
                    f"HTTP {raw.status_code} from {raw.url}", retry_after_s=retry_after
                )
                self._after_failure(url, attempt, last_error, retry_after=retry_after)
                continue

            # 4xx other than the retryable ones: retrying cannot help.
            self.breaker.on_success()
            raise PermanentError(f"HTTP {raw.status_code} from {raw.url}")

        assert last_error is not None
        raise last_error

    def _after_failure(
        self, url: str, attempt: int, error: Exception, *, retry_after: float | None = None
    ) -> None:
        self.breaker.on_failure()
        self._emit("http.retry", {"url": url, "attempt": attempt, "error": str(error)})
        if attempt > self.config.max_retries:
            return
        delay = retry_after if retry_after is not None else self._backoff(attempt)
        self._sleep(delay)

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.config.backoff_max_s, self.config.backoff_base_s * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)


def _parse_retry_after(value: str | None) -> float | None:
    """Respect ``Retry-After``: it is the source telling us exactly how to behave."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
