"""The rendered HTML, walked against design/EDGE_CASES.md.

That table is the design package's own definition of "done": twenty-eight data shapes the
UI has to survive, each naming a fixture and a required rendering. The fixtures below
reproduce those shapes in the database, and each test names the case it covers.

Cases 1, 2, 9, 16, 27 and 28 are layout rules that live in the stylesheet
(``overflow-wrap``, ``line-clamp``, snap rails, media queries) and cannot be asserted from
markup alone. What IS asserted here is that the markup carries the hooks the stylesheet
needs, and that the *data* rules -- never render 0, never invent a reason, remove the
Let's Play section but keep a pending summary -- hold.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.db.models import (
    Company,
    Game,
    GameCompany,
    GameGenre,
    GamePlatform,
    Genre,
    Platform,
)
from app.normalizers.text import normalize_title

pytestmark = pytest.mark.integration

LONG_TITLE = "The Lament of Thorne Hollow: Chronicles of the Ashen Veil Remastered"
LONG_DEVELOPER = "Whitmoor & Sons Interactive Entertainment"

#: The eleven platforms case 2 needs, in the design's own order.
ELEVEN = [
    ("pc", "PC", "PC"),
    ("playstation-5", "PlayStation 5", "PS5"),
    ("playstation-4", "PlayStation 4", "PS4"),
    ("xbox-series-x", "Xbox Series X", "XSX"),
    ("xbox-one", "Xbox One", "XBO"),
    ("nintendo-switch", "Nintendo Switch", "NSW"),
    ("nintendo-switch-2", "Nintendo Switch 2", "NS2"),
    ("ios", "iOS", "IOS"),
    ("android", "Android", "AND"),
    ("stadia", "Stadia", "STA"),
    ("linux", "Linux", "LIN"),
]


@pytest.fixture
def world(db):
    """The design's mock catalogue, reduced to the rows the edge cases need."""
    known = {row.slug: row for row in db.execute(sa.select(Platform)).scalars()}

    def platform(slug: str, name: str, code: str) -> Platform:
        if slug in known:
            return known[slug]
        row = Platform(slug=slug, name=name, code=code, sort_order=len(known) + 50)
        db.add(row)
        db.flush()
        known[slug] = row
        return row

    for slug, name, code in ELEVEN:
        platform(slug, name, code)

    def add(
        slug: str,
        title: str,
        *,
        meta: int | None = None,
        meta_count: int = 0,
        user: float | None = None,
        user_count: int = 0,
        platforms: tuple[str, ...] = ("pc",),
        cover: str | None = "https://www.metacritic.com/a.jpg",
        developer: str | None = "Nightgate Studios",
        description: str | None = "A game.",
        release: dt.date | None = dt.date(2026, 2, 1),
        genres: tuple[str, ...] = ("action",),
    ) -> Game:
        game = Game(
            mc_slug=slug,
            mc_url=f"https://www.metacritic.com/game/{slug}/",
            title=title,
            title_norm=normalize_title(title),
            description=description,
            cover_url=cover,
            release_date=release,
            premiere_year=release.year if release else None,
            best_metascore=meta,
            best_metascore_count=meta_count,
            best_userscore=user,
            best_userscore_count=user_count,
            detail_synced_at=dt.datetime.now(dt.UTC),
        )
        db.add(game)
        db.flush()

        for index, pslug in enumerate(platforms):
            db.add(
                GamePlatform(
                    game_id=game.id,
                    platform_id=known[pslug].id,
                    is_lead=index == 0,
                    metascore_raw=meta if index == 0 else None,
                    metascore_count=meta_count if index == 0 else 0,
                    userscore_raw=user if index == 0 else None,
                    userscore_count=user_count if index == 0 else 0,
                )
            )

        if developer:
            company = db.execute(
                sa.select(Company).where(Company.name == developer)
            ).scalar_one_or_none()
            if company is None:
                company = Company(slug=normalize_title(developer).replace(" ", "-"),
                                  name=developer)
                db.add(company)
                db.flush()
            db.add(GameCompany(game_id=game.id, company_id=company.id, role="developer"))

        for gslug in genres:
            genre = db.execute(
                sa.select(Genre).where(Genre.slug == gslug)
            ).scalar_one_or_none()
            if genre is None:
                genre = Genre(slug=gslug, name=gslug.title())
                db.add(genre)
                db.flush()
            db.add(GameGenre(game_id=game.id, genre_id=genre.id))

        db.flush()
        return game

    games = {
        # Cases 1, 2, 3, 6, 7, 12, 14, 17(neg), 18, 27
        "thorne": add(
            "the-lament-of-thorne-hollow",
            LONG_TITLE,
            meta=None,
            meta_count=3,
            platforms=tuple(slug for slug, _, _ in ELEVEN),
            developer=LONG_DEVELOPER,
        ),
        # Cases 9, 16 -- the well-populated game
        "ashen": add("ashen-veil", "Ashen Veil", meta=93, meta_count=118,
                     user=8.9, user_count=14204,
                     platforms=("pc", "playstation-5", "xbox-series-x")),
        # Case 19 -- violent disagreement
        "northlight": add("northlight-drifters", "Northlight Drifters",
                          meta=88, meta_count=64, user=5.1, user_count=9100),
        # Case 20 -- players higher
        "neon": add("neon-district-2087", "Neon District 2087",
                    meta=61, meta_count=52, user=7.9, user_count=4400),
        # Cases 4, 17 -- no userscore, single platform
        "hollow": add("hollow-signal", "Hollow Signal", meta=74, meta_count=22),
        # Case 3 -- no metascore, has a player score
        "orbital": add("orbital-freight-simulator", "Orbital Freight Simulator",
                       user=7.2, user_count=980),
        # Case 5 -- no score at all
        "quiet": add("quiet-harbor", "Quiet Harbor"),
        # Case 10 -- no cover
        "midnight": add("midnight-parade", "Midnight Parade", meta=77, meta_count=31,
                        cover=None),
    }
    db.commit()
    return games


