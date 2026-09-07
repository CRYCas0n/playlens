"""HTML extraction helpers for the fallback source.

Order of preference, from durable to brittle:

1. ``application/ld+json`` with ``@type: VideoGame`` — a documented, stable contract.
2. OpenGraph meta tags.
3. ``data-testid`` anchors (``product-card``, ``product-score``, ``global-score-header``).

**CSS selectors on Tailwind utility classes are forbidden** and the ban is enforced by a
test, not by discipline: those classes are generated and change with any redesign
(ADR-001).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)


def json_ld_blocks(html: str) -> list[Any]:
    blocks: list[Any] = []
    for raw in _JSONLD_RE.findall(html):
        try:
            blocks.append(json.loads(raw.strip()))
        except (ValueError, TypeError):
            continue
    return blocks


def find_video_game(html: str) -> dict[str, Any] | None:
    """The one JSON-LD block Metacritic puts on a game page."""
    for block in json_ld_blocks(html):
        for candidate in block if isinstance(block, list) else [block]:
            if isinstance(candidate, dict) and candidate.get("@type") in {
                "VideoGame",
                "Game",
                "Product",
            }:
                return candidate
    return None


def meta_tags(html: str) -> dict[str, str]:
    return {key.lower(): value for key, value in _META_RE.findall(html)}


class _TestIdCollector(HTMLParser):
    """Collect elements carrying a ``data-testid``, with their text and attributes."""

    def __init__(self, wanted: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.wanted = wanted
        self.found: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any] | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k.lower(): (v or "") for k, v in attrs}
        testid = attributes.get("data-testid")
        entry: dict[str, Any] | None = None
        if testid in self.wanted:
            entry = {"testid": testid, "tag": tag, "attrs": attributes, "text": ""}
            self.found.append(entry)
        self._stack.append(entry)

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        for entry in self._stack:
            if entry is not None:
                entry["text"] += data


def elements_by_testid(html: str, wanted: set[str]) -> list[dict[str, Any]]:
    parser = _TestIdCollector(wanted)
    parser.feed(html)
    return parser.found


def first_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def first_float(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None
