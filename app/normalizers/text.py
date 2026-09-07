"""Pure text helpers shared by parsers, search and the corpus builder.

No IO, no imports beyond the standard library — these are the cheapest and most valuable
functions in the codebase to test, which is why they live outside ``adapters``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Sequences that could be mistaken for prompt structure once a review is embedded in a
# corpus. Stripped on the way in, so an injected delimiter never reaches the model.
_PROMPT_ARTIFACT_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s|===+|---+\s*$|\[/?(?:INST|SYS|SYSTEM|USER|ASSISTANT)\]|"
    r"<\|[^>]{0,32}\|>|```)"
)
#: C0/C1 controls plus the invisible formatting characters: zero-width spaces and
#: joiners, the word joiner, the BOM and the bidirectional overrides. They survive
#: every visual review, and inside a review body their only use is to hide
#: something -- a split keyword, or text that renders right-to-left over the rest
#: of the line.
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")


def collapse_whitespace(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def clean_body(value: str | None) -> str | None:
    """Normalise a review body for storage.

    Drops carriage returns, collapses runs of blank lines, strips control characters and
    HTML tags. Cosmetic edits upstream therefore do not produce a new ``body_hash``.
    """
    if value is None:
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip()
    return text or None


def sanitize_for_prompt(value: str, *, max_chars: int) -> str:
    """Make a review safe to embed in a prompt (ADR-019 T7).

    Not a claim that prompt injection is solved — the real defence is that a claim
    without valid evidence references is discarded by the validator. This just removes
    the easy structural tricks and caps the length.
    """
    text = _CONTROL_RE.sub(" ", value)
    text = _PROMPT_ARTIFACT_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = collapse_whitespace(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(title: str) -> str:
    """Search key: lowercase, accent-free, punctuation-free, single-spaced.

    ``NieR: Automata`` -> ``nier automata``; ``Assassin's Creed`` -> ``assassins creed``.
    Apostrophes are removed rather than replaced by a space so that ``assassins`` matches.
    """
    text = strip_accents(title).lower()
    text = text.replace("'", "").replace("’", "").replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    return collapse_whitespace(text)


def slugify(value: str) -> str:
    text = strip_accents(value).lower()
    text = text.replace("'", "").replace("’", "")
    text = _SLUG_RE.sub("-", text)
    return text.strip("-")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def body_hash(body: str | None) -> str:
    """Stable hash of a review body, insensitive to case and whitespace churn."""
    if not body:
        return sha256_text("")
    return sha256_text(collapse_whitespace(body).lower())


def stable_fingerprint(payload: object) -> str:
    """Order-independent fingerprint of a nested structure.

    Dict key order must not change the result: the source is free to reorder JSON keys,
    and a fingerprint that flips on reordering would trigger a full re-sync every hour.
    """
    return sha256_text(_canonical(payload))


def _canonical(value: object) -> str:
    if isinstance(value, dict):
        items = sorted((str(k), _canonical(v)) for k, v in value.items())
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, set):
        return "[" + ",".join(sorted(_canonical(v) for v in value)) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def token_set(value: str) -> frozenset[str]:
    return frozenset(t for t in normalize_title(value).split(" ") if t)


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of normalised tokens. Used for title relevance and lexical similarity."""
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def coverage(needle: str, haystack: str) -> float:
    """Share of ``needle``'s tokens present in ``haystack``.

    Asymmetric on purpose: for "is this video about this game" the question is whether the
    game's title is covered by the video title, not whether the two are similar overall.
    """
    tn, th = token_set(needle), token_set(haystack)
    if not tn:
        return 0.0
    return len(tn & th) / len(tn)
