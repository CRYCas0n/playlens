"""The first-run mistake: a database that exists but has no tables.

This is not a hypothetical. It is what happens when someone runs the app before running
the migrations, and SQLite makes it easy by creating the file on connect — so the process
starts cleanly and then fails on every single request.

What it used to produce was worse than a crash:

    {"status":500,"detail":"This is not your connection. We have logged it and it
     usually resolves within a minute or two."}

Every clause of that is wrong. It is not a server having a bad minute, nothing was
logged that helps, and it never resolves. The reader was one command away and was told
to wait.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.db.readiness import (
    MIGRATE_COMMAND,
    NOT_MIGRATED_MESSAGE,
    DatabaseNotMigrated,
    is_missing_table_error,
    schema_exists,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """A real database file with no tables in it, and an app pointed at it."""
    from fastapi.testclient import TestClient

    from app.config import reset_settings_cache
    from app.container import reset_container
    from app.main import create_app

    url = f"sqlite+pysqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ADMIN_TOKEN", "e" * 32)
    monkeypatch.setenv("APP_ENV", "test")
    reset_settings_cache()
    reset_container()

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client, url

    reset_settings_cache()
    reset_container()


class TestDetection:
    def test_an_empty_database_is_not_ready(self, tmp_path):
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'e.db'}")
        try:
            assert schema_exists(engine) is False
        finally:
            engine.dispose()

    def test_a_migrated_database_is_ready(self, engine):
        assert schema_exists(engine) is True

    def test_reachability_is_not_readiness(self, tmp_path):
        """`SELECT 1` succeeds against an empty database. That is the whole trap."""
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'e.db'}")
        try:
            with engine.connect() as conn:
                assert conn.execute(sa.text("SELECT 1")).scalar_one() == 1
            assert schema_exists(engine) is False
        finally:
            engine.dispose()

    def test_sqlite_missing_table_errors_are_recognised(self, tmp_path):
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'e.db'}")
        try:
            with engine.connect() as conn, pytest.raises(Exception) as caught:
                conn.execute(sa.text("SELECT * FROM games"))
            assert is_missing_table_error(caught.value)
        finally:
            engine.dispose()

    @pytest.mark.parametrize(
        "message",
        [
            "(sqlite3.OperationalError) no such table: games",
            'relation "games" does not exist',
            "UndefinedTable: relation does not exist",
        ],
    )
    def test_every_dialect_we_support_is_recognised(self, message):
        exc = sa.exc.OperationalError("SELECT 1", {}, Exception(message))
        assert is_missing_table_error(exc)

    def test_an_ordinary_error_is_not_mistaken_for_a_missing_schema(self):
        """Telling someone to run migrations they have already run would send them the
        wrong way for a genuine bug."""
        assert not is_missing_table_error(ValueError("something else"))
        assert not is_missing_table_error(
            sa.exc.OperationalError("SELECT 1", {}, Exception("database is locked"))
        )

    def test_the_dedicated_error_carries_the_command(self):
        assert MIGRATE_COMMAND in str(DatabaseNotMigrated())
        assert is_missing_table_error(DatabaseNotMigrated())


class TestWhatAPersonSees:
    def test_a_browser_gets_a_page_that_names_the_command(self, empty_db):
        client, _ = empty_db
        response = client.get("/", headers={"Accept": "text/html"})
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("text/html")
        assert MIGRATE_COMMAND in response.text
        assert "has no tables yet" in response.text

    @pytest.mark.parametrize("path", ["/", "/games", "/games/anything", "/admin/monitoring"])
    def test_every_page_says_the_same_thing(self, empty_db, path):
        client, _ = empty_db
        response = client.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 503
        assert MIGRATE_COMMAND in response.text

    def test_it_never_claims_the_problem_will_pass(self, empty_db):
        """The old message said it "usually resolves within a minute or two". It does not."""
        client, _ = empty_db
        text = client.get("/", headers={"Accept": "text/html"}).text
        assert "resolves within a minute" not in text
        assert "not your connection" not in text

    def test_the_page_is_a_real_page_not_a_stack_trace(self, empty_db):
        client, _ = empty_db
        text = client.get("/", headers={"Accept": "text/html"}).text
        assert "<title>" in text
        assert "Traceback" not in text
        assert "sqlalchemy" not in text.lower()


class TestWhatAClientSees:
    def test_the_api_answers_with_a_problem_document(self, empty_db):
        client, _ = empty_db
        response = client.get("/api/v1/games")
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["title"] == "The database has not been set up yet"
        assert body["detail"] == NOT_MIGRATED_MESSAGE
        assert body["type"].endswith("database-not-migrated")

    def test_the_api_keeps_json_even_for_a_browser(self, empty_db):
        """Someone opening an API URL in a browser is a developer who wants the JSON."""
        client, _ = empty_db
        response = client.get("/api/v1/games", headers={"Accept": "text/html"})
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_it_is_503_not_500(self, empty_db):
        """503 says "not ready"; 500 says "we are broken". One of those is actionable."""
        client, _ = empty_db
        assert client.get("/api/v1/games").status_code == 503


class TestHealth:
    def test_health_distinguishes_not_migrated_from_down(self, empty_db):
        client, _ = empty_db
        payload = client.get("/api/v1/health").json()
        assert payload["checks"]["database"] == "not_migrated"
        assert payload["checks"]["fix"] == MIGRATE_COMMAND
        assert payload["status"] == "down"

    def test_a_migrated_database_reports_ok(self, app_client):
        payload = app_client.get("/api/v1/health").json()
        assert payload["checks"]["database"] == "ok"
        assert "fix" not in payload["checks"]


class TestTheSeedScript:
    def test_it_refuses_and_says_what_to_run(self, tmp_path, monkeypatch, capsys):
        """It used to end in thirty lines of SQLAlchemy internals and leave an empty
        database file behind to confuse the next attempt."""
        from app.config import reset_settings_cache
        from scripts.seed_demo import main

        monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'e.db'}")
        monkeypatch.setenv("ADMIN_TOKEN", "s" * 32)
        reset_settings_cache()

        assert main() == 2
        assert MIGRATE_COMMAND in capsys.readouterr().err
        reset_settings_cache()
