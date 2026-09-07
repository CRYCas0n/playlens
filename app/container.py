"""Composition root.

The only place that knows which concrete implementation stands behind each protocol.
Swapping the Metacritic source, the LLM provider or the queue happens here and nowhere
else — which is what makes the abstractions in ``adapters/`` worth having.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.http.ratelimit import BucketConfig, NullRateLimiter, RateLimiter
from app.adapters.http.transport import HttpTransport, TransportConfig
from app.adapters.llm.base import LLMProvider, NullLLMProvider
from app.adapters.metacritic.provider import MetacriticProvider, build_provider
from app.config import Settings, get_settings
from app.db.base import make_engine, make_session_factory
from app.db.uow import UnitOfWork
from app.logging import configure_logging, get_logger
from app.services.crawl_orchestrator import CrawlOrchestrator
from app.services.game_sync import GameSyncService
from app.services.review_sync import ReviewSyncService
from app.services.similarity_service import SimilarityService
from app.services.snapshot_service import SnapshotService
from app.services.summary_service import SummaryService

log = get_logger(__name__)


@dataclass
class Container:
    settings: Settings

    @cached_property
    def engine(self) -> Engine:
        return make_engine(self.settings)

    @cached_property
    def session_factory(self) -> sessionmaker[Session]:
        return make_session_factory(self.engine)

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.session_factory)

    # ------------------------------------------------------------------ adapters

    @cached_property
    def rate_limiter(self):
        if self.settings.app_env == "test":
            return NullRateLimiter()
        return RateLimiter(
            self.session_factory,
            BucketConfig(
                key="metacritic",
                rate_per_s=self.settings.metacritic_rps,
                burst=float(self.settings.metacritic_burst),
            ),
        )

    @cached_property
    def transport(self) -> HttpTransport:
        return HttpTransport(
            TransportConfig(
                user_agent=self.settings.user_agent,
                timeout_s=self.settings.request_timeout_s,
                connect_timeout_s=self.settings.request_connect_timeout_s,
                max_retries=self.settings.request_max_retries,
                backoff_base_s=self.settings.request_backoff_base_s,
                backoff_max_s=self.settings.request_backoff_max_s,
                circuit_fail_threshold=self.settings.circuit_fail_threshold,
                circuit_reset_timeout_s=self.settings.circuit_reset_timeout_s,
            ),
            rate_limiter=self.rate_limiter,
        )

    @cached_property
    def metacritic(self) -> MetacriticProvider:
        return build_provider(self.settings, self.transport)

    @cached_property
    def llm(self) -> LLMProvider:
        """Null unless explicitly enabled AND configured.

        A missing key is a supported state, not a crash: summaries stay pending and every
        other feature works (ADR-016 applies the same principle to YouTube).
        """
        if not self.settings.llm_enabled:
            return NullLLMProvider()
        if self.settings.llm_provider == "fixture":
            from app.adapters.llm.fixture import FixtureLLMProvider

            return FixtureLLMProvider(self.settings.llm_model)
        if self.settings.llm_provider == "null":
            return NullLLMProvider()
        key = self.settings.llm_api_key.get_secret_value()
        if not key:
            log.warning("llm.disabled", extra={"reason": "LLM_API_KEY is empty"})
            return NullLLMProvider()
        if self.settings.llm_provider == "openai":
            from app.adapters.llm.openai_client import OpenAILLMProvider

            return OpenAILLMProvider(
                api_key=key,
                model=self.settings.llm_model,
                timeout_s=self.settings.llm_timeout_s,
                max_retries=self.settings.llm_max_retries,
            )

        from app.adapters.llm.anthropic_client import AnthropicLLMProvider

        return AnthropicLLMProvider(
            api_key=key,
            model=self.settings.llm_model,
            timeout_s=self.settings.llm_timeout_s,
            max_retries=self.settings.llm_max_retries,
        )

    @cached_property
    def youtube(self):
        from app.adapters.youtube.provider import build_youtube_provider

        return build_youtube_provider(self.settings, self.transport)

    # ------------------------------------------------------------------ services

    @cached_property
    def snapshots(self) -> SnapshotService:
        return SnapshotService(self.settings)

    @cached_property
    def crawl(self) -> CrawlOrchestrator:
        return CrawlOrchestrator(self.metacritic, self.settings)

    @cached_property
    def game_sync(self) -> GameSyncService:
        return GameSyncService(self.metacritic, self.settings)

    @cached_property
    def review_sync(self) -> ReviewSyncService:
        return ReviewSyncService(self.metacritic, self.settings)

    @cached_property
    def summaries(self) -> SummaryService:
        return SummaryService(self.llm, self.settings, snapshots=self.snapshots)

    @cached_property
    def similarity(self) -> SimilarityService:
        return SimilarityService(self.settings)

    @cached_property
    def letsplay(self):
        from app.services.letsplay_service import LetsPlayService, build_cascade

        return LetsPlayService(
            self.youtube,
            self.llm,
            self.settings,
            cascade=build_cascade(self.settings, self.transport),
        )

    def close(self) -> None:
        self.transport.close()


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        settings = get_settings()
        configure_logging(settings)
        _container = Container(settings=settings)
    return _container


def reset_container() -> None:
    """Test helper."""
    global _container
    _container = None
