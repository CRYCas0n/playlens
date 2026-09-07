"""YouTube candidates, the selected video and its transcript state."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import ApiBudget, YoutubeTranscript, YoutubeVideo
from app.domain.enums import TranscriptStatus
from app.domain.models import VideoCandidate
from app.repositories.base import utc_now


class YoutubeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record_candidates(
        self,
        game_id: int,
        candidates: list[tuple[VideoCandidate, float | None, dict[str, float], str | None]],
    ) -> dict[str, int]:
        """Store every candidate, including rejected ones.

        Keeping rejects with their ``rejected_reason`` is the only way to answer "why did
        it pick that video" later; a ranking you cannot inspect is a ranking you cannot fix.
        """
        ids: dict[str, int] = {}
        for candidate, score, components, rejected in candidates:
            existing = self.session.execute(
                sa.select(YoutubeVideo).where(
                    YoutubeVideo.game_id == game_id,
                    YoutubeVideo.video_id == candidate.video_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = YoutubeVideo(game_id=game_id, video_id=candidate.video_id)
                self.session.add(existing)
            existing.url = candidate.url
            existing.title = candidate.title
            existing.channel_id = candidate.channel_id
            existing.channel_title = candidate.channel_title
            existing.description = candidate.description
            existing.duration_s = candidate.duration_s
            existing.view_count = candidate.view_count
            existing.like_count = candidate.like_count
            existing.published_at = candidate.published_at
            existing.has_captions = candidate.has_captions
            existing.default_audio_language = candidate.default_audio_language
            existing.live_broadcast = candidate.live_broadcast
            existing.thumbnail_url = candidate.thumbnail_url
            existing.rank_score = score
            existing.rank_components = components
            existing.rejected_reason = rejected
            existing.updated_at = utc_now()
            self.session.flush()
            ids[candidate.video_id] = existing.id
        return ids

    def select(self, game_id: int, video_id: int) -> YoutubeVideo:
        # Clear first: the partial unique index permits exactly one selected video.
        self.session.execute(
            sa.update(YoutubeVideo)
            .where(YoutubeVideo.game_id == game_id, YoutubeVideo.is_selected.is_(True))
            .values(is_selected=False)
        )
        self.session.flush()
        row = self.session.get(YoutubeVideo, video_id)
        assert row is not None
        row.is_selected = True
        self.session.flush()
        return row

    def selected(self, game_id: int) -> YoutubeVideo | None:
        return self.session.execute(
            sa.select(YoutubeVideo).where(
                YoutubeVideo.game_id == game_id, YoutubeVideo.is_selected.is_(True)
            )
        ).scalar_one_or_none()

    def transcript(self, video_row_id: int) -> YoutubeTranscript | None:
        return self.session.execute(
            sa.select(YoutubeTranscript).where(YoutubeTranscript.video_id == video_row_id)
        ).scalar_one_or_none()

    def save_transcript(
        self,
        *,
        video_row_id: int,
        status: TranscriptStatus,
        source: str | None,
        text: str | None,
        language: str | None,
        is_auto_generated: bool | None,
        attempts: list[dict[str, Any]],
        error_message: str | None = None,
    ) -> YoutubeTranscript:
        row = self.transcript(video_row_id)
        if row is None:
            row = YoutubeTranscript(video_id=video_row_id)
            self.session.add(row)
        row.status = status.value
        row.source = source
        row.text = text
        row.char_count = len(text or "")
        row.language = language
        row.is_auto_generated = is_auto_generated
        # Every attempt is kept: an empty HTTP 200 must be visible as a provider failure,
        # not silently indistinguishable from "this video has no subtitles" (ADR-016).
        row.attempts = attempts
        row.error_message = error_message
        row.fetched_at = utc_now()
        row.updated_at = utc_now()
        self.session.flush()
        return row

    def stale_unavailable(self, *, older_than: dt.datetime, limit: int = 50) -> list[int]:
        rows = self.session.execute(
            sa.select(YoutubeTranscript.video_id)
            .where(
                YoutubeTranscript.status == TranscriptStatus.UNAVAILABLE.value,
                YoutubeTranscript.fetched_at < older_than,
            )
            .limit(limit)
        ).all()
        return [int(r[0]) for r in rows]

    def transcript_stats(self) -> dict[str, int]:
        rows = self.session.execute(
            sa.select(YoutubeTranscript.status, sa.func.count()).group_by(
                YoutubeTranscript.status
            )
        ).all()
        return {str(k): int(v) for k, v in rows}


class BudgetRepository:
    """Daily external-API budgets. Exhaustion defers work; it never fails a game."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, provider: str, day: dt.date, *, limit: float) -> ApiBudget:
        row = self.session.get(ApiBudget, (provider, day))
        if row is None:
            row = ApiBudget(provider=provider, usage_date=day, units_used=0, units_limit=limit)
            self.session.add(row)
            self.session.flush()
        row.units_limit = limit
        return row

    def remaining(self, provider: str, day: dt.date, *, limit: float) -> float:
        row = self.get(provider, day, limit=limit)
        return max(0.0, row.units_limit - row.units_used)

    def spend(self, provider: str, day: dt.date, *, units: float, call: str, limit: float) -> float:
        row = self.get(provider, day, limit=limit)
        row.units_used += units
        calls = dict(row.calls or {})
        calls[call] = calls.get(call, 0) + 1
        row.calls = calls
        row.updated_at = utc_now()
        self.session.flush()
        return max(0.0, row.units_limit - row.units_used)
