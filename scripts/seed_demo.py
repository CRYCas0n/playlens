"""Fill a database with the design package's fixture catalogue.

For looking at the site, and for the browser smoke test to have something to render.

Deliberately not production data and deliberately not a fallback anything reaches for at
runtime: it writes only when a person runs it, into whatever ``DATABASE_URL`` points at,
and every row is obviously fictional. The crawler remains the only way real games arrive.

    DATABASE_URL=sqlite+pysqlite:///./demo.db python -m scripts.seed_demo
"""

from __future__ import annotations

import datetime as dt
import sys

from app.config import get_settings
from app.db.base import make_engine, make_session_factory
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
from app.db.readiness import MIGRATE_COMMAND, schema_exists
from app.db.seed import seed_platforms
from app.normalizers.text import normalize_title

#: A real cover path from the source, so the image proxy is genuinely exercised when
#: someone looks at the demo. The games are fictional; the picture is only a picture.
COVER = "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1787423832.jpg"

#: Every shape design/EDGE_CASES.md names, so a browser pass walks the real table rather
#: than nine variations of "a game that works".
DEMO = [
    {
        "slug": "ashen-veil", "title": "Ashen Veil",
        "meta": 93, "meta_n": 118, "user": 8.9, "user_n": 14204,
        "platforms": ("pc", "playstation-5", "xbox-series-x"),
        "dev": "Nightgate Studios", "genres": ("action", "rpg"), "cover": COVER,
        "description": (
            "A hand-drawn action RPG about a cartographer mapping a country that keeps "
            "rearranging itself. Combat is deliberate and unforgiving, and the map is the "
            "real antagonist. Long enough to make the point about clamped summaries: the "
            "description on this page runs past the fold on a phone and has to wrap "
            "without pushing the verdict off the screen."
        ),
    },
    {
        "slug": "northlight-drifters", "title": "Northlight Drifters",
        "meta": 88, "meta_n": 64, "user": 5.1, "user_n": 9100,
        "platforms": ("pc", "playstation-5"), "dev": "Thornhill Interactive",
        "genres": ("adventure",), "cover": COVER,
        "description": "An open-water survival game where every island is generated once.",
    },
    {
        "slug": "neon-district-2087", "title": "Neon District 2087",
        "meta": 61, "meta_n": 52, "user": 7.9, "user_n": 4400,
        "platforms": ("pc",), "dev": "Vantage Softworks",
        "genres": ("action",), "cover": COVER,
        "description": "A cyberpunk brawler that reviewed poorly and sold anyway.",
    },
    {
        "slug": "hollow-signal", "title": "Hollow Signal",
        "meta": 74, "meta_n": 22, "user": None, "user_n": 0,
        "platforms": ("pc",), "dev": "Nightgate Studios",
        "genres": ("adventure",), "cover": COVER,
        "description": "A radio operator alone on a research station.",
    },
    {
        "slug": "orbital-freight-simulator", "title": "Orbital Freight Simulator",
        "meta": None, "meta_n": 0, "user": 7.2, "user_n": 980,
        "platforms": ("pc",), "dev": "Vantage Softworks",
        "genres": ("simulation",), "cover": COVER,
        "description": "Exactly what it says. Reviewed by nobody, played by thousands.",
    },
    {
        "slug": "quiet-harbor", "title": "Quiet Harbor",
        "meta": None, "meta_n": 0, "user": None, "user_n": 0,
        "platforms": ("nintendo-switch",), "dev": "Paper Lantern Games",
        "genres": ("adventure",), "cover": COVER,
        "description": "Released last week. Nothing has been written about it yet.",
    },
    {
        "slug": "midnight-parade", "title": "Midnight Parade",
        "meta": 77, "meta_n": 31, "user": 7.4, "user_n": 2200,
        "platforms": ("pc", "nintendo-switch"), "dev": "Paper Lantern Games",
        "genres": ("adventure",), "cover": None,
        "description": "A festival that happens once a year, in a town that only exists then.",
    },
    {
        "slug": "nba-2k27", "title": "NBA 2K27",
        "meta": 79, "meta_n": 40, "user": 0, "user_n": 2,
        "platforms": ("playstation-5", "xbox-series-x"), "dev": "Vantage Softworks",
        "genres": ("sports",), "cover": COVER,
        "description": "Two people have rated this, and both gave it zero.",
    },
    {
        "slug": "the-lament-of-thorne-hollow",
        "title": "The Lament of Thorne Hollow: Chronicles of the Ashen Veil Remastered",
        "meta": None, "meta_n": 3, "user": None, "user_n": 0,
        "platforms": (
            "pc", "playstation-5", "playstation-4", "xbox-series-x", "xbox-one",
            "nintendo-switch", "nintendo-switch-2", "ios", "android", "stadia", "linux",
        ),
        "dev": "Whitmoor & Sons Interactive Entertainment",
        "genres": ("rpg",), "cover": COVER,
        "description": "Eleven platforms, seventy-two characters of title, three reviews.",
    },
]

