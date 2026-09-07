from __future__ import annotations

import httpx
import pytest

from app.adapters.http.transport import (
    CircuitBreaker,
    HttpResponse,
    HttpTransport,
    TransportConfig,
)
from app.adapters.metacritic.provider import MetacriticProvider
from app.domain.enums import ReviewKind
from app.domain.errors import CircuitOpen, PermanentError, ProviderError, RetryableError
from tests.fakes import FakeMetacriticSource


def transport(handler, **overrides) -> HttpTransport:
    config = TransportConfig(user_agent="test/1.0", max_retries=overrides.pop("max_retries", 2))
    for key, value in overrides.items():
        setattr(config, key, value)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpTransport(config, client=client, sleep=lambda _s: None)


class TestRetryPolicy:
    def test_retries_on_5xx_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        response = transport(handler).get("https://example.test/x")
        assert response.json() == {"ok": True}
        assert calls["n"] == 3

    def test_gives_up_after_max_retries(self):
        with pytest.raises(RetryableError, match="503"):
            transport(lambda _r: httpx.Response(503)).get("https://example.test/x")

    def test_4xx_is_permanent_and_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(404)

        with pytest.raises(PermanentError, match="404"):
            transport(handler).get("https://example.test/x")
        assert calls["n"] == 1

    def test_429_is_retryable_and_respects_retry_after(self):
        slept: list[float] = []
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json={})

        config = TransportConfig(user_agent="t", max_retries=3)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        HttpTransport(config, client=client, sleep=slept.append).get("https://example.test/x")
        assert slept == [7.0]

    def test_timeout_is_retryable(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(RetryableError, match="timeout"):
            transport(handler).get("https://example.test/x")


class TestEmptyBody:
    def test_empty_200_is_a_provider_error_not_empty_data(self):
        """The exact YouTube timedtext failure mode (ADR-016).

        A zero-length 200 must never be read as "there is nothing here".
        """
        response = HttpResponse(200, "https://x", {}, b"", 1)
        with pytest.raises(ProviderError, match="empty body"):
            response.json()


class TestCircuitBreaker:
    def test_opens_after_threshold_and_fails_fast(self):
        clock = {"t": 0.0}
        breaker = CircuitBreaker(fail_threshold=3, reset_timeout_s=60)
        breaker._clock = lambda: clock["t"]
        for _ in range(3):
            breaker.on_failure()
        assert breaker.state == "open"
        with pytest.raises(CircuitOpen):
            breaker.before_request()

    def test_half_opens_after_the_timeout(self):
        clock = {"t": 0.0}
        breaker = CircuitBreaker(fail_threshold=1, reset_timeout_s=60)
        breaker._clock = lambda: clock["t"]
        breaker.on_failure()
        clock["t"] = 61.0
        assert breaker.state == "half_open"
        breaker.before_request()  # must not raise

    def test_success_closes_it(self):
        breaker = CircuitBreaker(fail_threshold=2)
        breaker.on_failure()
        breaker.on_success()
        breaker.on_failure()
        assert breaker.state == "closed"

    def test_transport_opens_its_breaker_on_repeated_failure(self):
        t = transport(lambda _r: httpx.Response(500), max_retries=0, circuit_fail_threshold=1)
        with pytest.raises(RetryableError):
            t.get("https://example.test/x")
        with pytest.raises(CircuitOpen):
            t.get("https://example.test/x")


class TestUserAgent:
    def test_identifies_us(self):
        seen: dict[str, str] = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, json={})

        config = TransportConfig(user_agent="Playlens/1.0 (+https://x/contact)")
        client = httpx.Client(transport=httpx.MockTransport(handler), headers={
            "User-Agent": config.user_agent
        })
        HttpTransport(config, client=client, sleep=lambda _s: None).get("https://example.test/x")
        assert "Playlens" in seen["user-agent"]


class TestAdaptivePagination:
    """C-01: the critic endpoint returns 10 per page whatever `limit` says."""

    def test_critic_pages_are_measured_not_assumed(self):
        source = FakeMetacriticSource(critic_total=93, critic_page_size=10)
        provider = MetacriticProvider(source, critic_page_size=100, user_page_size=100)

        pages = list(
            provider.iter_reviews("elden-ring", "playstation-5", kind=ReviewKind.CRITIC, cap=200)
        )
        collected = [r for page in pages for r in page.items]

        assert len(collected) == 93
        assert len(pages) == 10                      # 93 / 10, last page short
        assert len({r.dedupe_key for r in collected}) == 93   # no duplicates
        offsets = [c[1]["offset"] for c in source.calls if c[0] == "reviews_page"]
        assert offsets == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]

    def test_user_pages_use_the_requested_limit(self):
        source = FakeMetacriticSource(user_total=250)
        provider = MetacriticProvider(source, critic_page_size=100, user_page_size=100)
        pages = list(
            provider.iter_reviews("elden-ring", "playstation-5", kind=ReviewKind.USER, cap=500)
        )
        assert [len(p.items) for p in pages] == [100, 100, 50]

    def test_offset_never_exceeds_total(self):
        """Guards the known HTTP 500 when offset > totalResults."""
        source = FakeMetacriticSource(critic_total=25, critic_page_size=10)
        provider = MetacriticProvider(source, critic_page_size=100, user_page_size=100)
        list(provider.iter_reviews("g", "pc", kind=ReviewKind.CRITIC, cap=1000))
        offsets = [c[1]["offset"] for c in source.calls if c[0] == "reviews_page"]
        assert max(offsets) < 25

    def test_cap_is_respected(self):
        source = FakeMetacriticSource(critic_total=1000, critic_page_size=10)
        provider = MetacriticProvider(source, critic_page_size=100, user_page_size=100)
        pages = list(provider.iter_reviews("g", "pc", kind=ReviewKind.CRITIC, cap=35))
        assert sum(len(p.items) for p in pages) == 40  # stops at the first page past the cap

    def test_max_pages_is_respected(self):
        source = FakeMetacriticSource(critic_total=1000, critic_page_size=10)
        provider = MetacriticProvider(source, critic_page_size=100, user_page_size=100)
        pages = list(
            provider.iter_reviews("g", "pc", kind=ReviewKind.CRITIC, cap=1000, max_pages=3)
        )
        assert len(pages) == 3

    def test_empty_page_stops_iteration(self):
        source = FakeMetacriticSource(critic_total=0, critic_page_size=10)
        provider = MetacriticProvider(source, critic_page_size=100, user_page_size=100)
        assert list(provider.iter_reviews("g", "pc", kind=ReviewKind.CRITIC, cap=100)) == []


class TestEligibilityIsExplicit:
    def test_discovery_queries_filter_by_metascore(self):
        from app.adapters.metacritic import endpoints

        for _path, params in (
            endpoints.new_releases(limit=20),
            endpoints.browse_new(offset=0, limit=24),
            endpoints.browse_top(offset=0, limit=24),
        ):
            # ADR-015: the eligibility rule is visible in the query, not hidden in code.
            assert params["metaScoreMin"] == 1

    def test_new_releases_matches_the_researched_call(self):
        from app.adapters.metacritic import endpoints

        path, params = endpoints.new_releases(limit=20)
        assert path == "/finder/metacritic/web"
        assert params["componentName"] == "new-releases-carousel"
        assert params["sortBy"] == "-releaseDate"
        assert params["mcoTypeId"] == 13
        assert params["limit"] == 20
