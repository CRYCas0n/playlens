from __future__ import annotations

import json
import logging

import pytest

from app.config import Settings, get_settings, reset_settings_cache
from app.logging import configure_logging, correlate, get_logger, redact


class TestSettings:
    def test_defaults_match_the_assignment(self):
        s = Settings()
        # CONTRADICTIONS C-26: the assignment says 20 per run, the old blueprint said 40.
        assert s.crawl_max_games_per_run == 20
        assert s.new_releases_limit == 20
        # Bonuses off by default: no keys are configured out of the box (ADR-016).
        assert s.youtube_enabled is False
        assert s.llm_enabled is False
        assert s.yt_asr_enabled is False

    def test_thresholds_from_adrs(self):
        s = Settings()
        assert s.userscore_zero_min_ratings == 20        # ADR-002
        assert s.agreement_threshold == 7                # ADR-012 / design
        assert s.summary_min_critic_reviews == 5         # ADR-009
        assert s.summary_min_user_reviews == 20          # ADR-009
        assert s.claim_min_support == 3                  # ADR-008
        assert s.temporal_min_span_days == 30            # ADR-008

    def test_rate_limit_guard_rejects_impolite_values(self):
        with pytest.raises(ValueError, match="polite"):
            Settings(metacritic_rps=50)

    def test_rate_limit_guard_rejects_nonsense(self):
        with pytest.raises(ValueError):
            Settings(metacritic_rps=0)

    def test_secrets_are_masked_in_repr(self, monkeypatch):
        monkeypatch.setenv("ADMIN_TOKEN", "super-secret-value")
        monkeypatch.setenv("LLM_API_KEY", "sk-ant-do-not-print-me")
        reset_settings_cache()
        text = repr(get_settings())
        assert "super-secret-value" not in text
        assert "sk-ant-do-not-print-me" not in text
        assert "***" in text

    def test_public_api_key_is_not_treated_as_a_secret(self):
        # It is lifted from Metacritic's own frontend and is not validated server-side.
        # Pretending it is a secret would be theatre (ADR-001).
        s = Settings()
        assert s.metacritic_api_key

    def test_require_admin_token_fails_loudly_when_unset(self):
        s = Settings(admin_token="")
        with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
            s.require_admin_token()

    def test_derived_lists(self):
        s = Settings(worker_queues="crawl, enrich ,ai", image_allowed_widths="96,200,640")
        assert s.queues == ["crawl", "enrich", "ai"]
        assert s.allowed_image_widths == {96, 200, 640}
        # Metacritic for covers, YouTube for Let'''s Play thumbnails -- the two hosts the
        # templates actually render. The list is the SSRF control, so it tracks what is
        # rendered rather than being kept deliberately short.
        assert s.allowed_image_hosts == {"www.metacritic.com", "i.ytimg.com", "img.youtube.com"}

    def test_user_agent_identifies_us_with_a_contact(self):
        ua = Settings(contact_url="https://example.org/contact").user_agent
        assert "https://example.org/contact" in ua
        assert "not affiliated" in ua


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        ["api_key", "ADMIN_TOKEN", "db_password", "Authorization", "cookie", "llm_api_key"],
    )
    def test_secret_keys_are_blanked(self, key):
        assert redact({key: "value"})[key] == "***"

    def test_ordinary_keys_survive(self):
        assert redact({"slug": "elden-ring"})["slug"] == "elden-ring"

    def test_nested_structures(self):
        payload = {"outer": {"api_key": "x", "items": [{"token": "y", "id": 1}]}}
        out = redact(payload)
        assert out["outer"]["api_key"] == "***"
        assert out["outer"]["items"][0]["token"] == "***"
        assert out["outer"]["items"][0]["id"] == 1

    def test_recursion_is_bounded(self):
        deep: dict = {"a": {}}
        node = deep["a"]
        for _ in range(20):
            node["a"] = {}
            node = node["a"]
        redact(deep)  # must not blow the stack


class TestLogging:
    def test_correlation_fields_reach_the_record(self, capsys):
        settings = Settings(log_format="json", log_level="INFO")
        configure_logging(settings)
        log = get_logger("test")
        with correlate(crawl_run_id=412, game_id=8123, slug="elden-ring", stage="fetching"):
            log.info("game.synced")
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["crawl_run_id"] == 412
        assert payload["game_id"] == 8123
        assert payload["slug"] == "elden-ring"
        assert payload["stage"] == "fetching"
        assert payload["level"] == "INFO"

    def test_correlation_is_scoped(self, capsys):
        configure_logging(Settings(log_format="json"))
        log = get_logger("test")
        with correlate(game_id=1):
            pass
        log.info("outside")
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "game_id" not in payload

    def test_secrets_never_reach_the_stream(self, capsys):
        configure_logging(Settings(log_format="json"))
        get_logger("test").info("call", extra={"api_key": "sk-leak"})
        assert "sk-leak" not in capsys.readouterr().out

    def test_console_format_is_readable(self, capsys):
        configure_logging(Settings(log_format="console"))
        with correlate(slug="elden-ring"):
            get_logger("test").warning("slow")
        out = capsys.readouterr().out
        assert "slug=elden-ring" in out
        assert "slow" in out

    def test_noisy_libraries_are_quietened(self):
        configure_logging(Settings())
        assert logging.getLogger("httpx").level == logging.WARNING
