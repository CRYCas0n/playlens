"""Application settings.

The ONLY place in the codebase that reads the environment. Everything else receives a
``Settings`` instance. Missing required variables fail at startup, not on the first request.

Secrets never appear in ``repr`` — see ``SecretStr`` fields and ``__repr__`` below.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Field names whose values must never be logged or shown in repr. The logging redactor
# (app/logging.py) uses the same list, so the two cannot drift.
SECRET_FIELD_MARKERS = ("_key", "_token", "_password", "_secret", "authorization", "cookie")


#: Removed rather than left declared-and-unread. A setting that looks like a control but
#: changes nothing is worse than no setting: it invites an operator to turn a knob during
#: an incident and conclude the system is broken when nothing happens.
#:
#: * ``METACRITIC_MAX_CONCURRENCY`` -- requests to the source are serialised through one
#:   token bucket by design (ADR-004). Concurrency is bounded at 1 by construction.
#: * ``HTTP_CACHE_ENABLED`` / ``HTTP_CACHE_DIR`` -- a response cache was never built; the
#:   tests replay captured fixtures instead, which is the same benefit without a cache to
#:   invalidate.
#: * ``YOUTUBE_ALLOW_SCRAPE_FALLBACK`` / ``YT_POT_PROVIDER_URL`` -- ADR-016 describes both
#:   as possible paths; neither is implemented, and a flag that silently does nothing when
#:   switched on is a trap during an incident.
#: * ``WORKER_CONCURRENCY`` -- one worker process runs one job at a time on purpose. The
#:   unit of concurrency is the process, so scaling means more replicas (docker compose
#:   ``--scale worker=N``), not a number in a file.
#: * ``JOB_DEFAULT_MAX_ATTEMPTS`` -- every task declares its own ceiling in
#:   ``app/tasks/contracts.py``, and a global default could only ever contradict it.
_REMOVED_SETTINGS = (
    "METACRITIC_MAX_CONCURRENCY",
    "HTTP_CACHE_ENABLED",
    "HTTP_CACHE_DIR",
    "YOUTUBE_ALLOW_SCRAPE_FALLBACK",
    "YT_POT_PROVIDER_URL",
    "WORKER_CONCURRENCY",
    "JOB_DEFAULT_MAX_ATTEMPTS",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- core
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Playlens"
    app_version: str = "1.0.0"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    contact_url: str = "https://example.com/contact"

    # ---------------------------------------------------------------- database
    database_url: str = "sqlite+pysqlite:///./playlens.db"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 15_000

    # ---------------------------------------------------------------- api
    api_prefix: str = "/api/v1"
    cors_allow_origins: str = ""
    admin_token: SecretStr = SecretStr("")
    admin_rate_limit_per_min: int = 10
    public_rate_limit_per_min: int = 120
    offset_hard_limit: int = 10_000

    # ---------------------------------------------------------------- metacritic
    metacritic_source: Literal["json_api", "html"] = "json_api"
    metacritic_base_url: str = "https://www.metacritic.com"
    metacritic_api_base_url: str = "https://backend.metacritic.com"
    # Public value lifted from Metacritic's own frontend config. Verified NOT to be
    # validated by the server (200 with no key and with a wrong key). Not a secret.
    metacritic_api_key: str = "1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u"
    metacritic_rps: float = 2.0
    metacritic_burst: int = 4
    request_timeout_s: float = 20.0
    request_connect_timeout_s: float = 5.0
    request_max_retries: int = 5
    request_backoff_base_s: float = 1.0
    request_backoff_max_s: float = 32.0
    circuit_fail_threshold: int = 5
    circuit_reset_timeout_s: int = 300
    schema_drift_threshold: int = 10

    # ---------------------------------------------------------------- crawler
    crawl_enabled: bool = True
    crawl_interval_cron: str = "7 * * * *"
    crawl_timezone: str = "UTC"
    # ASSIGNMENT REQUIREMENT: at most 20 games per hourly run (CONTRADICTIONS C-26).
    # Configurable downwards; the admin endpoint may lower it but never raise it.
    crawl_max_games_per_run: int = 20
    new_releases_limit: int = 20
    browse_page_size: int = 24
    browse_max_offset: int = 100_000
    crawl_item_lease_ttl_s: int = 1800
    crawl_item_max_attempts: int = 3
    reconcile_enabled: bool = True
    reconcile_cron: str = "0 3 * * 0"
    reconcile_max_new: int = 2000

    # ---------------------------------------------------------------- reviews
    reviews_enabled: bool = True
    reviews_page_size: int = 100
    reviews_critic_cap: int = 200
    reviews_user_cap: int = 500
    reviews_max_platforms: int = 3
    reviews_min_platform_critics: int = 3
    reviews_min_platform_users: int = 50
    reviews_stale_page_tolerance: int = 2
    reviews_incremental_max_pages: int = 5
    reviews_full_resync_days: int = 30
    reviews_min_interval_h: int = 24

    # ---------------------------------------------------------------- scores (ADR-002)
    userscore_zero_min_ratings: int = 20
    low_sample_threshold: int = 20
    agreement_threshold: int = 7
    gap_min_points: int = 7
    platform_gap_min_points: int = 10

    # ---------------------------------------------------------------- corpus (ADR-007)
    corpus_min_chars: int = 80
    corpus_max_review_chars: int = 1500
    corpus_min_stratum_share: float = 0.15
    corpus_ordering_version: int = 1
    corpus_sampling_version: int = 1
    snapshot_retention_days: int = 90

    # ---------------------------------------------------------------- ai (ADR-008/009)
    llm_enabled: bool = False
    llm_provider: Literal["anthropic", "openai", "null", "fixture"] = "anthropic"
    llm_api_key: SecretStr = SecretStr("")
    #: Must belong to the chosen provider. A Claude model name with LLM_PROVIDER=openai
    #: is a configuration mistake that would otherwise surface as a 404 on the first
    #: summary, hours later; `_llm_model_matches_provider` catches it at startup.
    llm_model: str = "claude-sonnet-5"
    llm_max_output_tokens: int = 4000
    llm_timeout_s: float = 300.0
    llm_max_retries: int = 6
    prompt_version_critic: str = "critic-v1"
    prompt_version_user: str = "user-v1"
    prompt_version_letsplay: str = "letsplay-v1"
    prompt_version_gap: str = "gap-v1"
    params_version: str = "p1"
    ai_max_input_tokens: int = 60_000
    ai_min_new_reviews: int = 10
    ai_min_new_ratio: float = 0.20
    ai_score_move_points: int = 3
    ai_max_summary_age_days: int = 90
    ai_daily_cost_limit_usd: float = 10.0
    ai_max_calls_per_hour: int = 100
    summary_min_critic_reviews: int = 5
    summary_min_user_reviews: int = 20
    claim_min_support: int = 3
    claim_min_support_small_corpus: int = 2
    small_corpus_threshold: int = 20
    temporal_min_span_days: int = 30
    summary_history_keep: int = 10

    # ---------------------------------------------------------------- similarity (ADR-011)
    similarity_top_n: int = 12
    similarity_candidate_limit: int = 300
    similarity_min_score: float = 0.34
    similarity_w_metadata: float = 0.62
    similarity_w_aspect: float = 0.26
    similarity_w_lexical: float = 0.12
    similarity_refresh_stale_days: int = 7
    similarity_refresh_cron: str = "0 4 * * *"
    reason_min_contribution: float = 0.18

    # ---------------------------------------------------------------- youtube (ADR-016)
    youtube_enabled: bool = False
    youtube_api_key: SecretStr = SecretStr("")
    youtube_daily_unit_budget: int = 9500
    youtube_search_max_results: int = 25
    yt_min_score: int = 60
    yt_min_duration_s: int = 600
    yt_max_duration_s: int = 25_200
    yt_allowed_langs: str = "en"
    yt_min_transcript_chars: int = 2000
    yt_research_after_days: int = 30
    yt_transcript_providers: str = "ytdlp,hosted_api,metadata_only"
    yt_proxy_url: str = ""
    yt_transcript_api_url: str = ""
    yt_transcript_api_key: SecretStr = SecretStr("")
    yt_asr_enabled: bool = False

    # ---------------------------------------------------------------- queue / worker
    worker_queues: str = "crawl,enrich,ai"
    worker_poll_interval_s: float = 1.0
    job_lease_ttl_s: int = 900
    job_retry_backoff_base_s: float = 2.0
    job_retry_backoff_max_s: float = 600.0
    scheduler_tick_interval_s: float = 20.0

    # ---------------------------------------------------------------- monitoring
    sse_heartbeat_s: int = 15
    sse_poll_interval_ms: int = 400
    sse_max_clients: int = 50
    sse_max_backlog: int = 2000
    events_retention_days: int = 30
    jobs_retention_days: int = 90
    crawl_items_retention_days: int = 180
    metrics_enabled: bool = True

    # ---------------------------------------------------------------- images (ADR-017)
    image_proxy_enabled: bool = True
    # The allow-list is the SSRF control (ADR-019), so it is a list of hosts this
    # application is known to render, not a convenience. i.ytimg.com is on it because the
    # Let's Play section renders YouTube thumbnails: without it every game page with a
    # video asked the proxy for an image it then refused, and showed a broken one.
    image_allowed_hosts: str = "www.metacritic.com,i.ytimg.com,img.youtube.com"
    image_allowed_widths: str = "96,200,320,480,640"
    image_cache_dir: str = ".cache/img"
    image_cache_max_mb: int = 512
    image_fetch_timeout_s: float = 10.0
    image_max_bytes: int = 8 * 1024 * 1024
    #: The design's typography (Space Grotesk numerals in particular) is part of its
    #: identity, and the fonts are loaded from Google's CDN as the prototype does. Turn
    #: this off to keep every request first-party; the token fallback stacks then apply.
    web_fonts_enabled: bool = True

    # ---------------------------------------------------------------- derived helpers
    @model_validator(mode="after")
    def _llm_model_matches_provider(self) -> Settings:
        """Catch a Claude model name pointed at OpenAI, and the reverse.

        Without this the mistake surfaces as a 404 on the first summary generation, which
        happens minutes to hours after the process starts and looks like a broken feature
        rather than a typo. The check costs nothing and fires at import.

        Only obviously-wrong pairings are rejected. A model this list has never heard of
        is allowed through: new ones ship constantly, and refusing to start because a
        name is unfamiliar would be worse than the problem being solved.
        """
        families = {"anthropic": ("claude",), "openai": ("gpt", "o1", "o3", "o4", "chatgpt")}
        wanted = families.get(self.llm_provider)
        if wanted is None:
            return self

        model = self.llm_model.lower()
        other = {p: fams for p, fams in families.items() if p != self.llm_provider}
        for provider, fams in other.items():
            if any(model.startswith(f) for f in fams) and not any(
                model.startswith(f) for f in wanted
            ):
                raise ValueError(
                    f"LLM_MODEL={self.llm_model!r} is a {provider} model, but "
                    f"LLM_PROVIDER={self.llm_provider!r}. Set one to match the other: "
                    f"for {self.llm_provider} try "
                    + ("claude-sonnet-5" if self.llm_provider == "anthropic" else "gpt-4o")
                )
        return self

    @field_validator("metacritic_rps")
    @classmethod
    def _rps_is_polite(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("metacritic_rps must be positive")
        if v > 5:
            raise ValueError(
                "metacritic_rps above 5 is not polite towards the source; "
                "see ADR-001 (rate limiting is a deliberate default, not an accident)"
            )
        return v

    @field_validator("similarity_w_metadata")
    @classmethod
    def _weights_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("similarity weights must be positive")
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def queues(self) -> list[str]:
        return [q.strip() for q in self.worker_queues.split(",") if q.strip()]

    @property
    def transcript_providers(self) -> list[str]:
        return [p.strip() for p in self.yt_transcript_providers.split(",") if p.strip()]

    @property
    def allowed_image_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.image_allowed_hosts.split(",") if h.strip()
        )

    @property
    def allowed_image_widths(self) -> frozenset[int]:
        return frozenset(
            int(w.strip()) for w in self.image_allowed_widths.split(",") if w.strip().isdigit()
        )

    @property
    def allowed_transcript_langs(self) -> frozenset[str]:
        return frozenset(
            x.strip().lower() for x in self.yt_allowed_langs.split(",") if x.strip()
        )

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def user_agent(self) -> str:
        return (
            f"{self.app_name}/{self.app_version} "
            f"(+{self.contact_url}; educational project; not affiliated with Metacritic)"
        )

    def require_admin_token(self) -> str:
        """Fail loudly rather than silently accepting an empty token."""
        token = self.admin_token.get_secret_value()
        if not token:
            raise RuntimeError(
                "ADMIN_TOKEN is not set. Admin endpoints are disabled until it is configured."
            )
        return token

    def __repr__(self) -> str:  # pragma: no cover - trivial
        redacted = {
            name: "***"
            if isinstance(getattr(self, name), SecretStr)
            or any(m in name for m in SECRET_FIELD_MARKERS)
            else getattr(self, name)
            for name in type(self).model_fields
        }
        return f"Settings({redacted})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: forget the cached instance so a new environment takes effect."""
    get_settings.cache_clear()
