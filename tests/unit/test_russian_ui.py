"""No English left where a reader looks.

The site is read in Russian. Chrome that stayed English is not a cosmetic problem: a
half-translated page reads as unfinished, and the strings that get missed are always the
rare ones -- empty states, error pages, the aria-labels a screen reader announces -- which
are exactly the ones nobody opens while checking.

Model output is a separate matter and is not covered here: `claim` is deliberately English
because it is the wording checked against English reviews, and `claim_ru` is what the page
shows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "app" / "web" / "templates"

#: Latin words that legitimately appear: names, codes, technical identifiers.
ALLOWED = {
    "playlens", "metacritic", "youtube", "fandom", "ai", "id", "url", "api", "css", "js",
    "svg", "http", "https", "pc", "xsx", "ps4", "ps5", "mc", "usr", "sse",
}

#: Attributes whose value a person actually reads or hears.
HUMAN_ATTRS = ("aria-label", "title", "placeholder", "data-more", "data-less")

CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
LATIN_PHRASE = re.compile(r"\b[A-Za-z][a-z]{2,}(?:\s+[a-z]{2,}){1,}\b")


def visible_text(html: str) -> list[str]:
    """Text nodes and human-facing attribute values, with Jinja and comments removed."""
    html = re.sub(r"\{#.*?#\}", " ", html, flags=re.S)  # Jinja comments: English is fine
    html = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)

    found: list[str] = []
    for attr in HUMAN_ATTRS:
        found += re.findall(rf'{attr}="([^"{{}}]+)"', html)
    body = re.sub(r"<[^>]+>", "\x00", html)
    found += body.split("\x00")
    return [re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", f).strip() for f in found]


def english_phrases(path: Path) -> list[str]:
    problems = []
    for fragment in visible_text(path.read_text(encoding="utf-8")):
        if not fragment or CYRILLIC.search(fragment):
            continue
        for phrase in LATIN_PHRASE.findall(fragment):
            if all(word.lower() in ALLOWED for word in phrase.split()):
                continue
            problems.append(phrase)
    return problems


@pytest.mark.parametrize(
    "path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: p.name
)
def test_no_english_prose_in_a_template(path: Path):
    problems = english_phrases(path)
    assert not problems, f"{path.name} still shows English: {sorted(set(problems))[:6]}"


def test_the_page_declares_russian():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'lang="ru"' in base, "a screen reader would read Russian with English phonemes"


def test_the_detector_would_catch_a_regression():
    """A test that cannot fail is worse than no test."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.html"
        probe.write_text('<p>Browse all games</p>', encoding="utf-8")
        assert english_phrases(probe), "the detector does not detect anything"


def test_the_copy_a_reader_sees_from_python_is_russian():
    """Verdict lines, tier labels and the empty-section notes are Python constants."""
    from app.ai.validator import empty_section_note
    from app.domain.enums import ClaimSide, Tier
    from app.domain.scores import TIER_LABELS
    from app.domain.verdict import AXIS_CAPTION, CRITIC_COPY, NOTHING_TO_SAY, PLAYER_COPY

    strings = [
        *TIER_LABELS.values(),
        *CRITIC_COPY.values(),
        *PLAYER_COPY.values(),
        NOTHING_TO_SAY,
        AXIS_CAPTION,
        empty_section_note(side=ClaimSide.NEGATIVE, positive_count=9, negative_count=0),
        empty_section_note(side=ClaimSide.POSITIVE, positive_count=0, negative_count=9),
    ]
    assert Tier.EXCELLENT.value == "excellent", "the enum value is a CSS class, not copy"
    for text in strings:
        assert CYRILLIC.search(text), f"still English: {text!r}"
