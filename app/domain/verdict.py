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
    Tier.EXCELLENT: "Критики относят игру к лучшему за год",
    Tier.GOOD: "Критики советуют её с оговорками",
    Tier.MIXED: "Мнения критиков разделились",
    Tier.POOR: "Критики не рекомендуют её",
}

PLAYER_COPY: dict[Tier, str] = {
    Tier.EXCELLENT: "Игроки относят игру к лучшему за год",
    Tier.GOOD: "Игроки советуют её с оговорками",
    Tier.MIXED: "Мнения игроков разделились",
    Tier.POOR: "Игроки не рекомендуют её",
}

NOTHING_TO_SAY = "Рецензий пока слишком мало, чтобы сказать что-то полезное."


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
        line = f"{PLAYER_COPY[user.tier]}. Оценки критиков пока нет."
        return Verdict(line, VerdictKind.PLAYER_ONLY, None, platform_line, strengths, watch_outs)

    if u is None:
        line = f"{CRITIC_COPY[critic.tier]}. Оценки игроков пока нет."
        return Verdict(line, VerdictKind.CRITIC_ONLY, None, platform_line, strengths, watch_outs)

    delta = c - u
    head = CRITIC_COPY[critic.tier]

    if abs(delta) < agreement_threshold:
        return Verdict(
            f"{head}, и игроки с ними согласны.",
            VerdictKind.AGREE,
            delta,
            platform_line,
            strengths,
            watch_outs,
        )
    if delta > 0:
        return Verdict(
            f"{head} — но игроки ставят на {delta} баллов ниже.",
            VerdictKind.PLAYERS_LOWER,
            delta,
            platform_line,
            strengths,
            watch_outs,
        )
    return Verdict(
        f"{head} — а игроки ставят на {abs(delta)} баллов выше.",
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
    return f"На {selected_name} критики оценивают игру на {delta} баллов ниже, чем на {best_name}."


# ----------------------------------------------------------------- consensus note

CONSENSUS_AGREE = (
    "Критики и игроки в целом сходятся (разница {delta} баллов). Когда обе аудитории "
    "приходят к одному, оценке можно верить."
)
CONSENSUS_LOWER = (
    "Игроки оценивают игру на {delta} баллов ниже критиков. Такой разрыв обычно означает "
    "состояние на релизе, монетизацию или проблемы на конкретной платформе. Прочитайте "
    "резюме отзывов игроков, прежде чем решать."
)
CONSENSUS_HIGHER = (
    "Игроки оценивают игру на {delta} баллов выше критиков. Часто это признак игры, "
    "которая раскрывается уже после выхода рецензий, или игры для своей аудитории."
)
CONSENSUS_ONE_SIDED = (
    "Пока оценила только одна аудитория, сравнивать не с чем. "
    "Загляните позже, когда появится вторая."
)

AXIS_CAPTION = (
    "Оценки игроков публикуются по шкале 0–10 и показаны здесь умноженными на десять. "
    "Диапазоны: 85+ отлично · 70–84 хорошо · 50–69 смешанно · ниже 50 плохо."
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
