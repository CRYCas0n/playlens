"""Load a Release 0 harvest file into a database.

The harvest is twenty-one real captured API responses -- games, per-platform scores and
the full review corpora -- taken during Release 0. Reproducing that measurement means
running against exactly those bytes, so the loader lives here rather than only in the
test suite, and both use the same one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_harvest(db) -> Callable[..., tuple[int, int]]:
    """Load a Release 0 harvest (real API responses) into the database.

    Using the captured corpora rather than invented ones means the AI pipeline tests are
    exercised against the exact data that produced the Release 0 findings — including
    Elden Ring's zero negative critic reviews and NBA 2K27's userScore of 0.
    """
    import datetime as _dt
    import json as _json
    from pathlib import Path as _Path

    from app.db.models import Game, GamePlatform, Platform, Review
    from app.db.seed import platform_code_for
    from app.normalizers.text import body_hash, normalize_title, sha256_text

    root = REPO_ROOT / "docs" / "research-fixtures" / "release0" / "harvest"

    def _load(slug: str, platform_slug: str | None = None) -> tuple[int, int]:
        payload = _json.loads(_Path(root / f"{slug}.json").read_text(encoding="utf-8"))
        platform_slug = platform_slug or next(iter(payload["per_platform"]))
        block = payload["per_platform"][platform_slug]

        platform = db.execute(
            sa.select(Platform).where(Platform.slug == platform_slug)
        ).scalar_one_or_none()
        if platform is None:
            name = platform_slug.replace("-", " ").title()
            platform = Platform(
                slug=platform_slug,
                name=name,
                code=platform_code_for(platform_slug, name),
                sort_order=500,
            )
            db.add(platform)
            db.flush()

        game = Game(
            mc_slug=payload["slug"],
            mc_title_id=abs(hash(payload["slug"])) % 10**9,
            mc_url=f"https://www.metacritic.com/game/{payload['slug']}/",
            title=payload["title"],
            title_norm=normalize_title(payload["title"]),
            description=payload.get("description"),
            release_date=(
                _dt.date.fromisoformat(payload["releaseDate"])
                if payload.get("releaseDate")
                else None
            ),
            premiere_year=payload.get("premiereYear"),
            source_fingerprint="harvest",
        )
        db.add(game)
        db.flush()

        critic_stats = block.get("critic_stats") or {}
        user_stats = block.get("user_stats") or {}
        gp = GamePlatform(
            game_id=game.id,
            platform_id=platform.id,
            is_lead=True,
            metascore_raw=critic_stats.get("score"),
            metascore_count=critic_stats.get("reviewCount") or 0,
            metascore_positive=critic_stats.get("positiveCount") or 0,
            metascore_neutral=critic_stats.get("neutralCount") or 0,
            metascore_negative=critic_stats.get("negativeCount") or 0,
            userscore_raw=user_stats.get("score"),
            userscore_count=user_stats.get("reviewCount") or 0,
            userscore_positive=user_stats.get("positiveCount") or 0,
            userscore_neutral=user_stats.get("neutralCount") or 0,
            userscore_negative=user_stats.get("negativeCount") or 0,
        )
        db.add(gp)
        db.flush()

        for index, raw in enumerate(block.get("critic_reviews") or []):
            body = raw.get("quote") or ""
            db.add(
                Review(
                    game_id=game.id,
                    game_platform_id=gp.id,
                    kind="critic",
                    dedupe_key=sha256_text(f"{raw.get('pub')}|{raw.get('date')}|{index}")[:64],
                    publication_name=raw.get("pub"),
                    publication_slug=(raw.get("pub") or "").lower().replace(" ", "-") or None,
                    score=raw.get("score"),
                    score_max=100,
                    score_normalized=raw.get("score"),
                    body=body,
                    body_hash=body_hash(body),
                    char_count=len(body),
                    published_on=(
                        _dt.date.fromisoformat(raw["date"]) if raw.get("date") else None
                    ),
                    url=raw.get("url") or None,
                )
            )
        for index, raw in enumerate(block.get("user_reviews") or []):
            body = raw.get("quote") or ""
            db.add(
                Review(
                    game_id=game.id,
                    game_platform_id=gp.id,
                    kind="user",
                    dedupe_key=f"u{index:05d}",
                    source_review_id=f"u{index:05d}",
                    author=raw.get("author"),
                    score=raw.get("score"),
                    score_max=10,
                    score_normalized=(raw.get("score") or 0) * 10,
                    body=body,
                    body_hash=body_hash(body),
                    char_count=len(body),
                    published_on=(
                        _dt.date.fromisoformat(raw["date"]) if raw.get("date") else None
                    ),
                    is_spoiler=bool(raw.get("spoiler")),
                )
            )
        db.commit()
        return game.id, gp.id

    return _load

