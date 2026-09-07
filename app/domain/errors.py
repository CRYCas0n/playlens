"""Domain error taxonomy.

The distinction that matters operationally is *retryable vs permanent*: it decides whether
a job goes back into the queue or straight to ``failed``. Everything else is detail.
"""

from __future__ import annotations


class PlaylensError(Exception):
    """Base for everything this application raises deliberately."""

    code = "error"


class RetryableError(PlaylensError):
    """Transient: 429, 5xx, timeout, network, open circuit. The job returns to the queue."""

    code = "retryable"

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class PermanentError(PlaylensError):
    """Retrying will not help: 404, invalid slug, validation failure after normalisation."""

    code = "permanent"


class SchemaDriftError(PermanentError):
    """A required field disappeared from an upstream response.

    Permanent for the item, but an ALERT for the system: it means the source changed shape
    and the parser needs attention. Counted separately so the auto-fallback to HtmlSource
    can trigger on a threshold (ADR-001).
    """

    code = "schema_drift"


class BudgetExhausted(RetryableError):
    """An external quota or a cost ceiling was hit. The job is deferred, not failed."""

    code = "budget_exhausted"


class CircuitOpen(RetryableError):
    """The circuit breaker is open; we fail fast without touching the network."""

    code = "circuit_open"


class ProviderError(RetryableError):
    """A provider in a cascade failed. Includes the case of an empty HTTP 200 body.

    That case matters enough to state twice: YouTube's timedtext endpoint returns
    ``200`` with a zero-length body when a proof-of-origin token is missing. Treating it
    as "no subtitles exist" is the single most common way to get this wrong (ADR-016).
    """

    code = "provider_error"


class NotFoundError(PermanentError):
    code = "not_found"


class ConflictError(PlaylensError):
    """A state conflict a caller can act on, e.g. a crawl run is already active."""

    code = "conflict"


class ValidationRejected(PermanentError):
    """Model output failed evidence validation and must not be published (ADR-008)."""

    code = "validation_rejected"
