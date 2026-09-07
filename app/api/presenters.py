"""ORM rows -> response schemas.

The single place where domain rules meet presentation. Both the JSON API and the HTML
templates call these functions, so a rule applied here (a missing score renders as
"Not rated"; an empty criticism list gets honest copy; a similarity reason appears only
when one exists) holds on every surface at once.
"""

from __future__ import annotations

from app.ai.validator import empty_section_note
from app.api.schemas import (
    ClaimOut,
    CompanyOut,
    ConsensusOut,
    GameDetailOut,
    GameListItemOut,
    GapOut,
    GenreOut,
    LetsPlayOut,
    PlatformOut,
    PlatformScoreOut,
    ProvenanceOut,
    ScoreOut,
    SimilarGameOut,
    SuggestionOut,
    SummaryOut,
    VerdictOut,
    VideoOut,
)
from app.config import Settings
from app.domain.enums import Audience, ClaimSide, ClaimValidation, ScoreStatus
from app.domain.scores import ScoreValue, critic_score, user_score
from app.domain.verdict import AXIS_CAPTION, consensus_note, platform_line, verdict_line

MAX_STRENGTHS = 3
MAX_WATCH_OUTS = 2


def score_out(value: ScoreValue) -> ScoreOut:
    return ScoreOut(**value.as_dict())  # type: ignore[arg-type]


def platform_out(platform) -> PlatformOut:
    return PlatformOut(slug=platform.slug, name=platform.name, code=platform.code)


def game_scores(game, settings: Settings) -> tuple[ScoreValue, ScoreValue]:
    """The headline pair: best available critic and player score across platforms."""
    critic = critic_score(
        game.best_metascore,
        game.best_metascore_count,
        low_sample_threshold=settings.low_sample_threshold,
    )
    player = user_score(
        game.best_userscore,
        game.best_userscore_count,
        zero_min_ratings=settings.userscore_zero_min_ratings,
        low_sample_threshold=settings.low_sample_threshold,
    )
    return critic, player


def platform_scores(game_platform, settings: Settings) -> tuple[ScoreValue, ScoreValue]:
    critic = critic_score(
        game_platform.metascore_raw,
        game_platform.metascore_count,
        low_sample_threshold=settings.low_sample_threshold,
    )
    player = user_score(
        game_platform.userscore_raw,
        game_platform.userscore_count,
        zero_min_ratings=settings.userscore_zero_min_ratings,
        low_sample_threshold=settings.low_sample_threshold,
    )
    return critic, player


def list_item(game, settings: Settings) -> GameListItemOut:
    critic, player = game_scores(game, settings)
    developer = next(
        (gc.company.name for gc in game.companies if gc.role == "developer" and gc.company),
        None,
    )
    gap = None
    if critic.normalized is not None and player.normalized is not None:
        gap = critic.normalized - player.normalized
    return GameListItemOut(
        slug=game.mc_slug,
        title=game.title,
        developer=developer,
        cover_url=game.cover_url,
        release_year=game.premiere_year,
        metascore=score_out(critic),
        userscore=score_out(player),
        platforms=[platform_out(gp.platform) for gp in game.platforms if gp.platform],
        gap=gap,
        updated_at=game.updated_at,
    )


def suggestion(game, settings: Settings) -> SuggestionOut:
    critic, _ = game_scores(game, settings)
    return SuggestionOut(
        slug=game.mc_slug,
        title=game.title,
        cover_url=game.cover_url,
        release_year=game.premiere_year,
        metascore=score_out(critic),
    )


def _claims(summary, side: ClaimSide) -> list[ClaimOut]:
    """Only claims that survived validation reach a reader (ADR-008)."""
    return [
        ClaimOut(
            aspect=c.aspect,
            claim=c.claim,
            claim_type=c.claim_type,
            evidence=list(c.evidence_refs or []),
            strength=c.strength,
        )
        for c in sorted(summary.claims, key=lambda c: c.position)
        if c.side == side.value and c.validation == ClaimValidation.ACCEPTED.value
    ]


