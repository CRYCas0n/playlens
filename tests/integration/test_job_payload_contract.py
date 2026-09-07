"""Every queued job must carry what its handler reads out of the payload.

This is the seam that produced the worst defect of the deployment, and the one the whole
suite walked straight past. `GameSync._dispatch` queued its follow-ups with
`payload={"slug": slug}`, passing `game_id` as a *column*. Three handlers subscript
`payload["game_id"]`. On the server every crawl reported success and every job it queued
died with `KeyError: 'game_id'` after burning its retries — no reviews, no similarity, no
YouTube, and therefore no summaries, all behind a green crawl and a healthy API.

Both sides were tested. The join between them was not: the handler tests build a payload
by hand, and the dispatch tests assert which job *types* get queued. Neither ever passed
one side's output to the other.

A static version of this test was written first and passed against the bug, which is
worth recording. `_dispatch` queues through a local helper that takes `job_type` as a
parameter, so the call site has no literal to read — the very shape that hid the defect
also defeats the parser. So the payloads here come from running the real service against
a real database, and only the *requirements* are read statically, from the handlers
themselves, so that no list has to be kept up to date by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.models import Job
from app.db.uow import UnitOfWork
from app.domain.models import ScoreStats
from app.parsers.metacritic import parse_game_detail
from app.services.game_sync import GameSyncService
from tests.fakes import FakeMetacriticSource

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "app" / "tasks" / "registry.py"
BASE = "https://www.metacritic.com"


def required_payload_keys() -> dict[str, set[str]]:
    """job_type -> keys its handler subscripts, read from the registry's own table.

    Only `payload["x"]` counts. `payload.get("x", default)` has a default and is by
    definition optional.
    """
    tree = ast.parse(REGISTRY.read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def keys_of(func: ast.FunctionDef) -> set[str]:
        return {
            node.slice.value
            for node in ast.walk(func)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "payload"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        }

    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 3:
            continue
        job_type, handler = node.args[0], node.args[2]
        if (
            isinstance(job_type, ast.Constant)
            and isinstance(job_type.value, str)
            and isinstance(handler, ast.Name)
            and handler.id in functions
        ):
            out[job_type.value] = keys_of(functions[handler.id])
    return out


@pytest.fixture
def provider(composer_elden_ring):
    from app.domain.enums import ReviewKind

    source = FakeMetacriticSource()
    source.details["elden-ring"] = parse_game_detail(composer_elden_ring, base_url=BASE)
    source.stats[("elden-ring", "playstation-5", ReviewKind.USER.value)] = ScoreStats(
        score=8.4, scale_max=10, review_count=24_375, sentiment="Generally favorable"
    )

    class Provider:
        def game_detail(self, slug: str):
            return source.game_detail(slug)

        def user_stats(self, slug: str, platform_slug: str) -> ScoreStats:
            return source.score_stats(slug, platform_slug, kind=ReviewKind.USER)

    return Provider()


def test_the_requirements_could_be_read(self=None):
    required = required_payload_keys()
    assert required.get("reviews.sync") == {"game_id"}, required.get("reviews.sync")
    assert "game_id" in required.get("similarity.recompute", set())


def test_every_job_a_crawl_queues_satisfies_its_handler(session_factory, db, provider):
    """The whole point: real sync, real rows, real payloads."""
    settings = Settings(
        admin_token="t" * 32,
        app_env="test",
        youtube_enabled=True,  # so youtube.discover is queued and checked too
        reviews_enabled=True,
    )
    with UnitOfWork(session_factory) as uow:
        outcome = GameSyncService(provider, settings).sync(uow, "elden-ring")

    assert outcome.follow_ups, "nothing was queued, so nothing was checked"

    required = required_payload_keys()
    jobs = db.execute(sa.select(Job)).scalars().all()
    assert jobs, "the sync queued nothing at all"

    problems: list[str] = []
    for job in jobs:
        missing = required.get(job.job_type, set()) - set(job.payload or {})
        if missing:
            problems.append(
                f"{job.job_type} queued without {sorted(missing)} "
                f"(payload has {sorted(job.payload or {})})"
            )
    assert not problems, (
        "; ".join(problems)
        + ". Passing game_id as a column does not put it in the payload."
    )


def test_the_follow_ups_are_the_ones_that_were_dead_in_production(
    session_factory, db, provider
):
    """Named rather than counted, so a regression says which capability it broke."""
    settings = Settings(
        admin_token="t" * 32, app_env="test", youtube_enabled=True, reviews_enabled=True
    )
    with UnitOfWork(session_factory) as uow:
        GameSyncService(provider, settings).sync(uow, "elden-ring")

    queued = {
        job.job_type: job.payload for job in db.execute(sa.select(Job)).scalars().all()
    }
    for job_type in ("reviews.sync", "similarity.recompute", "youtube.discover"):
        assert job_type in queued, f"{job_type} was not queued"
        assert "game_id" in queued[job_type], f"{job_type} still has no game_id"
