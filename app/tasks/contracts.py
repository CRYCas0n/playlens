"""Retry ceilings, declared once and imported by everyone.

Kept apart from ``registry.py`` because the registry holds handlers that need the
container, and the crawl orchestrator — which the container builds — has to ask how many
attempts a job gets. Splitting the numbers out breaks that cycle, and it also makes the
rule enforceable: the enqueue site cannot invent a ceiling, it can only look one up.

The numbers themselves are economics, not taste:

* ``crawl.tick`` runs once. The next hour will do it again anyway, so a retry only makes
  two ticks race for the same run slot.
* ``summary.generate`` gets the most, because it is the most expensive thing to lose and
  the model provider is the flakiest dependency. The ceiling has to outlive the
  provider's rate-limit *window*, not just a blip: with a base of 2s the backoff doubles,
  so six attempts spanned about two minutes and a per-minute token quota that the queue
  itself was saturating outlasted every one of them. Nine spans roughly seventeen, which
  rides out a burst. Extra attempts are close to free here -- a rejected request spends
  no tokens, and the fingerprint guard means a late success is not a duplicate.
* ``youtube.discover`` and ``youtube.transcript`` get few: nothing downstream waits on
  them. ``youtube.summarise`` is the exception, because it calls the same model as the
  summaries and therefore meets the same per-minute quota.
"""

from __future__ import annotations

#: A job type nobody declared. Deliberately one: an undeclared task is a mistake, and
#: retrying a mistake five times only makes it slower to notice.
UNDECLARED_TASK_MAX_ATTEMPTS = 1

MAX_ATTEMPTS: dict[str, int] = {
    "crawl.tick": 1,
    "crawl.reap": 3,
    "crawl.reconcile": 3,
    "game.sync": 5,
    "reviews.sync": 5,
    "summary.generate": 9,
    "summary.gap": 9,
    "similarity.recompute": 3,
    "similarity.refresh_stale": 3,
    "youtube.discover": 3,
    "youtube.transcript": 4,
    "youtube.summarise": 9,
    "maintenance.cleanup": 2,
}


def max_attempts_for(job_type: str) -> int:
    """The declared retry ceiling for a task, or the undeclared-task fallback."""
    return MAX_ATTEMPTS.get(job_type, UNDECLARED_TASK_MAX_ATTEMPTS)
