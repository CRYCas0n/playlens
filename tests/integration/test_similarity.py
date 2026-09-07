"""Similarity: explainable, thresholded, and honest when it has nothing to say."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.models import (
    Company,
    Franchise,
    Game,
    GameCompany,
    GameGenre,
    GamePlatform,
    Genre,
    Platform,
    SimilarGame,
)
from app.db.uow import UnitOfWork
from app.normalizers.text import normalize_title, slugify
from app.services.similarity_service import SimilarityService

pytestmark = pytest.mark.integration


def settings(**overrides) -> Settings:
    base = {"admin_token": "t" * 32, "app_env": "test", "similarity_min_score": 0.34}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def make_game(db):
    def _make(
        slug: str,
        *,
        title: str | None = None,
        genres: tuple[str, ...] = (),
        developer: str | None = None,
        franchise: str | None = None,
        family_id: int | None = None,
        metascore: int | None = 80,
        year: int = 2024,
        platforms: tuple[str, ...] = ("pc",),
        description: str = "",
        user_reviews: int = 100,
    ) -> Game:
        franchise_id = None
        if franchise:
            row = db.execute(
                sa.select(Franchise).where(Franchise.slug == slugify(franchise))
            ).scalar_one_or_none()
            if row is None:
                row = Franchise(slug=slugify(franchise), name=franchise)
                db.add(row)
                db.flush()
            franchise_id = row.id

        resolved = title or slug.replace("-", " ").title()
        game = Game(
            mc_slug=slug,
            mc_url=f"https://x/game/{slug}/",
            title=resolved,
            title_norm=normalize_title(resolved),
            description=description,
            best_metascore=metascore,
            best_metascore_count=40 if metascore else 0,
            premiere_year=year,
            release_date=dt.date(year, 6, 1),
            franchise_id=franchise_id,
            mc_family_id=family_id,
            user_reviews_total=user_reviews,
        )
        db.add(game)
        db.flush()

        for name in genres:
            genre = db.execute(
                sa.select(Genre).where(Genre.slug == slugify(name))
            ).scalar_one_or_none()
            if genre is None:
                genre = Genre(slug=slugify(name), name=name)
                db.add(genre)
                db.flush()
            db.add(GameGenre(game_id=game.id, genre_id=genre.id))

        if developer:
            company = db.execute(
                sa.select(Company).where(Company.slug == slugify(developer))
            ).scalar_one_or_none()
            if company is None:
                company = Company(slug=slugify(developer), name=developer)
                db.add(company)
                db.flush()
            db.add(GameCompany(game_id=game.id, company_id=company.id, role="developer"))

        for index, code in enumerate(platforms):
            platform = db.execute(sa.select(Platform).where(Platform.slug == code)).scalar_one()
            db.add(
                GamePlatform(
                    game_id=game.id,
                    platform_id=platform.id,
                    is_lead=index == 0,
                    metascore_raw=metascore,
                    metascore_count=40 if metascore else 0,
                )
            )
        db.commit()
        return game

    return _make


class TestScoring:
    def test_same_franchise_scores_high(self, session_factory, db, make_game):
        a = make_game(
            "elden-ring", genres=("Action RPG",), developer="From Software",
            franchise="Elden Ring", metascore=96,
        )
        make_game(
            "elden-ring-nightreign", genres=("Action RPG",), developer="From Software",
            franchise="Elden Ring", metascore=90,
        )
        service = SimilarityService(settings())
        with UnitOfWork(session_factory) as uow:
            published = service.recompute(uow, a.id)
        assert published == 1
        row = db.execute(sa.select(SimilarGame)).scalars().one()
        assert row.reason == "Same series"
        assert row.score > 0.5

    def test_unrelated_genres_score_low(self, session_factory, db, make_game):
        a = make_game(
            "dark-fantasy-rpg", genres=("Action RPG",), developer="Studio A",
            metascore=90, description="A punishing action role playing game",
        )
        make_game(
            "farm-puzzle", genres=("Puzzle",), developer="Studio B", metascore=55,
            year=2012, description="A relaxed farming puzzle game",
        )
        service = SimilarityService(settings())
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
        assert db.execute(sa.select(SimilarGame)).scalars().all() == []

    def test_same_studio_is_named(self, session_factory, db, make_game):
        a = make_game("game-one", genres=("Shooter",), developer="Shared Studio")
        make_game("game-two", genres=("Strategy",), developer="Shared Studio")
        service = SimilarityService(settings(similarity_min_score=0.2))
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
        row = db.execute(sa.select(SimilarGame)).scalars().one()
        assert row.reason == "Same studio"

    def test_a_dlc_is_penalised_below_a_genuine_peer(self, session_factory, db, make_game):
        base = make_game(
            "main-game", genres=("Action RPG",), developer="Studio",
            family_id=999, metascore=95,
        )
        make_game(
            "main-game-dlc", genres=("Action RPG",), developer="Studio",
            family_id=999, metascore=94,
        )
        make_game("peer-game", genres=("Action RPG",), developer="Studio", metascore=93)
        service = SimilarityService(settings(similarity_min_score=0.1))
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, base.id)
        rows = db.execute(sa.select(SimilarGame).order_by(SimilarGame.rank)).scalars().all()
        slugs = [db.get(Game, r.similar_game_id).mc_slug for r in rows]
        assert slugs[0] == "peer-game"

    def test_scoring_is_deterministic(self, session_factory, db, make_game):
        a = make_game("a-game", genres=("Action RPG",), developer="Studio")
        make_game("b-game", genres=("Action RPG",), developer="Studio")
        service = SimilarityService(settings(similarity_min_score=0.1))

        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
        first = [
            (r.similar_game_id, r.score)
            for r in db.execute(sa.select(SimilarGame)).scalars()
        ]
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
        db.expire_all()
        second = [
            (r.similar_game_id, r.score)
            for r in db.execute(sa.select(SimilarGame)).scalars()
        ]
        assert first == second


class TestHonesty:
    def test_a_game_with_no_close_match_publishes_nothing(self, session_factory, db, make_game):
        """design/EDGE_CASES.md case 14: an honest empty state, never a padded rail."""
        a = make_game("lonely-game", genres=("Rhythm",), developer="Solo Dev")
        make_game("unrelated", genres=("Sports",), developer="Other", metascore=40, year=2005)
        service = SimilarityService(settings())
        with UnitOfWork(session_factory) as uow:
            assert service.recompute(uow, a.id) == 0

    def test_a_game_never_appears_in_its_own_list(self, session_factory, db, make_game):
        a = make_game("self-game", genres=("Action RPG",), developer="Studio")
        make_game("other", genres=("Action RPG",), developer="Studio")
        service = SimilarityService(settings(similarity_min_score=0.05))
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
        ids = [r.similar_game_id for r in db.execute(sa.select(SimilarGame)).scalars()]
        assert a.id not in ids

    def test_components_are_stored_for_inspection(self, session_factory, db, make_game):
        a = make_game("insp-a", genres=("Action RPG",), developer="Studio")
        make_game("insp-b", genres=("Action RPG",), developer="Studio")
        service = SimilarityService(settings(similarity_min_score=0.1))
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
        row = db.execute(sa.select(SimilarGame)).scalars().first()
        assert set(row.components) >= {"metadata", "lexical", "genre", "company", "penalty"}


class TestReasons:
    def test_no_reason_is_produced_when_nothing_is_strong(self):
        from app.services.similarity_service import Components

        service = SimilarityService(settings())
        weak = Components(metadata=0.2, aspect=None, lexical=0.1, genre=0.3, company=0.0)
        assert service.reason_for(weak) is None

    def test_aspect_profile_produces_a_reason(self):
        from app.services.similarity_service import Components

        service = SimilarityService(settings())
        strong = Components(metadata=0.4, aspect=0.8, lexical=0.3)
        assert service.reason_for(strong) == "Praised for similar things"


class TestColdStart:
    def test_works_without_any_aspect_profile(self, session_factory, db, make_game):
        """Before any summary exists the engine rides on metadata + lexical (ADR-011)."""
        a = make_game("cold-a", genres=("Action RPG",), developer="Studio")
        make_game("cold-b", genres=("Action RPG",), developer="Studio")
        service = SimilarityService(settings(similarity_min_score=0.1))
        with UnitOfWork(session_factory) as uow:
            assert service.recompute(uow, a.id) == 1
        row = db.execute(sa.select(SimilarGame)).scalars().one()
        assert row.method == "metadata"
        assert row.components["aspect"] is None


def test_recompute_is_idempotent(session_factory, db, make_game):
    a = make_game("idem-a", genres=("Action RPG",), developer="Studio")
    make_game("idem-b", genres=("Action RPG",), developer="Studio")
    service = SimilarityService(settings(similarity_min_score=0.1))
    for _ in range(3):
        with UnitOfWork(session_factory) as uow:
            service.recompute(uow, a.id)
    count = db.execute(sa.select(sa.func.count()).select_from(SimilarGame)).scalar_one()
    assert count == 1
