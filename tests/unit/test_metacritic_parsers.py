"""Parser tests against the REAL captured responses.

Every assertion here is a fact about data we actually received, not about data we hope
to receive. When Metacritic changes, these fail first.
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from app.domain.enums import ReviewKind
from app.domain.errors import SchemaDriftError
from app.parsers.metacritic import (
    image_url,
    parse_game_detail,
    parse_listing,
    parse_reviews,
    parse_score_stats,
    parse_sitemap_index,
    parse_sitemap_shard,
)

BASE = "https://www.metacritic.com"


class TestListing:
    def test_new_releases_fixture(self, finder_new_releases):
        listing = parse_listing(finder_new_releases, offset=0, limit=20)
        assert listing.total_results == 18_524       # games WITH a Metascore (ADR-015)
        assert len(listing.items) == 20              # the assignment's "first 20"
        first = listing.items[0]
        assert first.slug == "onimusha-way-of-the-sword"
        assert first.metascore == 85
        assert first.release_date == dt.date(2026, 9, 4)
        assert first.cover_path == "/provider/7/2/7-1781631535.jpg"

    def test_listing_userscore_can_be_absent(self, finder_new_releases):
        listing = parse_listing(finder_new_releases, offset=0, limit=20)
        # Fresh releases have no player score yet; that must arrive as None, not 0.
        assert listing.items[0].userscore is None

    def test_browse_fixture(self, finder_browse):
        listing = parse_listing(finder_browse, offset=24, limit=24)
        assert listing.offset == 24
        assert listing.total_results > 0

    def test_description_is_not_taken_from_the_listing(self, finder_new_releases):
        """Research found listing descriptions belonging to other games entirely."""
        listing = parse_listing(finder_new_releases, offset=0, limit=20)
        assert not hasattr(listing.items[0], "description")


class TestGameDetail:
    def test_elden_ring(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert game.slug == "elden-ring"
        assert game.title == "Elden Ring"
        assert game.mc_title_id == 1_300_501_979
        assert game.mc_url == "https://www.metacritic.com/game/elden-ring/"
        assert game.release_date == dt.date(2022, 2, 25)
        assert game.esrb_rating == "M"
        assert game.must_play is True
        assert game.description and "Hidetaka Miyazaki" in game.description

    def test_developer_and_publisher_are_separated(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert [c.name for c in game.developers] == ["From Software"]
        assert "Bandai Namco Games" in [c.name for c in game.publishers]
        # Capcom-style case: the same company can hold both roles.
        assert {c.role for c in game.companies} == {"developer", "publisher"}

    def test_cover_is_the_unsigned_original(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert game.cover_url == (
            "https://www.metacritic.com/a/img/catalog/provider/6/12/6-1-824956-52.jpg"
        )
        assert "resize" not in game.cover_url

    def test_video_is_captured_but_is_not_necessarily_a_trailer(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert game.video_url.startswith("https://cdn.jwplayer.com/")
        # The real title of this "video" is a guide, which is exactly why the UI must not
        # label the slot "Trailer" (C-27).
        assert "Scadutree" in game.video_title

    def test_per_platform_scores(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        by_slug = {p.slug: p for p in game.platforms}
        assert len(by_slug) == 5
        assert by_slug["pc"].metascore == 94
        assert by_slug["pc"].metascore_count == 63
        # Xbox One is listed with NO critic score -- must be None, never 0.
        assert by_slug["xbox-one"].metascore is None
        assert by_slug["xbox-one"].metascore_count == 0

    def test_exactly_one_lead_platform(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert sum(1 for p in game.platforms if p.is_lead) == 1

    def test_two_lead_platforms_from_the_source_become_one(self, composer_elden_ring):
        """Real data, found in production, not an invented edge case.

        A game came back from Metacritic with `isLeadPlatform` true on two entries -- a
        re-release carrying the flag alongside the original. The database enforces one
        (`uq_game_platforms_one_lead`), so the insert failed with a UniqueViolation and
        the game was never ingested at all: a permanent dead job, one game missing from
        the catalogue, and nothing on the page to say so.

        The parser normalised the empty case and not this one. Both are the source being
        inconsistent, and both belong here rather than at the database.
        """
        payload = copy.deepcopy(composer_elden_ring)
        platforms = payload["components"][0]["data"]["item"]["platforms"]
        assert len(platforms) >= 2, "fixture needs at least two platforms"
        for entry in platforms[:2]:
            entry["isLeadPlatform"] = True

        game = parse_game_detail(payload, base_url=BASE)
        leads = [p for p in game.platforms if p.is_lead]
        assert len(leads) == 1, [p.slug for p in leads]
        # The source's own order decides, so the choice is deterministic.
        assert leads[0].slug == game.platforms[0].slug

    def test_userscore_is_absent_from_the_composer(self, composer_elden_ring):
        """`?platform=` is ignored and the composer carries no per-platform user score."""
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert all(p.userscore is None for p in game.platforms)

    def test_taxonomy(self, composer_elden_ring):
        game = parse_game_detail(composer_elden_ring, base_url=BASE)
        assert game.franchise and game.franchise.slug == "elden-ring"
        assert game.mc_family_id == 1_200_497_664
        assert [g.slug for g in game.genres] == ["action-rpg"]


class TestFingerprint:
    def test_is_stable_across_parses(self, composer_elden_ring):
        a = parse_game_detail(composer_elden_ring, base_url=BASE)
        b = parse_game_detail(copy.deepcopy(composer_elden_ring), base_url=BASE)
        assert a.source_fingerprint == b.source_fingerprint

    def test_ignores_key_order(self, composer_elden_ring):
        shuffled = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in shuffled["components"] if c["meta"]["componentName"] == "product"
        )
        item = product["data"]["item"]
        product["data"]["item"] = dict(reversed(list(item.items())))
        assert (
            parse_game_detail(shuffled, base_url=BASE).source_fingerprint
            == parse_game_detail(composer_elden_ring, base_url=BASE).source_fingerprint
        )

    def test_changes_when_a_score_moves(self, composer_elden_ring):
        changed = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in changed["components"] if c["meta"]["componentName"] == "product"
        )
        product["data"]["item"]["platforms"][1]["criticScoreSummary"]["score"] = 42
        assert (
            parse_game_detail(changed, base_url=BASE).source_fingerprint
            != parse_game_detail(composer_elden_ring, base_url=BASE).source_fingerprint
        )


class TestSchemaDrift:
    def test_missing_required_field_raises(self, composer_elden_ring):
        broken = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in broken["components"] if c["meta"]["componentName"] == "product"
        )
        del product["data"]["item"]["title"]
        with pytest.raises(SchemaDriftError):
            parse_game_detail(broken, base_url=BASE)

    def test_missing_component_raises(self, composer_elden_ring):
        broken = copy.deepcopy(composer_elden_ring)
        broken["components"] = [
            c for c in broken["components"] if c["meta"]["componentName"] != "product"
        ]
        with pytest.raises(SchemaDriftError, match="product"):
            parse_game_detail(broken, base_url=BASE)

    def test_unknown_field_is_tolerated(self, composer_elden_ring):
        extended = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in extended["components"] if c["meta"]["componentName"] == "product"
        )
        product["data"]["item"]["someBrandNewField"] = {"nested": [1, 2, 3]}
        assert parse_game_detail(extended, base_url=BASE).slug == "elden-ring"

    def test_null_optionals_are_tolerated(self, composer_elden_ring):
        sparse = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in sparse["components"] if c["meta"]["componentName"] == "product"
        )
        item = product["data"]["item"]
        item["video"] = None
        item["rating"] = None
        item["description"] = None
        game = parse_game_detail(sparse, base_url=BASE)
        assert game.video_url is None and game.esrb_rating is None and game.description is None


class TestReviews:
    def test_critic_reviews(self, reviews_critic):
        page = parse_reviews(reviews_critic, kind=ReviewKind.CRITIC, offset=0)
        assert page.total_results == 93
        first = page.items[0]
        assert first.publication_name == "Areajugones"
        assert first.score == 100
        assert first.score_max == 100
        assert first.published_on == dt.date(2022, 2, 23)
        assert first.source_review_id is None      # critics have no id at the source

    def test_critic_dedupe_key_is_stable_and_discriminating(self, reviews_critic):
        a = parse_reviews(reviews_critic, kind=ReviewKind.CRITIC, offset=0).items
        b = parse_reviews(reviews_critic, kind=ReviewKind.CRITIC, offset=0).items
        assert [r.dedupe_key for r in a] == [r.dedupe_key for r in b]
        assert len({r.dedupe_key for r in a}) == len(a)

    def test_critic_dedupe_key_survives_cosmetic_edits(self, reviews_critic):
        import copy as _copy

        edited = _copy.deepcopy(reviews_critic)
        edited["data"]["items"][0]["quote"] = (
            "  " + edited["data"]["items"][0]["quote"].upper() + "\r\n"
        )
        original = parse_reviews(reviews_critic, kind=ReviewKind.CRITIC, offset=0).items[0]
        changed = parse_reviews(edited, kind=ReviewKind.CRITIC, offset=0).items[0]
        assert original.dedupe_key == changed.dedupe_key

    def test_user_reviews(self, reviews_user):
        page = parse_reviews(reviews_user, kind=ReviewKind.USER, offset=0)
        assert page.total_results == 6593
        first = page.items[0]
        assert first.source_review_id == "118f1971-00ad-4ad3-aae4-ca2740f116f6"
        assert first.dedupe_key == first.source_review_id
        assert first.score == 10
        assert first.score_max == 10
        assert first.score_normalized == 100
        assert first.source_version == 1651631173000
        assert first.is_spoiler is False

    def test_sentiment_bucket_is_recorded_as_a_stratum(self, reviews_user):
        page = parse_reviews(
            reviews_user, kind=ReviewKind.USER, offset=0, sentiment_bucket="positive"
        )
        # Kept for sampling only. Release 0 proved these buckets are built on the SCORE,
        # not the text, so they are never ground truth (C-07).
        assert page.items[0].sentiment_bucket == "positive"


class TestStats:
    def test_user_stats(self, harvest_loader):
        harvest = harvest_loader("elden-ring")
        raw = {"data": {"item": harvest["per_platform"]["playstation-5"]["user_stats"]}}
        stats = parse_score_stats(raw, scale_max=10)
        assert stats.score == 8.4
        assert stats.review_count == 24_375
        assert stats.sentiment == "Generally favorable"

    def test_critic_stats_can_be_all_positive(self, harvest_loader):
        """Elden Ring: 86 positive, 0 neutral, 0 negative — the case that broke 3+3."""
        harvest = harvest_loader("elden-ring")
        raw = {"data": {"item": harvest["per_platform"]["playstation-5"]["critic_stats"]}}
        stats = parse_score_stats(raw, scale_max=100)
        assert (stats.positive, stats.neutral, stats.negative) == (86, 0, 0)

    def test_userscore_zero_is_parsed_as_zero_and_judged_later(self, harvest_loader):
        """The parser must not editorialise; the presentation rule lives in the domain."""
        harvest = harvest_loader("nba-2k27")
        raw = {"data": {"item": harvest["per_platform"]["playstation-5"]["user_stats"]}}
        stats = parse_score_stats(raw, scale_max=10)
        assert stats.score == 0
        assert stats.review_count == 2


class TestSitemap:
    def test_index_lists_shards(self, sitemap_index_xml):
        locs = parse_sitemap_index(sitemap_index_xml)
        assert len(locs) > 200
        assert all(loc.startswith("http") for loc in locs)

    def test_shard_yields_slugs(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.metacritic.com/game/elden-ring/</loc></url>
          <url><loc>https://www.metacritic.com/game/hades-ii</loc></url>
          <url><loc>https://www.metacritic.com/movie/dune/</loc></url>
        </urlset>"""
        assert parse_sitemap_shard(xml) == ["elden-ring", "hades-ii"]


def test_image_url_helper():
    assert image_url("/provider/6/12/x.jpg", BASE) == (
        "https://www.metacritic.com/a/img/catalog/provider/6/12/x.jpg"
    )
    assert image_url(None, BASE) is None
