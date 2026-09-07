"""Similar games storage and candidate selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Game,
    GameCompany,
    GameGenre,
    GamePlatform,
    SimilarGame,
)
from app.repositories.base import utc_now


@dataclass(frozen=True, slots=True)
class SimilarRow:
    similar_game_id: int
    rank: int
    score: float
    components: dict[str, float]
    reason: str | None


class SimilarityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def candidates(self, game: Game, *, limit: int) -> list[Game]:
        """Candidates come from OUR catalogue only — the assignment's constraint, met by
        construction rather than by a filter that could be forgotten."""
        genre_ids = [gg.genre_id for gg in game.genres]
        company_ids = [gc.company_id for gc in game.companies]

        selects = []

        if genre_ids:
            selects.append(
                sa.select(Game.id)
                .join(GameGenre, GameGenre.game_id == Game.id)
                .where(GameGenre.genre_id.in_(genre_ids), Game.id != game.id)
                .order_by(Game.best_metascore.desc())
                .limit(limit)
            )
        if company_ids:
            selects.append(
                sa.select(Game.id)
                .join(GameCompany, GameCompany.game_id == Game.id)
                .where(GameCompany.company_id.in_(company_ids), Game.id != game.id)
                .limit(max(20, limit // 3))
            )
        if game.mc_family_id:
            selects.append(
                sa.select(Game.id)
                .where(Game.mc_family_id == game.mc_family_id, Game.id != game.id)
                .limit(20)
            )
        if game.franchise_id:
            selects.append(
                sa.select(Game.id)
                .where(Game.franchise_id == game.franchise_id, Game.id != game.id)
                .limit(20)
            )
        if not selects:
            # No metadata at all: fall back to a small slice of the catalogue by score so
            # the game is not silently excluded from similarity forever.
            selects.append(
                sa.select(Game.id)
                .where(Game.id != game.id, Game.best_metascore.is_not(None))
                .order_by(Game.best_metascore.desc())
                .limit(min(limit, 50))
            )

        ids: list[int] = []
        seen: set[int] = set()
        for stmt in selects:
            for (gid,) in self.session.execute(stmt).all():
                if gid not in seen:
                    seen.add(int(gid))
                    ids.append(int(gid))
            if len(ids) >= limit:
                break

        if not ids:
            return []
        return list(
            self.session.execute(
                sa.select(Game)
                .where(Game.id.in_(ids[:limit]))
                .options(
                    selectinload(Game.genres),
                    selectinload(Game.companies),
                    selectinload(Game.platforms).selectinload(GamePlatform.platform),
                )
            ).scalars()
        )

    def replace(self, game_id: int, rows: list[SimilarRow], *, method: str) -> None:
        """DELETE + INSERT in one transaction.

        Idempotent, and it leaves no window in which a game has no similar games at all —
        which a naive delete-then-compute would.
        """
        self.session.execute(sa.delete(SimilarGame).where(SimilarGame.game_id == game_id))
        now = utc_now()
        self.session.add_all(
            SimilarGame(
                game_id=game_id,
                similar_game_id=row.similar_game_id,
                rank=row.rank,
                score=row.score,
                method=method,
                components=row.components,
                reason=row.reason,
                computed_at=now,
            )
            for row in rows
        )
        self.session.flush()

    def for_game(self, game_id: int, *, limit: int = 12) -> list[tuple[SimilarGame, Game]]:
        rows = self.session.execute(
            sa.select(SimilarGame, Game)
            .join(Game, Game.id == SimilarGame.similar_game_id)
            .where(SimilarGame.game_id == game_id)
            .order_by(SimilarGame.rank)
            .limit(limit)
            .options(selectinload(Game.platforms).selectinload(GamePlatform.platform))
        ).all()
        return [(row[0], row[1]) for row in rows]

    def coverage(self, min_count: int = 3) -> dict[str, Any]:
        total = self.session.execute(sa.select(sa.func.count()).select_from(Game)).scalar_one()
        counted = self.session.execute(
            sa.select(sa.func.count()).select_from(
                sa.select(SimilarGame.game_id)
                .group_by(SimilarGame.game_id)
                .having(sa.func.count() >= min_count)
                .subquery()
            )
        ).scalar_one()
        return {"games": int(total), "with_similar": int(counted)}
