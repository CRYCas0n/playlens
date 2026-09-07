"""Evidence validation.

The mechanical half of ADR-008, and the reason the pipeline can be trusted without
trusting the model. Everything here is deterministic and runs without a network:
it is the same kind of check ``verify.py``/``score.py`` performed by hand during
Release 0, moved into the pipeline so it runs on every summary rather than once.

A claim that fails is KEPT with its reason. Discarding it silently would make prompt or
model regressions invisible until a user noticed one.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from app.ai.schemas import ClaimOut
from app.domain.enums import ClaimSide, ClaimType, ClaimValidation

#: Phrases that would be true of almost any game. Release 0 found only 10 of 207 claims
#: were like this, but each one is a claim that tells the reader nothing.
_VAGUE_PATTERNS = (
    r"\bmixed reviews?\b",
    r"\breviews?\s+(?:are|is|were|was)\s+mixed\b",
    r"\brecept(?:ion|ions)\s+(?:is|was)\s+mixed\b",
    r"\bgenerally (?:positive|negative|favou?rable)\b",
    r"\bopinions? (?:are|is) divided\b",
    r"\bsome (?:players|critics|reviewers) (?:like|dislike)\b",
    r"\bnot for everyone\b",
    r"\bhit or miss\b",
    r"\boverall (?:good|bad|fine|okay)\b",
    r"\bwell received\b",
)
_VAGUE_RE = re.compile("|".join(_VAGUE_PATTERNS), re.IGNORECASE)

#: Words that assert change over time. A claim containing one must be backed by dates.
_TEMPORAL_PATTERNS = (
    r"\bafter (?:the |a )?(?:patch|update|release|launch)\b",
    r"\bat launch\b",
    r"\bover time\b",
    r"\bsince (?:the |its )?(?:launch|release|patch)\b",
    r"\beventually\b",
    r"\blater reviews?\b",
    r"\bearly reviews?\b",
    r"\bno longer\b",
    r"\bnowadays\b",
    r"\bthese days\b",
)
_TEMPORAL_RE = re.compile("|".join(_TEMPORAL_PATTERNS), re.IGNORECASE)

_QUOTE_RE = re.compile(r"[\"“‘']([^\"”’']{12,})[\"”’']")

_STOPWORDS = frozenset(
    """
    the a an and or but of in on at to for with without from is are was were be been
    being it its this that these those they them their there here as by not no than then
    so very
    """.split()
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    ref: str
    text: str
    published_on: dt.date | None


@dataclass(frozen=True, slots=True)
class ValidatedClaim:
    side: ClaimSide
    claim: ClaimOut
    validation: ClaimValidation
    detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.validation is ClaimValidation.ACCEPTED


@dataclass(frozen=True, slots=True)
class ValidationReport:
    claims: tuple[ValidatedClaim, ...]

    @property
    def accepted(self) -> tuple[ValidatedClaim, ...]:
        return tuple(c for c in self.claims if c.accepted)

    def accepted_side(self, side: ClaimSide) -> tuple[ValidatedClaim, ...]:
        return tuple(c for c in self.accepted if c.side is side)

    @property
    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for claim in self.claims:
            if not claim.accepted:
                counts[claim.validation.value] = counts.get(claim.validation.value, 0) + 1
        return counts

    @property
    def acceptance_rate(self) -> float:
        return len(self.accepted) / len(self.claims) if self.claims else 1.0


def _content_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z][a-z\-']{2,}", text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def validate_claim(
    claim: ClaimOut,
    side: ClaimSide,
    *,
    evidence: dict[str, EvidenceItem],
    min_support: int,
    temporal_min_span_days: int,
    require_aspect_overlap: bool = True,
) -> ValidatedClaim:
    def reject(reason: ClaimValidation, detail: str) -> ValidatedClaim:
        return ValidatedClaim(side=side, claim=claim, validation=reason, detail=detail)

    refs = list(dict.fromkeys(claim.evidence))

    # 1. Every reference must exist IN THIS SNAPSHOT. This is what makes an injected
    #    instruction unable to manufacture a supported claim, and it is the check that
    #    caught four broken citations in Release 0's own summaries.
    missing = [ref for ref in refs if ref not in evidence]
    if missing:
        return reject(
            ClaimValidation.REJECTED_MISSING_REF,
            f"unknown evidence references: {', '.join(missing)}",
        )

    # 2. Enough independent voices.
    if len(refs) < min_support:
        return reject(
            ClaimValidation.REJECTED_LOW_SUPPORT,
            f"{len(refs)} supporting reviews, {min_support} required",
        )

    # 3. Say something.
    if not claim.claim.strip() or _VAGUE_RE.search(claim.claim):
        return reject(
            ClaimValidation.REJECTED_VAGUE,
            "phrasing would be true of almost any game",
        )

    items = [evidence[ref] for ref in refs]

    # 4. A temporal assertion must be supported by the dates of the reviews it cites.
    #    Release 0's single unsupported PV2 conclusion was exactly this shape (C-10).
    looks_temporal = claim.claim_type is ClaimType.TEMPORAL or bool(
        _TEMPORAL_RE.search(claim.claim)
    )
    if looks_temporal:
        dates = sorted(i.published_on for i in items if i.published_on)
        if len(dates) < 2:
            return reject(
                ClaimValidation.REJECTED_TEMPORAL_UNSUPPORTED,
                "a claim about change over time needs dated evidence",
            )
        span = (dates[-1] - dates[0]).days
        if span < temporal_min_span_days:
            return reject(
                ClaimValidation.REJECTED_TEMPORAL_UNSUPPORTED,
                f"cited reviews span {span} days, {temporal_min_span_days} required",
            )

    # 5. A quotation must actually appear in a cited review.
    quoted = _QUOTE_RE.search(claim.claim)
    if quoted:
        needle = " ".join(quoted.group(1).lower().split())
        haystacks = [" ".join(i.text.lower().split()) for i in items]
        if not any(needle in hay for hay in haystacks):
            return reject(
                ClaimValidation.REJECTED_QUOTE_NOT_FOUND,
                "quoted text does not appear in the cited reviews",
            )

    # 6. The claim must share vocabulary with the reviews it cites. A claim about frame
    #    pacing citing three reviews that never mention performance is not supported by
    #    them, however many references it lists.
    #
    #    The bar is a MAJORITY of the cited reviews, not all of them: reviewers phrase the
    #    same observation differently, and demanding unanimity would reject sound claims
    #    for a vocabulary mismatch in one citation.
    if require_aspect_overlap:
        claim_tokens = _content_tokens(claim.claim)
        supporting = sum(
            1 for item in items if claim_tokens & _content_tokens(item.text)
        )
        needed = max(2, (len(items) + 1) // 2)
        if claim_tokens and supporting < needed:
            return reject(
                ClaimValidation.REJECTED_ASPECT_UNSUPPORTED,
                f"only {supporting} of {len(items)} cited reviews mention anything "
                f"in this claim; {needed} required",
            )

    return ValidatedClaim(side=side, claim=claim, validation=ClaimValidation.ACCEPTED)


def validate_summary(
    *,
    positive: list[ClaimOut],
    negative: list[ClaimOut],
    evidence: dict[str, EvidenceItem],
    corpus_size: int,
    min_support: int,
    min_support_small_corpus: int,
    small_corpus_threshold: int,
    temporal_min_span_days: int,
) -> ValidationReport:
    """Validate both sides.

    The support threshold relaxes on small corpora, matching Release 0's own methodology
    (>=3 normally, >=2 below twenty reviews) — otherwise a four-review indie corpus can
    never produce a valid claim at all.
    """
    required = (
        min_support_small_corpus if corpus_size < small_corpus_threshold else min_support
    )
    results: list[ValidatedClaim] = []
    for side, claims in ((ClaimSide.POSITIVE, positive), (ClaimSide.NEGATIVE, negative)):
        for claim in claims:
            results.append(
                validate_claim(
                    claim,
                    side,
                    evidence=evidence,
                    min_support=required,
                    temporal_min_span_days=temporal_min_span_days,
                )
            )
    return ValidationReport(claims=tuple(results))


def empty_section_note(*, side: ClaimSide, positive_count: int, negative_count: int) -> str:
    """Honest copy for a section with no claims.

    Release 0's key finding: for a game with 86 positive and 0 negative critic reviews,
    "there is almost nothing to criticise" is both TRUE and more informative than a
    manufactured complaint (C-06).
    """
    if side is ClaimSide.NEGATIVE:
        if negative_count == 0 and positive_count > 0:
            return (
                f"Рецензенты почти ни к чему не придираются: положительных рецензий "
                f"{positive_count}, отрицательных нет."
            )
        return "Ни одна претензия не повторялась в рецензиях достаточно часто, чтобы её приводить."
    if positive_count == 0 and negative_count > 0:
        return (
            f"Рецензентам почти не за что хвалить: отрицательных рецензий "
            f"{negative_count}, положительных нет."
        )
    return "Ни одна похвала не повторялась в рецензиях достаточно часто, чтобы её приводить."
