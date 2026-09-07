"""RFC 7807 problem responses.

Two rules, both from the design's copy guidance: a bare status code is never the whole
message, and the message never leaks a secret or a stack trace.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db.readiness import NOT_MIGRATED_MESSAGE, is_missing_table_error
from app.domain.errors import ConflictError, NotFoundError, PermanentError, RetryableError
from app.logging import get_logger, redact

log = get_logger(__name__)

MEDIA_TYPE = "application/problem+json"
BASE_TYPE = "https://playlens.example/errors"

TITLES = {
    400: "Invalid request",
    401: "Not authorised",
    404: "Not found",
    409: "Conflict",
    422: "Invalid parameters",
    429: "Too many requests",
    500: "Something went wrong on our side",
    503: "Service unavailable",
}

#: Titles that say what to do rather than what happened.
SLUG_TITLES = {
    "database-not-migrated": "The database has not been set up yet",
}


def problem(
    status: int,
    detail: str | None = None,
    *,
    instance: str | None = None,
    slug: str | None = None,
    extra: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "type": f"{BASE_TYPE}/{slug or status}",
        "title": SLUG_TITLES.get(slug or "", TITLES.get(status, "Error")),
        "status": status,
        "detail": detail,
        "instance": instance,
    }
    if extra:
        body.update(redact(extra))
    return JSONResponse(
        status_code=status, content=body, media_type=MEDIA_TYPE, headers=headers
    )


def wants_html(request: Request):
    """A page for a browser, a problem document for everything else.

    Imported lazily and only here: the error layer does not otherwise know that a web
    layer exists, and one setup page is not worth a plugin registry. A browser that asked
    for a page and received raw JSON has been told nothing it can act on.
    """
    # The API answers with a problem document whatever the Accept header says: someone
    # opening /api/v1/games in a browser is a developer who wants to see the JSON.
    from app.config import get_settings

    if request.url.path.startswith(get_settings().api_prefix):
        return None
    if "text/html" not in request.headers.get("accept", ""):
        return None
    try:
        from app.web.routes import setup_required_page

        return setup_required_page(request)
    except Exception:  # the page itself must never be the thing that fails
        log.exception("api.setup_page_failed")
        return None


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        # Headers set on the exception are part of the answer, not decoration: a 429
        # without its Retry-After, or a 401 without WWW-Authenticate, is a worse response.
        return problem(
            exc.status_code,
            str(exc.detail),
            instance=str(request.url.path),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return problem(
            422,
            "One or more parameters were rejected.",
            instance=str(request.url.path),
            extra={"errors": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]},
        )

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError):
        return problem(404, str(exc), instance=str(request.url.path))

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError):
        return problem(
            409, str(exc), instance=str(request.url.path), slug="crawl-already-running"
        )

    @app.exception_handler(PermanentError)
    async def _permanent(request: Request, exc: PermanentError):
        return problem(400, str(exc), instance=str(request.url.path))

    @app.exception_handler(RetryableError)
    async def _retryable(request: Request, exc: RetryableError):
        return problem(503, str(exc), instance=str(request.url.path))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Logged in full; reported without internals. The reader gets a fact, not a trace.
        # A database with no tables is not a server having a bad minute: it is a setup
        # step nobody ran, it will never fix itself, and the person is one command away.
        # Telling them to wait would be false.
        if is_missing_table_error(exc):
            log.error("api.database_not_migrated", extra={"path": str(request.url.path)})
            html = wants_html(request)
            if html is not None:
                return html
            return problem(
                503,
                NOT_MIGRATED_MESSAGE,
                instance=str(request.url.path),
                slug="database-not-migrated",
            )

        log.exception("api.unhandled", extra={"path": str(request.url.path)})
        return problem(
            500,
            "This is not your connection. We have logged it and it usually resolves "
            "within a minute or two.",
            instance=str(request.url.path),
        )
