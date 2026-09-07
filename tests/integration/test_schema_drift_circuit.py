"""The schema-drift circuit (ADR-005, `SCHEMA_DRIFT_THRESHOLD`).

One game that will not parse is noise. Ten in a row is the source having changed shape,
and continuing to crawl against a broken parser produces nothing but dead jobs and an
alert that fires every hour until somebody mutes it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import Settings
from app.services.drift import (
    SCHEMA_CIRCUIT_EVENT,
    SCHEMA_DRIFT_EVENT,
    SCHEMA_OK_EVENT,
    consecutive_drift,
    drift_circuit_open,
)

pytestmark = pytest.mark.integration


def drift(uow, n: int = 1) -> None:
    for _ in range(n):
        uow.events.emit(SCHEMA_DRIFT_EVENT, level="error")


def ok(uow) -> None:
    uow.events.emit(SCHEMA_OK_EVENT)


class TestTheCounter:
    def test_nothing_recorded_means_nothing_wrong(self, db, uow):
        assert consecutive_drift(uow) == 0

    def test_it_counts_drift_events(self, db, uow):
        drift(uow, 3)
        db.flush()
        assert consecutive_drift(uow) == 3

    def test_one_successful_parse_resets_it(self, db, uow):
        """The threshold is about CONSECUTIVE failures, not a lifetime total."""
        drift(uow, 9)
        ok(uow)
        drift(uow, 2)
        db.flush()
        assert consecutive_drift(uow) == 2

    def test_the_count_is_not_held_in_a_process(self, db, uow, session_factory):
        """A counter that resets on restart would never reach a threshold of ten."""
        from app.db.uow import UnitOfWork

        drift(uow, 5)
        db.commit()
        with UnitOfWork(session_factory) as fresh:
            assert consecutive_drift(fresh) == 5


class TestTheCircuit:
    def test_it_stays_closed_below_the_threshold(self, db, uow):
        drift(uow, 9)
        db.flush()
        assert drift_circuit_open(uow, 10) is None

    def test_it_opens_at_the_threshold(self, db, uow):
        drift(uow, 10)
        db.flush()
        reason = drift_circuit_open(uow, 10)
        assert reason is not None
        assert "10 consecutive parse failures" in reason
        assert "METACRITIC_SOURCE=html" in reason, "the message has to name the stopgap"

    def test_a_threshold_of_zero_disables_it(self, db, uow):
        """0 means 'no limit' everywhere else in the config; it must here too."""
        drift(uow, 500)
        db.flush()
        assert drift_circuit_open(uow, 0) is None


class TestTheCrawlStops:
    @pytest.fixture
    def orchestrator(self, db_settings):
        from app.services.crawl_orchestrator import CrawlOrchestrator
        from tests.integration.test_crawl_orchestrator import Provider, source_with

        settings = Settings(
            admin_token="t" * 32,
            app_env="test",
            database_url=db_settings.database_url,
            schema_drift_threshold=3,
        )
        source = source_with(["ashen-veil", "northlight-drifters"])
        return CrawlOrchestrator(Provider(source), settings)

    def test_a_tick_refuses_to_run_and_says_why(self, db, uow, orchestrator):
        drift(uow, 3)
        db.flush()
        result = orchestrator.tick(uow)
        assert result.run_id is None
        assert result.reason == "schema_drift_circuit_open"

    def test_the_alert_fires_once_not_every_hour(self, db, uow, orchestrator):
        """An alert that repeats hourly is an alert an operator learns to ignore."""
        drift(uow, 3)
        db.flush()
        for _ in range(4):
            orchestrator.tick(uow)
        db.flush()
        fired = [e for e in uow.events.recent(limit=50) if e.event == SCHEMA_CIRCUIT_EVENT]
        assert len(fired) == 1

    def test_a_successful_parse_closes_the_circuit_again(self, db, uow, orchestrator):
        drift(uow, 3)
        db.flush()
        assert orchestrator.tick(uow).run_id is None

        ok(uow)
        db.flush()
        result = orchestrator.tick(uow)
        assert result.run_id is not None, "one good parse has to be enough to resume"

    def test_below_the_threshold_the_crawl_runs_normally(self, db, uow, orchestrator):
        drift(uow, 2)
        db.flush()
        assert orchestrator.tick(uow).run_id is not None


class TestTheTaskRecordsIt:
    def test_a_drift_error_is_recorded_and_a_good_parse_resets_it(
        self, db, uow, db_settings, monkeypatch
    ):
        """Without these two emits the counter has nothing to count."""
        from app.container import Container
        from app.domain.errors import SchemaDriftError
        from app.tasks.registry import game_sync

        container = Container(settings=db_settings)

        class Boom:
            def sync(self, *args, **kwargs):
                raise SchemaDriftError("title vanished")

        container.__dict__["game_sync"] = Boom()
        with pytest.raises(SchemaDriftError):
            game_sync(container, uow, {"slug": "ashen-veil"})
        db.flush()
        assert consecutive_drift(uow) == 1

        class Outcome:
            game_id, created, changed, follow_ups = 1, True, True, ()

        class Fine:
            def sync(self, *args, **kwargs):
                return Outcome()

        container.__dict__["game_sync"] = Fine()
        game_sync(container, uow, {"slug": "ashen-veil"})
        db.flush()
        assert consecutive_drift(uow) == 0


def test_the_event_names_are_stable(db, uow):
    """They are written to a durable log and queried by name; renaming one is a
    migration, not a refactor."""
    assert SCHEMA_DRIFT_EVENT == "schema.drift"
    assert SCHEMA_OK_EVENT == "schema.ok"
    assert SCHEMA_CIRCUIT_EVENT == "schema.drift_circuit_open"
    assert dt is not None


class TestItIsVisibleToAnOperator:
    def test_the_dashboard_reports_down_with_the_specific_reason(
        self, app_client, db, uow, db_settings, monkeypatch
    ):
        monkeypatch.setattr(db_settings, "schema_drift_threshold", 3)
        drift(uow, 3)
        db.commit()

        system = app_client.get("/api/v1/monitoring/status").json()["system"]
        assert system["status"] == "down"
        assert "consecutive parse failures" in system["message"]
        assert system["schema_drift_streak"] == 3

    def test_the_streak_is_reported_even_while_healthy(self, app_client, db, uow):
        drift(uow, 2)
        db.commit()
        system = app_client.get("/api/v1/monitoring/status").json()["system"]
        assert system["schema_drift_streak"] == 2
        assert system["status"] != "down", "two failures is noise, not an outage"
