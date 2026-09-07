"""Request dependencies: unit of work, settings, admin auth, rate limiting."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.container import Container, get_container
from app.db.uow import UnitOfWork


def settings_dep() -> Settings:
    return get_settings()


def container_dep() -> Container:
    return get_container()


def uow_dep(container: Container = Depends(container_dep)) -> Iterator[UnitOfWork]:
    with container.uow() as uow:
        yield uow


class SlidingWindowLimiter:
    """A small in-process limiter.

    Enough for the two things that need one: a public read surface and an admin token.
    A shared limiter across replicas would need the database; the note in ADR-019 says so
    rather than pretending this scales further than it does.
    """

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


_admin_limiter: SlidingWindowLimiter | None = None
_public_limiter: SlidingWindowLimiter | None = None


def _limiters(settings: Settings) -> tuple[SlidingWindowLimiter, SlidingWindowLimiter]:
    global _admin_limiter, _public_limiter
    if _admin_limiter is None:
        _admin_limiter = SlidingWindowLimiter(settings.admin_rate_limit_per_min)
    if _public_limiter is None:
        _public_limiter = SlidingWindowLimiter(settings.public_rate_limit_per_min)
    return _admin_limiter, _public_limiter


def reset_limiters() -> None:
    global _admin_limiter, _public_limiter
    _admin_limiter = _public_limiter = None


def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    settings: Settings = Depends(settings_dep),
) -> str:
    """Guard every mutating endpoint.

    The token protects the source as much as us: an unauthenticated "Run now" is a way to
    aim our crawler at Metacritic and our budget at the LLM (ADR-019 T1).
    """
    expected = settings.admin_token.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints are disabled because ADMIN_TOKEN is not configured.",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token.")

    admin_limiter, _ = _limiters(settings)
    if not admin_limiter.check("admin"):
        raise HTTPException(
            status_code=429,
            detail="Too many admin requests.",
            headers={"Retry-After": str(int(admin_limiter.window_s))},
        )
    return x_admin_token


def public_rate_limit(
    request: Request, settings: Settings = Depends(settings_dep)
) -> None:
    _, public_limiter = _limiters(settings)
    client = request.client.host if request.client else "unknown"
    if not public_limiter.check(client):
        # A 429 without Retry-After tells a client to back off without saying how long,
        # so every client invents its own answer and the worst one wins.
        raise HTTPException(
            status_code=429,
            detail="Too many requests.",
            headers={"Retry-After": str(int(public_limiter.window_s))},
        )
