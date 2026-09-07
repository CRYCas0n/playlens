"""Let's Play enrichment: discover, transcribe, summarise.

Three separate jobs rather than one, because they fail for three unrelated reasons — a
quota, a missing caption track, an LLM budget — and merging them would make one failure
retry the other two. None of them is a precondition for anything else in the pipeline:
this is the isolation ADR-016 section 1 requires, and it is what makes the assignment's
scenario 3 ("YouTube quota exhausted -> the game is not failed") true rather than hoped.

Every method returns a dict describing what happened. The worker records it; nothing here
raises to signal an ordinary negative outcome, because "no suitable video exists" is an
answer, not an error.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from app.adapters.llm.base import LLMUnavailable
from app.adapters.youtube import ranking
from app.adapters.youtube.provider import search_query
from app.adapters.youtube.transcripts.base import TranscriptCascade
from app.ai import prompts
from app.ai.schemas import LetsPlayOut
from app.config import Settings
from app.domain.enums import (
    Audience,
    ClaimSide,
    ClaimType,
    ClaimValidation,
    SummaryStatus,
    TranscriptStatus,
)
from app.domain.errors import BudgetExhausted, ProviderError
from app.logging import get_logger
from app.normalizers.text import sanitize_for_prompt, stable_fingerprint
from app.services.summary_service import ai_budget_exceeded

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)

YOUTUBE_BUDGET = "youtube"

#: The transcript is raw material for the model, not content for a reader, but it still
#: has to fit in a context window: three hours of speech is roughly 200,000 characters.
TRANSCRIPT_PROMPT_CHARS = 48_000


class LetsPlayService:
    def __init__(self, provider, llm, settings: Settings, *, cascade=None) -> None:
        self._provider = provider
        self._llm = llm
        self._settings = settings
        self._cascade = cascade

    # ------------------------------------------------------------------ 1. discover

    def discover(self, uow: UnitOfWork, *, game_id: int) -> dict[str, Any]:
        """Spend one search on a game, once in its lifetime."""
        if not self._settings.youtube_enabled or not self._provider.enabled:
            return {"status": "skipped", "reason": "youtube_disabled"}

        game = uow.games.get(game_id)
        if game is None:
            return {"status": "skipped", "reason": "unknown_game"}

        gate = self._gate(game)
        if gate is not None:
            return {"status": "skipped", "reason": gate}

        units = self._provider.search_units()
        today = dt.date.today()
        remaining = uow.budgets.remaining(
            YOUTUBE_BUDGET, today, limit=self._settings.youtube_daily_unit_budget
        )
        if remaining < units:
            # Deferred, not failed: tomorrow's budget will run it, and the game's own
            # status is untouched (assignment scenario 3).
            return {"status": "deferred", "reason": "quota_exhausted", "retry_after_days": 1}

        try:
            candidates = self._provider.discover(
                search_query(game.title),
                max_results=self._settings.youtube_search_max_results,
            )
        except BudgetExhausted as exc:
            uow.events.emit(
                "youtube.quota_exhausted", level="warning", game_id=game_id,
                message=str(exc)[:500],
            )
            return {"status": "deferred", "reason": "quota_exhausted"}
        except ProviderError as exc:
            uow.events.emit(
                "youtube.discovery_failed", level="warning", game_id=game_id,
                message=str(exc)[:500],
            )
            return {"status": "failed", "reason": "provider_error"}
        finally:
            # The units are spent whether or not the response was useful. Counting only
            # successes is how a budget silently overruns.
            uow.budgets.spend(
                YOUTUBE_BUDGET, today, units=units, call="search+videos",
                limit=self._settings.youtube_daily_unit_budget,
            )

        uow.games.touch(game_id, youtube_searched_at=dt.datetime.now(dt.UTC))

        if not candidates:
            return {"status": "empty", "candidates": 0}

        ranked = ranking.rank(
            candidates,
            game_title=game.title,
            release_date=game.release_date,
            min_duration_s=self._settings.yt_min_duration_s,
            max_duration_s=self._settings.yt_max_duration_s,
            allowed_languages=self._settings.allowed_transcript_langs,
        )
        ids = uow.youtube.record_candidates(
            game_id,
            [(r.candidate, r.score, r.components, r.rejected_reason) for r in ranked],
        )

        chosen = ranking.select(ranked, min_score=self._settings.yt_min_score / 100)
        if chosen is None:
            # No video is a supported outcome: the section disappears (EDGE_CASES 12).
            # Padding it with the best of a bad field would be worse than showing nothing.
            uow.events.emit(
                "youtube.no_suitable_video", game_id=game_id,
                data={"candidates": len(ranked),
                      "rejected": [r.rejected_reason for r in ranked if r.rejected_reason]},
            )
            return {"status": "no_match", "candidates": len(ranked)}

        row = uow.youtube.select(game_id, ids[chosen.candidate.video_id])
        uow.events.emit(
            "youtube.video_selected", game_id=game_id,
            data={"video_id": chosen.candidate.video_id, "score": chosen.score},
        )
        return {
            "status": "selected",
            "video_id": chosen.candidate.video_id,
            "row_id": row.id,
            "score": chosen.score,
            "candidates": len(ranked),
        }

    def _gate(self, game) -> str | None:
        """Quota is finite; spend it on games somebody is looking for (ADR-016 §2)."""
        if game.youtube_searched_at is not None:
            age_days = (dt.datetime.now(dt.UTC) - game.youtube_searched_at).days
            if age_days < self._settings.yt_research_after_days:
                return "already_searched"
        if game.best_metascore is None:
            return "no_metascore"
        if (
            game.best_metascore < self._settings.yt_min_score
            and (game.user_reviews_total or 0) < 50
        ):
            return "below_interest_threshold"
        return None

    # ------------------------------------------------------------------ 2. transcript

    def fetch_transcript(self, uow: UnitOfWork, *, game_id: int) -> dict[str, Any]:
        if not self._settings.youtube_enabled:
            return {"status": "skipped", "reason": "youtube_disabled"}

        video = uow.youtube.selected(game_id)
        if video is None:
            return {"status": "skipped", "reason": "no_selected_video"}

        existing = uow.youtube.transcript(video.id)
        if existing is not None and existing.status == TranscriptStatus.AVAILABLE.value:
            return {"status": "cached", "chars": existing.char_count}

        cascade = self._cascade
        if cascade is None:
            return {"status": "skipped", "reason": "no_transcript_providers"}

        outcome = cascade.fetch(self._as_candidate(video))

        if outcome.result is None:
            # Deliberately UNAVAILABLE with the whole attempt log attached. "No captions"
            # and "every provider failed" look identical from the outside, and the log is
            # the only thing that distinguishes them later (ADR-016 section 5).
            uow.youtube.save_transcript(
                video_row_id=video.id,
                status=TranscriptStatus.UNAVAILABLE,
                source=None,
                text=None,
                language=None,
                is_auto_generated=None,
                attempts=outcome.attempts,
                error_message="; ".join(
                    str(a.get("error")) for a in outcome.attempts if not a.get("ok")
                )[:1000],
            )
            uow.events.emit(
                "youtube.transcript_unavailable", level="warning", game_id=game_id,
                data={"attempts": outcome.attempts},
            )
            return {"status": "unavailable", "attempts": outcome.attempts}

        uow.youtube.save_transcript(
            video_row_id=video.id,
            status=TranscriptStatus.AVAILABLE,
            source=outcome.result.source,
            text=outcome.result.text,
            language=outcome.result.language,
            is_auto_generated=outcome.result.is_auto_generated,
            attempts=outcome.attempts,
        )
        return {
            "status": "available",
            "source": outcome.result.source,
            "chars": outcome.result.char_count,
        }

    @staticmethod
    def _as_candidate(video):
        from app.domain.models import VideoCandidate

        return VideoCandidate(
            video_id=video.video_id,
            title=video.title,
            channel_id=video.channel_id,
            channel_title=video.channel_title,
            description=video.description,
            duration_s=video.duration_s,
            view_count=video.view_count,
            like_count=video.like_count,
            published_at=video.published_at,
            has_captions=video.has_captions,
            default_audio_language=video.default_audio_language,
            live_broadcast=video.live_broadcast,
            thumbnail_url=video.thumbnail_url,
        )

    # ------------------------------------------------------------------ 3. summarise

    def summarise(self, uow: UnitOfWork, *, game_id: int) -> dict[str, Any]:
        """A summary of the playthrough, or nothing at all.

        There is no middle path. Writing an "impression of how it plays" from a video
        title is the single thing ADR-016 forbids outright: maximum invention, minimum
        value, and indistinguishable to a reader from a summary grounded in the video.
        """
        if not self._settings.youtube_enabled:
            return {"status": "skipped", "reason": "youtube_disabled"}

        video = uow.youtube.selected(game_id)
        if video is None:
            return {"status": "skipped", "reason": "no_selected_video"}

        transcript = uow.youtube.transcript(video.id)
        if transcript is None or transcript.status != TranscriptStatus.AVAILABLE.value:
            return {"status": "skipped", "reason": "no_transcript"}

        game = uow.games.get(game_id)
        lead = next((gp for gp in uow.games.platforms_for(game_id) if gp.is_lead), None)
        if game is None or lead is None:
            return {"status": "skipped", "reason": "no_lead_platform"}

        fingerprint = stable_fingerprint(
            [video.video_id, str(transcript.char_count), self._settings.prompt_version_letsplay]
        )
        current = uow.summaries.current(
            game_platform_id=lead.id, audience=Audience.LETSPLAY
        )
        if current is not None and current.input_fingerprint == fingerprint:
            return {"status": "unchanged"}

        # The same ceiling as review summaries: they draw on one budget, so a Let's Play
        # backlog must not be able to spend the day's allowance on its own.
        over = ai_budget_exceeded(uow, self._settings)
        if over is not None:
            reason, retry_after_s = over
            uow.events.emit(
                "ai.budget_exhausted", level="warning", game_id=game_id, message=reason
            )
            # Raised, not returned: "deferred" as a return value completed the job and
            # spent its key, which is a deferral in name only.
            raise BudgetExhausted(reason, retry_after_s=retry_after_s)

        text = sanitize_for_prompt(transcript.text or "", max_chars=TRANSCRIPT_PROMPT_CHARS)
        prompt = prompts.build_letsplay_prompt(
            title=game.title,
            video_title=video.title,
            channel=video.channel_title or "unknown channel",
            duration=_hms(video.duration_s),
            transcript=text,
        )

        try:
            result = self._llm.complete_structured(
                prompt=prompt,
                schema=LetsPlayOut,
                max_tokens=self._settings.llm_max_output_tokens,
            )
        except LLMUnavailable:
            return {"status": "skipped", "reason": "llm_disabled"}
        except ProviderError as exc:
            uow.summaries.save(
                game_id=game_id,
                game_platform_id=lead.id,
                audience=Audience.LETSPLAY,
                snapshot_id=None,
                status=SummaryStatus.FAILED,
                input_fingerprint=fingerprint,
                claims=[],
                error_message=str(exc)[:1000],
            )
            return {"status": "failed", "reason": "provider_error"}

        output: LetsPlayOut = result.value
        claims = [
            {
                # The enums, not string literals. `claim_type` was written as
                # "observation", which is not a ClaimType and never was: the check
                # constraint refused every row, so no Let's Play summary could be saved
                # at all. It went unnoticed because the chain that reaches this code was
                # itself never joined up. A point drawn from a transcript is descriptive.
                "side": ClaimSide.POSITIVE.value,
                "aspect": point.aspect,
                "claim": point.text,
                "claim_type": ClaimType.DESCRIPTIVE.value,
                "evidence_refs": [],
                # There is no per-claim evidence to validate against: the whole summary is
                # grounded in one transcript, and that provenance is stated in the UI
                # rather than implied by a citation count that does not exist.
                "validation": ClaimValidation.ACCEPTED.value,
                "validation_detail": "single-source transcript",
            }
            for point in output.points
        ]

        uow.summaries.save(
            game_id=game_id,
            game_platform_id=lead.id,
            audience=Audience.LETSPLAY,
            snapshot_id=None,
            status=SummaryStatus.FRESH,
            input_fingerprint=fingerprint,
            claims=claims,
            heading=output.heading,
            overall=output.overall,
            confidence=output.confidence,
            reviews_used=1,
            reviews_candidates=1,
            llm_provider=result.provider,
            llm_model=result.model,
            prompt_version=self._settings.prompt_version_letsplay,
            params_version=self._settings.params_version,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )
        return {"status": "generated", "points": len(claims)}


def _hms(seconds: int | None) -> str:
    if not seconds:
        return "unknown length"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_cascade(settings: Settings, transport) -> TranscriptCascade:
    from app.adapters.youtube.transcripts.providers import build_transcript_providers

    return TranscriptCascade(build_transcript_providers(settings, transport), settings)