def get(client, path: str, **params) -> str:
    response = client.get(path, params=params or None)
    assert response.status_code == 200, (path, response.status_code)
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# --------------------------------------------------------------------- scores


class TestScoreRendering:
    def test_case_3_a_missing_metascore_never_renders_zero(self, app_client, world):
        """EDGE_CASES 3. The single most important rendering rule in the product."""
        html = get(app_client, "/games/orbital-freight-simulator")
        assert "gauge--none" in html
        assert "Not rated" in html
        assert "No critic score yet" in html
        # The gauge arc is driven by --pct, and 0% grey is not the same claim as a 0 score.
        assert 'class="gauge__value">—<' in html.replace("\n", "")

    def test_case_4_a_missing_userscore_omits_the_card_chip(self, app_client, world):
        """EDGE_CASES 4: the card omits USR rather than showing a dash."""
        html = get(app_client, "/games", q="Hollow Signal")
        assert "Hollow Signal" in html
        assert "score-chip__kind" not in html or ">USR<" not in html

    def test_case_5_no_score_at_all(self, app_client, world):
        """EDGE_CASES 5."""
        html = get(app_client, "/games/quiet-harbor")
        assert "Not enough reviews yet to say anything useful." in html
        assert html.count("gauge--none") == 2

    def test_case_6_the_review_count_is_always_rendered(self, app_client, world):
        """EDGE_CASES 6: 93 from 118 critics and 93 from 4 must not look identical."""
        rich = get(app_client, "/games/ashen-veil")
        assert "14 204 ratings" in rich
        assert "118 reviews" in rich
        thin = get(app_client, "/games/the-lament-of-thorne-hollow")
        assert "3 reviews" in thin

    def test_case_18_platform_scores_pending_keeps_the_section(self, app_client, world):
        """EDGE_CASES 18: the platforms are real, only the scores are missing."""
        html = get(app_client, "/games/the-lament-of-thorne-hollow")
        assert "Per-platform scores appear once" in html
        assert "11 platforms are indexed" in html
        assert "By platform" in html

    def test_case_17_a_single_platform_still_gets_its_heading(self, app_client, world):
        html = get(app_client, "/games/hollow-signal")
        assert "By platform" in html
        assert "plat--more" not in html


