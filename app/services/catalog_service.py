"""Read-side assembly for a game page.

Kept as a service rather than living in the router so the HTML page and the JSON endpoint
call exactly the same code and cannot disagree about what a game looks like (ADR-017).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.api import presenters
from app.api.schemas import GameDetailOut
from app.config import Settings

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork


@dataclass(frozen=True, slots=True)
class HomeSections:
    spotlight: object | None
    just_released: list
    disagree: list
    highest_rated: list
    recently_updated: list


class CatalogService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def game_detail(
        self, uow: UnitOfWork, slug: str, *, selected_platform: str | None = None
    ) -> GameDetailOut | None:
        game = uow.games.get_by_slug(slug)
        if game is None:
            return None

        summaries = uow.summaries.current_for_game(game.id)
        similar = uow.similarity.for_game(game.id, limit=self._settings.similarity_top_n)

        gap = None
        lead = next((gp for gp in game.platforms if gp.is_lead), None)
        if lead is not None:
            gap = uow.gaps.current(lead.id)

        lets_play = None
        video = uow.youtube.selected(game.id)
        if video is not None:
            lets_play = (video, uow.youtube.transcript(video.id))

        return presenters.game_detail(
            game,
            settings=self._settings,
            summaries=summaries,
            similar=similar,
            gap=gap,
            lets_play=lets_play,
            selected_platform_slug=selected_platform,
        )

    def home(self, uow: UnitOfWork) -> HomeSections:
        """The four blocks the design specifies, each with a stated rule.

        ``disagree`` is the one a competitor's front page cannot show, so it is not an
        optional extra: it is what teaches the product's idea before a user opens a game
        (design/PAGES.md section 2.3).
        """
        just_released = uow.games.just_released(limit=12)
        disagree = uow.games.biggest_gaps(
            limit=12,
            min_gap=10,
            zero_min_ratings=self._settings.userscore_zero_min_ratings,
        )
        highest_rated = uow.games.highest_rated(limit=12)
        recently_updated = uow.games.recently_updated(limit=5)

        spotlight = None
        for candidate in (disagree, highest_rated, just_released, recently_updated):
            if candidate:
                spotlight = candidate[0]
                break

        return HomeSections(
            spotlight=spotlight,
            just_released=just_released,
            disagree=disagree,
            highest_rated=highest_rated,
            recently_updated=recently_updated,
        )
