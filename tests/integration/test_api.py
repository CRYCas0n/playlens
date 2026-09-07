"""REST API contract."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.db.models import Game, GamePlatform, Platform
from app.normalizers.text import normalize_title

pytestmark = pytest.mark.integration


@pytest.fixture
def catalogue(db):
    """A small catalogue covering the score shapes the UI has to survive."""
    platforms = {
        row.slug: row for row in db.execute(sa.select(Platform)).scalars()
    }

    def add(
        slug: str,
        title: str,
        *,
        meta: int | None,
        meta_count: int = 40,
        user: float | None,
        user_count: int = 500,
        platform_slugs: tuple[str, ...] = ("pc",),
        year: int = 2024,
    ) -> Game:
        game = Game(
            mc_slug=slug,
            mc_url=f"https://www.metacritic.com/game/{slug}/",
            title=title,
            title_norm=normalize_title(title),
            description=f"{title} is a game.",
            best_metascore=meta,
            best_metascore_count=meta_count if meta is not None else 0,
            best_userscore=user,
            best_userscore_count=user_count if user is not None else 0,
            premiere_year=year,
            release_date=dt.date(year, 3, 1),
        )
        db.add(game)
        db.flush()
        for index, code in enumerate(platform_slugs):
            db.add(
                GamePlatform(
                    game_id=game.id,
                    platform_id=platforms[code].id,
                    is_lead=index == 0,
                    metascore_raw=meta,
                    metascore_count=meta_count if meta is not None else 0,
                    userscore_raw=user,
                    userscore_count=user_count if user is not None else 0,
                )
            )
        db.flush()
        return game

    games = {
        "ashen": add("ashen-veil", "Ashen Veil", meta=93, user=8.9, meta_count=118,
                     user_count=14204, platform_slugs=("pc", "playstation-5")),
        "gap": add("northlight-drifters", "Northlight Drifters", meta=88, user=5.1),
        "higher": add("neon-district", "Neon District 2087", meta=61, user=7.9),
        # The Release 0 false zero: two ratings and a player score of 0.
        "falsezero": add("nba-2k27", "NBA 2K27", meta=79, user=0, user_count=2),
        "unrated": add("orbital-freight", "Orbital Freight Simulator", meta=None, user=7.2),
        "nouser": add("hollow-signal", "Hollow Signal", meta=74, user=None, user_count=0),
    }
    db.commit()
    return games


class TestCatalogEndpoint:
    def test_lists_games(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games").json()
        assert payload["total"] == 6
        assert {item["slug"] for item in payload["items"]} == {
            "ashen-veil", "northlight-drifters", "neon-district", "nba-2k27",
            "orbital-freight", "hollow-signal",
        }

    def test_scores_are_objects_never_bare_numbers(self, app_client, catalogue):
        items = {i["slug"]: i for i in app_client.get("/api/v1/games").json()["items"]}
        score = items["ashen-veil"]["metascore"]
        assert score["value"] == 93
        assert score["normalized"] == 93
        assert score["tier_label"] == "Отлично"
        assert score["status"] == "valid"

    def test_a_false_zero_is_reported_as_unavailable(self, app_client, catalogue):
        """NBA 2K27: Metascore 79, player score 0 from two ratings (ADR-002)."""
        items = {i["slug"]: i for i in app_client.get("/api/v1/games").json()["items"]}
        user = items["nba-2k27"]["userscore"]
        assert user["status"] == "unavailable"
        assert user["value"] is None
        assert user["normalized"] is None
        assert user["tier_label"] == "Без оценки"

    def test_search_normalises_punctuation(self, app_client, db, catalogue):
        game = Game(
            mc_slug="nier-automata", mc_url="https://x/game/nier-automata/",
            title="NieR: Automata", title_norm=normalize_title("NieR: Automata"),
            best_metascore=88, best_metascore_count=90,
        )
        db.add(game)
        db.commit()
        payload = app_client.get("/api/v1/games", params={"q": "nier automata"}).json()
        assert [i["slug"] for i in payload["items"]] == ["nier-automata"]

    def test_platform_filter_is_an_or(self, app_client, catalogue):
        payload = app_client.get(
            "/api/v1/games", params=[("platform", "playstation-5")]
        ).json()
        assert [i["slug"] for i in payload["items"]] == ["ashen-veil"]

    def test_score_band_filter_is_cumulative_as_its_label_promises(self, app_client, catalogue):
        """The chip reads "Excellent 85+", so it means 85 and above."""
        payload = app_client.get("/api/v1/games", params={"score_band": "excellent"}).json()
        assert {i["slug"] for i in payload["items"]} == {"ashen-veil", "northlight-drifters"}
        good = app_client.get("/api/v1/games", params={"score_band": "good"}).json()
        assert {i["slug"] for i in good["items"]} >= {"ashen-veil", "northlight-drifters", "nba-2k27"}

    def test_unrated_band_finds_games_without_a_score(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games", params={"score_band": "unrated"}).json()
        assert [i["slug"] for i in payload["items"]] == ["orbital-freight"]

    def test_sort_by_gap_is_a_first_class_option(self, app_client, catalogue):
        """The catalogue expression of the product's core idea."""
        payload = app_client.get("/api/v1/games", params={"sort": "gap"}).json()
        assert payload["items"][0]["slug"] == "northlight-drifters"  # 88 vs 51 = 37
        assert payload["items"][0]["gap"] == 37

    def test_ascending_sort_does_not_float_unrated_games_to_the_top(
        self, app_client, catalogue
    ):
        """The failure Release 0 warned about in section 5.5."""
        payload = app_client.get(
            "/api/v1/games", params={"sort": "userscore", "order": "asc"}
        ).json()
        statuses = [i["userscore"]["status"] for i in payload["items"]]
        assert statuses[0] == "valid"
        assert statuses[-1] == "unavailable"

    def test_pagination(self, app_client, catalogue):
        first = app_client.get("/api/v1/games", params={"limit": 2, "offset": 0}).json()
        second = app_client.get("/api/v1/games", params={"limit": 2, "offset": 2}).json()
        assert first["has_more"] is True
        assert len(first["items"]) == 2
        assert {i["slug"] for i in first["items"]} & {i["slug"] for i in second["items"]} == set()

    def test_deep_offsets_are_refused(self, app_client, catalogue):
        response = app_client.get("/api/v1/games", params={"offset": 999999})
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_facets_carry_counts(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games").json()
        facets = {f["slug"]: f["count"] for f in payload["facets"]}
        assert facets["pc"] == 6
        assert facets["playstation-5"] == 1

    def test_invalid_parameters_produce_a_problem_document(self, app_client, catalogue):
        response = app_client.get("/api/v1/games", params={"limit": 5000})
        assert response.status_code == 422
        body = response.json()
        assert body["status"] == 422
        assert body["title"] == "Invalid parameters"


class TestGameEndpoint:
    def test_returns_the_full_detail(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games/ashen-veil").json()
        assert payload["title"] == "Ashen Veil"
        assert payload["verdict"]["derived"] is True
        assert payload["consensus"]["axis_caption"].startswith("Оценки игроков публикуются")
        assert len(payload["platforms"]) == 2

    def test_the_verdict_never_contradicts_the_scores(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games/northlight-drifters").json()
        assert payload["verdict"]["kind"] == "players-lower"
        assert payload["verdict"]["delta"] == 37
        assert "на 37 баллов ниже" in payload["verdict"]["line"]

    def test_players_higher_is_distinguishable(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games/neon-district").json()
        assert payload["verdict"]["kind"] == "players-higher"
        assert "на 18 баллов выше" in payload["verdict"]["line"]

    def test_a_missing_side_is_stated(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games/hollow-signal").json()
        assert payload["verdict"]["kind"] == "critic-only"
        assert "Оценки игроков пока нет" in payload["verdict"]["line"]

    def test_no_critic_score_falls_back_to_the_player_sentence(self, app_client, catalogue):
        payload = app_client.get("/api/v1/games/orbital-freight").json()
        assert payload["verdict"]["kind"] == "player-only"
        assert "Оценки критиков пока нет" in payload["verdict"]["line"]

    def test_unknown_slug_is_a_problem_document(self, app_client, catalogue):
        response = app_client.get("/api/v1/games/does-not-exist")
        assert response.status_code == 404
        assert response.json()["title"] == "Not found"

    def test_a_selected_platform_adds_the_second_sentence(self, app_client, db, catalogue):
        """Cyberpunk-shaped case: the same game, 29 points apart across platforms."""
        game = catalogue["ashen"]
        db.execute(
            sa.update(GamePlatform)
            .where(GamePlatform.game_id == game.id, GamePlatform.is_lead.is_(False))
            .values(metascore_raw=57, metascore_count=38)
        )
        db.commit()
        payload = app_client.get(
            "/api/v1/games/ashen-veil", params={"platform": "playstation-5"}
        ).json()
        assert payload["verdict"]["platform_line"]
        assert "PlayStation 5" in payload["verdict"]["platform_line"]
        assert "ниже, чем на PC" in payload["verdict"]["platform_line"]


class TestOtherEndpoints:
    def test_suggest(self, app_client, catalogue):
        payload = app_client.get("/api/v1/search/suggest", params={"q": "ash"}).json()
        assert payload[0]["slug"] == "ashen-veil"

    def test_suggest_rejects_a_single_character(self, app_client, catalogue):
        assert app_client.get("/api/v1/search/suggest", params={"q": "a"}).status_code == 422

    def test_platforms(self, app_client, catalogue):
        payload = app_client.get("/api/v1/platforms").json()
        assert {p["slug"] for p in payload} >= {"pc", "playstation-5"}

    def test_stats(self, app_client, catalogue):
        payload = app_client.get("/api/v1/stats").json()
        assert payload["games_total"] == 6
        assert payload["games_with_metascore"] == 5

    def test_similar_is_empty_rather_than_padded(self, app_client, catalogue):
        assert app_client.get("/api/v1/games/ashen-veil/similar").json() == []

    def test_health(self, app_client):
        payload = app_client.get("/api/v1/health").json()
        assert payload["status"] == "ok"
        assert payload["checks"]["database"] == "ok"

    def test_openapi_is_complete(self, app_client):
        spec = app_client.get("/api/v1/openapi.json").json()
        paths = set(spec["paths"])
        for required in (
            "/api/v1/games",
            "/api/v1/games/{slug}",
            "/api/v1/games/{slug}/similar",
            "/api/v1/games/{slug}/summaries",
            "/api/v1/platforms",
            "/api/v1/genres",
            "/api/v1/search/suggest",
            "/api/v1/monitoring/status",
            "/api/v1/monitoring/runs",
            "/api/v1/monitoring/stream",
            "/api/v1/admin/crawl/run",
            "/api/v1/health",
        ):
            assert required in paths, required

    def test_html_routes_are_not_in_the_openapi_document(self, app_client):
        """The API contract describes the API, not the pages built on it."""
        spec = app_client.get("/api/v1/openapi.json").json()
        assert "/games" not in spec["paths"]
        assert "/admin/monitoring" not in spec["paths"]


class TestSecurityHeaders:
    def test_every_response_carries_the_baseline_headers(self, app_client, catalogue):
        response = app_client.get("/api/v1/games")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Request-ID"]

    def test_caching_headers_on_read_endpoints(self, app_client, catalogue):
        assert "max-age=60" in app_client.get("/api/v1/games").headers["Cache-Control"]
        assert "max-age=300" in app_client.get("/api/v1/games/ashen-veil").headers["Cache-Control"]


class TestMonitoringContract:
    """The Bonus 2 indicators, asserted as a shape rather than a 200."""

    def test_status_carries_every_indicator_the_assignment_lists(self, app_client, catalogue):
        status = app_client.get("/api/v1/monitoring/status").json()
        assert set(status) >= {
            "system", "schedule", "counters24h", "current_job", "stages",
            "workers", "queues", "runs", "problems", "data_quality", "ai",
        }
        assert status["system"]["status"] in {"healthy", "degraded", "down"}
        assert status["system"]["message"]
        assert set(status["schedule"]) >= {
            "cadence", "last_run_at", "last_success_at", "next_run_at",
            "last_run_duration_ms", "max_games_per_run",
        }
        assert set(status["counters24h"]) >= {
            "runs", "games_processed", "succeeded", "failed",
            "skipped_duplicates", "success_rate", "avg_duration_ms",
        }

    def test_the_stage_list_is_this_products_pipeline(self, app_client, catalogue):
        """A generic job list would not locate a stall; these are our stages."""
        stages = app_client.get("/api/v1/monitoring/status").json()["stages"]
        assert [s["name"] for s in stages] == [
            "Обход Metacritic", "Загрузка рецензий", "Резюме и переводы",
            "Похожие игры", "Поиск на YouTube",
        ]
        assert all(set(s) >= {"state", "queued", "running", "failed"} for s in stages)

    def test_a_disabled_feature_reads_as_disabled_not_as_healthy(self, app_client, catalogue):
        stages = {s["name"]: s for s in app_client.get(
            "/api/v1/monitoring/status").json()["stages"]}
        assert stages["Поиск на YouTube"]["state"] == "disabled"

    def test_data_quality_is_reported_as_product_metrics(self, app_client, catalogue):
        """An operator needs to know users are seeing empty cards, not row counts."""
        quality = app_client.get("/api/v1/monitoring/status").json()["data_quality"]
        assert quality["games_total"] == 6
        assert quality["with_metascore"] == 5
        assert quality["summary_coverage_pct"] == 0.0

    def test_with_no_successful_crawl_the_system_is_degraded_and_says_why(
        self, app_client, catalogue
    ):
        system = app_client.get("/api/v1/monitoring/status").json()["system"]
        assert system["status"] == "degraded"
        assert "Успешных обходов пока не было" in system["message"]

    def test_runs_and_events_are_listable(self, app_client, catalogue):
        assert app_client.get("/api/v1/monitoring/runs").json() == {"items": []}
        assert app_client.get("/api/v1/monitoring/events").json() == {"items": []}
        assert app_client.get("/api/v1/monitoring/runs/999").status_code == 404

    def test_metrics_are_exposed_in_prometheus_text_format(self, app_client, catalogue):
        response = app_client.get("/api/v1/metrics")
        assert response.status_code == 200
        body = response.text
        assert "# HELP" in body and "# TYPE" in body
        assert "playlens_games_total 6" in body



class TestEventStreamContract:
    """What can be asserted about SSE without an ASGI transport that never terminates.

    The endpoint's generator loops until the client disconnects, and the in-process test
    transport has no way to signal that mid-iteration -- reading one chunk and leaving the
    block hangs. So the two halves are tested separately: the replay SOURCE here and in
    ``test_job_queue.py::test_events_are_ordered_and_resumable``, and the wire format by
    ``scripts/smoke.py`` against a real server. The gap is recorded in FINAL_AUDIT.
    """

    def test_the_backlog_a_reconnecting_client_would_receive(self, app_client, db, catalogue):
        """``job_events.id`` IS the Last-Event-ID: a reconnect replays, it does not skip."""
        from app.db.uow import UnitOfWork

        uow = UnitOfWork.bound(db)
        missed = [
            uow.events.emit("run.started", message=f"event {index}") for index in range(3)
        ]
        db.commit()
        cursor = min(missed) - 1

        replayed = uow.events.after(cursor, limit=200)
        assert [e.id for e in replayed] == sorted(missed)
        assert [e.message for e in replayed] == ["event 0", "event 1", "event 2"]

    def test_a_client_too_far_behind_is_given_a_fresh_snapshot_instead(
        self, app_client, db, db_settings, catalogue
    ):
        """Replaying a million events to one late client is worse than a resync."""
        from app.db.uow import UnitOfWork

        uow = UnitOfWork.bound(db)
        latest = uow.events.emit("run.started")
        db.commit()
        assert latest - (latest - db_settings.sse_max_backlog - 1) > db_settings.sse_max_backlog

    def test_the_stream_route_is_registered_with_the_right_media_type(self, app_client):
        spec = app_client.get("/api/v1/openapi.json").json()
        assert "/api/v1/monitoring/stream" in spec["paths"]


class TestGenres:
    def test_genres_are_listed_by_how_many_games_carry_them(self, app_client, db, catalogue):
        from app.db.models import Game, GameGenre, Genre

        genres = {}
        for slug, name in (("action", "Action"), ("racing", "Racing")):
            row = Genre(slug=slug, name=name)
            db.add(row)
            db.flush()
            genres[slug] = row
        games = db.execute(sa.select(Game)).scalars().all()
        for game in games[:4]:
            db.add(GameGenre(game_id=game.id, genre_id=genres["action"].id))
        db.add(GameGenre(game_id=games[0].id, genre_id=genres["racing"].id))
        db.commit()

        payload = app_client.get("/api/v1/genres").json()
        assert [g["slug"] for g in payload] == ["action", "racing"]
        assert payload[0]["count"] == 4
        assert payload[1]["count"] == 1

    def test_a_genre_with_no_games_is_hidden_by_default(self, app_client, db, catalogue):
        from app.db.models import Genre

        db.add(Genre(slug="ghost", name="Ghost"))
        db.commit()
        assert app_client.get("/api/v1/genres").json() == []
        with_empty = app_client.get(
            "/api/v1/genres", params={"only_with_games": "false"}
        ).json()
        assert [g["slug"] for g in with_empty] == ["ghost"]


class TestTheCatalogFragment:
    """`/games/fragment` returns the grid alone, for filtering without a page reload.

    It shares a template with the full page, so the two cannot disagree about what a
    result looks like — but nothing was checking that it works at all, which is how a
    route ends up broken in a way only a person clicking a filter would notice.
    """

    def test_it_returns_the_grid_without_the_page_around_it(self, app_client, catalogue):
        response = app_client.get("/games/fragment")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "grid-games" in body
        assert "<html" not in body, "a fragment must not carry a whole document"
        assert "<title>" not in body

    def test_it_honours_the_same_filters_as_the_full_page(self, app_client, catalogue):
        fragment = app_client.get(
            "/games/fragment", params={"score_band": "excellent"}
        ).text
        full = app_client.get("/games", params={"score_band": "excellent"}).text
        assert "ashen-veil" in fragment
        assert "neon-district" not in fragment
        # Same rows in both surfaces: one template, one filter path.
        for slug in ("ashen-veil", "northlight-drifters"):
            assert (slug in fragment) == (slug in full)

    def test_an_empty_result_renders_the_same_empty_state(self, app_client, catalogue):
        fragment = app_client.get("/games/fragment", params={"q": "zzzznothing"}).text
        assert "zzzznothing" in fragment
        assert "Очистить поиск" in fragment

    def test_it_is_not_in_the_public_api_contract(self, app_client):
        spec = app_client.get("/api/v1/openapi.json").json()
        assert "/games/fragment" not in spec["paths"]