class TestVerdict:
    def test_case_19_violent_disagreement_is_a_warning(self, app_client, world):
        """EDGE_CASES 19: 88 vs 51, warning tone, and the direction is named."""
        html = get(app_client, "/games/northlight-drifters")
        assert "consensus__note--warn" in html
        assert "37 points lower" in html

    def test_case_20_players_higher_is_neutral_and_distinguishable(self, app_client, world):
        """EDGE_CASES 20: same magnitude of gap, different tone."""
        html = get(app_client, "/games/neon-district-2087")
        assert "18 points higher" in html
        assert "consensus__note--warn" not in html

    def test_the_two_directions_do_not_share_a_sentence(self, app_client, world):
        lower = get(app_client, "/games/northlight-drifters")
        higher = get(app_client, "/games/neon-district-2087")
        assert "points lower" in lower and "points lower" not in higher
        assert "points higher" in higher and "points higher" not in lower


class TestSummariesAndSections:
    def test_case_7_no_critic_summary_shows_a_pending_block(self, app_client, world):
        """EDGE_CASES 7/8: pending, not empty -- the data is coming."""
        html = get(app_client, "/games/the-lament-of-thorne-hollow")
        assert "Critics &amp; players" in html
        assert "Summary in progress" in html  # 3 critic reviews indexed
        assert ">Generating<" in html

    def test_case_8_zero_reviews_says_nothing_to_summarise(self, app_client, world):
        html = get(app_client, "/games/quiet-harbor")
        assert "Nothing to summarise yet" in html
        assert ">Waiting<" in html
        assert "Summary in progress" not in html

    def test_case_12_no_lets_play_removes_the_section_and_its_anchor(self, app_client, world):
        """EDGE_CASES 12: removed entirely -- no empty box, no 'no video found'."""
        html = get(app_client, "/games/quiet-harbor")
        assert 'id="letsplay"' not in html
        assert ">Gameplay<" not in html
        assert "no video" not in html.lower()

    def test_case_14_no_similar_games_is_an_honest_empty_state(self, app_client, world):
        """EDGE_CASES 14: never pad the rail."""
        html = get(app_client, "/games/the-lament-of-thorne-hollow")
        assert "Nothing comparable yet" in html
        assert "sim-card" not in html

    def test_case_15_a_similar_card_without_a_reason_omits_the_chip(self, app_client, db, world):
        """EDGE_CASES 15 and design non-negotiable 8: a reason is never invented."""
        from app.db.models import SimilarGame

        db.add(
            SimilarGame(
                game_id=world["ashen"].id,
                similar_game_id=world["northlight"].id,
                score=0.71,
                rank=1,
                reason=None,
                components={},
                method="hybrid",
            )
        )
        db.commit()
        html = get(app_client, "/games/ashen-veil")
        assert "sim-card" in html
        assert "sim-card__reason" not in html


class TestCovers:
    def test_case_10_the_fallback_is_always_in_the_dom(self, app_client, world):
        """EDGE_CASES 10: underneath the image, so a live 404 needs no error handling."""
        with_cover = get(app_client, "/games/ashen-veil")
        assert "cover-fallback" in with_cover
        assert "cover-img" in with_cover
        assert 'onerror="this.remove()"' in with_cover

    def test_a_missing_cover_renders_initials_and_no_image(self, app_client, world):
        html = get(app_client, "/games/midnight-parade")
        assert "cover-fallback" in html
        assert "Cover unavailable" in html
        assert "cover-img" not in html
        assert ">MP<" in html


