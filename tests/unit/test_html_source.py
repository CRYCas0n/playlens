"""HTML fallback source.

NOTE ON FIXTURE PROVENANCE: unlike every other parser test in this suite, these run
against a SYNTHESISED page rather than a captured one — the research phase saved JSON
responses, not HTML. The structure reproduced here is the structure the research
documented (a single ``application/ld+json`` VideoGame block plus ``data-testid``
anchors). Verifying the fallback against a real capture is recorded as a validation task
in OPEN_QUESTIONS (OQ-V2 family), and this limitation is stated in FINAL_AUDIT rather
than glossed over.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.http.transport import HttpTransport, TransportConfig
from app.adapters.metacritic.source_html import HtmlSource
from app.domain.enums import ReviewKind
from app.domain.errors import PermanentError, SchemaDriftError

GAME_HTML = """<!doctype html>
<html><head>
<meta property="og:title" content="Elden Ring">
<meta property="og:description" content="A fantasy action-RPG adventure.">
<meta property="og:image" content="https://www.metacritic.com/a/img/catalog/x.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"VideoGame","name":"Elden Ring",
 "datePublished":"2022-02-25",
 "description":"A New World Created By Hidetaka Miyazaki And George R. R. Martin",
 "image":"https://www.metacritic.com/a/img/catalog/provider/6/12/6-1-824956-52.jpg"}
</script>
</head><body>
<div data-testid="global-score-header"><span>96</span></div>
<div data-testid="global-score-review-count">Based on 86 Critic Reviews</div>
<div class="shrink-0 grow-0 p-4 rounded-lg">tailwind noise that must never be selected</div>
</body></html>"""

LISTING_HTML = """<!doctype html>
<html><body>
<a data-testid="product-card" href="/game/elden-ring/" aria-label="Elden Ring"></a>
<a data-testid="product-card" href="/game/baldurs-gate-3/" aria-label="Baldur's Gate 3"></a>
<a data-testid="product-card" href="/game/elden-ring/" aria-label="Elden Ring"></a>
<a href="/movie/dune/">not a game</a>
</body></html>"""


def html_source(body: str) -> HtmlSource:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=body)))
    transport = HttpTransport(
        TransportConfig(user_agent="t"), client=client, sleep=lambda _s: None
    )
    return HtmlSource(transport, site_base_url="https://www.metacritic.com")


class TestDetail:
    def test_prefers_json_ld(self):
        game = html_source(GAME_HTML).game_detail("elden-ring")
        assert game.title == "Elden Ring"
        assert game.release_date.isoformat() == "2022-02-25"
        assert "Miyazaki" in game.description
        assert game.cover_url.endswith("6-1-824956-52.jpg")

    def test_reads_the_header_score_from_a_testid_anchor(self):
        game = html_source(GAME_HTML).game_detail("elden-ring")
        assert len(game.platforms) == 1
        assert game.platforms[0].metascore == 96
        assert game.platforms[0].metascore_count == 86
        assert game.platforms[0].is_lead is True

    def test_fingerprint_is_populated(self):
        assert html_source(GAME_HTML).game_detail("elden-ring").source_fingerprint

    def test_missing_everything_raises_drift(self):
        with pytest.raises(SchemaDriftError):
            html_source("<html><body>nothing here</body></html>").game_detail("x")


class TestListing:
    def test_extracts_slugs_and_deduplicates(self):
        listing = html_source(LISTING_HTML).browse(offset=0, limit=10)
        assert [i.slug for i in listing.items] == ["elden-ring", "baldurs-gate-3"]

    def test_ignores_non_game_links(self):
        listing = html_source(LISTING_HTML).new_releases(limit=10)
        assert all("/movie/" not in i.slug for i in listing.items)

    def test_missing_cards_raise_drift(self):
        with pytest.raises(SchemaDriftError, match="product-card"):
            html_source("<html><body></body></html>").browse(offset=0, limit=10)


class TestHonestLimitations:
    """The fallback says what it cannot do instead of returning something plausible."""

    def test_reviews_are_refused_with_an_explanation(self):
        with pytest.raises(PermanentError, match="not supported by the HTML fallback"):
            html_source(GAME_HTML).reviews_page(
                "g", "pc", kind=ReviewKind.CRITIC, offset=0, limit=10, sentiment="all"
            )

    def test_per_platform_stats_are_refused(self):
        with pytest.raises(PermanentError, match="ignores \\?platform="):
            html_source(GAME_HTML).score_stats("g", "pc", kind=ReviewKind.USER)
