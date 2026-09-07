"""Choosing one playthrough out of twenty-five search results (ADR-016 sections 3-4).

The assignment says "the most popular relevant video". Those two words pull in opposite
directions: on any game query the most-viewed result is the launch trailer, and a trailer
has nothing to summarise. So popularity is one weighted signal out of six rather than the
answer, and three separate barriers stand between a trailer and selection.

Everything here is pure: candidates in, scores and reasons out. No HTTP, no database. That
is what makes a twenty-five-candidate fixture suite possible.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass

from app.domain.models import VideoCandidate
from app.normalizers.text import coverage

#: Barrier 3. A trailer is not a playthrough no matter how many views it has.
TRAILER_RE = re.compile(
    r"\b(trailer|teaser|announcement|announce|reveal|cinematic|opening\s+movie)\b", re.I
)

#: Not a playthrough either, but for different reasons -- these are commentary ABOUT the
#: game rather than a recording OF it, and a summary drawn from them would describe
#: someone's opinion while claiming to describe how the game plays.
OFF_FORMAT_RE = re.compile(
    r"\b(review|tier\s*list|news|ost|soundtrack|speedrun|all\s+cutscenes|the\s+movie|"
    r"tips|guide|how\s+to|best\s+settings|benchmark|fps\s+test|mods?|ranking|"
    r"every\s+ending|all\s+endings|explained|lore|theory|reaction)\b",
    re.I,
)

LETSPLAY_RE = re.compile(
    r"\b(let'?s\s*play|playthrough|walkthrough|full\s+game|gameplay|first\s+look|"
    r"blind\s+run|no\s+commentary|part\s*\d+|episode\s*\d+|ep\.?\s*\d+)\b",
    re.I,
)

#: The single most important penalty for THIS task: a silent playthrough is a perfect
#: video and a useless transcript.
NO_COMMENTARY_RE = re.compile(r"\bno\s+commentary\b", re.I)
SHORTS_RE = re.compile(r"#shorts\b", re.I)
COMPILATION_RE = re.compile(r"\b(compilation|montage|highlights|best\s+moments)\b", re.I)
FIRST_PART_RE = re.compile(r"\b(part|episode|ep\.?)\s*0*1\b|\bpilot\b", re.I)

WEIGHTS = {
    "popularity": 0.32,
    "title_relevance": 0.28,
    "format_fit": 0.14,
    "engagement": 0.12,
    "recency_fit": 0.08,
    "series_signal": 0.06,
}

PENALTIES = {
    "no_commentary": 0.25,
    "shorts": 0.15,
    "compilation": 0.10,
    "no_captions": 0.20,
}

#: Share of the GAME's title words that appear in the video's title. Asymmetric on
#: purpose: "Ashen Veil Let's Play - Part 1" is a perfect match even though most of its
#: words are not in the game's name, and a symmetric measure would reject it. 0.6 is
#: deliberately strict -- a wrong video is worse than no video, because the section then
#: confidently shows a playthrough of a different game.
MIN_TITLE_COVERAGE = 0.6


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: VideoCandidate
    score: float | None
    components: dict[str, float]
    rejected_reason: str | None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None


def reject_reason(
    candidate: VideoCandidate,
    *,
    game_title: str,
    min_duration_s: int,
    max_duration_s: int,
    allowed_languages: frozenset[str],
) -> str | None:
    """The hard filters. A rejected candidate is stored with its reason, never dropped."""
    title = candidate.title or ""

    if candidate.live_broadcast not in (None, "none"):
        return "live_broadcast"
    if TRAILER_RE.search(title):
        return "trailer"
    if OFF_FORMAT_RE.search(title):
        return "off_format"
    if candidate.duration_s is None:
        return "duration_unknown"
    if candidate.duration_s < min_duration_s:
        return "too_short"
    if candidate.duration_s > max_duration_s:
        return "too_long"
    if coverage(game_title, title) < MIN_TITLE_COVERAGE:
        return "title_mismatch"
    language = (candidate.default_audio_language or "").split("-")[0].lower()
    if language and allowed_languages and language not in allowed_languages:
        return "language"
    if candidate.embeddable is False and candidate.has_captions is False:
        # Neither watchable in place nor readable by us: nothing left to offer.
        return "not_embeddable_no_captions"
    return None


def _popularity(views: int | None, max_views: int) -> float:
    """Logarithmic on purpose.

    Linear popularity makes one seven-million-view video beat every alternative on that
    axis alone; on a log scale a 150k playthrough with a matching title can still win.
    """
    if not views or max_views <= 0:
        return 0.0
    return math.log10(views + 1) / math.log10(max_views + 1)


def _title_relevance(title: str, game_title: str) -> float:
    keyword = 1.0 if LETSPLAY_RE.search(title or "") else 0.0
    return 0.6 * coverage(game_title, title) + 0.4 * keyword


def _format_fit(duration_s: int | None) -> float:
    """A bell peaking between 30 minutes and 3 hours.

    Under 30 minutes there is not enough play to characterise a game; past three hours the
    transcript is mostly one late chapter and the earlier impression is diluted.
    """
    if not duration_s:
        return 0.0
    low, high = 1800, 10800
    if low <= duration_s <= high:
        return 1.0
    if duration_s < low:
        return max(0.0, duration_s / low)
    return max(0.0, math.exp(-(duration_s - high) / 7200))


def _engagement(likes: int | None, views: int | None) -> float:
    if not likes or not views:
        return 0.0
    return min(1.0, (likes / views) / 0.05)


def _recency_fit(published: dt.datetime | None, release: dt.date | None) -> float:
    """Videos uploaded around release describe the game people are asking about.

    A video published BEFORE release scores zero rather than negative: it is usually a
    preview build, which is a different game from the one that shipped.
    """
    if published is None or release is None:
        return 0.0
    release_dt = dt.datetime.combine(release, dt.time.min, tzinfo=dt.UTC)
    days = (published - release_dt).total_seconds() / 86400
    if days < 0:
        return 0.0
    return math.exp(-days / 120)


def _series_signal(candidate: VideoCandidate, channel_counts: dict[str, int]) -> float:
    """A channel with several episodes indexed is playing the game, not sampling it."""
    if not candidate.channel_id:
        return 0.0
    return 1.0 if channel_counts.get(candidate.channel_id, 0) >= 3 else 0.0


def _penalty(candidate: VideoCandidate) -> tuple[float, dict[str, float]]:
    title = candidate.title or ""
    applied: dict[str, float] = {}
    if NO_COMMENTARY_RE.search(title):
        applied["no_commentary"] = PENALTIES["no_commentary"]
    if SHORTS_RE.search(title):
        applied["shorts"] = PENALTIES["shorts"]
    if COMPILATION_RE.search(title):
        applied["compilation"] = PENALTIES["compilation"]
    if candidate.has_captions is False:
        applied["no_captions"] = PENALTIES["no_captions"]
    return sum(applied.values()), applied


def rank(
    candidates: list[VideoCandidate],
    *,
    game_title: str,
    release_date: dt.date | None,
    min_duration_s: int = 600,
    max_duration_s: int = 25200,
    allowed_languages: frozenset[str] = frozenset({"en"}),
) -> list[RankedCandidate]:
    """Score every candidate. Rejected ones keep their reason and a ``None`` score."""
    channel_counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.channel_id:
            channel_counts[candidate.channel_id] = (
                channel_counts.get(candidate.channel_id, 0) + 1
            )

    survivors = [
        (
            candidate,
            reject_reason(
                candidate,
                game_title=game_title,
                min_duration_s=min_duration_s,
                max_duration_s=max_duration_s,
                allowed_languages=allowed_languages,
            ),
        )
        for candidate in candidates
    ]

    # The popularity axis is normalised against the accepted field only. Normalising
    # against a 12-million-view trailer we already rejected would flatten every real
    # candidate into the bottom of the scale.
    max_views = max(
        (c.view_count or 0 for c, reason in survivors if reason is None), default=0
    )

    ranked: list[RankedCandidate] = []
    for candidate, reason in survivors:
        if reason is not None:
            ranked.append(RankedCandidate(candidate, None, {}, reason))
            continue

        components = {
            "popularity": _popularity(candidate.view_count, max_views),
            "title_relevance": _title_relevance(candidate.title, game_title),
            "format_fit": _format_fit(candidate.duration_s),
            "engagement": _engagement(candidate.like_count, candidate.view_count),
            "recency_fit": _recency_fit(candidate.published_at, release_date),
            "series_signal": _series_signal(candidate, channel_counts),
        }
        total = sum(WEIGHTS[key] * value for key, value in components.items())
        penalty, applied = _penalty(candidate)
        components.update({f"penalty_{k}": -v for k, v in applied.items()})
        components["first_part"] = 1.0 if FIRST_PART_RE.search(candidate.title or "") else 0.0
        ranked.append(
            RankedCandidate(candidate, round(max(0.0, total - penalty), 6), components, None)
        )

    ranked.sort(
        key=lambda r: (
            r.score if r.score is not None else -1.0,
            # Tie-break towards the start of a playthrough: episode one characterises the
            # game, episode fourteen characterises one late area.
            r.components.get("first_part", 0.0),
            # Still tied: prefer the longer session, which shows more of the game.
            r.candidate.duration_s or 0,
        ),
        reverse=True,
    )
    return ranked


def select(ranked: list[RankedCandidate], *, min_score: float) -> RankedCandidate | None:
    """The best candidate, or nothing.

    Returning nothing is a supported outcome: the section disappears (EDGE_CASES 12), and
    that is strictly better than showing a video that is not a playthrough of this game.
    """
    for item in ranked:
        if item.accepted and item.score is not None and item.score >= min_score:
            return item
    return None
