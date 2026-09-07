"""Text that comes from the source, rendered for a Russian reader.

Three different kinds of English reached the page, and they are not one problem:

* chrome the templates own — fixed by translating them;
* claims the model writes — fixed by asking for a Russian rendering beside the verified
  English one;
* text the *source* publishes: the genre list and the game description.

This file covers the third. A genre is a closed vocabulary, so it is a table: free,
instant, identical every time, and reviewable by someone who knows the subject. A
description is prose, so it is a model call — and the page says which of the two texts it
is showing rather than leaving the reader to wonder whether we edited it.
"""

from __future__ import annotations

import re

import pytest

from app.domain.genre_names import GENRE_RU, genre_ru

CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")

#: Terms Russian players use in English. Translating these makes them harder to read,
#: not easier, and each one is a deliberate entry rather than an oversight.
KEPT_IN_ENGLISH = {"FPS", "RPG", "JRPG", "Action RPG", "Roguelike", "Point-and-click"}


class TestGenres:
    def test_an_unknown_genre_falls_back_to_the_source_wording(self):
        """A genre this table has not met yet shows the source's own name. An
        untranslated chip is a small blemish; an invented translation for a term of art
        is a larger one."""
        assert genre_ru("Bullet Heaven") == "Bullet Heaven"

    def test_the_table_is_not_a_pass_through(self):
        translated = [v for k, v in GENRE_RU.items() if v != k]
        assert len(translated) > 40, "most of the vocabulary should actually be translated"

    @pytest.mark.parametrize("source,russian", sorted(GENRE_RU.items()))
    def test_each_entry_is_russian_or_deliberately_not(self, source: str, russian: str):
        if russian in KEPT_IN_ENGLISH:
            return
        assert CYRILLIC.search(russian), f"{source!r} -> {russian!r} has no Russian in it"

    def test_no_entry_is_empty(self):
        for source, russian in GENRE_RU.items():
            assert russian.strip(), source


class TestTheModelIsToldRussianIsMandatory:
    """Optional was not enough, and the failure was expensive and silent.

    gpt-4o simply omitted `claim_ru` every time it was merely optional: summaries
    regenerated, cost money, and came back in the language they were already in. Made
    required in Python instead, one missing field would reject an otherwise valid summary
    and lose every claim in it.

    So the two differ on purpose. The tool contract insists; the parser forgives.
    """

    def test_the_tool_schema_demands_it(self):
        from app.ai.schemas import SummaryOut

        claim = SummaryOut.model_json_schema()["$defs"]["ClaimOut"]
        assert "claim_ru" in claim["required"], claim.get("required")

    def test_the_parser_still_accepts_a_model_that_ignores_that(self):
        from app.ai.schemas import ClaimOut

        parsed = ClaimOut(aspect="other", claim="Reviewers praise the soundtrack.")
        assert parsed.claim_ru == ""

    def test_a_missing_translation_falls_back_to_the_verified_english(self):
        from app.api.schemas import ClaimOut as ClaimDTO

        english = "Reviewers praise the soundtrack."
        assert ClaimDTO(
            aspect="audio", claim=english, claim_type="descriptive", evidence=["C01"]
        ).text == english
        assert ClaimDTO(
            aspect="audio", claim=english, claim_ru="Рецензенты хвалят саундтрек.",
            claim_type="descriptive", evidence=["C01"],
        ).text == "Рецензенты хвалят саундтрек."


class TestTheBudgetsFitTheLanguage:
    """A length limit should never be the thing that loses a good answer.

    The budgets were set against English and the output language then changed. Russian
    runs 15-20% longer for the same information, so a model writing a correct, concise
    paragraph had it rejected at 704 characters against a 700 limit -- and the whole
    summary went with it, claims included.
    """

    def test_the_prompt_tells_the_model_the_budget(self):
        """A model cannot respect a limit it is never given."""
        from app.ai.prompts import _RULES
        from app.ai.schemas import MAX_CLAIM_CHARS, MAX_HEADING_CHARS, MAX_OVERALL_CHARS

        for limit in (MAX_CLAIM_CHARS, MAX_HEADING_CHARS, MAX_OVERALL_CHARS):
            assert str(limit) in _RULES, limit

    def test_the_budgets_allow_for_russian_running_longer(self):
        from app.ai.schemas import MAX_CLAIM_CHARS, MAX_HEADING_CHARS, MAX_OVERALL_CHARS

        # The English-era values, kept here as the thing being compared against.
        for english, now in ((240, MAX_CLAIM_CHARS), (700, MAX_OVERALL_CHARS), (90, MAX_HEADING_CHARS)):
            assert now >= english * 1.15, f"{now} leaves no room over the English {english}"
            assert now <= english * 1.4, f"{now} is no longer a limit worth having"


class TestDescription:
    def test_the_translation_prompt_keeps_names_alone(self):
        """A reader searches for "Road to Glory", not for a translation of it."""
        from app.ai.prompts import build_translation_prompt

        prompt = build_translation_prompt(title="Big Walk", text="A co-op walking game.")
        assert "Russian" in prompt.system
        assert "original form" in prompt.system
        assert "Big Walk" in prompt.context
        assert "A co-op walking game." in prompt.corpus

    def test_a_translation_is_not_validated_against_reviews(self):
        """It is source text about the game, not a claim about what reviewers said.
        Sending it through claim validation would reject every sentence."""
        from app.ai.schemas import TranslationOut

        assert set(TranslationOut.model_fields) == {"text"}

    def test_the_page_says_which_text_it_is_showing(self):
        """"Unedited" and "translated" are different promises, and the old copy made the
        first one while the new path makes the second."""
        from pathlib import Path

        game = (
            Path(__file__).resolve().parents[2]
            / "app" / "web" / "templates" / "game.html"
        ).read_text(encoding="utf-8")
        assert "game.description_ru" in game
        assert "переведённое на русский" in game
        assert 'lang="en"' in game, "the untranslated fallback must declare its language"
