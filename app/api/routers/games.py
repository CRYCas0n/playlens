"""Catalogue endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api import presenters
from app.api.deps import public_rate_limit, settings_dep, uow_dep
from app.api.schemas import (
    FacetOut,
    GameDetailOut,
    GameListOut,
    GenreFacetOut,
    PlatformOut,
    SimilarGameOut,
    StatsOut,
    SuggestionOut,
    SummaryOut,
)
from app.config import Settings
from app.db.uow import UnitOfWork
from app.domain.enums import Audience
from app.repositories.games import CatalogFilters
from app.services.catalog_service import CatalogService

router = APIRouter(tags=["catalog"], dependencies=[Depends(public_rate_limit)])

SortField = Literal["metascore", "userscore", "release_date", "gap", "title"]
ScoreBand = Literal["any", "excellent", "good", "mixed", "unrated"]
ReleaseWindow = Literal["any", "last30", "2026", "earlier"]


def build_filters(
    q: str | None,
    platform: list[str],
    genre: list[str],
    score_band: ScoreBand,
    released: ReleaseWindow,
    sort: SortField,
    order: Literal["asc", "desc"],
    limit: int,
    offset: int,
    settings: Settings,
) -> CatalogFilters:
    if offset > settings.offset_hard_limit:
        # Deep offsets are a cheap way to make the database work hard for nothing.
        raise HTTPException(
            status_code=400,
            detail=f"offset above {settings.offset_hard_limit} is not supported; "
            "narrow the filters instead.",
        )
    return CatalogFilters(
        q=q,
        platforms=tuple(platform),
        genres=tuple(genre),
        score_band=score_band,
        released=released,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
        userscore_zero_min_ratings=settings.userscore_zero_min_ratings,
    )


@router.get("/games", response_model=GameListOut)
def list_games(
    response: Response,
    q: str | None = Query(None, min_length=2, max_length=100),
    platform: list[str] = Query(default=[]),
    genre: list[str] = Query(default=[]),
    score_band: ScoreBand = "any",
    released: ReleaseWindow = "any",
    sort: SortField = "metascore",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> GameListOut:
    filters = build_filters(
        q, platform, genre, score_band, released, sort, order, limit, offset, settings
    )
    games, total = uow.games.search(filters)
    facets = uow.games.platform_facets(filters)
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return GameListOut(
        items=[presenters.list_item(g, settings) for g in games],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(games) < total,
        facets=[
            FacetOut(slug=slug, name=name, code=code, count=count)
            for slug, name, code, count in facets
        ],
    )


@router.get("/games/{slug}", response_model=GameDetailOut)
def get_game(
    slug: str,
    response: Response,
    platform: str | None = Query(None, description="Highlight this platform in the verdict"),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> GameDetailOut:
    detail = CatalogService(settings).game_detail(uow, slug, selected_platform=platform)
    if detail is None:
        raise HTTPException(status_code=404, detail="We don't have this game yet.")
    response.headers["Cache-Control"] = "public, max-age=300"
    return detail


@router.get("/games/{slug}/similar", response_model=list[SimilarGameOut])
def get_similar(
    slug: str,
    limit: int = Query(12, ge=1, le=24),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> list[SimilarGameOut]:
    game = uow.games.get_by_slug(slug)
    if game is None:
        raise HTTPException(status_code=404, detail="We don't have this game yet.")
    rows = uow.similarity.for_game(game.id, limit=limit)
    return [
        SimilarGameOut(
            slug=other.mc_slug,
            title=other.title,
            cover_url=other.cover_url,
            metascore=presenters.score_out(presenters.game_scores(other, settings)[0]),
            reason=row.reason,
            score=row.score,
            components=dict(row.components or {}),
        )
        for row, other in rows
    ]


@router.get("/games/{slug}/summaries", response_model=list[SummaryOut])
def get_summaries(
    slug: str,
    audience: Audience | None = None,
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> list[SummaryOut]:
    game = uow.games.get_by_slug(slug)
    if game is None:
        raise HTTPException(status_code=404, detail="We don't have this game yet.")
    platforms = {gp.id: gp.platform for gp in game.platforms}
    out: list[SummaryOut] = []
    for summary in uow.summaries.current_for_game(game.id):
        if audience and summary.audience != audience.value:
            continue
        if summary.status != "fresh":
            continue
        out.append(
            presenters.summary_out(
                summary,
                platform=platforms.get(summary.game_platform_id),
                source_url=game.mc_url,
            )
        )
    return out


@router.get("/platforms", response_model=list[PlatformOut])
def list_platforms(
    only_with_games: bool = True, uow: UnitOfWork = Depends(uow_dep)
) -> list[PlatformOut]:
    return [
        PlatformOut(slug=slug, name=name, code=code)
        for slug, name, code, _count in uow.games.platform_facets(
            CatalogFilters(), include_empty=not only_with_games
        )
    ]


@router.get("/genres", response_model=list[GenreFacetOut])
def list_genres(
    only_with_games: bool = True, uow: UnitOfWork = Depends(uow_dep)
) -> list[GenreFacetOut]:
    """Genres, ordered by how many games carry them.

    Alphabetical order would put Adventure first on every catalogue in the world; the
    useful order is the one that shows what this index actually contains.
    """
    return [
        GenreFacetOut(slug=slug, name=name, count=count)
        for slug, name, count in uow.games.genre_facets(
            CatalogFilters(), include_empty=not only_with_games
        )
    ]


@router.get("/search/suggest", response_model=list[SuggestionOut])
def suggest(
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(8, ge=1, le=20),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> list[SuggestionOut]:
    return [presenters.suggestion(g, settings) for g in uow.games.suggest(q, limit)]


@router.get("/stats", response_model=StatsOut)
def stats(uow: UnitOfWork = Depends(uow_dep)) -> StatsOut:
    payload = uow.games.stats()
    return StatsOut(
        games_total=payload["games_total"],
        games_with_metascore=payload["games_with_metascore"],
        games_with_summaries=payload["games_with_summaries"],
        reviews_total=uow.reviews.total_count(),
        last_crawl_at=payload["last_crawl_at"],
    )
