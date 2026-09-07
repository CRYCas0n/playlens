"""Database fixtures.

The schema is built by running the real Alembic migrations, not ``create_all``: a
migration that drifts from the models is a production incident, so the tests exercise the
path that production uses.

By default the dialect is SQLite. Setting ``TEST_DATABASE_URL`` points the same suite at
PostgreSQL (OQ-V1) -- no test is written twice.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, reset_settings_cache
from app.db.base import Base, make_engine, make_session_factory
from app.db.seed import seed_platforms

REPO_ROOT = Path(__file__).resolve().parents[2]


def _database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        return external
    path = tmp_path_factory.mktemp("db") / "integration.db"
    return f"sqlite+pysqlite:///{path}"


@pytest.fixture(scope="session")
def db_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _database_url(tmp_path_factory)


@pytest.fixture(scope="session")
def db_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        admin_token="test-admin-token-0123456789abcdef",
        app_env="test",
        llm_enabled=False,
        youtube_enabled=False,
    )


@pytest.fixture(scope="session")
def engine(db_settings: Settings) -> Iterator[Engine]:
    eng = make_engine(db_settings)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["DATABASE_URL"] = db_settings.database_url
    os.environ["ADMIN_TOKEN"] = "test-admin-token-0123456789abcdef"
    reset_settings_cache()
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


def _truncate_all(engine: Engine) -> None:
    """Delete in dependency order.

    Deliberately does NOT disable foreign keys: on SQLite ``PRAGMA foreign_keys`` is a
    no-op inside a transaction, so turning it off and back on leaves the pooled
    connection with enforcement disabled — and every later FK test silently passes.
    ``reversed(sorted_tables)`` is children-first, so no pragma is needed.
    """
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "alembic_version":
                continue
            conn.execute(sa.delete(table))


@pytest.fixture
def db(session_factory: sessionmaker[Session], engine: Engine) -> Iterator[Session]:
    _truncate_all(engine)
    session = session_factory()
    seed_platforms(session)
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def uow(db: Session):
    """A UnitOfWork over the test's own session.

    Sharing one session means an assertion sees what the service just wrote without a
    commit in between, and a rollback in the fixture undoes both.
    """
    from app.db.uow import UnitOfWork

    return UnitOfWork.bound(db)


@pytest.fixture
def platform_ids(db: Session) -> dict[str, int]:
    from app.db.models import Platform

    rows = db.execute(sa.select(Platform.slug, Platform.id)).all()
    return dict(rows)


@pytest.fixture
def load_harvest(db: Session):
    """Load a Release 0 harvest (real API responses) into the database.

    The loader itself lives in ``scripts/harvest.py`` so that ``make release0`` can
    reproduce the measurement against exactly the same bytes. One copy, two callers.
    """
    from scripts.harvest import load_harvest as loader

    return loader(db)


@pytest.fixture
def app_client(db_settings, session_factory, engine, monkeypatch):
    """A TestClient wired to the test database.

    The seams are the two module-level accessors the request dependencies call --
    ``deps.get_settings`` and ``deps.get_container``. Patching ``settings_dep`` itself
    would look right and do nothing: the routers captured the original function object in
    their ``Depends(...)`` defaults at import time, so a replacement module attribute is
    never consulted.
    """
    from fastapi.testclient import TestClient

    import app.container as container_module
    from app.api import deps
    from app.container import Container, reset_container

    container = Container(settings=db_settings)
    container.__dict__["session_factory"] = session_factory
    container.__dict__["engine"] = engine

    monkeypatch.setattr(container_module, "_container", container, raising=False)
    monkeypatch.setattr(deps, "get_container", lambda: container)
    monkeypatch.setattr(deps, "get_settings", lambda: db_settings)
    monkeypatch.setattr("app.main.get_settings", lambda: db_settings)
    deps.reset_limiters()

    from app.main import create_app

    with TestClient(create_app()) as client:
        client.container = container
        yield client

    deps.reset_limiters()
    reset_container()
