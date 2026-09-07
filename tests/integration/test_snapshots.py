"""Snapshot immutability, built on the real Release 0 corpora."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.models import ReviewSnapshot, SnapshotReview
from app.db.uow import UnitOfWork
from app.domain.enums import ReviewKind
from app.services.snapshot_service import SnapshotService

pytestmark = pytest.mark.integration


def settings(**overrides) -> Settings:
    base = {"admin_token": "t" * 32, "app_env": "test", "corpus_min_chars": 80}
    base.update(overrides)
    return Settings(**base)


class TestBuild:
    def test_builds_from_real_reviews(self, session_factory, db, load_harvest):
        game_id, gp_id = load_harvest("cyberpunk-2077", "playstation-4")
        service = SnapshotService(settings())

        with UnitOfWork(session_factory) as uow:
            result = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )

        assert result.created is True
        assert result.review_count > 0
        assert result.candidate_count >= result.review_count
        stored = db.execute(sa.select(sa.func.count()).select_from(SnapshotReview)).scalar_one()
        assert stored == result.review_count

    def test_evidence_refs_are_sequential_and_prefixed(self, session_factory, db, load_harvest):
        game_id, gp_id = load_harvest("elden-ring", "playstation-5")
        service = SnapshotService(settings())
        with UnitOfWork(session_factory) as uow:
            result = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.CRITIC
            )
        refs = [ref for ref, _item in result.entries]
        assert refs[0] == "C00"
        assert refs == sorted(refs)
        assert all(ref.startswith("C") for ref in refs)

    def test_rejections_are_recorded_for_provenance(self, session_factory, db, load_harvest):
        game_id, gp_id = load_harvest("the-lord-of-the-rings-gollum", "playstation-5")
        service = SnapshotService(settings())
        with UnitOfWork(session_factory) as uow:
            result = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )
        # Gollum is the documented contamination case: something must have been filtered,
        # and the counters are what the UI shows as "N of M indexed reviews".
        assert result.candidate_count > result.review_count
        assert sum(result.rejected.values()) > 0

    def test_corpus_lines_carry_dates(self, session_factory, db, load_harvest):
        game_id, gp_id = load_harvest("redfall", "xbox-series-x")
        service = SnapshotService(settings())
        with UnitOfWork(session_factory) as uow:
            result = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )
        text = result.rendered(max_review_chars=400)
        assert text
        for line in text.splitlines():
            assert " | date=" in line, "C-10: every corpus line must carry its date"


class TestImmutability:
    def test_rebuilding_the_same_corpus_reuses_the_snapshot(
        self, session_factory, db, load_harvest
    ):
        game_id, gp_id = load_harvest("skull-and-bones", "playstation-5")
        service = SnapshotService(settings())

        with UnitOfWork(session_factory) as uow:
            first = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )
        with UnitOfWork(session_factory) as uow:
            second = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )

        assert second.created is False
        assert second.snapshot_id == first.snapshot_id
        assert second.content_hash == first.content_hash
        count = db.execute(sa.select(sa.func.count()).select_from(ReviewSnapshot)).scalar_one()
        assert count == 1

    def test_references_keep_pointing_at_the_same_reviews(
        self, session_factory, db, load_harvest
    ):
        """The exact defect Release 0 hit: a rebuilt corpus silently re-numbered."""
        game_id, gp_id = load_harvest("forspoken", "playstation-5")
        service = SnapshotService(settings())

        with UnitOfWork(session_factory) as uow:
            first = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )
            before = {ref: item.review_id for ref, item in first.entries}

        with UnitOfWork(session_factory) as uow:
            second = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )
            after = {ref: item.review_id for ref, item in second.entries}

        assert before == after

    def test_new_reviews_produce_a_new_snapshot_not_a_mutated_one(
        self, session_factory, db, load_harvest
    ):
        from app.db.models import Review
        from app.normalizers.text import body_hash

        game_id, gp_id = load_harvest("crimson-moon", "pc")
        service = SnapshotService(settings())

        with UnitOfWork(session_factory) as uow:
            first = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.CRITIC
            )

        body = (
            "A late review that arrived weeks after launch and discusses the patched "
            "state of the game in enough detail to clear the corpus length filter."
        )
        db.add(
            Review(
                game_id=game_id,
                game_platform_id=gp_id,
                kind="critic",
                dedupe_key="late-arrival",
                publication_name="Late Outlet",
                publication_slug="late-outlet",
                score=77,
                score_max=100,
                score_normalized=77,
                body=body,
                body_hash=body_hash(body),
                char_count=len(body),
            )
        )
        db.commit()

        with UnitOfWork(session_factory) as uow:
            second = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.CRITIC
            )

        assert second.created is True
        assert second.snapshot_id != first.snapshot_id
        assert second.content_hash != first.content_hash
        # The original snapshot is untouched, so any summary citing it still resolves.
        surviving = db.execute(
            sa.select(sa.func.count()).select_from(SnapshotReview).where(
                SnapshotReview.snapshot_id == first.snapshot_id
            )
        ).scalar_one()
        assert surviving == first.review_count


class TestThinCorpora:
    def test_a_platform_with_almost_no_text_yields_almost_nothing(
        self, session_factory, db, load_harvest
    ):
        """Onimusha: 77 ratings but only 10 with text (Release 0 section 5.4)."""
        game_id, gp_id = load_harvest("onimusha-way-of-the-sword", "playstation-5")
        service = SnapshotService(settings())
        with UnitOfWork(session_factory) as uow:
            result = service.build(
                uow, game_id=game_id, game_platform_id=gp_id, kind=ReviewKind.USER
            )
        assert result.review_count < 20, "the threshold must be judged on TEXTS, not ratings"

    def test_a_game_with_no_reviews_produces_an_empty_snapshot(self, session_factory, db):
        from app.db.models import Game, GamePlatform, Platform

        game = Game(
            mc_slug="empty", mc_url="https://x/game/empty/", title="Empty",
            title_norm="empty",
        )
        db.add(game)
        db.flush()
        platform = db.execute(sa.select(Platform).limit(1)).scalar_one()
        gp = GamePlatform(game_id=game.id, platform_id=platform.id, is_lead=True)
        db.add(gp)
        db.commit()

        service = SnapshotService(settings())
        with UnitOfWork(session_factory) as uow:
            result = service.build(
                uow, game_id=game.id, game_platform_id=gp.id, kind=ReviewKind.USER
            )
        assert result.review_count == 0
        assert result.rendered(max_review_chars=100) == ""
