"""HTML fallback source.

Exists so that ADR-001's mitigation is real rather than aspirational: if the internal JSON
API closes, ``METACRITIC_SOURCE=html`` keeps the catalogue alive.

It is honest about what it cannot do. Review LISTS are not available through HTML at
acceptable cost or reliability, so this source raises a clear, documented error for them.
The consequence is stated rather than hidden: in HTML mode games and scores keep updating
while review ingestion and therefore new AI summaries pause. Existing summaries continue
to be served, because they live in our database.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.adapters.http.transport import HttpTransport
from app.domain.enums import ReviewKind
from app.domain.errors import PermanentError, SchemaDriftError
from app.domain.models import (
    GameDetail,
    Listing,
    ListingItem,
    PlatformScores,
    ReviewPage,
    ScoreStats,
)
from app.normalizers.text import clean_body, normalize_title, slugify, stable_fingerprint
from app.parsers.jsonld import (
    elements_by_testid,
    find_video_game,
    first_int,
    meta_tags,
)

_GAME_HREF = re.compile(r"/game/([a-z0-9][a-z0-9\-]*)/?")


class HtmlSource:
    name = "html"

    def __init__(self, transport: HttpTransport, *, site_base_url: str) -> None:
        self._transport = transport
        self._base = site_base_url.rstrip("/")

    # ------------------------------------------------------------------ listings

    def new_releases(self, *, limit: int) -> Listing:
        html = self._transport.get(f"{self._base}/game/").text
        items = self._listing_items(html)[:limit]
        return Listing(items=tuple(items), total_results=len(items), offset=0, limit=limit)

    def browse(self, *, offset: int, limit: int, by_score: bool = False) -> Listing:
        page = offset // max(1, limit) + 1
        sort = "metascore" if by_score else "new"
        html = self._transport.get(
            f"{self._base}/browse/game/all/all/all-time/{sort}/", params={"page": page}
        ).text
        items = self._listing_items(html)[:limit]
        return Listing(items=tuple(items), total_results=len(items), offset=offset, limit=limit)

    def _listing_items(self, html: str) -> list[ListingItem]:
        """Anchors carrying ``data-testid="product-card"`` — the only semi-stable hook."""
        seen: set[str] = set()
        items: list[ListingItem] = []
        for element in elements_by_testid(html, {"product-card", "product-card-content"}):
            href = element["attrs"].get("href", "")
            match = _GAME_HREF.search(href)
            if not match:
                continue
            slug = match.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            title = (element["attrs"].get("aria-label") or element["text"]).strip()
            items.append(
                ListingItem(slug=slug, title=title or slug.replace("-", " ").title())
            )
        if not items:
            raise SchemaDriftError("no product-card anchors found in listing HTML")
        return items

    # ------------------------------------------------------------------ detail

    def game_detail(self, slug: str) -> GameDetail:
        html = self._transport.get(f"{self._base}/game/{slug}/").text
        ld = find_video_game(html)
        meta = meta_tags(html)

        title = (ld or {}).get("name") or meta.get("og:title")
        if not title:
            raise SchemaDriftError(f"no JSON-LD VideoGame and no og:title for {slug}")

        description = clean_body((ld or {}).get("description") or meta.get("og:description"))
        cover = (ld or {}).get("image") or meta.get("og:image")
        release_date = _parse_date((ld or {}).get("datePublished"))

        metascore, review_count = self._header_score(html)
        platforms: tuple[PlatformScores, ...] = ()
        if metascore is not None:
            # HTML shows the lead platform only (?platform= is ignored). We record it as
            # a single unnamed platform rather than pretending to know the breakdown.
            platforms = (
                PlatformScores(
                    slug="lead",
                    name="Lead platform",
                    mc_platform_id=None,
                    is_lead=True,
                    release_date=release_date,
                    metascore=metascore,
                    metascore_count=review_count or 0,
                ),
            )

        detail = GameDetail(
            slug=slug,
            title=str(title),
            title_norm=normalize_title(str(title)),
            mc_url=f"{self._base}/game/{slug}/",
            description=description,
            cover_url=str(cover) if cover else None,
            release_date=release_date,
            premiere_year=release_date.year if release_date else None,
            genres=(),
            companies=(),
            platforms=platforms,
        )
        payload = {
            "slug": slug,
            "title": detail.title,
            "description": detail.description,
            "cover": detail.cover_url,
            "metascore": metascore,
        }
        from dataclasses import replace

        return replace(detail, source_fingerprint=stable_fingerprint(payload))

    def _header_score(self, html: str) -> tuple[int | None, int | None]:
        score: int | None = None
        count: int | None = None
        for element in elements_by_testid(
            html, {"global-score-header", "product-score", "global-score-review-count"}
        ):
            if element["testid"] in {"global-score-header", "product-score"} and score is None:
                score = first_int(element["text"])
            elif element["testid"] == "global-score-review-count":
                count = first_int(element["text"])
        return score, count

    # ------------------------------------------------------------------ unsupported

    def score_stats(self, slug: str, platform_slug: str, *, kind: ReviewKind) -> ScoreStats:
        raise PermanentError(
            "per-platform stats are not available from the HTML source; "
            "the site renders the lead platform only and ignores ?platform="
        )

    def reviews_page(self, *args: Any, **kwargs: Any) -> ReviewPage:
        raise PermanentError(
            "review ingestion is not supported by the HTML fallback. "
            "Scores and catalogue data continue to update; existing summaries continue "
            "to be served from our database, but no new ones are generated while this "
            "source is active (ADR-001)."
        )

    # ------------------------------------------------------------------ sitemap

    def sitemap_shards(self) -> list[str]:
        from app.parsers.metacritic import parse_sitemap_index

        return parse_sitemap_index(self._transport.get(f"{self._base}/games.xml").text)

    def sitemap_slugs(self, shard_url: str) -> list[str]:
        from app.parsers.metacritic import parse_sitemap_shard

        return parse_sitemap_shard(self._transport.get(shard_url).text)


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def slug_from_title(title: str) -> str:
    return slugify(title)
