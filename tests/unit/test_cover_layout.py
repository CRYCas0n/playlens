"""Cover images have to stay inside the box that holds them.

The `cover` macro renders `.cover-img` as `position: absolute; inset: 0`, so it is laid
out against its nearest *positioned* ancestor. `.spotlight__cover` had no rule in the
stylesheet at all, so the image resolved against `.spotlight` instead and stretched over
the entire hero — headline, verdict line, both score chips and both buttons underneath
it, present in the DOM and unreachable by a mouse.

Nothing caught it. It is invisible to the HTML tests, which only see markup; the browser
pass measured overflow and console errors and never asked what was on top of the button;
and it looks, at a glance, like a slightly odd crop rather than a covered hero.

So: every container the macro is used in must be a positioned box, and its shape must
match the shape the source actually sends.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")
TEMPLATES = ROOT / "app" / "web" / "templates"

#: Every class the `cover` macro is rendered into. Derived below, not maintained by hand.
CONTAINER_RE = re.compile(
    r'<div class="([a-z_-]+(?:__[a-z-]+)?)[^"]*">\s*\{\{[ -]*(?:m\.)?cover\(', re.S
)


def cover_containers() -> set[str]:
    found: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        found |= set(CONTAINER_RE.findall(path.read_text(encoding="utf-8")))
    return found


def rule_body(selector: str) -> str:
    """The declarations of the first rule whose selector list contains `selector`."""
    pattern = re.compile(
        r"(?:^|\})[^{}]*(?<![\w-])" + re.escape(selector) + r"(?![\w-])[^{}]*\{([^}]*)\}",
        re.S | re.M,
    )
    match = pattern.search(CSS)
    return match.group(1) if match else ""


def test_the_containers_were_found():
    containers = cover_containers()
    assert {"game-card__cover", "spotlight__cover", "ghero__cover"} <= containers, containers


@pytest.mark.parametrize("container", sorted(cover_containers()))
def test_every_cover_container_is_a_positioned_box(container: str):
    """`.cover-img` is `position: absolute; inset: 0`.

    A static parent does not contain it: the image escapes to the nearest positioned
    ancestor and covers whatever is there. `.spotlight__cover` was exactly that, and it
    put the hero art on top of the hero's own text and buttons.
    """
    body = rule_body("." + container)
    assert body, f".{container} has no rule in app.css at all"
    assert "position" in body and ("relative" in body or "absolute" in body), (
        f".{container} is not a positioned box: {body.strip()[:120]}"
    )


@pytest.mark.parametrize(
    "container", ["game-card__cover", "sim-card__cover", "ghero__cover", "spotlight__cover"]
)
def test_cover_boxes_are_landscape(container: str):
    """Measured, not assumed: 45 covers from production, every one landscape, median
    ratio 1.78 — 16/9 exactly — and the commonest size the 460x215 Steam capsule. The
    3/4 boxes these used to be were built for box art this source never sends, and they
    showed roughly a third of each image."""
    body = rule_body("." + container)
    match = re.search(r"aspect-ratio:\s*([\d.]+)\s*/\s*([\d.]+)", body)
    assert match, f".{container} sets no aspect-ratio: {body.strip()[:120]}"
    ratio = float(match.group(1)) / float(match.group(2))
    assert ratio > 1.4, f".{container} is {ratio:.2f}, portrait; every cover is landscape"


def test_the_skeleton_reserves_the_same_shape_as_the_cover():
    """Otherwise the page jumps when the image lands."""
    skeleton = re.search(r"aspect-ratio:\s*([\d.]+)\s*/\s*([\d.]+)", rule_body(".sk--cover"))
    card = re.search(
        r"aspect-ratio:\s*([\d.]+)\s*/\s*([\d.]+)", rule_body(".game-card__cover")
    )
    assert skeleton and card
    assert skeleton.groups() == card.groups(), "the skeleton and the cover disagree"


def test_the_spotlight_markup_matches_the_stylesheet():
    """The stylesheet describes `.spotlight__inner` as the grid that holds the readable
    content, over `.spotlight__art` and `.spotlight__scrim`. The template rendered
    neither wrapper, so the section was a plain block and the absolute cover had nothing
    to sit inside."""
    index = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    for required in ("spotlight__art", "spotlight__scrim", "spotlight__inner"):
        assert required in index, f"app.css styles .{required} and index.html never renders it"
    assert "display: grid" in rule_body(".spotlight__inner")


def test_covers_are_anchored_to_the_top_of_the_art():
    """Metacritic bakes the game's title into the top of the cover. A crop that takes it
    from the top removes the title, which is what made one game's art unreadable."""
    for container in ("game-card__cover", "ghero__cover", "sim-card__cover"):
        body = rule_body(f".{container} img")
        assert "object-position" in body and "top" in body, f".{container} img: {body[:80]}"
