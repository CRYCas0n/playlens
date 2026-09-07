"""How many SQL statements each page and endpoint actually costs.

`selectinload` was placed everywhere collections are read, and nothing measured whether
it worked. An N+1 does not fail a test — it passes every one of them and then takes the
site down at 200 games. The numbers below are ceilings, deliberately a little above what
is measured today: the point is to notice a jump, not to freeze an implementation.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from app.db.models import (
    Company,
    Game,
    GameCompany,
    GameGenre,
    GamePlatform,
    Genre,
    Platform,
    SimilarGame,
)
from app.normalizers.text import normalize_title

pytestmark = pytest.mark.integration

GAMES = 30


@contextmanager
def counted(engine):
    """Count the statements SQLAlchemy sends while the block runs."""
    statements: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


@pytest.fixture
def catalogue(db):
    """Thirty games, each with platforms, genres and companies.

    Thirty, not three: with three, an N+1 and a constant look the same.
    """
    platforms = list(db.execute(sa.select(Platform)).scalars())[:4]
    genres = []
    for slug in ("action", "adventure", "rpg"):
        row = Genre(slug=slug, name=slug.title())
        db.add(row)
        db.flush()
        genres.append(row)
    companies = []
    for name in ("Nightgate", "Thornhill", "Vantage"):
        row = Company(slug=name.lower(), name=name)
        db.add(row)
        db.flush()
        companies.append(row)

    games = []
    for index in range(GAMES):
        title = f"Test Game {index:02d}"
        game = Game(
            mc_slug=f"test-game-{index:02d}",
            mc_url=f"https://www.metacritic.com/game/test-game-{index:02d}/",
            title=title,
            title_norm=normalize_title(title),
            description="A game.",
            best_metascore=60 + (index % 40),
            best_metascore_count=40,
            best_userscore=5.0 + (index % 50) / 10,
            best_userscore_count=900,
            release_date=dt.date(2026, 1, 1),
            premiere_year=2026,
            detail_synced_at=dt.datetime.now(dt.UTC),
        )
        db.add(game)
        db.flush()
        for position, platform in enumerate(platforms[: 1 + index % 4]):
            db.add(
                GamePlatform(
                    game_id=game.id,
                    platform_id=platform.id,
                    is_lead=position == 0,
                    metascore_raw=60 + (index % 40),
                    metascore_count=40,
                    userscore_raw=5.0 + (index % 50) / 10,
                    userscore_count=900,
                )
            )
        for genre in genres[: 1 + index % 3]:
            db.add(GameGenre(game_id=game.id, genre_id=genre.id))
        for company in companies[: 1 + index % 3]:
            db.add(GameCompany(game_id=game.id, company_id=company.id, role="developer"))
        games.append(game)

    for rank, other in enumerate(games[1:9], start=1):
        db.add(
            SimilarGame(
                game_id=games[0].id,
                similar_game_id=other.id,
                score=0.9 - rank / 100,
                rank=rank,
                method="hybrid",
                components={"metadata": 0.5},
                reason="Та же студия",
            )
        )
    db.commit()
    return games


class TestTheCatalogue:
    def test_listing_thirty_games_is_a_constant_number_of_queries(
        self, app_client, engine, catalogue
    ):
        with counted(engine) as statements:
            payload = app_client.get("/api/v1/games", params={"limit": GAMES}).json()
        assert len(payload["items"]) == GAMES
        assert len(statements) <= 10, (
            f"{len(statements)} statements for {GAMES} games -- "
            f"that is an N+1:\n" + "\n".join(statements[:12])
        )

    def test_the_count_does_not_grow_with_the_page_size(self, app_client, engine, catalogue):
        """The definitive N+1 test: same work, ten times the rows."""
        with counted(engine) as small:
            app_client.get("/api/v1/games", params={"limit": 3})
        with counted(engine) as large:
            app_client.get("/api/v1/games", params={"limit": 30})
        assert len(large) == len(small), (
            f"{len(small)} statements for 3 games, {len(large)} for 30"
        )

    def test_the_html_catalogue_costs_the_same_as_the_json_one(
        self, app_client, engine, catalogue
    ):
        """Both surfaces use the same presenters; the query profile should match."""
        with counted(engine) as api:
            app_client.get("/api/v1/games", params={"limit": 24})
        with counted(engine) as html:
            app_client.get("/games")
        # HTML additionally asks for facet counts and, when empty, the index total.
        assert len(html) <= len(api) + 3


class TestTheGamePage:
    def test_a_game_detail_is_a_bounded_number_of_queries(
        self, app_client, engine, catalogue
    ):
        with counted(engine) as statements:
            response = app_client.get("/api/v1/games/test-game-00")
        assert response.status_code == 200
        assert len(statements) <= 15, "\n".join(statements)

    def test_the_similar_rail_does_not_cost_a_query_per_neighbour(
        self, app_client, engine, db, catalogue
    ):
        """The question is not "how many" but "does it grow". Eight neighbours must cost
        exactly what two cost."""
        with counted(engine) as many:
            payload = app_client.get("/api/v1/games/test-game-00/similar").json()
        assert len(payload) == 8

        db.execute(
            sa.delete(SimilarGame).where(
                SimilarGame.game_id == catalogue[0].id, SimilarGame.rank > 2
            )
        )
        db.commit()
        with counted(engine) as few:
            assert len(app_client.get("/api/v1/games/test-game-00/similar").json()) == 2

        assert len(many) == len(few), (
            f"{len(few)} statements for 2 neighbours, {len(many)} for 8 -- "
            "the rail is loading each neighbour on its own"
        )

    def test_the_html_game_page_is_bounded_too(self, app_client, engine, catalogue):
        with counted(engine) as statements:
            response = app_client.get("/games/test-game-00")
        assert response.status_code == 200
        assert len(statements) <= 18, "\n".join(statements)


class TestTheHomePage:
    def test_five_rails_are_a_fixed_number_of_queries(self, app_client, engine, catalogue):
        """Four rails plus a spotlight; each is one query and its eager loads."""
        with counted(engine) as statements:
            assert app_client.get("/").status_code == 200
        assert len(statements) <= 26, "\n".join(statements)

    def test_the_home_page_does_not_get_dearer_as_the_catalogue_grows(
        self, app_client, engine, db, catalogue
    ):
        """The question that matters. Thirty games or fifty, the same statements run."""
        with counted(engine) as before:
            app_client.get("/")

        for index in range(GAMES, GAMES + 20):
            title = f"Extra Game {index:02d}"
            db.add(
                Game(
                    mc_slug=f"extra-game-{index:02d}",
                    mc_url=f"https://www.metacritic.com/game/extra-game-{index:02d}/",
                    title=title,
                    title_norm=normalize_title(title),
                    best_metascore=88,
                    best_metascore_count=40,
                    best_userscore=5.2,
                    best_userscore_count=900,
                    release_date=dt.date(2026, 1, 1),
                    detail_synced_at=dt.datetime.now(dt.UTC),
                )
            )
        db.commit()

        with counted(engine) as after:
            app_client.get("/")
        # Not "exactly equal": a rail that becomes empty skips its eager loads, so the
        # count moves by one or two either way. What must not happen is growth with the
        # row count -- twenty more games must not mean twenty more statements.
        assert len(after) <= len(before) + 2, (
            f"{len(before)} statements for {GAMES} games, {len(after)} for {GAMES + 20}"
        )


class TestTheCounterItself:
    def test_it_would_actually_catch_an_n_plus_one(self, db, engine, catalogue):
        """A test that cannot fail is not a test. This is the shape it is looking for."""
        with counted(engine) as statements:
            games = list(db.execute(sa.select(Game).limit(GAMES)).scalars())
            for game in games:
                _ = [gp.platform.name for gp in game.platforms]
        assert len(statements) > GAMES, "lazy loading should produce one query per game"