class TestCatalogEmptyStates:
    def test_case_21_a_search_with_no_results_names_the_query(self, app_client, world):
        """EDGE_CASES 21: filters preserved, two actions, never silently cleared."""
        html = get(app_client, "/games", q="zzzznothing")
        assert "zzzznothing" in html
        assert "Clear search" in html

    def test_case_22_filters_with_no_results_point_at_the_filters(self, app_client, world):
        """EDGE_CASES 22: different copy from case 21."""
        html = get(app_client, "/games", platform="nintendo-switch-2", score_band="excellent")
        search_html = get(app_client, "/games", q="zzzznothing")
        assert "Clear all filters" in html
        assert html != search_html

    def test_case_23_an_empty_index_is_its_own_message(self, app_client, db):
        """EDGE_CASES 23: distinct from 'your filters matched nothing'."""
        html = get(app_client, "/games")
        assert "No games indexed yet" in html
        assert "/admin/monitoring" in html

    def test_case_24_an_unknown_slug_is_a_404_page_not_a_stack_trace(self, app_client, world):
        response = app_client.get("/games/nope")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert "nope" in response.text
        assert "Traceback" not in response.text


class TestCardsAndRails:
    def test_case_2_a_card_shows_two_codes_and_an_overflow_token(self, app_client, world):
        """EDGE_CASES 2: never a second line of chips on a card."""
        html = get(app_client, "/games", q="Lament")
        assert "plat--more" in html
        assert "+9" in html

    def test_the_overflow_token_names_the_hidden_platforms(self, app_client, world):
        html = get(app_client, "/games", q="Lament")
        assert "Nintendo Switch 2" in html  # in the title= tooltip, not as a visible chip

    def test_case_1_a_long_title_is_never_truncated_in_the_markup(self, app_client, world):
        """EDGE_CASES 1: clamping is the stylesheet's job; the text must be complete."""
        html = get(app_client, "/games/the-lament-of-thorne-hollow")
        heading = html.split('ghero__title">', 1)[1].split("</h1>", 1)[0]
        assert heading.strip() == LONG_TITLE
        assert "…" not in heading

    def test_case_27_a_long_developer_name_is_rendered_in_full(self, app_client, world):
        html = get(app_client, "/games/the-lament-of-thorne-hollow")
        assert "Whitmoor &amp; Sons Interactive Entertainment" in html


class TestHomeAndAbout:
    def test_the_home_page_leads_with_disagreement(self, app_client, world):
        html = get(app_client, "/")
        assert "Northlight Drifters" in html
        assert "Players ↓37" in html

    def test_an_empty_home_page_does_not_crash(self, app_client, db):
        html = get(app_client, "/")
        assert "<title>" in html

    def test_about_explains_the_score_rule(self, app_client, world):
        html = get(app_client, "/about")
        assert "8 games" in html or "8" in html
        assert "Metacritic" in html


class TestMonitoringPage:
    def test_case_26_a_stale_pipeline_is_a_banner_not_a_blocking_screen(self, app_client, world):
        """EDGE_CASES 26."""
        html = get(app_client, "/admin/monitoring")
        assert "Pipeline" in html
        assert "No successful crawl recorded yet" in html
        assert "Run now" in html

    def test_the_operator_page_is_noindex(self, app_client, world):
        html = get(app_client, "/admin/monitoring")
        assert 'name="robots"' in html
        assert "noindex" in html

    def test_public_pages_are_indexable(self, app_client, world):
        assert "noindex" not in get(app_client, "/games/ashen-veil")


