"""The schema-drift circuit.

``SchemaDriftError`` is permanent for one game: a required field vanished from one
response, that game is skipped, the crawl continues. That is right for one game and wrong
for the tenth in a row — ten consecutive parse failures is not ten odd games, it is the
source having changed shape, and continuing to crawl hourly against a parser that no
longer works produces nothing but dead jobs.

``SCHEMA_DRIFT_THRESHOLD`` is the line between those two readings. The count lives in the
append-only event log rather than in a process, because the thing being counted spans
runs, workers and restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

#: Emitted when a game cannot be parsed at all.
SCHEMA_DRIFT_EVENT = "schema.drift"
#: Emitted when one can. Resets the counter — the threshold is about CONSECUTIVE failures.
SCHEMA_OK_EVENT = "schema.ok"
#: Emitted once when the circuit opens, so the alert fires exactly once per incident.
SCHEMA_CIRCUIT_EVENT = "schema.drift_circuit_open"


def consecutive_drift(uow: UnitOfWork) -> int:
    return uow.events.consecutive(SCHEMA_DRIFT_EVENT, reset_event=SCHEMA_OK_EVENT)


def circuit_already_announced(uow: UnitOfWork) -> bool:
    """Has this incident already been alerted on?

    Same reset event as the counter, so one successful parse both closes the circuit and
    re-arms the alert for the next incident.
    """
    return uow.events.consecutive(SCHEMA_CIRCUIT_EVENT, reset_event=SCHEMA_OK_EVENT) > 0


def drift_circuit_open(uow: UnitOfWork, threshold: int) -> str | None:
    """The reason crawling must stop, or ``None``.

    A threshold of 0 or less disables the circuit, matching every other limit in the
    configuration: 0 means "no limit", never "block everything".
    """
    if threshold <= 0:
        return None
    count = consecutive_drift(uow)
    if count < threshold:
        return None
    return (
        f"{count} consecutive parse failures with no successful parse in between. "
        "The source has almost certainly changed shape; crawling is stopped so it does "
        "not fill the queue with dead jobs. Fix the parser, or set "
        "METACRITIC_SOURCE=html as a stopgap, then run a crawl manually to clear this."
    )
