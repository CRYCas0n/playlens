"""Every class a template uses must have a rule behind it.

This defect has now appeared three times, and each time it rendered:

* `.spotlight__cover` had no rule, so the absolutely-positioned cover image resolved
  against the whole hero and covered the headline, the verdict and both buttons;
* `.visually-hidden` had no rule, so every filter chip drew its raw checkbox beside it;
* `.catalog__layout` had no rule, so the filter form had no grid and a submit button
  meant for narrow screens landed on top of the search field.

None of it is visible to a markup assertion: the HTML is exactly what the template says.
It is only visible in a browser, and only if you happen to open the page it breaks. A
class with no rule is not a style question, it is a missing implementation, and this is
the cheapest place to notice.

ADR-017 makes the stylesheet the ported artefact and the markup the thing that conforms
to it, so where both have a name for the same idea — `pager__item` and `pager__link` —
the template is what changes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "web" / "static"
TEMPLATES = ROOT / "app" / "web" / "templates"

#: A class name built by interpolation — `badge--{{ tone }}`. The prefix is all the
#: template commits to; which modifier appears is a runtime value, so the assertion is
#: that the *base* class exists.
INTERPOLATED = re.compile(r"^([a-z][\w-]*?)--$")


def stylesheet() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(STATIC.glob("*.css"))
    )


def defined_classes() -> set[str]:
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", stylesheet()))


def classes_in(path: Path) -> set[str]:
    """Class tokens in a template, with Jinja expressions removed first.

    `class="chip{% if x %} is-selected{% endif %}"` yields `chip` and `is-selected`.
    """
    used: set[str] = set()
    for attribute in re.findall(r'class="([^"]*)"', path.read_text(encoding="utf-8")):
        cleaned = re.sub(r"\{[%{].*?[%}]\}", " ", attribute)
        used |= {tok for tok in cleaned.split() if re.fullmatch(r"[a-z][\w-]*-*", tok)}
    return used


TEMPLATE_FILES = sorted(TEMPLATES.rglob("*.html"))
DEFINED = defined_classes()


def test_the_stylesheet_and_templates_were_found():
    assert TEMPLATE_FILES, "no templates"
    assert len(DEFINED) > 100, f"only {len(DEFINED)} classes parsed from the stylesheet"


@pytest.mark.parametrize("path", TEMPLATE_FILES, ids=lambda p: p.name)
def test_every_class_in_the_template_has_a_rule(path: Path):
    missing = []
    for name in sorted(classes_in(path)):
        interpolated = INTERPOLATED.match(name)
        # `badge--{{ tone }}`: check the base, since the modifier is a runtime value.
        base = interpolated.group(1) if interpolated else name
        if base not in DEFINED:
            missing.append(name)
    assert not missing, f"{path.name} uses classes with no rule: {missing}"


def test_the_check_would_notice_a_new_one():
    """A test that cannot fail is worse than no test."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.html"
        probe.write_text('<div class="totally-invented-class"></div>', encoding="utf-8")
        assert "totally-invented-class" in classes_in(probe)
        assert "totally-invented-class" not in DEFINED


class TestTheOnesThatBroke:
    """Named individually: each of these was a visible defect in production."""

    def test_visually_hidden_hides(self):
        """Without it the filter chips draw their own checkboxes."""
        assert re.search(r"\.visually-hidden\s*\{[^}]*clip", stylesheet(), re.S)

    def test_the_catalogue_form_carries_the_grid_not_the_page(self):
        """`.catalog` on the container made the page heading a grid item and squeezed it
        into the sidebar column."""
        catalog = (TEMPLATES / "catalog.html").read_text(encoding="utf-8")
        assert 'class="container catalog"' not in catalog
        assert 'class="catalog" id="catalog-form"' in catalog

    def test_the_responsive_utilities_exist_in_both_directions(self):
        css = stylesheet()
        for name in ("hide-mobile", "hide-tablet", "hide-desktop"):
            assert re.search(rf"\.{name}\s*\{{", css), name

    def test_the_cover_containers_are_positioned(self):
        """Covered by test_cover_layout.py in detail; asserted here so the two files
        cannot both be deleted without something complaining."""
        assert re.search(r"\.spotlight__cover\s*\{[^}]*position:\s*relative", stylesheet(), re.S)
