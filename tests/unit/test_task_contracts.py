"""The retry ceilings, checked against the thing they have to survive.

A ceiling is not a taste question: with exponential backoff it buys a span of wall clock,
and that span either outlives the failure it exists for or it does not. Six attempts on
`summary.generate` bought about two minutes, and the failure was a per-minute token quota
the queue itself was saturating -- so all six burned inside one window and the job died
against a limit that would have cleared on its own.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.tasks.contracts import MAX_ATTEMPTS, max_attempts_for


def backoff_span_s(attempts: int, settings: Settings) -> float:
    """Total wall clock a job survives: the sum of its backoff waits."""
    total = 0.0
    for attempt in range(attempts - 1):
        total += min(
            settings.job_retry_backoff_base_s * (2**attempt),
            settings.job_retry_backoff_max_s,
        )
    return total


def test_an_undeclared_task_is_not_retried():
    assert max_attempts_for("nothing.declared.this") == 1


#: Everything that calls the language model, and so meets its per-minute quota.
MODEL_JOBS = ["summary.generate", "summary.gap", "youtube.summarise"]


@pytest.mark.parametrize("job_type", MODEL_JOBS)
def test_model_jobs_outlive_a_rate_limit_window(job_type: str):
    """A provider's token quota resets every minute; riding one out needs several."""
    settings = Settings(admin_token="t" * 32, app_env="test")
    span = backoff_span_s(MAX_ATTEMPTS[job_type], settings)
    assert span >= 300, (
        f"{job_type} survives only {span:.0f}s of backoff; a per-minute quota that the "
        "queue is saturating lasts longer than that"
    )


def test_the_crawl_tick_is_never_retried():
    """Two ticks racing for one run slot is worse than a missed tick: the next hour
    does it again anyway."""
    assert MAX_ATTEMPTS["crawl.tick"] == 1


def test_every_declared_ceiling_is_at_least_one():
    for job_type, attempts in MAX_ATTEMPTS.items():
        assert attempts >= 1, job_type
