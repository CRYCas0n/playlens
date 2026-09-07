"""Game persistence and catalogue queries.

The upsert of a game, its platforms, genres, companies and rollups happens in ONE
transaction (FINAL_SPEC section 23). Anything less allows a game with no platforms, or
rollups that disagree with the rows they summarise.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Company,
    Franchise,
    Game,
    GameCompany,
    GameGenre,
    GamePlatform,
    Genre,
    Platform,
)
from app.db.seed import platform_code_for
from app.domain.models import GameDetail
from app.normalizers.text import normalize_title
from app.repositories.base import dialect_name, nulls_last, upsert_returning_id, utc_now
from app.repositories.score_sql import effective_metascore, effective_userscore, gap_expr


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    game_id: int
    created: bool
    changed: bool
    slug_changed: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    q: str | None = None
    platforms: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    score_band: str = "any"        # any | excellent | good | mixed | unrated
    released: str = "any"          # any | last30 | 2026 | earlier
    sort: str = "metascore"        # metascore | userscore | release_date | gap | title
    order: str = "desc"
    limit: int = 24
    offset: int = 0
    #: Carried on the filter object because the rule belongs to configuration, not to
    #: the repository: the same query has to agree with the presenter (ADR-002).
    userscore_zero_min_ratings: int = 20


class GameRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ identity

    def find(self, *, mc_title_id: int | None, slug: str) -> Game | None:
        """Identity resolution: durable id first, natural key second.

        A game that is renamed keeps its ``mc_title_id`` and gets a new slug. Looking up
        by slug alone would create a duplicate; looking up by id alone would miss games
        the source never gave an id for.
        """
        if mc_title_id is not None:
            found = self.session.execute(
                sa.select(Game).where(Game.mc_title_id == mc_title_id)
            ).scalar_one_or_none()
            if found is not None:
                return found
        return self.session.execute(
            sa.select(Game).where(Game.mc_slug == slug)
        ).scalar_one_or_none()

    def get_by_slug(self, slug: str) -> Game | None:
        return self.session.execute(
            sa.select(Game)
            .options(
                selectinload(Game.platforms).selectinload(GamePlatform.platform),
                selectinload(Game.genres).selectinload(GameGenre.genre),
                selectinload(Game.companies).selectinload(GameCompany.company),
                selectinload(Game.franchise),
            )
            .where(Game.mc_slug == slug)
        ).scalar_one_or_none()

    def get(self, game_id: int) -> Game | None:
        return self.session.get(Game, game_id)

    def get_full(self, game_id: int) -> Game | None:
        """A game with everything similarity scoring needs, in one round trip."""
        return self.session.execute(
            sa.select(Game)
            .where(Game.id == game_id)
            .options(
                selectinload(Game.platforms).selectinload(GamePlatform.platform),
                selectinload(Game.genres).selectinload(GameGenre.genre),
                selectinload(Game.companies).selectinload(GameCompany.company),
            )
        ).scalar_one_or_none()

    def get_platform(self, game_platform_id: int) -> GamePlatform | None:
        return self.session.execute(
            sa.select(GamePlatform)
            .where(GamePlatform.id == game_platform_id)
            .options(selectinload(GamePlatform.platform), selectinload(GamePlatform.game))
        ).scalar_one_or_none()

    def platforms_for(self, game_id: int) -> list[GamePlatform]:
        return list(
            self.session.execute(
                sa.select(GamePlatform)
                .where(GamePlatform.game_id == game_id)
                .options(selectinload(GamePlatform.platform))
                .order_by(GamePlatform.is_lead.desc(), GamePlatform.id)
            ).scalars()
        )

    # ------------------------------------------------------------------ upsert

    def upsert(self, detail: GameDetail) -> UpsertOutcome:
        existing = self.find(mc_title_id=detail.mc_title_id, slug=detail.slug)
        created = existing is None
        slug_changed = None

        if existing is not None and existing.mc_slug != detail.slug:
            slug_changed = existing.mc_slug

        changed = created or existing.source_fingerprint != detail.source_fingerprint

        if created:
            game = Game(mc_slug=detail.slug, mc_url=detail.mc_url, title=detail.title,
                        title_norm=detail.title_norm)
            self.session.add(game)
        else:
            game = existing

        franchise_id = self._upsert_franchise(detail)

        game.mc_slug = detail.slug
        game.mc_title_id = detail.mc_title_id
        game.mc_family_id = detail.mc_family_id
        game.mc_url = detail.mc_url
        game.title = detail.title
        game.title_norm = detail.title_norm or normalize_title(detail.title)
        game.description = detail.description
        game.cover_url = detail.cover_url
        game.card_url = detail.card_url
        game.video_url = detail.video_url
        game.video_title = detail.video_title
        game.video_duration_s = detail.video_duration_s
        game.release_date = detail.release_date
        game.premiere_year = detail.premiere_year
        game.esrb_rating = detail.esrb_rating
        game.must_play = detail.must_play
        game.franchise_id = franchise_id
        game.source_fingerprint = detail.source_fingerprint
        game.detail_synced_at = utc_now()
        game.updated_at = utc_now()
        self.session.flush()

        self._upsert_genres(game, detail)
        self._upsert_companies(game, detail)
        self._upsert_platforms(game, detail)
        self.refresh_rollups(game)
        self.session.flush()

        return UpsertOutcome(
            game_id=game.id, created=created, changed=changed, slug_changed=slug_changed
        )

    def _upsert_franchise(self, detail: GameDetail) -> int | None:
        if detail.franchise is None:
            return None
        return upsert_returning_id(
            self.session,
            Franchise.__table__,
            {
                "slug": detail.franchise.slug,
                "name": detail.franchise.name,
                "mc_franchise_id": detail.franchise.mc_franchise_id,
            },
            index_elements=["slug"],
            update_columns=["name"],
            id_column=Franchise.__table__.c.id,
        )

    def _upsert_genres(self, game: Game, detail: GameDetail) -> None:
        wanted: set[int] = set()
        for ref in detail.genres:
            genre_id = upsert_returning_id(
                self.session,
                Genre.__table__,
                {"slug": ref.slug, "name": ref.name},
                index_elements=["slug"],
                update_columns=["name"],
                id_column=Genre.__table__.c.id,
            )
            wanted.add(genre_id)
        self._sync_link_table(GameGenre, game.id, "genre_id", wanted)

    def _upsert_companies(self, game: Game, detail: GameDetail) -> None:
        wanted: set[tuple[int, str]] = set()
        for ref in detail.companies:
            company_id = upsert_returning_id(
                self.session,
                Company.__table__,
                {"slug": ref.slug, "name": ref.name, "mc_company_id": ref.mc_company_id},
                index_elements=["slug"],
                update_columns=["name"],
                id_column=Company.__table__.c.id,
            )
            wanted.add((company_id, ref.role))

        current = {
            (row.company_id, row.role)
            for row in self.session.execute(
                sa.select(GameCompany).where(GameCompany.game_id == game.id)
            ).scalars()
        }
        for company_id, role in wanted - current:
            self.session.add(GameCompany(game_id=game.id, company_id=company_id, role=role))
        for company_id, role in current - wanted:
            self.session.execute(
                sa.delete(GameCompany).where(
                    GameCompany.game_id == game.id,
                    GameCompany.company_id == company_id,
                    GameCompany.role == role,
                )
            )
        self.session.flush()

    def _sync_link_table(self, model: Any, game_id: int, column: str, wanted: set[int]) -> None:
        current = {
            getattr(row, column)
            for row in self.session.execute(
                sa.select(model).where(model.game_id == game_id)
            ).scalars()
        }
        for value in wanted - current:
            self.session.add(model(game_id=game_id, **{column: value}))
        if current - wanted:
            self.session.execute(
                sa.delete(model).where(
                    model.game_id == game_id, getattr(model, column).in_(current - wanted)
                )
            )
        self.session.flush()

    def _upsert_platforms(self, game: Game, detail: GameDetail) -> None:
        existing = {
            row.platform_id: row
            for row in self.session.execute(
                sa.select(GamePlatform).where(GamePlatform.game_id == game.id)
            ).scalars()
        }
        # Clear the lead flag first: the partial unique index allows exactly one, so
        # setting a new lead before clearing the old one would violate it.
        for row in existing.values():
            row.is_lead = False
        self.session.flush()

        for scores in detail.platforms:
            platform_id = self._platform_id(scores)
            row = existing.get(platform_id)
            if row is None:
                row = GamePlatform(game_id=game.id, platform_id=platform_id)
                self.session.add(row)
                existing[platform_id] = row
            row.mc_related_game_id = scores.mc_related_game_id
            row.is_lead = scores.is_lead
            row.release_date = scores.release_date
            row.metascore_raw = scores.metascore
            row.metascore_count = scores.metascore_count
            row.metascore_positive = scores.metascore_positive
            row.metascore_neutral = scores.metascore_neutral
            row.metascore_negative = scores.metascore_negative
            row.critic_sentiment = scores.critic_sentiment
            # Userscore fields are only overwritten when the stats call actually supplied
            # them; a base sync must not wipe values fetched by a later enrichment pass.
            if scores.userscore is not None or scores.userscore_count:
                row.userscore_raw = scores.userscore
                row.userscore_count = scores.userscore_count
                row.userscore_positive = scores.userscore_positive
                row.userscore_neutral = scores.userscore_neutral
                row.userscore_negative = scores.userscore_negative
                row.user_sentiment = scores.user_sentiment
            if scores.critic_reviews_with_text:
                row.critic_reviews_with_text = scores.critic_reviews_with_text
            if scores.user_reviews_with_text:
                row.user_reviews_with_text = scores.user_reviews_with_text
            row.updated_at = utc_now()
        # A platform that vanished upstream is NOT deleted: history matters more than
        # tidiness, and the row simply stops being updated.
        self.session.flush()

    def _platform_id(self, scores) -> int:
        return upsert_returning_id(
            self.session,
            Platform.__table__,
            {
                "slug": scores.slug,
                "name": scores.name,
                "code": platform_code_for(scores.slug, scores.name),
                "mc_platform_id": scores.mc_platform_id,
            },
            index_elements=["slug"],
            update_columns=["name"],
            id_column=Platform.__table__.c.id,
        )

    # ------------------------------------------------------------------ rollups

    def refresh_rollups(self, game: Game) -> None:
        """Recompute the denormalised scores.

        Called inside the same transaction as the platform rows it summarises, so the two
        cannot drift. ``best_*`` ignores rows without a score rather than treating a
        missing value as zero (ADR-002).
        """
        rows = list(
            self.session.execute(
                sa.select(GamePlatform).where(GamePlatform.game_id == game.id)
            ).scalars()
        )
        game.platform_count = len(rows)

        lead = next((r for r in rows if r.is_lead), rows[0] if rows else None)
        game.lead_platform_id = lead.platform_id if lead else None
        game.lead_metascore = lead.metascore_raw if lead else None
        game.lead_metascore_count = lead.metascore_count if lead else 0
        game.lead_userscore = lead.userscore_raw if lead else None
        game.lead_userscore_count = lead.userscore_count if lead else 0

        rated_meta = [r for r in rows if r.metascore_raw is not None]
        best_meta = max(rated_meta, key=lambda r: r.metascore_raw) if rated_meta else None
        game.best_metascore = best_meta.metascore_raw if best_meta else None
        game.best_metascore_count = best_meta.metascore_count if best_meta else 0

        rated_user = [r for r in rows if r.userscore_raw is not None and r.userscore_count > 0]
        best_user = max(rated_user, key=lambda r: r.userscore_raw) if rated_user else None
        game.best_userscore = best_user.userscore_raw if best_user else None
        game.best_userscore_count = best_user.userscore_count if best_user else 0

        game.critic_reviews_total = sum(r.critic_reviews_with_text for r in rows)
        game.user_reviews_total = sum(r.user_reviews_with_text for r in rows)
        self.session.flush()

    def touch(self, game_id: int, **fields: dt.datetime | None) -> None:
        """Stamp a synced-at column.

        Through the ORM object rather than a bulk UPDATE. The session is built with
        ``expire_on_commit=False`` so that presenters can read a detached game after the
        unit of work closes; the cost is that a bulk UPDATE leaves an already-loaded
        instance holding the old value. A once-per-game gate reading that stale value runs
        twice. ``session.get`` hits the identity map, so this is not an extra query.
        """
        game = self.session.get(Game, game_id)
        if game is None:
            return
        for column, value in fields.items():
            setattr(game, column, value)
        self.session.flush()

    # ------------------------------------------------------------------ catalogue

    def _base_query(self, filters: CatalogFilters):
        stmt = sa.select(Game)

        if filters.q:
            stmt = self._apply_search(stmt, filters.q)

        if filters.platforms:
            stmt = stmt.where(
                sa.exists(
                    sa.select(1)
                    .select_from(GamePlatform.__table__.join(Platform.__table__))
                    .where(
                        GamePlatform.game_id == Game.id,
                        Platform.slug.in_(filters.platforms),
                    )
                    .correlate(Game)
                )
            )

        if filters.genres:
            stmt = stmt.where(
                sa.exists(
                    sa.select(1)
                    .select_from(GameGenre.__table__.join(Genre.__table__))
                    .where(GameGenre.game_id == Game.id, Genre.slug.in_(filters.genres))
                    .correlate(Game)
                )
            )

        # Cumulative, because the chips say so: "Good 70+" promises everything at 70 and
        # above, not a 70-84 slice. The filter must mean what its label says.
        band = filters.score_band
        meta = effective_metascore(filters.userscore_zero_min_ratings)
        if band == "excellent":
            stmt = stmt.where(meta >= 85)
        elif band == "good":
            stmt = stmt.where(meta >= 70)
        elif band == "mixed":
            stmt = stmt.where(meta >= 50)
        elif band == "unrated":
            stmt = stmt.where(meta.is_(None))

        today = dt.date.today()
        if filters.released == "last30":
            stmt = stmt.where(Game.release_date >= today - dt.timedelta(days=30))
        elif filters.released == "2026":
            stmt = stmt.where(Game.premiere_year == 2026)
        elif filters.released == "earlier":
            stmt = stmt.where(sa.or_(Game.premiere_year < 2026, Game.premiere_year.is_(None)))

        return stmt

    def _apply_search(self, stmt, q: str):
        """The one dialect-specific query in the project (ADR-018).

        Both branches rank identically: exact prefix first, then substring, then score.
        """
        needle = normalize_title(q)
        if not needle:
            return stmt
        prefix = f"{needle}%"
        contains = f"%{needle}%"
        if dialect_name(self.session) == "postgresql":
            # pg_trgm similarity plus prefix; the GIN index from migration 0002 serves it.
            return stmt.where(
                sa.or_(
                    Game.title_norm.like(prefix),
                    Game.title_norm.like(contains),
                    sa.func.similarity(Game.title_norm, needle) > 0.25,
                )
            )
        return stmt.where(
            sa.or_(Game.title_norm.like(prefix), Game.title_norm.like(contains))
        )

    def _apply_sort(self, stmt, filters: CatalogFilters):
        descending = filters.order != "asc"
        if filters.sort == "title":
            column = Game.title_norm
            return stmt.order_by(
                sa.desc(column) if descending else sa.asc(column), Game.id.desc()
            )
        if filters.sort == "release_date":
            return stmt.order_by(
                nulls_last(Game.release_date, descending=descending), Game.id.desc()
            )
        if filters.sort == "userscore":
            return stmt.order_by(
                nulls_last(
                    effective_userscore(filters.userscore_zero_min_ratings),
                    descending=descending,
                ),
                Game.id.desc(),
            )
        if filters.sort == "gap":
            # The catalogue expression of the product's core idea: biggest critic-player
            # disagreement first. Games missing either side cannot have a gap and sink.
            # Ascending is not offered -- "smallest disagreement" is not a question anyone
            # asks, and a near-zero gap is indistinguishable from agreement.
            return stmt.order_by(
                nulls_last(gap_expr(filters.userscore_zero_min_ratings), descending=True),
                Game.id.desc(),
            )
        return stmt.order_by(
            nulls_last(
                effective_metascore(filters.userscore_zero_min_ratings),
                descending=descending,
            ),
            Game.id.desc(),
        )

    def search(self, filters: CatalogFilters) -> tuple[list[Game], int]:
        stmt = self._base_query(filters)
        total = self.session.execute(
            sa.select(sa.func.count()).select_from(stmt.subquery())
        ).scalar_one()
        stmt = self._apply_sort(stmt, filters).limit(filters.limit).offset(filters.offset)
        stmt = stmt.options(
            selectinload(Game.platforms).selectinload(GamePlatform.platform),
            selectinload(Game.genres).selectinload(GameGenre.genre),
            selectinload(Game.companies).selectinload(GameCompany.company),
        )
        return list(self.session.execute(stmt).scalars()), int(total)

    def suggest(self, q: str, limit: int = 8) -> list[Game]:
        needle = normalize_title(q)
        if len(needle) < 2:
            return []
        stmt = (
            self._apply_search(sa.select(Game), q)
            .order_by(
                sa.case((Game.title_norm.like(f"{needle}%"), 0), else_=1),
                nulls_last(Game.best_metascore, descending=True),
                Game.id.desc(),
            )
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def platform_facets(
        self, filters: CatalogFilters, *, include_empty: bool = False
    ) -> list[tuple[str, str, str, int]]:
        """Result counts per platform, so the chips can show them (design/PAGES.md 3).

        ``include_empty`` switches the join from inner to outer. With an inner join a
        platform nobody has a game on simply is not in the result, so a caller asking for
        "all platforms" would silently get only the populated ones.
        """
        inner = self._base_query(
            CatalogFilters(
                q=filters.q, genres=filters.genres, score_band=filters.score_band,
                released=filters.released,
            )
        ).subquery()
        joined = (
            sa.select(
                Platform.slug,
                Platform.name,
                Platform.code,
                sa.func.count(sa.distinct(GamePlatform.game_id)),
            )
            .select_from(Platform)
            .join(
                GamePlatform,
                GamePlatform.platform_id == Platform.id,
                isouter=include_empty,
            )
            .join(inner, inner.c.id == GamePlatform.game_id, isouter=include_empty)
            .group_by(Platform.slug, Platform.name, Platform.code, Platform.sort_order)
            .order_by(Platform.sort_order)
        )
        rows = self.session.execute(joined).all()
        return [(slug, name, code, int(n)) for slug, name, code, n in rows]

    def genre_facets(
        self, filters: CatalogFilters, *, include_empty: bool = False
    ) -> list[tuple[str, str, int]]:
        """Genres with result counts, for the catalogue's genre filter.

        Counted the same way platforms are, and with the same self-exclusion: the genre
        currently being filtered on does not narrow its own count, or every chip but the
        selected one would read zero.
        """
        inner = self._base_query(
            CatalogFilters(
                q=filters.q, platforms=filters.platforms, score_band=filters.score_band,
                released=filters.released,
            )
        ).subquery()
        rows = self.session.execute(
            sa.select(Genre.slug, Genre.name, sa.func.count(sa.distinct(GameGenre.game_id)))
            .select_from(Genre)
            .join(GameGenre, GameGenre.genre_id == Genre.id, isouter=include_empty)
            .join(inner, inner.c.id == GameGenre.game_id, isouter=include_empty)
            .group_by(Genre.slug, Genre.name)
            .order_by(sa.func.count(sa.distinct(GameGenre.game_id)).desc(), Genre.name)
        ).all()
        return [(slug, name, int(n)) for slug, name, n in rows]

    # ------------------------------------------------------------------ discovery rails

    def recently_updated(self, limit: int = 5) -> list[Game]:
        return list(
            self.session.execute(
                sa.select(Game)
                .where(Game.detail_synced_at.is_not(None))
                .order_by(Game.updated_at.desc(), Game.id.desc())
                .limit(limit)
                .options(selectinload(Game.platforms).selectinload(GamePlatform.platform))
            ).scalars()
        )

    def just_released(self, limit: int = 12, days: int = 30) -> list[Game]:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        return list(
            self.session.execute(
                sa.select(Game)
                .where(Game.release_date.is_not(None), Game.release_date >= cutoff)
                .order_by(Game.release_date.desc(), Game.id.desc())
                .limit(limit)
                .options(selectinload(Game.platforms).selectinload(GamePlatform.platform))
            ).scalars()
        )

    def highest_rated(
        self, limit: int = 12, min_score: int = 85, min_reviews: int = 10
    ) -> list[Game]:
        return list(
            self.session.execute(
                sa.select(Game)
                .where(
                    Game.best_metascore >= min_score,
                    Game.best_metascore_count >= min_reviews,
                )
                .order_by(Game.best_metascore.desc(), Game.id.desc())
                .limit(limit)
                .options(selectinload(Game.platforms).selectinload(GamePlatform.platform))
            ).scalars()
        )

    def biggest_gaps(
        self, limit: int = 12, min_gap: int = 10, *, zero_min_ratings: int = 20
    ) -> list[Game]:
        """The differentiating rail: games the two audiences disagree about."""
        gap = gap_expr(zero_min_ratings)
        return list(
            self.session.execute(
                sa.select(Game)
                .where(gap >= min_gap)
                .order_by(gap.desc(), Game.id.desc())
                .limit(limit)
                .options(selectinload(Game.platforms).selectinload(GamePlatform.platform))
            ).scalars()
        )

    def needs_reviews(self, *, older_than: dt.datetime, limit: int = 50) -> list[Game]:
        return list(
            self.session.execute(
                sa.select(Game)
                .where(
                    sa.or_(Game.reviews_synced_at.is_(None), Game.reviews_synced_at < older_than)
                )
                .order_by(nulls_last(Game.reviews_synced_at, descending=False))
                .limit(limit)
            ).scalars()
        )

    def needs_similarity(self, *, older_than: dt.datetime, limit: int = 500) -> list[int]:
        rows = self.session.execute(
            sa.select(Game.id)
            .where(
                sa.or_(
                    Game.similarity_synced_at.is_(None), Game.similarity_synced_at < older_than
                )
            )
            .order_by(nulls_last(Game.similarity_synced_at, descending=False))
            .limit(limit)
        ).all()
        return [int(r[0]) for r in rows]

    def known_slugs(self) -> set[str]:
        return {
            str(r[0]) for r in self.session.execute(sa.select(Game.mc_slug)).all()
        }

    def stats(self) -> dict[str, Any]:
        total = self.session.execute(sa.select(sa.func.count()).select_from(Game)).scalar_one()
        with_meta = self.session.execute(
            sa.select(sa.func.count())
            .select_from(Game)
            .where(effective_metascore().is_not(None))
        ).scalar_one()
        with_summaries = self.session.execute(
            sa.select(sa.func.count())
            .select_from(Game)
            .where(Game.summaries_synced_at.is_not(None))
        ).scalar_one()
        last_crawl = self.session.execute(
            sa.select(sa.func.max(Game.detail_synced_at))
        ).scalar_one()
        return {
            "games_total": int(total),
            "games_with_metascore": int(with_meta),
            "games_with_summaries": int(with_summaries),
            "last_crawl_at": last_crawl,
        }
