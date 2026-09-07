"""A worker id is hostname:pid, so restarts accumulate rows nobody deletes.

After a day of deploys the monitoring page listed twenty-nine workers for a service
running one, twenty-eight of them labelled "idle" -- which is the wrong word for a
process that does not exist, and precisely the kind of permanently-wrong indicator an
operator stops reading.

Two separate fixes, so both are checked here: the rows are pruned by retention, and a
worker that has missed its heartbeat window reads as gone rather than resting.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.uow import UnitOfWork

pytestmark = pytest.mark.integration


def test_a_worker_that_stopped_reporting_is_forgotten(session_factory, db):
    with UnitOfWork(session_factory) as uow:
        uow.workers.heartbeat("old-host:1", queues="ai", active_jobs=0, version="1")
        uow.workers.heartbeat("live-host:2", queues="ai", active_jobs=1, version="1")

    # Age one of them past any plausible restart window.
    from app.db.models import WorkerHeartbeat

    stale = db.get(WorkerHeartbeat, "old-host:1")
    stale.heartbeat_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=2)
    db.commit()

    with UnitOfWork(session_factory) as uow:
        removed = uow.workers.purge_old(
            dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=6)
        )

    assert removed == 1
    with UnitOfWork(session_factory) as uow:
        remaining = {w.worker_id for w in uow.workers.all()}
    assert remaining == {"live-host:2"}


def test_retention_keeps_a_worker_that_is_merely_between_jobs(session_factory):
    """Six hours is generous on purpose: a quiet worker is not a dead one."""
    with UnitOfWork(session_factory) as uow:
        uow.workers.heartbeat("recent:3", queues="ai", active_jobs=0, version="1")

    with UnitOfWork(session_factory) as uow:
        removed = uow.workers.purge_old(
            dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=6)
        )

    assert removed == 0


def test_a_missed_heartbeat_reads_as_gone_not_idle(session_factory, db, db_settings):
    from app.db.models import WorkerHeartbeat
    from app.services.monitoring_service import MonitoringService

    with UnitOfWork(session_factory) as uow:
        uow.workers.heartbeat("silent:9", queues="ai", active_jobs=0, version="1")
    row = db.get(WorkerHeartbeat, "silent:9")
    row.heartbeat_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=3)
    db.commit()

    with UnitOfWork(session_factory) as uow:
        status = MonitoringService(db_settings).status(uow)

    states = {w["id"]: w["state"] for w in status.workers}
    assert states["silent:9"] == "gone", states
