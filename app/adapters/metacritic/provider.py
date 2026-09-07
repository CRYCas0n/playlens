"""MetacriticProvider — the only door to Metacritic.

Business logic sees this class and the DTOs it returns. It never sees HTTP, JSON, HTML or
the fact that the JSON API is unofficial. That isolation is the mitigation for the single
largest risk in the project: if the internal API closes, one environment variable swaps in
the HTML source and nothing above this layer changes (ADR-001).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from app.adapters.http.transport import HttpTransport
from app.adapters.metacritic import endpoints
from app.domain.enums import ReviewKind
from app.domain.errors import PermanentError
from app.domain.models import GameDetail, Listing, ReviewPage, ScoreStats
from app.logging import get_logger
from app.parsers import metacritic as parsers

log = get_logger(__name__)


class MetacriticSource(Protocol):
    """A source returns DOMAIN OBJECTS, not payloads.

    Returning DTOs rather than raw dicts is what makes the HTML fallback a genuine
    alternative instead of a shim that has to fake JSON.
    """

    name: str

    def new_releases(self, *, limit: int) -> Listing: ...
    def browse(self, *, offset: int, limit: int, by_score: bool = False) -> Listing: ...
    def game_detail(self, slug: str) -> GameDetail: ...
    def score_stats(self, slug: str, platform_slug: str, *, kind: ReviewKind) -> ScoreStats: ...
    def reviews_page(
        self,
        slug: str,
        platform_slug: str,
        *,
        kind: ReviewKind,
        offset: int,
        limit: int,
        sentiment: str,
    ) -> ReviewPage: ...
    def sitemap_shards(self) -> list[str]: ...
    def sitemap_slugs(self, shard_url: str) -> list[str]: ...


class JsonApiSource:
    """``backend.metacritic.com``. Typed JSON instead of generated CSS classes."""

    name = "json_api"

    def __init__(
        self, transport: HttpTransport, *, api_base_url: str, site_base_url: str, api_key: str
    ) -> None:
        self._transport = transport
        self._api_base_url = api_base_url.rstrip("/")
        self._site_base_url = site_base_url.rstrip("/")
        # Sent because their frontend sends it. Verified NOT to be validated: requests
        # with no key and with a wrong key both returned 200. We do not rely on it.
        self._api_key = api_key

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(params or {})
        if self._api_key:
            merged["apiKey"] = self._api_key
        return self._transport.get(f"{self._api_base_url}{path}", params=merged).json()

    def new_releases(self, *, limit: int) -> Listing:
        path, params = endpoints.new_releases(limit=limit)
        return parsers.parse_listing(self._get(path, params), offset=0, limit=limit)

    def browse(self, *, offset: int, limit: int, by_score: bool = False) -> Listing:
        build = endpoints.browse_top if by_score else endpoints.browse_new
        path, params = build(offset=offset, limit=limit)
        return parsers.parse_listing(self._get(path, params), offset=offset, limit=limit)

    def game_detail(self, slug: str) -> GameDetail:
        path, params = endpoints.game_detail(slug)
        return parsers.parse_game_detail(
            self._get(path, params), base_url=self._site_base_url
        )

    def score_stats(self, slug: str, platform_slug: str, *, kind: ReviewKind) -> ScoreStats:
        if kind is ReviewKind.USER:
            path, _ = endpoints.user_stats(slug, platform_slug)
            return parsers.parse_score_stats(self._get(path), scale_max=10)
        path, _ = endpoints.critic_stats(slug, platform_slug)
        return parsers.parse_score_stats(self._get(path), scale_max=100)

    def reviews_page(
        self,
        slug: str,
        platform_slug: str,
        *,
        kind: ReviewKind,
        offset: int,
        limit: int,
        sentiment: str,
    ) -> ReviewPage:
        build = endpoints.critic_reviews if kind is ReviewKind.CRITIC else endpoints.user_reviews
        path, params = build(
            slug, platform_slug, offset=offset, limit=limit, sentiment=sentiment
        )
        return parsers.parse_reviews(
            self._get(path, params),
            kind=kind,
            offset=offset,
            sentiment_bucket=None if sentiment == "all" else sentiment,
        )

    def sitemap_shards(self) -> list[str]:
        url = f"{self._site_base_url}{endpoints.SITEMAP_INDEX}"
        return parsers.parse_sitemap_index(self._transport.get(url).text)

    def sitemap_slugs(self, shard_url: str) -> list[str]:
        return parsers.parse_sitemap_shard(self._transport.get(shard_url).text)


class MetacriticProvider:
    """Facade with the pagination policy on top of a swappable source."""

    def __init__(
        self,
        source: MetacriticSource,
        *,
        critic_page_size: int = 100,
        user_page_size: int = 100,
    ) -> None:
        self.source = source
        self._critic_page_size = critic_page_size
        self._user_page_size = user_page_size

    @property
    def source_name(self) -> str:
        return self.source.name

    def new_releases(self, *, limit: int) -> Listing:
        return self.source.new_releases(limit=limit)

    def browse_new(self, *, page: int, page_size: int) -> Listing:
        return self.source.browse(offset=max(0, (page - 1) * page_size), limit=page_size)

    def browse_top(self, *, offset: int, limit: int) -> Listing:
        return self.source.browse(offset=offset, limit=limit, by_score=True)

    def game_detail(self, slug: str) -> GameDetail:
        return self.source.game_detail(slug)

    def user_stats(self, slug: str, platform_slug: str) -> ScoreStats:
        return self.source.score_stats(slug, platform_slug, kind=ReviewKind.USER)

    def critic_stats(self, slug: str, platform_slug: str) -> ScoreStats:
        return self.source.score_stats(slug, platform_slug, kind=ReviewKind.CRITIC)

    def sitemap_shards(self) -> list[str]:
        return self.source.sitemap_shards()

    def sitemap_slugs(self, shard_url: str) -> list[str]:
        return self.source.sitemap_slugs(shard_url)

    def iter_reviews(
        self,
        slug: str,
        platform_slug: str,
        *,
        kind: ReviewKind,
        cap: int,
        sentiment: str = "all",
        max_pages: int | None = None,
    ) -> Iterator[ReviewPage]:
        """Paginate reviews without trusting the ``limit`` parameter.

        The step comes from the LENGTH OF THE FIRST PAGE, not from what we asked for.
        The blueprint believed ``limit`` worked up to 500; Release 0 measured the critic
        endpoint returning exactly 10 regardless (C-01). Measuring rather than assuming is
        correct for both endpoints today and stays correct if either changes tomorrow.
        """
        requested = self._critic_page_size if kind is ReviewKind.CRITIC else self._user_page_size
        offset = 0
        total: int | None = None
        pages = 0
        seen = 0

        while seen < cap:
            if max_pages is not None and pages >= max_pages:
                return
            # Clamp: an offset beyond totalResults makes the source return HTTP 500
            # ("Cannot read properties of undefined"), so we never issue that request.
            if total is not None and offset >= total:
                return

            page = self.source.reviews_page(
                slug,
                platform_slug,
                kind=kind,
                offset=offset,
                limit=requested,
                sentiment=sentiment,
            )
            if not page.items:
                return

            yield page

            pages += 1
            seen += len(page.items)
            total = page.total_results
            offset += len(page.items)  # <-- the MEASURED page size


def build_provider(settings, transport: HttpTransport) -> MetacriticProvider:
    from app.adapters.metacritic.source_html import HtmlSource

    if settings.metacritic_source == "html":
        source: MetacriticSource = HtmlSource(
            transport, site_base_url=settings.metacritic_base_url
        )
    elif settings.metacritic_source == "json_api":
        source = JsonApiSource(
            transport,
            api_base_url=settings.metacritic_api_base_url,
            site_base_url=settings.metacritic_base_url,
            api_key=settings.metacritic_api_key,
        )
    else:  # pragma: no cover - guarded by the Literal in Settings
        raise PermanentError(f"unknown METACRITIC_SOURCE={settings.metacritic_source}")

    return MetacriticProvider(
        source,
        critic_page_size=settings.reviews_page_size,
        user_page_size=settings.reviews_page_size,
    )