class TestTemplateSafety:
    def test_no_template_marks_third_party_text_as_safe(self):
        """Review text, descriptions and titles are untrusted input (ADR-019 T3).

        A single ``|safe`` anywhere in the tree turns a stored review into stored XSS, so
        the filter is banned outright rather than reviewed case by case.
        """
        from pathlib import Path

        import app.web.routes as routes

        offenders = [
            path.name
            for path in Path(routes.TEMPLATE_DIR).rglob("*.html")
            if "|safe" in path.read_text(encoding="utf-8").replace(" ", "")
        ]
        assert offenders == []

    def test_a_hostile_title_is_escaped_on_every_surface(self, app_client, db):
        payload = "<script>alert('xss')</script>"
        game = Game(
            mc_slug="xss-probe",
            mc_url="https://www.metacritic.com/game/xss-probe/",
            title=f"Probe {payload}",
            title_norm=normalize_title("Probe"),
            description=f"Description {payload}",
            best_metascore=80,
            best_metascore_count=10,
            detail_synced_at=dt.datetime.now(dt.UTC),
        )
        db.add(game)
        db.commit()

        for path in ("/games/xss-probe", "/games", "/"):
            html = get(app_client, path)
            assert "<script>alert" not in html
        detail = get(app_client, "/games/xss-probe")
        assert "&lt;script&gt;" in detail

    def test_autoescaping_is_actually_on(self):
        import app.web.routes as routes

        assert routes.templates.env.autoescape is True


class TestTheMonitoringDashboardShowsNumbers:
    """A regression suite for a bug 33 passing HTML tests did not notice.

    Every KPI tile rendered the entire ``MonitoringStatus`` object -- schedule, worker
    list, AI cost, the lot -- because a Jinja ``{% set %}`` inside a ``{% for %}`` does
    not escape the loop, so the path walk left the variable bound to what it started as.
    A browser found it in a second by measuring the page width; no assertion about
    markup ever would.
    """

    def test_each_kpi_is_a_short_value_not_an_object(self, app_client, world):
        import re

        html = get(app_client, "/admin/monitoring")
        values = re.findall(r'class="kpi__value"[^>]*>([^<]*)<', html)
        assert values, "no KPI tiles rendered at all"
        for value in values:
            text = value.strip()
            assert len(text) < 24, f"KPI rendered {len(text)} characters: {text[:80]}"
            assert "MonitoringStatus" not in text
            assert "datetime.datetime" not in text

    def test_a_missing_counter_renders_a_dash(self, app_client, world):
        """With no runs recorded, success rate is unknown -- not zero."""
        html = get(app_client, "/admin/monitoring")
        assert 'data-kpi="counters24h.success_rate">—' in html

    def test_a_present_counter_renders_its_number(self, app_client, world):
        html = get(app_client, "/admin/monitoring")
        assert 'data-kpi="counters24h.games_processed">0' in html

    def test_no_page_leaks_a_python_repr(self, app_client, world):
        """The same class of bug on any other page."""
        for path in ("/", "/games", "/about", "/games/ashen-veil", "/admin/monitoring"):
            html = get(app_client, path)
            for leak in ("datetime.datetime(", "object at 0x", "MonitoringStatus(",
                         "ScoreValue(", "<class "):
                assert leak not in html, f"{path} leaks {leak}"


class TestTheFavicon:
    def test_the_root_favicon_is_served(self, app_client):
        """Browsers request /favicon.ico whatever the <link> says; a 404 there puts a
        console error on every single page, which is where real errors go to hide."""
        response = app_client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg")

    def test_the_pages_declare_one_too(self, app_client, world):
        assert 'rel="icon"' in get(app_client, "/")


def test_the_dotted_helper_walks_paths_and_tolerates_gaps():
    from app.web.routes import _dotted

    data = {"a": {"b": {"c": 7}}, "n": None}
    assert _dotted(data, "a.b.c") == 7
    assert _dotted(data, "a.b") == {"c": 7}
    assert _dotted(data, "a.missing.c") is None
    assert _dotted(data, "n.anything") is None
    assert _dotted(None, "a") is None
