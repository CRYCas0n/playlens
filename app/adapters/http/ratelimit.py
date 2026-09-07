"""Token bucket backed by the database.

Process-local buckets would let N workers do N times the configured rate against a source
we promised to be polite to (ADR-001). One row per host, one atomic update, exact at any
worker count.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import RateLimit
from app.repositories.base import insert_ignore_returning, utc_now


@dataclass(frozen=True, slots=True)
class BucketConfig:
    key: str
    rate_per_s: float
    burst: float


class RateLimiter:
    """Blocking token bucket. ``acquire`` returns once a token is available."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: BucketConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_s: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._sleep = sleep
        self._max_wait_s = max_wait_s

    def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens`` from the bucket, waiting if necessary. Returns seconds waited."""
        waited = 0.0
        while True:
            delay = self._try_take(tokens)
            if delay <= 0:
                return waited
            delay = min(delay, self._max_wait_s)
            self._sleep(delay)
            waited += delay

    def _try_take(self, tokens: float) -> float:
        """Return 0 if taken, otherwise the seconds to wait before trying again."""
        with self._session_factory() as session:
            now = utc_now()
            row = session.get(
                RateLimit, self._config.key, with_for_update=self._supports_lock(session)
            )
            if row is None:
                insert_ignore_returning(
                    session,
                    RateLimit.__table__,
                    {
                        "bucket_key": self._config.key,
                        "tokens": self._config.burst,
                        "updated_at": now,
                    },
                    index_elements=["bucket_key"],
                    returning=[RateLimit.__table__.c.bucket_key],
                )
                session.commit()
                row = session.get(RateLimit, self._config.key)
                assert row is not None

            elapsed = max(0.0, (now - row.updated_at).total_seconds())
            available = min(self._config.burst, row.tokens + elapsed * self._config.rate_per_s)

            if available >= tokens:
                row.tokens = available - tokens
                row.updated_at = now
                session.commit()
                return 0.0

            deficit = tokens - available
            # Persist the refill we accounted for so concurrent callers see it.
            row.tokens = available
            row.updated_at = now
            session.commit()
            return deficit / self._config.rate_per_s

    @staticmethod
    def _supports_lock(session: Session) -> bool:
        return session.get_bind().dialect.name != "sqlite"

    def reset(self) -> None:
        with self._session_factory() as session:
            session.execute(
                sa.delete(RateLimit).where(RateLimit.bucket_key == self._config.key)
            )
            session.commit()


class NullRateLimiter:
    """For tests and for adapters that talk to nothing."""

    def acquire(self, tokens: float = 1.0) -> float:
        return 0.0
