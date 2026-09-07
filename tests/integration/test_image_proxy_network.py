"""The image proxy against the real network.

Everything else about the proxy -- the host allow-list, the width list, the SSRF checks --
is tested offline in `test_security.py`, because those are decisions, not I/O. This file
asks the one question that needs a network: does a real Metacritic cover actually come
through, and does it come through smaller than it went in?

Marked `contract`: it makes one request to a third party. Run with `pytest -m contract`.
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.web.images import _resize, cache_size_bytes, evict_if_over_budget

pytestmark = [pytest.mark.contract, pytest.mark.integration]

ENABLED = os.environ.get("CONTRACT_TESTS") == "1"
requires_network = pytest.mark.skipif(
    not ENABLED, reason="set CONTRACT_TESTS=1 to fetch a real image"
)

#: A real cover, resolved from the live detail endpoint during this work.
REAL_COVER = "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1787423832.jpg"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        admin_token="i" * 32,
        app_env="test",
        image_cache_dir=str(tmp_path / "img"),
        image_cache_max_mb=8,
    )


@requires_network
class TestARealCover:
    @pytest.fixture(scope="class")
    def original(self):
        """Fetched the way the proxy fetches: identified. Anonymous requests get 403."""
        import httpx

        from app.config import Settings

        settings = Settings(admin_token="i" * 32, app_env="test")
        response = httpx.get(
            REAL_COVER,
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": settings.user_agent},
        )
        if response.status_code != 200:
            pytest.skip(f"the source returned {response.status_code} for the sample cover")
        return response.content

    def test_the_source_serves_unsigned_full_size_originals(self, original):
        """The reason a proxy exists at all: covers arrive at megabytes apiece, and the
        CDN's own resize endpoint is HMAC-signed (ADR-017)."""
        assert len(original) > 200_000, (
            f"the sample cover is only {len(original)} bytes; if the source started "
            "serving small images the proxy could be simplified"
        )

    def test_resizing_makes_it_dramatically_smaller(self, original):
        payload, kind = _resize(original, 320)
        if kind == "original":
            pytest.skip("Pillow is not installed; the proxy passes originals through")
        assert len(payload) < len(original) / 10, (
            f"{len(original)} bytes in, {len(payload)} out -- barely worth the CPU"
        )

    def test_the_resized_image_is_the_width_it_was_asked_for(self, original):
        from io import BytesIO

        pillow = pytest.importorskip("PIL.Image")
        payload, kind = _resize(original, 320)
        if kind == "original":
            pytest.skip("Pillow is not installed")
        with pillow.open(BytesIO(payload)) as image:
            assert image.width == 320

    def test_the_full_path_through_the_endpoint_works(self, app_client):
        """Allow-list, DNS check, fetch, resize, cache, response -- all of it.

        ``follow_redirects=False`` matters: on failure the proxy answers 302 to the
        original, and a following client would chase that back into the test app and
        report a confusing 404 instead of the proxy's own answer.
        """
        response = app_client.get(
            "/img", params={"u": REAL_COVER, "w": 320}, follow_redirects=False
        )
        assert response.status_code == 200, (
            "the proxy fell back to a redirect; the upstream fetch failed"
        )
        assert response.headers["content-type"].startswith("image/")
        assert len(response.content) < 400_000

    def test_the_proxy_identifies_itself_or_the_cdn_refuses(self):
        """Found by making a real request: the CDN answers 403 to httpx's default
        User-Agent. Every cover fell back to a redirect and the browser downloaded the
        2.3 MB original for every card on the page."""
        import httpx

        from app.config import Settings

        settings = Settings(admin_token="i" * 32, app_env="test")
        anonymous = httpx.get(REAL_COVER, timeout=30)
        identified = httpx.get(
            REAL_COVER, timeout=30, headers={"User-Agent": settings.user_agent}
        )
        assert identified.status_code == 200
        if anonymous.status_code == 200:
            pytest.skip("the CDN no longer filters on User-Agent")

    def test_the_second_request_is_served_from_cache(self, app_client):
        first = app_client.get(
            "/img", params={"u": REAL_COVER, "w": 480}, follow_redirects=False
        )
        second = app_client.get(
            "/img", params={"u": REAL_COVER, "w": 480}, follow_redirects=False
        )
        if first.status_code != 200:
            pytest.skip("the source did not serve the image")
        assert second.status_code == 200
        assert second.content == first.content


class TestWithoutTheNetwork:
    """Runs always: the parts of the pipeline that need no third party."""

    def test_resize_returns_the_original_when_pillow_is_missing(self, monkeypatch):
        """An optional dependency going missing must degrade, not blank the catalogue."""
        import builtins

        real_import = builtins.__import__

        def no_pillow(name, *args, **kwargs):
            if name.startswith("PIL"):
                raise ImportError("no Pillow")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pillow)
        payload, kind = _resize(b"not-an-image", 320)
        assert kind == "original"
        assert payload == b"not-an-image"

    def test_an_unreadable_body_raises_rather_than_caching_rubbish(self):
        """The endpoint catches this and falls back to a redirect; what must not happen
        is a corrupt file landing in the cache under a valid key."""
        pillow = pytest.importorskip("PIL")
        with pytest.raises(pillow.UnidentifiedImageError):
            _resize(b"definitely not an image", 320)

    def test_the_cache_directory_starts_empty_and_stays_within_budget(self, settings):
        assert cache_size_bytes(settings) == 0
        assert evict_if_over_budget(settings) == 0
