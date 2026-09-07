"""Cover image proxy.

The blueprint chose Next.js largely for ``next/image``: Metacritic serves unsigned
~140 KB originals, and its CDN resize endpoint is HMAC-signed, which we are not going to
forge. This is the replacement (ADR-017), and it is deliberately narrower than
``remotePatterns`` would be:

* an EXACT host allow-list, not a pattern;
* a fixed set of widths, because an arbitrary width is arbitrary CPU;
* no redirects followed, a response size cap and a timeout;
* private address ranges refused after DNS resolution.

Those five rules are the whole SSRF surface of the application (ADR-019 T4).
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.api.deps import settings_dep
from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["media"])

CACHE_CONTROL = "public, max-age=604800, immutable"


def _cache_root(settings: Settings) -> Path:
    return Path(settings.image_cache_dir)


def cache_size_bytes(settings: Settings) -> int:
    return sum(f.stat().st_size for f in _cache_root(settings).rglob("*.img") if f.is_file())


def evict_if_over_budget(settings: Settings, *, target_ratio: float = 0.8) -> int:
    """Keep the cache under ``IMAGE_CACHE_MAX_MB``, oldest-accessed first.

    Without this the directory grows until the disk does not, and the first symptom is
    the whole service failing to write anything at all. Eviction goes down to 80% of the
    limit rather than to exactly the limit, so a full cache does not run a scan on every
    single request afterwards.

    Least-recently-*accessed*, not least-recently-written: a cover on the front page is
    worth keeping however old the file is.
    """
    limit = settings.image_cache_max_mb * 1024 * 1024
    if limit <= 0:  # 0 means "no limit", as everywhere else in the configuration
        return 0

    files = [f for f in _cache_root(settings).rglob("*.img") if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    if total <= limit:
        return 0

    removed = 0
    for path in sorted(files, key=lambda f: f.stat().st_atime):
        if total <= limit * target_ratio:
            break
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            # Another worker got there first, or the file is locked. Not worth failing a
            # page render over: the next request will try again.
            continue
        total -= size
        removed += 1

    log.info(
        "image.cache_evicted",
        extra={"removed": removed, "bytes_remaining": total, "limit_bytes": limit},
    )
    return removed


def _cache_path(settings: Settings, url: str, width: int) -> Path:
    digest = hashlib.sha256(f"{url}|{width}".encode()).hexdigest()
    directory = Path(settings.image_cache_dir) / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{digest}.img"


def _host_is_allowed(url: str, settings: Settings) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return (parsed.hostname or "").lower() in settings.allowed_image_hosts


def _resolves_to_public_address(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local:
            return False
    return True


@router.get("/img")
def image_proxy(
    u: str = Query(..., max_length=1024, description="Source image URL"),
    w: int = Query(320, description="Target width"),
    settings: Settings = Depends(settings_dep),
) -> Response:
    if not settings.image_proxy_enabled:
        return RedirectResponse(u, status_code=302)

    if not _host_is_allowed(u, settings):
        raise HTTPException(status_code=400, detail="That image host is not allowed.")
    if w not in settings.allowed_image_widths:
        raise HTTPException(
            status_code=400,
            detail=f"Width must be one of {sorted(settings.allowed_image_widths)}.",
        )

    cached = _cache_path(settings, u, w)
    if cached.exists():
        return FileResponse(
            cached, media_type="image/webp", headers={"Cache-Control": CACHE_CONTROL}
        )

    hostname = urlparse(u).hostname or ""
    if not _resolves_to_public_address(hostname):
        raise HTTPException(status_code=400, detail="That image host is not reachable.")

    try:
        with httpx.Client(
            timeout=settings.image_fetch_timeout_s, follow_redirects=False
        ) as client:
            # The same User-Agent the rest of the application sends. Without it the CDN
            # answers 403 -- every cover fell back to a redirect, the resizer never ran,
            # and the browser downloaded the 2.3 MB original for every card on the page.
            # It is also the polite thing to do: the request says who is making it.
            response = client.get(u, headers={"User-Agent": settings.user_agent})
        if response.status_code != 200 or len(response.content) > settings.image_max_bytes:
            raise ValueError(f"unexpected response: {response.status_code}")
        payload = _resize(response.content, w)
    except Exception as exc:
        # A broken proxy must not break a page: fall back to the original rather than
        # returning a 500 and blanking every card on the screen.
        log.warning("image.proxy_failed", extra={"error": str(exc)})
        return RedirectResponse(u, status_code=302)

    media_type = "image/webp" if payload[1] == "webp" else "image/jpeg"
    cached.write_bytes(payload[0])
    # After the write, not before: the file just added is the one most worth keeping,
    # and checking first would let the cache sit one file over the limit indefinitely.
    evict_if_over_budget(settings)
    return Response(
        content=payload[0], media_type=media_type, headers={"Cache-Control": CACHE_CONTROL}
    )


def _resize(content: bytes, width: int) -> tuple[bytes, str]:
    """Resize with Pillow if it is installed; otherwise pass the original through.

    Pillow is an optional extra so a minimal deployment still serves images — just
    unresized. Failing to install an optional dependency should never blank the catalogue.
    """
    try:
        import io

        from PIL import Image
    except ImportError:
        return content, "original"

    with Image.open(io.BytesIO(content)) as image:
        image = image.convert("RGB")
        ratio = width / image.width
        if ratio < 1:
            image = image.resize((width, max(1, round(image.height * ratio))))
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=82, method=4)
        return buffer.getvalue(), "webp"
