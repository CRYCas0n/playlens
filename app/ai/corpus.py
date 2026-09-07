"""Corpus construction: quality filter, stratified sampling, deterministic ordering.

Three defects Release 0 found live here, each fixed structurally rather than by asking the
model to be careful:

* **Contaminated buckets.** ``filterBySentiment`` is built on the SCORE, not the text. On
  Gollum the "positive" bucket contains "GOTY 2023, better than Elden Ring" written
  ironically, and a review scored 10 whose text reads "jogo de merda". A pipeline that
  trusts the bucket puts those in "what players like" (C-07).
* **Biased sampling.** The first corpus took "the first N", which dropped the entire
  negative bucket and produced systematically flattering summaries (Release 0 section 6.2).
* **Missing dates.** Without them the model cannot support the most common kind of
  explanation — "opinion changed after the patch" — and guesses instead (C-10).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from app.normalizers.text import collapse_whitespace, sanitize_for_prompt, sha256_text

# --------------------------------------------------------------------------- lexicon
#
# Deliberately small and deliberately blunt. This is HYGIENE, not sentiment analysis as a
# feature: it only fires when a long text is UNAMBIGUOUSLY at odds with its own score.
# Release 0 section 6.3 is explicit that a lexical detector is a coarse filter and must
# never become an acceptance metric.

_POSITIVE = frozenset(
    """
    masterpiece amazing excellent outstanding brilliant superb fantastic wonderful
    incredible perfect flawless gorgeous beautiful addictive polished refined satisfying
    rewarding immersive captivating recommend recommended love loved enjoy enjoyed
    enjoyable best greatest stunning phenomenal solid great good fun
    """.split()
)

_NEGATIVE = frozenset(
    """
    terrible awful horrible garbage trash broken unplayable buggy crash crashes crashing
    unfinished disappointing disappointment boring tedious repetitive grindy shallow
    frustrating unfair clunky janky ugly bland lifeless soulless refund avoid waste
    worst hate hated mediocre lazy scam greedy predatory microtransactions paywall
    """.split()
)

_NEGATORS = frozenset({"not", "no", "never", "hardly", "barely", "isn't", "wasn't", "don't"})

#: Off-topic markers. Review bombing about matters unrelated to the game is a documented
#: contaminant (8 of 24 negative Cyberpunk PS4 reviews were about a market withdrawal).
_OFF_TOPIC = frozenset(
    """
    politics political boycott ukraine russia russian israel palestine woke agenda dei
    propaganda censorship lawsuit ceo layoffs union strike
    """.split()
)


class RejectReason(StrEnum):
    TOO_SHORT = "too_short"
    DUPLICATE = "duplicate"
    SCORE_TEXT_MISMATCH = "score_text_mismatch"
    OFF_TOPIC = "off_topic"


@dataclass(frozen=True, slots=True)
class CorpusItem:
    review_id: int
    body: str
    score: float | None
    score_max: int
    published_on: dt.date | None
    publication: str | None
    dedupe_key: str
    bucket: str

    @property
    def normalized_score(self) -> float | None:
        if self.score is None:
            return None
        return self.score * (100.0 / self.score_max)


@dataclass(frozen=True, slots=True)
class FilterResult:
    kept: list[CorpusItem]
    rejected: dict[str, int]
    candidate_count: int


#: How far a negation reaches. "not a masterpiece" needs more than one token of scope,
#: "not" ... twelve words ... "masterpiece" does not.
_NEGATION_WINDOW = 3


def polarity(text: str) -> int:
    """Crude signed polarity of a text. A negator flips the next few sentiment words."""
    tokens = [t.strip(".,!?;:()[]\"'").lower() for t in text.split()]
    score = 0
    negation_left = 0
    for token in tokens:
        if token in _NEGATORS:
            negation_left = _NEGATION_WINDOW
            continue
        value = 1 if token in _POSITIVE else (-1 if token in _NEGATIVE else 0)
        if value:
            score += -value if negation_left else value
            negation_left = 0
        elif negation_left:
            negation_left -= 1
    return score


def off_topic_ratio(text: str) -> float:
    tokens = [t.strip(".,!?;:()[]\"'").lower() for t in text.split()]
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if t in _OFF_TOPIC) / len(tokens)


def filter_corpus(
    items: list[CorpusItem],
    *,
    min_chars: int,
    mismatch_min_polarity: int = 3,
    off_topic_threshold: float = 0.06,
) -> FilterResult:
    """Drop what would poison a summary. Count everything dropped.

    The mismatch rule is intentionally conservative: it needs a LONG text with a STRONG,
    consistent polarity pointing the opposite way from a score in the top or bottom third.
    A merely lukewarm 8/10 is not a mismatch.
    """
    rejected: Counter[str] = Counter()
    kept: list[CorpusItem] = []
    seen_hashes: set[str] = set()

    for item in items:
        body = collapse_whitespace(item.body or "")
        if len(body) < min_chars:
            rejected[RejectReason.TOO_SHORT.value] += 1
            continue

        fingerprint = sha256_text(body.lower())
        if fingerprint in seen_hashes:
            rejected[RejectReason.DUPLICATE.value] += 1
            continue

        if off_topic_ratio(body) >= off_topic_threshold:
            rejected[RejectReason.OFF_TOPIC.value] += 1
            continue

        norm = item.normalized_score
        if norm is not None:
            signal = polarity(body)
            top_third, bottom_third = norm >= 70, norm <= 35
            if (top_third and signal <= -mismatch_min_polarity) or (
                bottom_third and signal >= mismatch_min_polarity
            ):
                rejected[RejectReason.SCORE_TEXT_MISMATCH.value] += 1
                continue

        seen_hashes.add(fingerprint)
        kept.append(item)

    return FilterResult(kept=kept, rejected=dict(rejected), candidate_count=len(items))


# --------------------------------------------------------------------------- sampling


def _bucket_of(item: CorpusItem) -> str:
    """Sampling stratum. Derived from the SCORE and used only to spread the sample.

    It is not a claim about what the text says — that distinction is the whole of C-07.
    """
    norm = item.normalized_score
    if norm is None:
        return "unscored"
    if norm >= 70:
        return "positive"
    if norm >= 50:
        return "neutral"
    return "negative"


def _rank(item: CorpusItem, *, newest: dt.date | None, oldest: dt.date | None) -> float:
    length = min(1.0, len(item.body) / 900.0)

    recency = 0.5
    if item.published_on and newest and oldest and newest != oldest:
        span = (newest - oldest).days or 1
        recency = (item.published_on - oldest).days / span

    norm = item.normalized_score
    extremity = 0.5 if norm is None else abs(norm - 50) / 50.0

    return 0.45 * length + 0.30 * recency + 0.15 * extremity + 0.10 * 0.5


def stratified_sample(
    items: list[CorpusItem], *, target: int, min_stratum_share: float = 0.15
) -> list[CorpusItem]:
    """Proportional quotas with a floor, so a minority opinion cannot vanish.

    Also split by TIME within each sentiment bucket: a game patched after launch has two
    populations of opinion, and a sample drawn only from the newer one cannot support —
    or refute — a claim that opinion changed.
    """
    if len(items) <= target:
        return list(items)

    dates = [i.published_on for i in items if i.published_on]
    newest, oldest = (max(dates), min(dates)) if dates else (None, None)
    midpoint = oldest + (newest - oldest) / 2 if newest and oldest and newest != oldest else None

    strata: dict[tuple[str, str], list[CorpusItem]] = {}
    for item in items:
        era = "old"
        if midpoint is not None and item.published_on is not None:
            era = "new" if item.published_on >= midpoint else "old"
        strata.setdefault((_bucket_of(item), era), []).append(item)

    non_empty = [key for key, group in strata.items() if group]
    floor = max(1, round(target * min_stratum_share / max(1, len(non_empty))))

    quotas: dict[tuple[str, str], int] = {}
    for key in non_empty:
        proportional = round(target * len(strata[key]) / len(items))
        quotas[key] = min(len(strata[key]), max(floor, proportional))

    # Trim or top up to hit the target exactly, largest strata first.
    def total() -> int:
        return sum(quotas.values())

    order = sorted(non_empty, key=lambda k: len(strata[k]), reverse=True)
    while total() > target:
        for key in order:
            if total() <= target:
                break
            if quotas[key] > floor:
                quotas[key] -= 1
    while total() < target:
        progressed = False
        for key in order:
            if total() >= target:
                break
            if quotas[key] < len(strata[key]):
                quotas[key] += 1
                progressed = True
        if not progressed:
            break

    picked: list[CorpusItem] = []
    for key in order:
        group = sorted(
            strata[key],
            key=lambda i: (-_rank(i, newest=newest, oldest=oldest), i.dedupe_key),
        )
        picked.extend(group[: quotas[key]])
    return picked


# --------------------------------------------------------------------------- ordering


def order_deterministically(items: list[CorpusItem]) -> list[CorpusItem]:
    """Stable order independent of database row order or upstream paging.

    Undated reviews sort last so the numbering of dated ones is not disturbed when a
    review's date arrives later.
    """
    return sorted(
        items,
        key=lambda i: (
            i.published_on is None,
            i.published_on or dt.date.min,
            i.dedupe_key,
        ),
    )


def evidence_ref(kind_prefix: str, position: int) -> str:
    return f"{kind_prefix}{position:02d}"


def content_hash(
    items: list[CorpusItem], *, ordering_version: int, sampling_version: int
) -> str:
    """Identity of a corpus: composition AND order AND the algorithms that produced them.

    Versioning the algorithms means a change in sampling produces a NEW snapshot rather
    than silently changing what an existing citation refers to (ADR-007).
    """
    payload = "|".join(
        f"{i.dedupe_key}:{i.score}:{i.published_on or ''}:{sha256_text(i.body)[:16]}"
        for i in items
    )
    return sha256_text(f"v{ordering_version}.{sampling_version}|{payload}")


# --------------------------------------------------------------------------- rendering


def render_corpus(
    items: list[tuple[str, CorpusItem]], *, max_review_chars: int
) -> str:
    """The text handed to the model.

    Every line carries its evidence reference AND its date. The date is not decoration:
    the validator uses it to reject temporal claims that the corpus cannot support.
    """
    lines = []
    for ref, item in items:
        score = "n/a" if item.score is None else f"{item.score:g}"
        date = item.published_on.isoformat() if item.published_on else "unknown"
        source = f" | pub={item.publication}" if item.publication else ""
        body = sanitize_for_prompt(item.body, max_chars=max_review_chars)
        lines.append(f"[{ref}] score={score} | date={date}{source} | {body}")
    return "\n".join(lines)