EXTRA_PLATFORMS = [
    ("ios", "iOS", "IOS"),
    ("android", "Android", "AND"),
    ("stadia", "Stadia", "STA"),
    ("linux", "Linux", "LIN"),
]

SIMILAR = [
    ("northlight-drifters", "Same studio"),
    ("hollow-signal", "Same studio"),
    ("midnight-parade", None),          # case 15: a card with no reason, in the same rail
    ("neon-district-2087", "Similar themes"),
    ("orbital-freight-simulator", None),
]


def main() -> int:
    settings = get_settings()
    engine = make_engine(settings)

    # Checked before anything is written. Without this the first thing a new person sees
    # is thirty lines of SQLAlchemy internals ending in "no such table: platforms", plus
    # an empty database file left behind to confuse the next attempt.
    if not schema_exists(engine):
        print(
            f"\n  The database at {settings.database_url} has no tables yet."
            f"\n  Create them first:  {MIGRATE_COMMAND}\n",
            file=sys.stderr,
        )
        engine.dispose()
        return 2

    session = make_session_factory(engine)()

    seed_platforms(session)
    for order, (slug, name, code) in enumerate(EXTRA_PLATFORMS, start=90):
        if not session.query(Platform).filter_by(slug=slug).first():
            session.add(Platform(slug=slug, name=name, code=code, sort_order=order))
    session.flush()

    platforms = {p.slug: p for p in session.query(Platform).all()}
    games: dict[str, Game] = {}

    for spec in DEMO:
        existing = session.query(Game).filter_by(mc_slug=spec["slug"]).first()
        if existing is not None:
            games[spec["slug"]] = existing
            continue

        game = Game(
            mc_slug=spec["slug"],
            mc_url=f"https://www.metacritic.com/game/{spec['slug']}/",
            title=spec["title"],
            title_norm=normalize_title(spec["title"]),
            description=spec["description"],
            cover_url=spec["cover"],
            release_date=dt.date(2026, 2, 10),
            premiere_year=2026,
            best_metascore=spec["meta"],
            best_metascore_count=spec["meta_n"],
            best_userscore=spec["user"],
            best_userscore_count=spec["user_n"],
            detail_synced_at=dt.datetime.now(dt.UTC),
        )
        session.add(game)
        session.flush()
        games[spec["slug"]] = game

        for index, platform_slug in enumerate(spec["platforms"]):
            session.add(
                GamePlatform(
                    game_id=game.id,
                    platform_id=platforms[platform_slug].id,
                    is_lead=index == 0,
                    metascore_raw=spec["meta"] if index == 0 else None,
                    metascore_count=spec["meta_n"] if index == 0 else 0,
                    userscore_raw=spec["user"] if index == 0 else None,
                    userscore_count=spec["user_n"] if index == 0 else 0,
                )
            )

        for genre_slug in spec["genres"]:
            genre = session.query(Genre).filter_by(slug=genre_slug).first()
            if genre is None:
                genre = Genre(slug=genre_slug, name=genre_slug.title())
                session.add(genre)
                session.flush()
            session.add(GameGenre(game_id=game.id, genre_id=genre.id))

        developer = spec["dev"]
        company = session.query(Company).filter_by(name=developer).first()
        if company is None:
            company = Company(
                slug=normalize_title(developer).replace(" ", "-"), name=developer
            )
            session.add(company)
            session.flush()
        session.add(GameCompany(game_id=game.id, company_id=company.id, role="developer"))

    session.flush()

    anchor = games.get("ashen-veil")
    if anchor is not None and not session.query(SimilarGame).filter_by(
        game_id=anchor.id
    ).first():
        for rank, (slug, reason) in enumerate(SIMILAR, start=1):
            other = games.get(slug)
            if other is None:
                continue
            session.add(
                SimilarGame(
                    game_id=anchor.id,
                    similar_game_id=other.id,
                    score=0.9 - rank / 50,
                    rank=rank,
                    method="hybrid",
                    components={"metadata": 0.6},
                    reason=reason,
                )
            )

    session.commit()
    total = session.query(Game).count()
    session.close()
    engine.dispose()
    print(f"demo catalogue ready: {total} games")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
