"""Test doubles.

No test in this suite touches the network. These fakes stand in for the three external
dependencies (Metacritic, the LLM, YouTube) and are shaped to reproduce the awkward
behaviours the research actually found, not idealised ones.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import ReviewKind
from app.domain.models import (
    GameDetail,
    Listing,
    ListingItem,
    PlatformScores,
    ReviewPage,
    ReviewRecord,
    ScoreStats,
)
from app.normalizers.text import body_hash, normalize_title


@dataclass
class FakeMetacriticSource:
    """A source whose review endpoints reproduce the measured page sizes.

    Critic pages are 10 long no matter what ``limit`` says, user pages honour ``limit`` —
    exactly the asymmetry Release 0 found and the reason the paginator measures instead
    of assuming (C-01).
    """

    name: str = "fake"
    critic_total: int = 93
    user_total: int = 6593
    critic_page_size: int = 10          # the source ignores `limit` here
    user_page_size: int | None = None   # None -> honour `limit`
    listings: dict[str, Listing] = field(default_factory=dict)
    details: dict[str, GameDetail] = field(default_factory=dict)
    stats: dict[tuple[str, str, str], ScoreStats] = field(default_factory=dict)
    fail_with: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    shards: list[str] = field(default_factory=list)
    shard_slugs: dict[str, list[str]] = field(default_factory=dict)

    # -------------------------------------------------------------- listings

    def new_releases(self, *, limit: int) -> Listing:
        self.calls.append(("new_releases", {"limit": limit}))
        self._maybe_fail()
        listing = self.listings.get("new_releases")
        if listing is None:
            return Listing(items=(), total_results=0, offset=0, limit=limit)
        return Listing(
            items=listing.items[:limit],
            total_results=listing.total_results,
            offset=0,
            limit=limit,
        )

    def browse(self, *, offset: int, limit: int, by_score: bool = False) -> Listing:
        self.calls.append(("browse", {"offset": offset, "limit": limit, "by_score": by_score}))
        self._maybe_fail()
        key = f"browse:{offset}"
        listing = self.listings.get(key)
        if listing is None:
            return Listing(items=(), total_results=0, offset=offset, limit=limit)
        return listing

    # -------------------------------------------------------------- detail

    def game_detail(self, slug: str) -> GameDetail:
        self.calls.append(("game_detail", {"slug": slug}))
        self._maybe_fail()
        if slug not in self.details:
            raise KeyError(f"no fake detail for {slug}")
        return self.details[slug]

    def score_stats(self, slug: str, platform_slug: str, *, kind: ReviewKind) -> ScoreStats:
        self.calls.append(("score_stats", {"slug": slug, "platform": platform_slug}))
        self._maybe_fail()
        key = (slug, platform_slug, kind.value)
        if key in self.stats:
            return self.stats[key]
        return ScoreStats(score=None, scale_max=10 if kind is ReviewKind.USER else 100,
                          review_count=0)

    # -------------------------------------------------------------- reviews

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
        self.calls.append(
            ("reviews_page", {"kind": kind.value, "offset": offset, "limit": limit})
        )
        self._maybe_fail()
        if kind is ReviewKind.CRITIC:
            total, size = self.critic_total, self.critic_page_size
        else:
            total = self.user_total
            size = self.user_page_size if self.user_page_size is not None else limit

        remaining = max(0, total - offset)
        n = min(size, remaining)
        items = tuple(
            make_review(kind, index=offset + i, sentiment=sentiment) for i in range(n)
        )
        return ReviewPage(items=items, total_results=total, offset=offset)

    # -------------------------------------------------------------- sitemap

    def sitemap_shards(self) -> list[str]:
        self.calls.append(("sitemap_shards", {}))
        return list(self.shards)

    def sitemap_slugs(self, shard_url: str) -> list[str]:
        self.calls.append(("sitemap_slugs", {"url": shard_url}))
        return list(self.shard_slugs.get(shard_url, []))

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with


def make_review(
    kind: ReviewKind,
    *,
    index: int,
    sentiment: str | None = None,
    body: str | None = None,
    score: float | None = None,
    published_on: dt.date | None = None,
) -> ReviewRecord:
    text = body if body is not None else (
        f"Review number {index}. It talks about the combat system, the pacing of the "
        f"opening hours and the technical state of the build in enough words to pass "
        f"the corpus minimum length filter comfortably."
    )
    resolved_score = score if score is not None else (80 if kind is ReviewKind.CRITIC else 8)
    scale = 100 if kind is ReviewKind.CRITIC else 10
    return ReviewRecord(
        kind=kind,
        dedupe_key=f"{kind.value}-{index:05d}",
        source_review_id=f"uuid-{index:05d}" if kind is ReviewKind.USER else None,
        body=text,
        body_hash=body_hash(text),
        score=resolved_score,
        score_max=scale,
        score_normalized=resolved_score * (100 / scale),
        published_on=published_on or (dt.date(2024, 1, 1) + dt.timedelta(days=index % 400)),
        publication_name=f"Outlet {index % 17}" if kind is ReviewKind.CRITIC else None,
        publication_slug=f"outlet-{index % 17}" if kind is ReviewKind.CRITIC else None,
        author=None if kind is ReviewKind.CRITIC else f"player{index}",
        char_count=len(text),
        sentiment_bucket=sentiment if sentiment != "all" else None,
    )


def make_detail(
    slug: str,
    *,
    title: str | None = None,
    platforms: tuple[PlatformScores, ...] | None = None,
    fingerprint: str = "fp-1",
    genres: tuple = (),
    companies: tuple = (),
    **kwargs: Any,
) -> GameDetail:
    resolved_title = title or slug.replace("-", " ").title()
    resolved_platforms = platforms or (
        PlatformScores(
            slug="pc",
            name="PC",
            mc_platform_id=1500000019,
            is_lead=True,
            release_date=dt.date(2024, 5, 1),
            metascore=82,
            metascore_count=40,
        ),
    )
    return GameDetail(
        slug=slug,
        title=resolved_title,
        title_norm=normalize_title(resolved_title),
        mc_url=f"https://www.metacritic.com/game/{slug}/",
        mc_title_id=kwargs.pop("mc_title_id", abs(hash(slug)) % 10**9),
        platforms=resolved_platforms,
        genres=genres,
        companies=companies,
        source_fingerprint=fingerprint,
        **kwargs,
    )


def make_listing(slugs: list[str], *, total: int | None = None, offset: int = 0) -> Listing:
    items = tuple(
        ListingItem(slug=slug, title=slug.replace("-", " ").title(), metascore=80)
        for slug in slugs
    )
    return Listing(
        items=items, total_results=total if total is not None else len(slugs),
        offset=offset, limit=len(slugs) or 1,
    )