def summary_out(summary, *, platform, source_url: str | None) -> SummaryOut:
    positive = _claims(summary, ClaimSide.POSITIVE)
    negative = _claims(summary, ClaimSide.NEGATIVE)
    snapshot = summary.snapshot

    positive_reviews = getattr(snapshot, "review_count", 0) or 0
    return SummaryOut(
        audience=summary.audience,
        platform=platform_out(platform) if platform else None,
        status=summary.status,
        heading=summary.heading,
        overall=summary.overall,
        positive=positive,
        negative=negative,
        # An empty section is stated, not padded. "86 positive reviews, none negative" is
        # more informative than an invented complaint (C-06).
        empty_positive_note=None
        if positive
        else empty_section_note(
            side=ClaimSide.POSITIVE,
            positive_count=positive_reviews,
            negative_count=len(negative),
        ),
        empty_negative_note=None
        if negative
        else empty_section_note(
            side=ClaimSide.NEGATIVE,
            positive_count=positive_reviews,
            negative_count=0,
        ),
        aspect_verdicts=dict(summary.aspect_verdicts or {}),
        provenance=ProvenanceOut(
            reviews_used=summary.reviews_used,
            reviews_candidates=summary.reviews_candidates,
            snapshot_id=summary.snapshot_id,
            model=summary.llm_model,
            prompt_version=summary.prompt_version,
            generated_at=summary.generated_at,
            source_url=source_url,
        ),
    )


def _signals(
    summary: SummaryOut | None, fallback: SummaryOut | None
) -> tuple[list[str], list[str]]:
    """Signal chips are LIFTED, never generated.

    design/UX_SPEC.md section 2: no new claim may appear at the top of the page, where the
    reader is least equipped to check it.
    """
    source = summary or fallback
    if source is None:
        return [], []
    strengths = [c.claim for c in source.positive][:MAX_STRENGTHS]
    watch_outs = [c.claim for c in source.negative][:MAX_WATCH_OUTS]
    if not strengths and fallback is not None and fallback is not source:
        strengths = [c.claim for c in fallback.positive][:MAX_STRENGTHS]
    if not watch_outs and fallback is not None and fallback is not source:
        watch_outs = [c.claim for c in fallback.negative][:MAX_WATCH_OUTS]
    return strengths, watch_outs


