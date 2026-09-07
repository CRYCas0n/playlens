"""Migrations, forwards and back.

The forward path is exercised by every integration test — the schema those tests run
against is built by Alembic, not by ``create_all``. The backward path was written and
never executed, which is the same as not having it: a rollback discovered to be broken
during an incident is worse than a rollback nobody promised.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.db.base import Base

pytestmark = pytest.mark.integration


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """A throwaway database, so a downgrade cannot touch the suite's own schema.

    ``migrations/env.py`` reads the URL from ``Settings``, not from ``alembic.ini`` --
    one place decides where the database is. So the environment is what has to be
    pointed elsewhere, and the settings cache reset around it.
    """
    import os
    from pathlib import Path

    from app.config import reset_settings_cache

    root = Path(__file__).resolve().parents[2]
    url = f"sqlite+pysqlite:///{tmp_path / 'roundtrip.db'}"
    previous = os.environ.get("DATABASE_URL")

    monkeypatch.setenv("DATABASE_URL", url)
    reset_settings_cache()

    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    yield cfg, url

    if previous is not None:
        os.environ["DATABASE_URL"] = previous
    reset_settings_cache()


def tables(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


class TestTheRoundTrip:
    def test_upgrade_then_downgrade_then_upgrade(self, alembic_cfg):
        cfg, url = alembic_cfg

        command.upgrade(cfg, "head")
        after_first = tables(url)
        assert "games" in after_first
        assert "alembic_version" in after_first

        command.downgrade(cfg, "base")
        after_down = tables(url)
        assert after_down <= {"alembic_version"}, (
            f"downgrade left tables behind: {sorted(after_down - {'alembic_version'})}"
        )

        command.upgrade(cfg, "head")
        assert tables(url) == after_first, "the second upgrade must rebuild the same schema"

    def test_every_model_table_exists_after_the_round_trip(self, alembic_cfg):
        cfg, url = alembic_cfg
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        expected = set(Base.metadata.tables)
        assert expected <= tables(url)

    def test_constraints_survive_the_round_trip(self, alembic_cfg):
        """A rebuilt schema without its constraints is a schema that lies."""
        from app.db.models import EXPECTED_PARTIAL_UNIQUE_INDEXES, EXPECTED_UNIQUE_CONSTRAINTS

        cfg, url = alembic_cfg
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        engine = sa.create_engine(url)
        try:
            inspector = sa.inspect(engine)
            for table, columns in EXPECTED_UNIQUE_CONSTRAINTS:
                sets = {
                    frozenset(c["column_names"])
                    for c in inspector.get_unique_constraints(table)
                }
                sets |= {
                    frozenset(i["column_names"])
                    for i in inspector.get_indexes(table)
                    if i.get("unique") and i.get("column_names")
                }
                assert frozenset(columns) in sets, (
                    f"{table}{columns} lost its uniqueness guarantee in the rebuild"
                )

            names: set[str] = set()
            for table in inspector.get_table_names():
                names.update(i["name"] for i in inspector.get_indexes(table) if i.get("name"))
            for expected in EXPECTED_PARTIAL_UNIQUE_INDEXES:
                assert expected in names, f"partial index {expected} lost in the rebuild"
        finally:
            engine.dispose()

    def test_one_step_back_and_forward(self, alembic_cfg):
        """The realistic rollback: undo the last deploy, not the whole history."""
        cfg, url = alembic_cfg
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")
        assert "summaries" in tables(url)

    def test_the_newest_migration_is_reversible_without_losing_other_columns(
        self, alembic_cfg
    ):
        """0003 adds one nullable column; going back must drop only that."""
        cfg, url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = sa.create_engine(url)
        try:
            before = {c["name"] for c in sa.inspect(engine).get_columns("summaries")}
        finally:
            engine.dispose()
        assert "score_at_generation" in before

        command.downgrade(cfg, "0002")
        engine = sa.create_engine(url)
        try:
            after = {c["name"] for c in sa.inspect(engine).get_columns("summaries")}
        finally:
            engine.dispose()
        assert after == before - {"score_at_generation"}


class TestTheRevisionChain:
    def test_there_is_exactly_one_head(self):
        """Two heads means somebody branched and nobody merged; upgrade would be ambiguous."""
        from pathlib import Path

        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1

    def test_every_revision_has_a_downgrade_that_is_not_a_stub(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        for path in (root / "migrations" / "versions").glob("*.py"):
            body = path.read_text(encoding="utf-8")
            assert "def downgrade" in body, f"{path.name} has no downgrade"
            after = body.split("def downgrade", 1)[1]
            # A downgrade whose whole body is `pass` is an irreversible migration wearing
            # a reversible one's clothes. If a revision really is irreversible it must say
            # so out loud, not silently succeed and leave the schema behind.
            stripped = [
                line.strip()
                for line in after.splitlines()[1:]
                if line.strip() and not line.strip().startswith(("#", '"'))
            ]
            assert stripped != ["pass"], f"{path.name}: downgrade is a silent no-op"
