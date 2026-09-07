"""The verdict — computed, never generated (ADR-012).

The single most important sentence in the product is arithmetic. It therefore cannot
contradict the two numbers printed above it, needs no AI disclaimer, costs nothing per
game, and exists even when no summary does.

Copy is taken verbatim from design/COPY.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.enums import Tier
from app.domain.scores import ScoreValue

CRITIC_COPY: dict[Tier, str] = {
    Tier.EXCELLENT: "Critics rate this among the year's best",
    Tier.GOOD: "Critics recommend it with reservations",
    Tier.MIXED: "Critics are split on this one",
    Tier.POOR: "Critics advise against it",
}

PLAYER_COPY: dict[Tier, str] = {
    Tier.EXCELLENT: "Players rate this among the year's best",
    Tier.GOOD: "Players recommend it with reservations",
    Tier.MIXED: "Players are split on this one",
    Tier.POOR: "Players advise against it",
}

NOTHING_TO_SAY = "Not enough reviews yet to say anything useful."


class VerdictKind(StrEnum):
    AGREE = "agree"
    PLAYERS_LOWER = "players-lower"
    PLAYERS_HIGHER = "players-higher"
    CRITIC_ONLY = "critic-only"
    PLAYER_ONLY = "player-only"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Verdict:
    line: str
    kind: VerdictKind
    delta: int | None = None
    platform_line: str | None = None
    strengths: tuple[str, ...] = field(default_factory=tuple)
    watch_outs: tuple[str, ...] = field(default_factory=tuple)

    #: Always true. Present in the API payload so a client can tell at a glance that this
    #: sentence is not model output and does not carry an AI disclaimer.
    derived: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "kind": self.kind.value,
            "delta": self.delta,
            "platform_line": self.platform_line,
            "strengths": list(self.strengths),
            "watch_outs": list(self.watch_outs),
            "derived": True,
        }


def agreement_delta(critic: ScoreValue, user: ScoreValue) -> int | None:
    """Critic minus player on the shared 0-100 axis, or ``None`` if either is missing."""
    c, u = critic.normalized, user.normalized
    if c is None or u is None:
        return None
    return c - u


def verdict_line(
    critic: ScoreValue,
    user: ScoreValue,
    *,
    agreement_threshold: int = 7,
    strengths: tuple[str, ...] = (),
    watch_outs: tuple[str, ...] = (),
    platform_line: str | None = None,
) -> Verdict:
    c, u = critic.normalized, user.normalized

    if c is None and u is None:
        return Verdict(NOTHING_TO_SAY, VerdictKind.NONE, None, platform_line, strengths, watch_outs)

    if c is None:
        line = f"{PLAYER_COPY[user.tier]}. No critic score yet."
        return Verdict(line, VerdictKind.PLAYER_ONLY, None, platform_line, strengths, watch_outs)

    if u is None:
        line = f"{CRITIC_COPY[critic.tier]}. No player score yet."
        return Verdict(line, VerdictKind.CRITIC_ONLY, None, platform_line, strengths, watch_outs)

    delta = c - u
    head = CRITIC_COPY[critic.tier]

    if abs(delta) < agreement_threshold:
        return Verdict(
            f"{head}, and players agree.",
            VerdictKind.AGREE,
            delta,
            platform_line,
            strengths,
            watch_outs,
        )
    if delta > 0:
        return Verdict(
            f"{head} — but players rate it {delta} points lower.",
            VerdictKind.PLAYERS_LOWER,
            delta,
            platform_line,
            strengths,
            watch_outs,
        )
    return Verdict(
        f"{head} — and players rate it {abs(delta)} points higher.",
        VerdictKind.PLAYERS_HIGHER,
        delta,
        platform_line,
        strengths,
        watch_outs,
    )


def platform_line(
    *,
    selected_name: str,
    selected_metascore: ScoreValue,
    best_name: str,
    best_metascore: ScoreValue,
    min_points: int = 10,
) -> str | None:
    """Second, platform-aware sentence — also arithmetic (ADR-010, ADR-012).

    This is the product's killer feature expressed without a single model call: a PS4
    owner is told, in words, that the 86 they might have seen belongs to the PC version.
    """
    sel, best = selected_metascore.normalized, best_metascore.normalized
    if sel is None or best is None or selected_name == best_name:
        return None
    delta = best - sel
    if delta < min_points:
        return None
    return f"On {selected_name} critics rate this {delta} points lower than on {best_name}."


# ----------------------------------------------------------------- consensus note

CONSENSUS_AGREE = (
    "Critics and players broadly agree ({delta} points apart). When both audiences land "
    "in the same place, the score is a reliable signal."
)
CONSENSUS_LOWER = (
    "Players rate this {delta} points lower than critics. A gap this wide usually means "
    "launch condition, monetisation or platform-specific problems. Read the player "
    "summary before deciding."
)
CONSENSUS_HIGHER = (
    "Players rate this {delta} points higher than critics. Often a sign of a game whose "
    "appeal grows past the review window, or one aimed at a specific audience."
)
CONSENSUS_ONE_SIDED = (
    "Only one audience has rated this so far, so there is nothing to compare yet. "
    "Check back once the other side is counted."
)

AXIS_CAPTION = (
    "Player scores are published on a 0–10 scale and shown here multiplied by ten. "
    "Tier bands: 85+ Excellent · 70–84 Good · 50–69 Mixed · below 50 Poor."
)


@dataclass(frozen=True, slots=True)
class ConsensusNote:
    text: str
    tone: str  # "neutral" | "warning"
    delta: int | None


def consensus_note(
    critic: ScoreValue, user: ScoreValue, *, agreement_threshold: int = 7
) -> ConsensusNote:
    delta = agreement_delta(critic, user)
    if delta is None:
        return ConsensusNote(CONSENSUS_ONE_SIDED, "neutral", None)
    if abs(delta) < agreement_threshold:
        return ConsensusNote(CONSENSUS_AGREE.format(delta=abs(delta)), "neutral", delta)
    if delta > 0:
        return ConsensusNote(CONSENSUS_LOWER.format(delta=delta), "warning", delta)
    return ConsensusNote(CONSENSUS_HIGHER.format(delta=abs(delta)), "neutral", delta)
