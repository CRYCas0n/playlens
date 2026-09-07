"""Metacritic endpoint map.

Every URL and query parameter the project uses is in this file. The eligibility rule
(``metaScoreMin=1``) is expressed here as a named constant rather than sprinkled through
the crawler, so ADR-015 stays visible instead of becoming folklore.
"""

from __future__ import annotations

from typing import Any

MCO_TYPE_GAME = 13

#: The eligibility rule (ADR-015). A game with no Metascore yields no verdict, no summary
#: and no useful similarity, so discovery does not spend a slot on it. Games without a
#: score are still stored and rendered as "Not rated" if they reach us another way.
ELIGIBILITY: dict[str, Any] = {"metaScoreMin": 1}


def new_releases(*, limit: int, offset: int = 0) -> tuple[str, dict[str, Any]]:
    """The exact call behind the site's New Releases carousel.

    Verified during research: the first 20 slugs matched the 20 cards on the page, in
    order. Note what it really is — ``sortBy=-releaseDate`` plus an eligibility filter,
    not "games released today" (C-02).
    """
    return "/finder/metacritic/web", {
        "componentName": "new-releases-carousel",
        "componentType": "ProductList",
        "sortBy": "-releaseDate",
        "mcoTypeId": MCO_TYPE_GAME,
        "offset": offset,
        "limit": limit,
        **ELIGIBILITY,
    }


def browse_new(*, offset: int, limit: int) -> tuple[str, dict[str, Any]]:
    return "/finder/metacritic/web", {
        "sortBy": "-releaseDate",
        "productType": "games",
        "offset": offset,
        "limit": limit,
        **ELIGIBILITY,
    }


def browse_top(*, offset: int, limit: int) -> tuple[str, dict[str, Any]]:
    """Used by the seed pass: the same pipeline, sorted by score instead of date (ADR-014)."""
    return "/finder/metacritic/web", {
        "sortBy": "-metaScore",
        "productType": "games",
        "offset": offset,
        "limit": limit,
        **ELIGIBILITY,
    }


def game_detail(slug: str) -> tuple[str, dict[str, Any]]:
    """One call returns product, platforms and per-platform Metascore."""
    return f"/composer/metacritic/pages/games/{slug}/web", {}


def critic_stats(slug: str, platform_slug: str) -> tuple[str, dict[str, Any]]:
    return f"/reviews/metacritic/critic/games/{slug}/platform/{platform_slug}/stats/web", {}


def user_stats(slug: str, platform_slug: str) -> tuple[str, dict[str, Any]]:
    """The ONLY source of a per-platform Userscore: ``?platform=`` is ignored (C-04)."""
    return f"/reviews/metacritic/user/games/{slug}/platform/{platform_slug}/stats/web", {}


def critic_reviews(
    slug: str, platform_slug: str, *, offset: int, limit: int, sentiment: str = "all"
) -> tuple[str, dict[str, Any]]:
    """Note: this endpoint IGNORES ``limit`` and returns 10 per page (C-01).

    The paginator does not rely on that constant — it measures the first page — but the
    parameter is still sent so behaviour changes upstream are visible rather than silent.
    """
    return f"/reviews/metacritic/critic/games/{slug}/platform/{platform_slug}/web", {
        "offset": offset,
        "limit": limit,
        "filterBySentiment": sentiment,
        "sort": "date",
    }


def user_reviews(
    slug: str, platform_slug: str, *, offset: int, limit: int, sentiment: str = "all"
) -> tuple[str, dict[str, Any]]:
    return f"/reviews/metacritic/user/games/{slug}/platform/{platform_slug}/web", {
        "offset": offset,
        "limit": limit,
        "filterBySentiment": sentiment,
        "sort": "date",
    }


SITEMAP_INDEX = "/games.xml"
