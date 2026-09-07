"""Similar games — an explainable hybrid, no embedding model (ADR-011).

Three components, all of which can be named in words:

* **metadata** — genres, franchise, studio, platforms, score proximity, era;
* **aspect profile** — cosine over the 15 aspect verdicts the AI summaries already
  produce, which is the "similar in feel" signal an embedding was wanted for, at zero
  extra cost and with a label a human can read;
* **lexical** — token overlap of a small similarity document.

The reason chip is derived from whichever component dominated. When none does, the reason
is ``None`` and the UI renders nothing — it never invents one
(design/README.md non-negotiable 8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import Settings
from app.domain.enums import AspectVerdict, SimilarityMethod
from app.logging import get_logger
from app.normalizers.text import token_overlap
from app.repositories.similarity import SimilarRow

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)

ASPECT_VALUES = {
    AspectVerdict.POSITIVE.value: 1.0,
    AspectVerdict.MIXED.value: 0.0,
    AspectVerdict.NEGATIVE.value: -1.0,
    AspectVerdict.ABSENT.value: 0.0,
}

# Metadata sub-weights. Deliberately favouring the HARD signals: sharing a franchise or a
# studio is a fact, sharing a genre with thirty thousand other games is barely a hint.
W_GENRE = 0.30
W_FRANCHISE = 0.20
W_COMPANY = 0.16
W_PLATFORM = 0.12
W_SCORE = 0.12
W_ERA = 0.10

PENALTY_BOTH_UNRATED = 0.35
PENALTY_SAME_FAMILY = 0.5


@dataclass(frozen=True, slots=True)
class Components:
    metadata: float
    aspect: float | None
    lexical: float
    genre: float = 0.0
    franchise: float = 0.0
    company: float = 0.0
    platform: float = 0.0
    score: float = 0.0
    era: float = 0.0
    penalty: float = 1.0

    def as_dict(self) -> dict[str, float]:
        return {
            "metadata": round(self.metadata, 4),
            "aspect": None if self.aspect is None else round(self.aspect, 4),
            "lexical": round(self.lexical, 4),
            "genre": round(self.genre, 4),
            "franchise": round(self.franchise, 4),
            "company": round(self.company, 4),
            "platform": round(self.platform, 4),
            "score": round(self.score, 4),
            "era": round(self.era, 4),
            "penalty": round(self.penalty, 4),
        }


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    # Map [-1, 1] onto [0, 1] so every component shares one range.
    return (dot / (na * nb) + 1) / 2


class SimilarityService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------ scoring

    def score_pair(self, a, b, *, aspects: dict[int, dict[str, str]]) -> tuple[float, Components]:
        genres_a = {gg.genre_id for gg in a.genres}
        genres_b = {gg.genre_id for gg in b.genres}
        devs_a = {gc.company_id for gc in a.companies if gc.role == "developer"}
        devs_b = {gc.company_id for gc in b.companies if gc.role == "developer"}
        pubs_a = {gc.company_id for gc in a.companies if gc.role == "publisher"}
        pubs_b = {gc.company_id for gc in b.companies if gc.role == "publisher"}
        plats_a = {gp.platform_id for gp in a.platforms}
        plats_b = {gp.platform_id for gp in b.platforms}

        genre = jaccard(genres_a, genres_b)

        franchise = 0.0
        if a.franchise_id and a.franchise_id == b.franchise_id:
            franchise = 1.0
        elif a.mc_family_id and a.mc_family_id == b.mc_family_id:
            franchise = 0.9

        company = 1.0 if devs_a & devs_b else (0.5 if pubs_a & pubs_b else 0.0)
        platform = jaccard(plats_a, plats_b)

        score = 0.0
        if a.best_metascore is not None and b.best_metascore is not None:
            score = 1 - abs(a.best_metascore - b.best_metascore) / 100

        era = 0.0
        if a.premiere_year and b.premiere_year:
            era = math.exp(-abs(a.premiere_year - b.premiere_year) / 6)

        metadata = (
            W_GENRE * genre
            + W_FRANCHISE * franchise
            + W_COMPANY * company
            + W_PLATFORM * platform
            + W_SCORE * score
            + W_ERA * era
        )

        aspect = self._aspect_similarity(aspects.get(a.id), aspects.get(b.id))
        lexical = token_overlap(self._document(a), self._document(b))

        weights = {"metadata": self._settings.similarity_w_metadata,
                   "lexical": self._settings.similarity_w_lexical}
        parts = {"metadata": metadata, "lexical": lexical}
        if aspect is not None:
            weights["aspect"] = self._settings.similarity_w_aspect
            parts["aspect"] = aspect
        # Renormalise so a missing component reduces confidence, not the scale. Before any
        # summary exists the system rides on metadata + lexical, which is the documented
        # cold-start behaviour rather than an outage.
        total_weight = sum(weights.values())
        combined = sum(weights[k] * parts[k] for k in weights) / total_weight

        penalty = 1.0
        both_unrated = a.best_metascore is None and b.best_metascore is None
        if both_unrated and a.user_reviews_total < 5 and b.user_reviews_total < 5:
            penalty *= PENALTY_BOTH_UNRATED
        if a.mc_family_id and a.mc_family_id == b.mc_family_id:
            # A DLC or re-release is genuinely similar, but a list made only of them is
            # not a recommendation.
            penalty *= PENALTY_SAME_FAMILY

        components = Components(
            metadata=metadata, aspect=aspect, lexical=lexical, genre=genre,
            franchise=franchise, company=company, platform=platform, score=score,
            era=era, penalty=penalty,
        )
        return combined * penalty, components

    def _aspect_similarity(
        self, a: dict[str, str] | None, b: dict[str, str] | None
    ) -> float | None:
        if not a or not b:
            return None
        keys = sorted(set(a) | set(b))
        vec_a = [ASPECT_VALUES.get(a.get(k, "absent"), 0.0) for k in keys]
        vec_b = [ASPECT_VALUES.get(b.get(k, "absent"), 0.0) for k in keys]
        if not any(vec_a) or not any(vec_b):
            return None
        return cosine(vec_a, vec_b)

    @staticmethod
    def _document(game) -> str:
        parts = [game.title]
        parts += [gg.genre.name for gg in game.genres if gg.genre]
        parts += [gc.company.name for gc in game.companies if gc.company]
        if game.description:
            parts.append(game.description[:600])
        return " ".join(parts)

    # ------------------------------------------------------------------ reasons

    def reason_for(self, components: Components) -> str | None:
        """A reason, or nothing. Never a guess.

        Each branch names a real, checkable fact about the pair. If no component is strong
        enough to name, the chip is simply absent — which the design treats as a normal
        card shape, not a gap.
        """
        floor = self._settings.reason_min_contribution
        if components.franchise >= 0.9:
            return "Same series"
        if components.company >= 1.0:
            return "Same studio"
        if components.genre >= 0.6:
            return "Similar genre and tone"
        if components.aspect is not None and components.aspect >= 0.75:
            return "Praised for similar things"
        if components.score >= 0.9 and components.metadata >= floor:
            return "Similar critic reception"
        return None

    # ------------------------------------------------------------------ orchestration

    def recompute(self, uow: UnitOfWork, game_id: int) -> int:
        game = uow.games.get_full(game_id)
        if game is None:
            return 0

        candidates = uow.similarity.candidates(
            game, limit=self._settings.similarity_candidate_limit
        )
        if not candidates:
            uow.similarity.replace(game_id, [], method=SimilarityMethod.HYBRID.value)
            uow.games.touch(game_id, similarity_synced_at=_now())
            return 0

        aspects = uow.summaries.aspect_profiles([game.id, *[c.id for c in candidates]])

        scored: list[tuple[float, Components, int]] = []
        for candidate in candidates:
            if candidate.id == game.id:
                continue
            value, components = self.score_pair(game, candidate, aspects=aspects)
            if value >= self._settings.similarity_min_score:
                scored.append((value, components, candidate.id))

        scored.sort(key=lambda row: (-row[0], row[2]))
        top = scored[: self._settings.similarity_top_n]

        rows = [
            SimilarRow(
                similar_game_id=candidate_id,
                rank=index + 1,
                score=round(value, 5),
                components=components.as_dict(),
                reason=self.reason_for(components),
            )
            for index, (value, components, candidate_id) in enumerate(top)
        ]
        method = (
            SimilarityMethod.HYBRID.value
            if any(r.components.get("aspect") is not None for r in rows)
            else SimilarityMethod.METADATA.value
        )
        uow.similarity.replace(game_id, rows, method=method)
        uow.games.touch(game_id, similarity_synced_at=_now())

        uow.events.emit(
            "similarity.computed",
            game_id=game_id,
            data={
                "candidates": len(candidates),
                "above_threshold": len(scored),
                "published": len(rows),
                "with_reason": sum(1 for r in rows if r.reason),
                "method": method,
            },
        )
        return len(rows)


def _now():
    import datetime as dt

    return dt.datetime.now(dt.UTC)