def game_detail(
    game,
    *,
    settings: Settings,
    summaries: list,
    similar: list,
    gap=None,
    lets_play=None,
    selected_platform_slug: str | None = None,
) -> GameDetailOut:
    critic, player = game_scores(game, settings)

    platform_rows: list[PlatformScoreOut] = []
    best_platform = None
    selected = None
    for gp in sorted(game.platforms, key=lambda p: (not p.is_lead, p.platform.sort_order)):
        if gp.platform is None:
            continue
        gp_critic, gp_player = platform_scores(gp, settings)
        platform_rows.append(
            PlatformScoreOut(
                platform=platform_out(gp.platform),
                is_lead=gp.is_lead,
                release_date=gp.release_date,
                metascore=score_out(gp_critic),
                userscore=score_out(gp_player),
                critic_reviews_with_text=gp.critic_reviews_with_text,
                user_reviews_with_text=gp.user_reviews_with_text,
            )
        )
        if gp_critic.normalized is not None and (
            best_platform is None or gp_critic.normalized > best_platform[1].normalized
        ):
            best_platform = (gp.platform, gp_critic)
        if selected_platform_slug and gp.platform.slug == selected_platform_slug:
            selected = (gp.platform, gp_critic)

    # The platform-aware second sentence. Also arithmetic (ADR-010/012).
    second_line = None
    if selected and best_platform:
        second_line = platform_line(
            selected_name=selected[0].name,
            selected_metascore=selected[1],
            best_name=best_platform[0].name,
            best_metascore=best_platform[1],
            min_points=settings.platform_gap_min_points,
        )

    critic_summary = next(
        (s for s in summaries if s.audience == Audience.CRITIC.value), None
    )
    user_summary = next((s for s in summaries if s.audience == Audience.USER.value), None)
    lets_play_summary = next(
        (s for s in summaries if s.audience == Audience.LETSPLAY.value), None
    )

    critic_out = (
        summary_out(
            critic_summary,
            platform=_platform_of(game, critic_summary),
            source_url=f"{game.mc_url}critic-reviews/",
        )
        if critic_summary and critic_summary.status == "fresh"
        else None
    )
    user_out = (
        summary_out(
            user_summary,
            platform=_platform_of(game, user_summary),
            source_url=f"{game.mc_url}user-reviews/",
        )
        if user_summary and user_summary.status == "fresh"
        else None
    )

    strengths, watch_outs = _signals(critic_out, user_out)
    verdict = verdict_line(
        critic,
        player,
        agreement_threshold=settings.agreement_threshold,
        strengths=tuple(strengths),
        watch_outs=tuple(watch_outs),
        platform_line=second_line,
    )
    note = consensus_note(critic, player, agreement_threshold=settings.agreement_threshold)

    return GameDetailOut(
        slug=game.mc_slug,
        title=game.title,
        description=game.description,
        cover_url=game.cover_url,
        source_url=game.mc_url,
        developers=[
            CompanyOut(slug=gc.company.slug, name=gc.company.name, role=gc.role)
            for gc in game.companies
            if gc.role == "developer" and gc.company
        ],
        publishers=[
            CompanyOut(slug=gc.company.slug, name=gc.company.name, role=gc.role)
            for gc in game.companies
            if gc.role == "publisher" and gc.company
        ],
        genres=[GenreOut(slug=gg.genre.slug, name=gg.genre.name) for gg in game.genres if gg.genre],
        esrb_rating=game.esrb_rating,
        release_date=game.release_date,
        release_year=game.premiere_year,
        updated_at=game.updated_at,
        metascore=score_out(critic),
        userscore=score_out(player),
        verdict=VerdictOut(**verdict.as_dict()),  # type: ignore[arg-type]
        consensus=ConsensusOut(
            note=note.text, tone=note.tone, delta=note.delta, axis_caption=AXIS_CAPTION
        ),
        platforms=platform_rows,
        # The section stays, with a pending block: the platforms are real, only their
        # scores are missing (design/EDGE_CASES.md case 18).
        platform_scores_pending=bool(platform_rows)
        and all(p.metascore.status != ScoreStatus.VALID.value for p in platform_rows),
        critic_summary=critic_out,
        user_summary=user_out,
        gap=(
            GapOut(
                gap_points=gap.gap_points or 0,
                explanation=gap.explanation or "",
                evidence=list(gap.evidence_refs or []),
                model=gap.llm_model,
                generated_at=gap.generated_at,
            )
            if gap is not None and gap.explanation
            else None
        ),
        similar=[
            SimilarGameOut(
                slug=other.mc_slug,
                title=other.title,
                cover_url=other.cover_url,
                metascore=score_out(game_scores(other, settings)[0]),
                reason=row.reason,
                score=row.score,
                components=dict(row.components or {}),
            )
            for row, other in similar
        ],
        lets_play=_lets_play_out(lets_play, lets_play_summary, game, settings),
        video=(
            VideoOut(
                url=game.video_url, title=game.video_title, duration_s=game.video_duration_s
            )
            if game.video_url
            else None
        ),
    )


def _platform_of(game, summary):
    if summary is None:
        return None
    for gp in game.platforms:
        if gp.id == summary.game_platform_id:
            return gp.platform
    return None


def _lets_play_out(lets_play, summary, game, settings) -> LetsPlayOut | None:
    """Absent video -> ``None`` -> the whole section disappears (design non-negotiable 9)."""
    if lets_play is None:
        return None
    video, transcript = lets_play
    return LetsPlayOut(
        video_id=video.video_id,
        url=video.url,
        title=video.title,
        channel=video.channel_title,
        views=video.view_count,
        published_at=video.published_at,
        duration_s=video.duration_s,
        thumbnail_url=video.thumbnail_url,
        transcript_status=transcript.status if transcript else "pending",
        transcript_source=transcript.source if transcript else None,
        # No transcript means no AI conclusions at all -- a link and metadata only.
        summary=(
            summary_out(summary, platform=None, source_url=video.url)
            if summary and summary.status == "fresh"
            else None
        ),
    )


__all__ = [
    "GapOut",
    "game_detail",
    "game_scores",
    "list_item",
    "platform_out",
    "platform_scores",
    "score_out",
    "suggestion",
    "summary_out",
]
