"""A deterministic stand-in for a model.

Used by the tests, and available in development (``LLM_PROVIDER=fixture``) so the whole
pipeline — snapshot, prompt, validation, storage, rendering — can be exercised without an
API key.

It is deliberately a *careful* fake: it builds each claim out of words that genuinely
recur across the reviews it cites, and it produces no claim where the reviews share
nothing. A fake that emitted generic sentences would make the validator tests circular —
they would pass by rejecting everything.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from app.adapters.llm.base import LLMResult, RenderedPrompt
from app.ai.schemas import ClaimOut, GapOut, SummaryOut
from app.domain.enums import Aspect, AspectVerdict, ClaimType

T = TypeVar("T", bound=BaseModel)

_LINE_RE = re.compile(
    r"^\[([CU]\d{2,})\]\s+score=(\S+)\s+\|\s+date=(\S+)\s*(?:\|[^|]*)?\|\s*(.*)$",
    re.MULTILINE,
)

_STOPWORDS = frozenset(
    """
    the a an and or but of in on at to for with without from is are was were be been being
    it its this that these those they them their there here as by not no than then so very
    you your i we our us he she his her have has had will would can could should may might
    all any some most more much many one two into out up down over under about after before
    game games play played playing player players review reviews score title
    """.split()
)

_ASPECT_HINTS: dict[Aspect, tuple[str, ...]] = {
    Aspect.PERFORMANCE: ("fps", "frame", "framerate", "performance", "stutter", "crash"),
    Aspect.BUGS: ("bug", "bugs", "glitch", "glitches", "broken", "buggy"),
    Aspect.STORY: ("story", "narrative", "writing", "character", "characters", "plot"),
    Aspect.GAMEPLAY: ("combat", "gameplay", "controls", "mechanics", "boss", "fights"),
    Aspect.GRAPHICS: ("graphics", "visual", "visuals", "art", "textures", "beautiful"),
    Aspect.DIFFICULTY: ("difficulty", "difficult", "hard", "challenge", "punishing"),
    Aspect.CONTENT_AMOUNT: ("content", "hours", "length", "grind", "world", "exploration"),
    Aspect.PRICE_VALUE: ("price", "value", "money", "worth", "microtransactions"),
}


@dataclass(frozen=True, slots=True)
class _Line:
    ref: str
    score: float | None
    date: str
    text: str


class FixtureLLMProvider:
    name = "fixture"
    enabled = True

    def __init__(
        self,
        model: str = "fixture-1",
        *,
        positives: int = 3,
        negatives: int = 3,
        support: int = 3,
        fail_with: Exception | None = None,
        claim_type: ClaimType = ClaimType.DESCRIPTIVE,
        invalid_refs: bool = False,
        vague: bool = False,
    ) -> None:
        self.model = model
        self.calls = 0
        self._positives = positives
        self._negatives = negatives
        self._support = support
        self._fail_with = fail_with
        self._claim_type = claim_type
        self._invalid_refs = invalid_refs
        self._vague = vague

    def complete_structured(
        self, *, prompt: RenderedPrompt, schema: type[T], max_tokens: int
    ) -> LLMResult:
        self.calls += 1
        if self._fail_with is not None:
            raise self._fail_with

        lines = _parse_corpus(prompt.corpus)

        if schema is GapOut:
            value: Any = GapOut(
                explanation=(
                    "Both audiences describe the same technical problems but weight them "
                    "differently."
                ),
                evidence=[line.ref for line in lines[: self._support]],
                confidence=0.6,
            )
        else:
            value = self._summary(lines)

        return LLMResult(
            value=value,
            model=self.model,
            provider=self.name,
            tokens_in=max(1, len(prompt.corpus) // 4),
            tokens_out=200,
            cost_usd=0.0,
            latency_ms=1,
        )

    # ------------------------------------------------------------------ internals

    def _summary(self, lines: list[_Line]) -> SummaryOut:
        high = [line for line in lines if line.score is not None and line.score >= 70]
        low = [line for line in lines if line.score is not None and line.score < 50]
        pool = high or [line for line in lines if line.score is None]

        positives = self._claims(pool, self._positives, positive=True)
        negatives = self._claims(low, self._negatives, positive=False)

        return SummaryOut(
            heading="Reviewers converge on a handful of points",
            overall=(
                "Reviewers return to the same qualities and describe the same "
                "reservations in similar terms."
            ),
            positive=positives,
            negative=negatives,
            aspect_verdicts={Aspect.GAMEPLAY: AspectVerdict.POSITIVE},
            confidence=0.7,
        )

    def _claims(self, lines: list[_Line], count: int, *, positive: bool) -> list[ClaimOut]:
        claims: list[ClaimOut] = []
        for index in range(count):
            window = lines[index * self._support : (index + 1) * self._support]
            if len(window) < self._support:
                break

            shared = _shared_vocabulary(window, minimum=max(2, (len(window) + 1) // 2))
            if not shared and not self._vague and not self._invalid_refs:
                # A careful model says nothing when the reviews share nothing.
                continue

            if self._vague:
                text = "Reviews are mixed"
            elif self._invalid_refs:
                text = "Reviewers repeatedly mention the same qualities"
            else:
                subject = ", ".join(shared[:2])
                verb = "praise" if positive else "criticise"
                text = f"Reviewers repeatedly {verb} the {subject}"

            claims.append(
                ClaimOut(
                    aspect=_aspect_for(shared),
                    claim=text,
                    claim_type=self._claim_type,
                    evidence=["Z99", "Z98", "Z97"]
                    if self._invalid_refs
                    else [line.ref for line in window],
                    strength="strong" if positive else "moderate",
                )
            )
        return claims


def _parse_corpus(corpus: str) -> list[_Line]:
    """Read back a rendered corpus, normalising scores onto the 0-100 axis.

    Critic lines are 0-100 and player lines 0-10, so the prefix decides the scale — the
    same distinction the domain model makes (ADR-002).
    """
    lines: list[_Line] = []
    for ref, score, date, text in _LINE_RE.findall(corpus):
        try:
            numeric: float | None = float(score)
        except ValueError:
            numeric = None
        if numeric is not None and ref.startswith("U"):
            numeric *= 10
        lines.append(_Line(ref=ref, score=numeric, date=date, text=text))
    return lines


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z][a-z\-']{3,}", text.lower()) if t not in _STOPWORDS
    }


def _shared_vocabulary(lines: list[_Line], *, minimum: int) -> list[str]:
    counts: Counter[str] = Counter()
    for line in lines:
        counts.update(_tokens(line.text))
    return [word for word, n in counts.most_common(6) if n >= minimum]


def _aspect_for(words: list[str]) -> Aspect:
    for aspect, hints in _ASPECT_HINTS.items():
        if any(word in hints for word in words):
            return aspect
    return Aspect.OTHER
