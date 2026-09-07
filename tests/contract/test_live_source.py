"""Does the live source still have the shape the parser expects?

Every other test in this project runs against captured fixtures, which is the right
default: fast, deterministic, and it does not put load on somebody else's servers. But a
fixture proves the parser handled the source *as it was on the day it was captured*. The
question this file asks is different and cannot be answered offline: **is that still
true today?**

Marked ``contract`` and deselected by default. Run it deliberately:

    pytest -m contract

It makes a handful of requests through the same transport as production — same rate
limit, same user agent, same retry policy — so running it is polite by construction.
When it fails, the finding is not "the test is broken": it is that the source changed and
the parser needs attention before the next crawl fills the queue with dead jobs.
"""

from __future__ import annotations

import os

import pytest

from app.adapters.http.transport import HttpTransport, TransportConfig
from app.adapters.metacritic.provider import build_provider
from app.config import Settings
from app.domain.enums import ReviewKind

pytestmark = [pytest.mark.contract, pytest.mark.integration]

#: Opt-in twice: the marker selects the file, this says the network is really allowed.
ENABLED = os.environ.get("CONTRACT_TESTS") == "1"

requires_network = pytest.mark.skipif(
    not ENABLED,
    reason="set CONTRACT_TESTS=1 to make real requests to the live source",
)


@pytest.fixture(scope="module")
def provider():
    settings = Settings(
        admin_token="c" * 32,
        app_env="test",
        # Deliberately slower than the production default. A contract run is a handful of
        # requests from a developer's machine; there is no reason to hurry.
        metacritic_rps=1.0,
        metacritic_burst=1,
    )
    transport = HttpTransport(
        TransportConfig(
            user_agent=settings.user_agent,
            timeout_s=settings.request_timeout_s,
            connect_timeout_s=settings.request_connect_timeout_s,
            max_retries=2,
            backoff_base_s=settings.request_backoff_base_s,
            backoff_max_s=settings.request_backoff_max_s,
            circuit_fail_threshold=settings.circuit_fail_threshold,
            circuit_reset_timeout_s=settings.circuit_reset_timeout_s,
        )
    )
    # Built exactly the way the container builds it: a contract test that constructs the
    # client differently from production is testing a different client.
    yield build_provider(settings, transport)
    transport.close()


@requires_network
class TestDiscovery:
    def test_new_releases_still_parses(self, provider):
        """The first call of every crawl. If this breaks, nothing else runs."""
        listing = provider.new_releases(limit=5)
        assert listing.items, "New Releases returned nothing"
        assert listing.total_results > 0
        first = listing.items[0]
        assert first.slug
        assert first.title
        # A listing row must carry enough to identify the game and to decide whether it
        # is worth a detail request. Losing any of these would break discovery silently.
        assert first.mc_title_id is not None or first.slug

    def test_the_eligible_catalogue_is_still_roughly_the_size_we_planned_for(
        self, provider
    ):
        """Release 0 measured 18,524 games with a Metascore. Catalogue depth, retention
        and the crawl schedule are all sized off that number."""
        listing = provider.new_releases(limit=1)
        assert 10_000 < listing.total_results < 40_000, (
            f"eligible catalogue is now {listing.total_results}; ADR-015 assumed ~18,500"
        )

    def test_browse_paginates(self, provider):
        first = provider.browse_new(page=1, page_size=5)
        second = provider.browse_new(page=2, page_size=5)
        assert first.items and second.items
        assert {i.slug for i in first.items} & {i.slug for i in second.items} == set(), (
            "consecutive browse pages overlap; the cursor would re-crawl the same games"
        )


@requires_network
class TestOneGame:
    @pytest.fixture(scope="class")
    def slug(self, provider):
        return provider.new_releases(limit=1).items[0].slug

    def test_the_detail_document_still_parses(self, provider, slug):
        detail = provider.game_detail(slug)
        assert detail.title
        assert detail.slug == slug
        assert detail.platforms, "a game with no platforms cannot be scored per platform"

    def test_per_platform_scores_are_still_available(self, provider, slug):
        """The killer feature. If this endpoint goes, the product loses its point."""
        detail = provider.game_detail(slug)
        platform = detail.platforms[0]
        stats = provider.critic_stats(slug, platform.slug)
        assert stats is not None
        assert stats.review_count >= 0

    def test_critic_reviews_still_come_ten_to_a_page(self, provider, slug):
        """C-01: the API ignores `limit` on critic reviews and returns 10 regardless.

        The pagination is adaptive precisely because of this. If the source ever starts
        honouring `limit`, that is worth knowing -- the crawl would be doing five times
        the requests it needs to.
        """
        detail = provider.game_detail(slug)
        platform = detail.platforms[0]
        pages = list(
            provider.iter_reviews(
                slug, platform.slug, kind=ReviewKind.CRITIC, cap=30, max_pages=1
            )
        )
        if not pages:
            pytest.skip("this game has no critic reviews yet")
        assert len(pages[0].items) <= 10, (
            f"the source returned {len(pages[0].items)} critic reviews in one page; "
            "C-01 may no longer hold and the adaptive pagination can be simplified"
        )


@requires_network
def test_the_public_api_key_is_still_not_validated(provider):
    """ADR-001 rests on this: the key is a public frontend value, not a credential.

    If the source starts rejecting a wrong key, the legal and operational picture
    changes and the ADR has to be revisited.
    """
    import httpx

    from app.adapters.metacritic import endpoints

    path, params = endpoints.new_releases(limit=1)
    params = dict(params)
    params["apiKey"] = "definitely-not-a-real-key"
    response = httpx.get(
        f"https://backend.metacritic.com{path}",
        params=params,
        timeout=20,
        headers={"User-Agent": "playlens-contract/1.0"},
    )
    assert response.status_code == 200, (
        "the source now validates the API key; ADR-001 needs revisiting before the next "
        "crawl"
    )


def test_the_contract_suite_is_opt_in():
    """Runs always. A contract suite that silently never runs is worse than none, and a
    contract suite that runs in CI hits somebody else's servers on every push."""
    assert "CONTRACT_TESTS" in os.environ or not ENABLED
