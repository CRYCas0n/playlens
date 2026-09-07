"""The contract the model must satisfy.

The single most important thing in this file is what is NOT here: there is no minimum
number of claims. Release 0 showed that a fixed "3 positives / 3 negatives" layout makes
a model invent criticism for games that received none — Elden Ring's critic corpus is
86 positive, 0 neutral, 0 negative — and it does so precisely on the famous games where a
fabricated complaint costs the most trust (C-06).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import Aspect, AspectVerdict, ClaimType


def _known_aspect(value: Any) -> Any:
    """An unrecognised aspect label becomes ``other`` instead of failing the summary.

    The aspect is a filing label: it groups claims on the page and feeds the similarity
    profile. The claim text and its evidence references are what a reader acts on, and
    those are validated properly elsewhere.

    Providers differ on how hard they enforce an enum. Anthropic's tool schema is binding;
    OpenAI's function parameters are guidance, and gpt-4o-mini returns e.g. ``"value"``
    often enough to matter. Losing an entire correct summary over one mislabelled
    category would be the schema being precious about the least important field in it.
    """
    if isinstance(value, str):
        try:
            return Aspect(value)
        except ValueError:
            return Aspect.OTHER
    return value

MAX_CLAIMS_PER_SIDE = 5
# Character budgets, in Russian.
#
# These were set against English and then the output language changed, which makes them
# roughly a fifth too small: the same information in Russian runs 15-20% longer, because
# the words are longer and the language does not contract the way English does. A model
# writing a correct, concise paragraph had it rejected for being 704 characters, and the
# whole summary went with it -- a length limit is not a quality control, and it should
# never be the thing that loses a good answer.
#
# The numbers are the English ones plus a fifth, rounded. They still bound the paragraph
# to something a reader will actually read, which is the point of having them.
MAX_CLAIM_CHARS = 290
MAX_OVERALL_CHARS = 850
MAX_HEADING_CHARS = 110


class ClaimOut(BaseModel):
    #: `claim_ru` is REQUIRED in the schema the model is given and OPTIONAL to parse.
    #:
    #: Those have to differ. Left merely optional, gpt-4o skipped it every time -- the
    #: summaries regenerated, cost money, and came back in English, which is how a whole
    #: catalogue was rewritten into the language it already had. Made required in Python
    #: instead, one missing field would reject an otherwise valid summary and lose every
    #: claim in it.
    #:
    #: So the tool contract insists and the parser forgives: the model is told the field
    #: is mandatory, and a model that ignores that costs the reader a translation rather
    #: than the finding.
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"required": ["aspect", "claim", "claim_ru"]},
    )

    aspect: Aspect
    #: English, in the reviews' own vocabulary. This is the field the validator checks:
    #: rule 6 intersects its tokens with the cited review text, and a Russian sentence
    #: shares no tokens with an English review, so validating the translation instead
    #: would reject every claim ever made.
    claim: str = Field(max_length=MAX_CLAIM_CHARS)
    #: The same claim in Russian, which is what a reader sees. Empty is allowed and falls
    #: back to `claim`: a missing translation should cost the reader a language, not the
    #: whole finding.
    claim_ru: str = Field(
        default="",
        max_length=MAX_CLAIM_CHARS,
        description=(
            "The same claim in natural Russian. Mandatory. A faithful rendering of "
            "`claim`, not a new thought: same aspect, same strength, nothing added."
        ),
    )

    _coerce_aspect = field_validator("aspect", mode="before")(_known_aspect)
    claim_type: ClaimType = ClaimType.DESCRIPTIVE
    #: Evidence references from the snapshot, e.g. ["C03", "C11", "C27"].
    evidence: list[str] = Field(default_factory=list, max_length=8)
    strength: str = "moderate"


class TranslationOut(BaseModel):
    """One field, because that is all a translation is.

    Deliberately not folded into a summary call: the description is source text about the
    game, not a claim about what reviewers said, and validating one against the review
    corpus would reject every sentence of it.
    """

    model_config = ConfigDict(extra="ignore")

    text: str = Field(default="", max_length=4000)


class SummaryOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    heading: str = Field(default="", max_length=MAX_HEADING_CHARS)
    overall: str = Field(default="", max_length=MAX_OVERALL_CHARS)
    #: 0..5. An empty list is a legitimate, informative answer.
    positive: list[ClaimOut] = Field(default_factory=list, max_length=MAX_CLAIMS_PER_SIDE)
    negative: list[ClaimOut] = Field(default_factory=list, max_length=MAX_CLAIMS_PER_SIDE)
    aspect_verdicts: dict[Aspect, AspectVerdict] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("aspect_verdicts", mode="before")
    @classmethod
    def _drop_unknown_aspects(cls, value: Any) -> Any:
        """Unknown keys are dropped rather than mapped to ``other``.

        Different from a claim's aspect: this is a dictionary, and folding two unknown
        labels into one key would silently discard one of their verdicts.
        """
        if not isinstance(value, dict):
            return value
        known = {a.value for a in Aspect}
        return {k: v for k, v in value.items() if str(k) in known}


class GapOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    explanation: str = Field(default="", max_length=MAX_OVERALL_CHARS)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class LetsPlayPointOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aspect: Aspect
    text: str = Field(max_length=MAX_CLAIM_CHARS)

    _coerce_aspect = field_validator("aspect", mode="before")(_known_aspect)


class LetsPlayOut(BaseModel):
    """A playthrough impression.

    No positive/negative split and no evidence references: this summary rests on ONE
    source, and pretending otherwise -- with citation counts, or with a balanced pros and
    cons layout -- would dress up a single person's session as a consensus (ADR-016).
    """

    model_config = ConfigDict(extra="ignore")

    heading: str = Field(default="", max_length=MAX_HEADING_CHARS)
    overall: str = Field(default="", max_length=MAX_OVERALL_CHARS)
    points: list[LetsPlayPointOut] = Field(default_factory=list, max_length=5)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


#: JSON Schema handed to providers that support structured output. Generated from the
#: models so the two cannot drift.
def summary_json_schema() -> dict:
    return SummaryOut.model_json_schema()


def gap_json_schema() -> dict:
    return GapOut.model_json_schema()


def letsplay_json_schema() -> dict:
    return LetsPlayOut.model_json_schema()
