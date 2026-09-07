from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "docs" / "research-fixtures"
MC_FIXTURES = FIXTURES / "metacritic"
R0_FIXTURES = FIXTURES / "release0"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def mc_fixtures() -> Path:
    return MC_FIXTURES


@pytest.fixture(scope="session")
def r0_fixtures() -> Path:
    return R0_FIXTURES


@pytest.fixture(scope="session")
def composer_elden_ring() -> dict[str, Any]:
    return load_json(MC_FIXTURES / "composer_elden_ring.json")


@pytest.fixture(scope="session")
def finder_new_releases() -> dict[str, Any]:
    return load_json(MC_FIXTURES / "finder_new_releases.json")


@pytest.fixture(scope="session")
def finder_browse() -> dict[str, Any]:
    return load_json(MC_FIXTURES / "finder_browse.json")


@pytest.fixture(scope="session")
def reviews_critic() -> dict[str, Any]:
    return load_json(MC_FIXTURES / "reviews_critic.json")


@pytest.fixture(scope="session")
def reviews_user() -> dict[str, Any]:
    return load_json(MC_FIXTURES / "reviews_user.json")


@pytest.fixture(scope="session")
def sitemap_index_xml() -> str:
    return (MC_FIXTURES / "games_sitemap_index.xml").read_text(encoding="utf-8")


def harvest(slug: str) -> dict[str, Any]:
    """A Release 0 harvest file: real per-platform stats and review corpora."""
    return load_json(R0_FIXTURES / "harvest" / f"{slug}.json")


@pytest.fixture(scope="session")
def harvest_loader():
    return harvest


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Tests never inherit a developer's .env, and never talk to the network."""
    from app.config import reset_settings_cache

    for key in list(os.environ):
        if key.upper().startswith(
            (
                "APP_",
                "DATABASE_",
                "LLM_",
                "YOUTUBE_",
                "METACRITIC_",
                "ADMIN_",
                "CRAWL_",
                "REVIEWS_",
                "AI_",
                "SIMILARITY_",
                "YT_",
                "WORKER_",
                "SSE_",
                "IMAGE_",
            )
        ):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("YOUTUBE_ENABLED", "false")
    monkeypatch.setenv("HTTP_CACHE_ENABLED", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token-0123456789abcdef")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("IMAGE_CACHE_DIR", str(tmp_path / "imgcache"))
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def test_settings(tmp_path: Path):
    """A plain Settings for unit tests that need thresholds but no database.

    Function-scoped and freshly constructed, so a test may mutate a threshold without
    leaking the change into the next one.
    """
    from app.config import Settings

    return Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'unit.db'}",
        admin_token="t" * 32,
    )
