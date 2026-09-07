"""Reference data seeded at deploy time.

Only the platform catalogue. Everything else — games, reviews, summaries — is crawled,
never shipped in the repository (assignment section 53: no hardcoded production data).
The seed CATALOGUE of games is a crawl with a different sort order, not a data file
(ADR-014).

Codes match design/COMPONENTS.md -> PlatformBadge, so the UI never has to map names.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Platform
from app.repositories.base import upsert_returning_id

# (slug, name, code, family, sort_order, mc_platform_id)
# The Metacritic platform ids that appear in the research fixtures are filled in; the rest
# are learned incrementally from the games we actually see.
PLATFORM_SEED: tuple[tuple[str, str, str, str, int, int | None], ...] = (
    ("pc", "PC", "PC", "pc", 10, 1500000019),
    ("playstation-5", "PlayStation 5", "PS5", "playstation", 20, 1500000128),
    ("playstation-4", "PlayStation 4", "PS4", "playstation", 30, 1500000062),
    ("xbox-series-x", "Xbox Series X|S", "XSX", "xbox", 40, 1500000129),
    ("xbox-one", "Xbox One", "XBO", "xbox", 50, 1500000121),
    ("nintendo-switch", "Nintendo Switch", "NSW", "nintendo", 60, None),
    ("nintendo-switch-2", "Nintendo Switch 2", "NS2", "nintendo", 65, None),
    ("playstation-3", "PlayStation 3", "PS3", "playstation", 70, None),
    ("xbox-360", "Xbox 360", "X360", "xbox", 75, None),
    ("wii-u", "Wii U", "WIIU", "nintendo", 80, None),
    ("3ds", "Nintendo 3DS", "3DS", "nintendo", 85, None),
    ("ios-iphoneipad", "iOS", "IOS", "mobile", 90, None),
    ("android", "Android", "AND", "mobile", 95, None),
    ("mac", "macOS", "MAC", "pc", 100, None),
    ("linux", "Linux", "LNX", "pc", 105, None),
    ("meta-quest", "Meta Quest", "VR", "vr", 110, None),
)

#: Fallback code for a platform we meet for the first time. Four characters keeps the
#: badge from breaking the card layout.
UNKNOWN_PLATFORM_CODE_LEN = 4


def platform_code_for(slug: str, name: str) -> str:
    """Derive a short badge code for a platform the seed does not know about."""
    for known_slug, _n, code, _f, _o, _id in PLATFORM_SEED:
        if known_slug == slug:
            return code
    words = [w for w in name.replace("|", " ").split() if w]
    if len(words) >= 2:
        candidate = "".join(w[0] for w in words)[:UNKNOWN_PLATFORM_CODE_LEN]
    else:
        candidate = name[:UNKNOWN_PLATFORM_CODE_LEN]
    return candidate.upper() or "GEN"


def seed_platforms(session: Session) -> int:
    """Idempotent: re-running updates names and codes but creates no duplicates."""
    count = 0
    for slug, name, code, family, order, mc_id in PLATFORM_SEED:
        upsert_returning_id(
            session,
            Platform.__table__,
            {
                "slug": slug,
                "name": name,
                "code": code,
                "family": family,
                "sort_order": order,
                "mc_platform_id": mc_id,
                "is_active": True,
            },
            index_elements=["slug"],
            update_columns=["name", "code", "family", "sort_order"],
            id_column=Platform.__table__.c.id,
        )
        count += 1
    return count
