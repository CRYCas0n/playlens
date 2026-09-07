"""Unit of work: one session, one set of repositories, explicit transaction boundaries.

Services take a ``UnitOfWork`` and never touch a session directly. The boundaries that
matter (FINAL_SPEC section 23) are then visible as ``with uow:`` blocks rather than
scattered ``commit()`` calls.
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from app.repositories.crawl import CrawlRepository
from app.repositories.games import GameRepository
from app.repositories.jobs import EventRepository, JobRepository, WorkerRepository
from app.repositories.reviews import ReviewRepository
from app.repositories.similarity import SimilarityRepository
from app.repositories.summaries import (
    GapRepository,
    SnapshotRepository,
    SummaryRepository,
)
from app.repositories.youtube import BudgetRepository, YoutubeRepository


class UnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> UnitOfWork:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    @classmethod
    def bound(cls, session: Session) -> UnitOfWork:
        """Wrap a session someone else owns.

        For callers already inside a transaction -- tests, and any future code that has to
        run two services against one session. The lifecycle stays with the owner: this
        object will not commit or close the session it was handed.
        """
        unit = cls(lambda: session)
        unit._session = session
        return unit

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("UnitOfWork used outside a `with` block")
        return self._session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def flush(self) -> None:
        self.session.flush()

    # ------------------------------------------------------------------ repositories

    @property
    def games(self) -> GameRepository:
        return GameRepository(self.session)

    @property
    def reviews(self) -> ReviewRepository:
        return ReviewRepository(self.session)

    @property
    def crawl(self) -> CrawlRepository:
        return CrawlRepository(self.session)

    @property
    def jobs(self) -> JobRepository:
        return JobRepository(self.session)

    @property
    def events(self) -> EventRepository:
        return EventRepository(self.session)

    @property
    def workers(self) -> WorkerRepository:
        return WorkerRepository(self.session)

    @property
    def snapshots(self) -> SnapshotRepository:
        return SnapshotRepository(self.session)

    @property
    def summaries(self) -> SummaryRepository:
        return SummaryRepository(self.session)

    @property
    def gaps(self) -> GapRepository:
        return GapRepository(self.session)

    @property
    def similarity(self) -> SimilarityRepository:
        return SimilarityRepository(self.session)

    @property
    def youtube(self) -> YoutubeRepository:
        return YoutubeRepository(self.session)

    @property
    def budgets(self) -> BudgetRepository:
        return BudgetRepository(self.session)
