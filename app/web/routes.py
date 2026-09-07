"""Server-rendered pages.

Every page is built from the SAME presenter output the JSON API returns (ADR-017), so a
rule fixed in one surface is fixed in both, and the OpenAPI contract describes what the
HTML actually shows.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import public_rate_limit, settings_dep, uow_dep
from app.api.presenters import list_item
from app.api.routers.games import ReleaseWindow, ScoreBand, SortField, build_filters
from app.api.schemas import GameListOut
from app.config import Settings
from app.db.uow import UnitOfWork
from app.domain.verdict import verdict_line
from app.services.catalog_service import CatalogService
from app.services.monitoring_service import MonitoringService

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

router = APIRouter(include_in_schema=False, dependencies=[Depends(public_rate_limit)])

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
# Autoescaping is on by default in Jinja2Templates. `|safe` is banned outright and a test
# enforces the ban: review text and descriptions are third-party content (ADR-019 T3).
templates.env.autoescape = True


# --------------------------------------------------------------------------- filters


def _tier_class(score: Any) -> str:
    tier = score.tier if hasattr(score, "tier") else score.get("tier")
    return "" if tier in (None, "none") else f"is-{tier}"


def _score_text(score: Any) -> str:
    """Native value: ``93`` for critics, ``8.9`` for players (ADR-002)."""
    value = score.value
    if value is None:
        return "—"
    return str(int(value)) if score.scale_max == 100 else f"{value:.1f}"


def _thousands(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _plural_ru(value: Any, one: str, few: str, many: str) -> str:
    """Russian has three plural forms, and the choice is not "1 vs the rest".

    ``1 игра``, ``2 игры``, ``5 игр`` -- and ``11 игр``, ``21 игра``, ``111 игр``. Writing
    ``игр{{ '' if n == 1 else 'ы' }}`` in a template is wrong for most numbers, so the
    rule lives here once.
    """
    try:
        number = abs(int(value))
    except (TypeError, ValueError):
        return many
    if number % 100 in range(11, 15):
        return many
    last = number % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def _compact(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if number >= 1_000:
        return f"{round(number / 1000)}K"
    return str(number)


def _ago(value: dt.datetime | None) -> str:
    """``17 минут назад``. Reads after the word "обновлено", so it needs no preposition."""
    if value is None:
        return "никогда"
    delta = dt.datetime.now(dt.UTC) - value
    hours = delta.total_seconds() / 3600
    if hours < 1:
        minutes = max(1, round(delta.total_seconds() / 60))
        return f"{minutes} {_plural_ru(minutes, 'минуту', 'минуты', 'минут')} назад"
    if hours < 24:
        whole = round(hours)
        return f"{whole} {_plural_ru(whole, 'час', 'часа', 'часов')} назад"
    days = round(hours / 24)
    return f"{days} {_plural_ru(days, 'день', 'дня', 'дней')} назад"


#: Genitive, because the form that reads correctly is "4 февраля 2026", not "февраль".
#: `strftime('%B')` would give whatever the server's locale is, which on a container is
#: usually C -- English -- and never depends on the reader.
_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _date_long(value: dt.date | dt.datetime | None) -> str:
    """``4 февраля 2026``.

    The day is formatted by hand: ``%-d`` is a glibc extension that raises
    ``ValueError`` on Windows, and a date filter that works only on the deployment host
    is a filter that crashes every game page on a developer's machine.
    """
    if value is None:
        return "неизвестно"
    if not hasattr(value, "strftime"):
        return str(value)
    return f"{value.day} {_MONTHS_RU[value.month - 1]} {value.year}"


def _duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _initials(title: str) -> str:
    words = [w for w in "".join(c if c.isalnum() else " " for c in title).split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _img(url: str | None, width: int = 320) -> str:
    """Route covers through our own resizer, with its host allow-list (ADR-017)."""
    if not url:
        return ""
    if url.startswith("/"):
        return url
    return f"/img?{urlencode({'u': url, 'w': width})}"


templates.env.filters.update(
    {
        "tier_class": _tier_class,
        "score_text": _score_text,
        "thousands": _thousands,
        "plural_ru": _plural_ru,
        "compact": _compact,
        "ago": _ago,
        "date_long": _date_long,
        "duration": _duration,
        "initials": _initials,
        "img": _img,
    }
)


def _dotted(obj: Any, path: str) -> Any:
    """``counters24h.success_rate`` -> the value, or ``None``.

    In Python, not in the template: Jinja's ``{% set %}`` does not escape a ``{% for %}``
    scope, so walking a path in a loop leaves the variable bound to whatever it started
    as -- which is how every KPI tile came to render the entire status object.
    """
    for part in path.split("."):
        if obj is None:
            return None
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
    return obj


def _page_numbers(current: int, total: int, window: int = 2) -> list[int | None]:
    if total <= 7:
        return list(range(1, total + 1))
    pages: list[int | None] = [1]
    if current - window > 2:
        pages.append(None)
    for page in range(max(2, current - window), min(total, current + window) + 1):
        pages.append(page)
    if current + window < total - 1:
        pages.append(None)
    if pages[-1] != total:
        pages.append(total)
    return pages


def _context(request: Request, settings: Settings, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "app_name": settings.app_name,
        "web_fonts": settings.web_fonts_enabled,
        "noindex": False,
        "page_numbers": _page_numbers,
        "dotted": _dotted,
        **extra,
    }


# --------------------------------------------------------------------------- pages


def setup_required_page(request: Request) -> HTMLResponse:
    """The database is reachable but empty. Rendered without touching it."""
    from app.config import get_settings
    from app.db.readiness import MIGRATE_COMMAND

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "setup_required.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "web_fonts": settings.web_fonts_enabled,
            "noindex": True,
            "active_nav": None,
            "migrate_command": MIGRATE_COMMAND,
        },
        status_code=503,
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Browsers ask for this whatever the <link> says. Answering costs one file."""
    return FileResponse(
        STATIC_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    home_sections = CatalogService(settings).home(uow)

    def present(games: list) -> list:
        return [list_item(g, settings) for g in games]

    spotlight = None
    if home_sections.spotlight is not None:
        spotlight = list_item(home_sections.spotlight, settings)
        critic, player = _scores_of(home_sections.spotlight, settings)
        spotlight.__dict__["verdict_line"] = verdict_line(
            critic, player, agreement_threshold=settings.agreement_threshold
        ).line

    return templates.TemplateResponse(
        request,
        "index.html",
        _context(
            request,
            settings,
            active_nav="discover",
            sections={
                "spotlight": spotlight,
                "just_released": present(home_sections.just_released),
                "disagree": present(home_sections.disagree),
                "highest_rated": present(home_sections.highest_rated),
                "recently_updated": present(home_sections.recently_updated),
            },
        ),
    )


def _scores_of(game, settings: Settings):
    from app.api.presenters import game_scores

    return game_scores(game, settings)


def _catalog_context(
    request: Request,
    uow: UnitOfWork,
    settings: Settings,
    **params: Any,
) -> dict[str, Any]:
    filters = build_filters(settings=settings, **params)
    games, total = uow.games.search(filters)
    facets = uow.games.platform_facets(filters)

    from app.api.schemas import FacetOut

    results = GameListOut(
        items=[list_item(g, settings) for g in games],
        total=total,
        limit=filters.limit,
        offset=filters.offset,
        has_more=filters.offset + len(games) < total,
        facets=[
            FacetOut(slug=slug, name=name, code=code, count=count)
            for slug, name, code, count in facets
        ],
    )

    base: dict[str, Any] = {}
    if filters.q:
        base["q"] = filters.q
    if filters.platforms:
        base["platform"] = list(filters.platforms)
    if filters.score_band != "any":
        base["score_band"] = filters.score_band
    if filters.released != "any":
        base["released"] = filters.released
    if filters.sort != "metascore":
        base["sort"] = filters.sort

    def page_url(page: int) -> str:
        page = max(1, min(page, max(1, math.ceil(total / filters.limit))))
        params_ = dict(base)
        if page > 1:
            params_["offset"] = (page - 1) * filters.limit
        return "/games" + ("?" + urlencode(params_, doseq=True) if params_ else "")

    def without(key: str, value: str | None = None) -> str:
        params_ = {k: v for k, v in base.items() if k != key}
        if key == "platform" and value:
            remaining = [p for p in filters.platforms if p != value]
            if remaining:
                params_["platform"] = remaining
        return "/games" + ("?" + urlencode(params_, doseq=True) if params_ else "")

    active_filters: list[tuple[str, str]] = []
    for slug in filters.platforms:
        active_filters.append((slug.upper(), without("platform", slug)))
    if filters.score_band != "any":
        active_filters.append((filters.score_band.title(), without("score_band")))
    if filters.released != "any":
        active_filters.append((filters.released, without("released")))

    return _context(
        request,
        settings,
        active_nav="games",
        # Only asked when there is nothing to show: "no results" and "nothing indexed
        # yet" are different situations and get different copy (EDGE_CASES 22 vs 23).
        index_is_empty=total == 0 and uow.games.stats()["games_total"] == 0,
        query=filters.q,
        filters=filters,
        results=results,
        active_filters=active_filters,
        page_url=page_url,
        clear_search_url=without("q"),
    )


def _catalog_params(
    q: str | None,
    platform: list[str],
    score_band: ScoreBand,
    released: ReleaseWindow,
    sort: SortField,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    return {
        "q": q,
        "platform": platform,
        "genre": [],
        "score_band": score_band,
        "released": released,
        "sort": sort,
        "order": "desc",
        "limit": limit,
        "offset": offset,
    }


@router.get("/games", response_class=HTMLResponse)
def catalog(
    request: Request,
    q: str | None = Query(None, max_length=100),
    platform: list[str] = Query(default=[]),
    score_band: ScoreBand = "any",
    released: ReleaseWindow = "any",
    sort: SortField = "metascore",
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    context = _catalog_context(
        request,
        uow,
        settings,
        **_catalog_params(q, platform, score_band, released, sort, offset, limit),
    )
    return templates.TemplateResponse(request, "catalog.html", context)


@router.get("/games/fragment", response_class=HTMLResponse)
def catalog_fragment(
    request: Request,
    q: str | None = Query(None, max_length=100),
    platform: list[str] = Query(default=[]),
    score_band: ScoreBand = "any",
    released: ReleaseWindow = "any",
    sort: SortField = "metascore",
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    """The grid alone, for filtering in place without a page reload.

    Same template as the full page, so the two cannot render a result differently.
    """
    context = _catalog_context(
        request,
        uow,
        settings,
        **_catalog_params(q, platform, score_band, released, sort, offset, limit),
    )
    return templates.TemplateResponse(request, "fragments/catalog_grid.html", context)


@router.get("/games/{slug}", response_class=HTMLResponse)
def game_page(
    request: Request,
    slug: str,
    platform: str | None = Query(None),
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    detail = CatalogService(settings).game_detail(uow, slug, selected_platform=platform)
    if detail is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            _context(request, settings, active_nav="games", slug=slug),
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "game.html",
        _context(
            request, settings, active_nav="games", game=detail, selected_platform=platform
        ),
    )


@router.get("/about", response_class=HTMLResponse)
def about(
    request: Request,
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "about.html",
        _context(request, settings, active_nav="about", stats=uow.games.stats()),
    )


@router.get("/admin/monitoring", response_class=HTMLResponse)
def monitoring_page(
    request: Request,
    uow: UnitOfWork = Depends(uow_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    """Operator surface. Not in the primary nav, and noindex (ADR-019 section 3)."""
    status = MonitoringService(settings).status(uow)
    context = _context(request, settings, active_nav=None, status=status)
    context["noindex"] = True
    return templates.TemplateResponse(request, "admin/monitoring.html", context)
