"""Retry ceilings, declared once and imported by everyone.

Kept apart from ``registry.py`` because the registry holds handlers that need the
container, and the crawl orchestrator — which the container builds — has to ask how many
attempts a job gets. Splitting the numbers out breaks that cycle, and it also makes the
rule enforceable: the enqueue site cannot invent a ceiling, it can only look one up.

The numbers themselves are economics, not taste:

* ``crawl.tick`` runs once. The next hour will do it again anyway, so a retry only makes
  two ticks race for the same run slot.
* ``summary.generate`` gets the most, because it is the most expensive thing to lose and
  the model provider is the flakiest dependency.
* ``youtube.*`` get few: nothing downstream waits on them.
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
    "summary.generate": 6,
    "summary.gap": 4,
    "similarity.recompute": 3,
    "similarity.refresh_stale": 3,
    "youtube.discover": 3,
    "youtube.transcript": 4,
    "youtube.summarise": 3,
    "maintenance.cleanup": 2,
}


def max_attempts_for(job_type: str) -> int:
    """The declared retry ceiling for a task, or the undeclared-task fallback."""
    return MAX_ATTEMPTS.get(job_type, UNDECLARED_TASK_MAX_ATTEMPTS)
