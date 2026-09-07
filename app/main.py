"""Application entry point.

One process serves the REST API, the server-rendered pages and the event stream
(ADR-017). They share a container, a database session factory and — crucially — the same
presenters, so the HTML and the JSON cannot describe a game differently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.errors import register_error_handlers
from app.api.routers import games, health, monitoring
from app.config import get_settings
from app.container import get_container
from app.db.readiness import MIGRATE_COMMAND, schema_exists
from app.db.seed import seed_platforms
from app.logging import configure_logging, correlate, get_logger, new_request_id

log = get_logger(__name__)

#: Google Fonts serves the stylesheet from one origin and the font files from another,
#: so both have to be named or ``WEB_FONTS_ENABLED=true`` silently renders in the fallback
#: stack -- a policy that blocks what the page itself asks for is a policy nobody trusts.
_FONT_STYLE_ORIGIN = "https://fonts.googleapis.com"
_FONT_FILE_ORIGIN = "https://fonts.gstatic.com"


def build_csp(*, web_fonts: bool) -> str:
    """The policy, derived from what the templates actually load.

    ``style-src`` keeps ``'unsafe-inline'`` because the ported design uses inline
    ``style="--pct:87"`` to drive gauge arcs from data. ``script-src`` does NOT: every
    line of JavaScript is in ``/static/app.js``, and a template that grew an inline
    handler would be caught by the browser rather than shipped.
    """
    style_src = ["'self'", "'unsafe-inline'"]
    font_src = ["'self'"]
    if web_fonts:
        style_src.append(_FONT_STYLE_ORIGIN)
        font_src.append(_FONT_FILE_ORIGIN)
    return "; ".join(
        [
            "default-src 'self'",
            "img-src 'self' data:",
            "script-src 'self'",
            f"style-src {' '.join(style_src)}",
            f"font-src {' '.join(font_src)}",
            "connect-src 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "frame-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ]
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)

    # Asked before anything else, because the answer changes what every later line does.
    # SQLite creates the file on connect, so "the database is reachable" is true even
    # when it is empty -- and the application would start, then fail on every request.
    container = get_container()
    if not schema_exists(container.engine):
        log.error(
            "app.database_not_migrated",
            extra={"database": container.settings.database_url, "fix": MIGRATE_COMMAND},
        )
        # Printed as well as logged: the logs are JSON, and the person who needs this
        # sentence is watching a terminal on their first run.
        print(
            "\n  The database has no tables yet, so every page will fail."
            f"\n  Create them with:  {MIGRATE_COMMAND}\n",
            flush=True,
        )

    # Reference data, not user data: the platform list carries the design's badge codes
    # (PS5, XSX, NSW) and display order. The crawler would create platform rows on demand
    # anyway, but in discovery order and without the codes the cards are laid out around,
    # so a fresh deployment would render a correct catalogue with wrong-looking chips.
    # Idempotent, and cheap enough to run on every boot.
    try:
        with container.uow() as uow:
            seeded = seed_platforms(uow.session)
        log.info("app.platforms_seeded", extra={"platforms": seeded})
    except Exception as exc:  # a missing table is a migration problem, not a boot problem
        log.warning("app.platform_seed_failed", extra={"error": str(exc)[:300]})

    log.info(
        "app.started",
        extra={
            "env": settings.app_env,
            "source": settings.metacritic_source,
            "llm_enabled": settings.llm_enabled,
            "youtube_enabled": settings.youtube_enabled,
        },
    )
    yield
    get_container().close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.app_version,
        description=(
            "Platform-aware verdicts and evidence-backed review summaries over Metacritic "
            "data. Not affiliated with Metacritic or any platform holder."
        ),
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["X-Admin-Token", "Content-Type"],
            allow_credentials=False,  # there are no cookies to protect
        )

    csp = build_csp(web_fonts=settings.web_fonts_enabled)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        with correlate(request_id=request_id):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Content-Security-Policy", csp)
        return response

    register_error_handlers(app)

    app.include_router(games.router, prefix=settings.api_prefix)
    app.include_router(monitoring.router, prefix=settings.api_prefix)
    app.include_router(monitoring.admin_router, prefix=settings.api_prefix)
    app.include_router(health.router, prefix=settings.api_prefix)

    # Server-rendered pages and the image proxy.
    from app.web import images, routes

    app.include_router(routes.router)
    app.include_router(images.router)
    app.mount(
        "/static",
        StaticFiles(directory=str(routes.STATIC_DIR)),
        name="static",
    )

    return app


app = create_app()
