"""`IMAGE_CACHE_MAX_MB` was declared and unread.

A cache directory with a limit that nothing enforces grows until the disk stops it, and
the first symptom is not "images are slow" but "the service cannot write anything".
"""

from __future__ import annotations

import os
import time

import pytest

from app.config import Settings
from app.web.images import cache_size_bytes, evict_if_over_budget


@pytest.fixture
def settings(tmp_path):
    def _make(max_mb: int) -> Settings:
        return Settings(
            admin_token="t" * 32,
            app_env="test",
            image_cache_dir=str(tmp_path / "img"),
            image_cache_max_mb=max_mb,
        )

    return _make


def write(settings: Settings, name: str, kb: int, *, accessed_ago_s: float = 0.0) -> None:
    from pathlib import Path

    directory = Path(settings.image_cache_dir) / name[:2]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.img"
    path.write_bytes(b"x" * (kb * 1024))
    if accessed_ago_s:
        when = time.time() - accessed_ago_s
        os.utime(path, (when, when))


class TestMeasurement:
    def test_an_empty_cache_is_zero(self, settings):
        assert cache_size_bytes(settings(10)) == 0

    def test_it_adds_up_across_shard_directories(self, settings):
        s = settings(10)
        write(s, "aa1", 100)
        write(s, "bb2", 150)
        assert cache_size_bytes(s) == 250 * 1024


class TestEviction:
    def test_a_cache_under_the_limit_is_left_alone(self, settings):
        s = settings(1)
        write(s, "aa1", 100)
        assert evict_if_over_budget(s) == 0
        assert cache_size_bytes(s) == 100 * 1024

    def test_going_over_the_limit_removes_files(self, settings):
        s = settings(1)  # 1 MB
        for i in range(15):
            write(s, f"f{i:03d}", 100)  # 1.5 MB total
        assert evict_if_over_budget(s) > 0
        assert cache_size_bytes(s) <= 1024 * 1024

    def test_it_evicts_to_below_the_limit_not_merely_to_it(self, settings):
        """Otherwise every subsequent request runs a full directory scan."""
        s = settings(1)
        for i in range(20):
            write(s, f"f{i:03d}", 100)
        evict_if_over_budget(s)
        assert cache_size_bytes(s) <= 0.8 * 1024 * 1024

    def test_the_least_recently_accessed_file_goes_first(self, settings):
        """A cover on the front page is worth keeping however old the file is."""
        from pathlib import Path

        s = settings(1)
        write(s, "old", 600, accessed_ago_s=86_400)
        write(s, "hot", 600, accessed_ago_s=1)
        evict_if_over_budget(s)
        remaining = {p.stem for p in Path(s.image_cache_dir).rglob("*.img")}
        assert remaining == {"hot"}

    def test_a_limit_of_zero_means_no_limit(self, settings):
        """0 means "unlimited" for every other setting; a cache that deletes everything
        on a config typo would be a nasty surprise."""
        s = settings(0)
        for i in range(20):
            write(s, f"f{i:03d}", 100)
        assert evict_if_over_budget(s) == 0
        assert cache_size_bytes(s) == 20 * 100 * 1024

    def test_a_negative_limit_is_treated_the_same_way(self, settings):
        s = settings(-5)
        write(s, "aa1", 500)
        assert evict_if_over_budget(s) == 0

    def test_a_missing_cache_directory_is_not_an_error(self, settings):
        s = settings(1)
        assert evict_if_over_budget(s) == 0
        assert cache_size_bytes(s) == 0

    def test_one_file_larger_than_the_whole_budget_is_removed(self, settings):
        """The pathological case: it can never fit, so keeping it wastes the whole cache."""
        s = settings(1)
        write(s, "huge", 2048)
        evict_if_over_budget(s)
        assert cache_size_bytes(s) == 0

    def test_a_file_vanishing_mid_eviction_does_not_raise(self, settings, monkeypatch):
        """Two workers can evict at once; losing that race is normal, not an error."""
        from pathlib import Path

        s = settings(1)
        for i in range(15):
            write(s, f"f{i:03d}", 100)

        real_unlink = Path.unlink
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError(self)
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", flaky)
        assert evict_if_over_budget(s) > 0
