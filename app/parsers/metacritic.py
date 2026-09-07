"""Pure parsers: Metacritic JSON -> domain DTOs.

No IO here, by design. These functions are the highest-value tests in the project because
they are the boundary where an unannounced upstream change becomes our bug, and they can
be exercised against the real captured responses in ``docs/research-fixtures``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from typing import Any

from app.adapters.metacritic.schemas import (
    McCriticReview,
    McListingData,
    McProduct,
    McScoreSummary,
    McUserReview,
    validate,
)
from app.domain.enums import ReviewKind
from app.domain.errors import SchemaDriftError
from app.domain.models import (
    CompanyRef,
    FranchiseRef,
    GameDetail,
    GenreRef,
    Listing,
    ListingItem,
    PlatformScores,
    ReviewPage,
    ReviewRecord,
    ScoreStats,
)
from app.normalizers.text import (
    body_hash,
    clean_body,
    normalize_title,
    sha256_text,
    slugify,
    stable_fingerprint,
)

#: Unsigned original. The CDN's resize endpoint is HMAC-signed and forging that signature
#: is out of bounds, so we store the original and resize on our own side (ADR-001/017).
IMAGE_BASE = "/a/img/catalog"

_SLUG_FROM_URL = re.compile(r"/game/([^/]+)/?$")


def image_url(bucket_path: str | None, base_url: str) -> str | None:
    if not bucket_path:
        return None
    return f"{base_url.rstrip('/')}{IMAGE_BASE}{bucket_path}"


def game_url(slug: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/game/{slug}/"


# --------------------------------------------------------------------------- listing


def parse_listing(payload: dict[str, Any], *, offset: int, limit: int) -> Listing:
    data = validate(McListingData, payload.get("data") or {}, context="finder.data")
    items = tuple(
        ListingItem(
            slug=item.slug,
            title=item.title,
            mc_title_id=item.id,
            release_date=item.release_date,
            premiere_year=item.premiere_year,
            metascore=_int_or_none(
                item.critic_score_summary.score if item.critic_score_summary else None
            ),
            metascore_count=(
                item.critic_score_summary.review_count if item.critic_score_summary else None
            ),
            userscore=item.user_score.score if item.user_score else None,
            cover_path=item.image.bucket_path if item.image else None,
            rating=item.rating,
        )
        for item in data.items
    )
    return Listing(items=items, total_results=data.total_results, offset=offset, limit=limit)


# --------------------------------------------------------------------------- game detail


def extract_component(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    for component in payload.get("components") or []:
        if (component.get("meta") or {}).get("componentName") == name:
            return component
    return None


def parse_game_detail(payload: dict[str, Any], *, base_url: str) -> GameDetail:
    component = extract_component(payload, "product")
    if component is None:
        raise SchemaDriftError("composer response has no 'product' component")
    item = ((component.get("data") or {}).get("item")) or {}
    product = validate(McProduct, item, context="composer.product")

    genres = tuple(
        GenreRef(name=g.name, slug=slugify(g.name), mc_genre_id=g.id) for g in product.genres
    )

    companies: list[CompanyRef] = []
    seen: set[tuple[str, str]] = set()
    for company in (product.production.companies if product.production else []):
        role = "developer" if (company.type_name or "").lower() == "developer" else "publisher"
        slug = slugify(company.name)
        if (slug, role) in seen:
            continue
        seen.add((slug, role))
        companies.append(
            CompanyRef(name=company.name, slug=slug, role=role, mc_company_id=company.id)
        )

    franchise = None
    taxonomy = product.game_taxonomy
    if taxonomy and taxonomy.franchises:
        first = taxonomy.franchises[0]
        franchise = FranchiseRef(
            name=first.name, slug=slugify(first.name), mc_franchise_id=first.id
        )

    platforms = tuple(_platform_scores(p) for p in product.platforms)
    # Every game must have exactly one lead so that "the main rating" is well defined,
    # and the source guarantees neither end of that. Games arrive with no lead, and --
    # found on real data, not imagined -- with two: a re-release carrying isLeadPlatform
    # alongside the original. The database says one (uq_game_platforms_one_lead), so a
    # game with two leads was rejected outright and never ingested at all.
    #
    # Both directions are normalised here, in the parser, where the source's shape is
    # already being turned into ours. The choice is the source's own order, which is
    # deterministic and stays visible in the data.
    lead_positions = [i for i, p in enumerate(platforms) if p.is_lead]
    if platforms and not lead_positions:
        platforms = (replace(platforms[0], is_lead=True), *platforms[1:])
    elif len(lead_positions) > 1:
        keep = lead_positions[0]
        platforms = tuple(
            replace(p, is_lead=(i == keep)) if p.is_lead else p
            for i, p in enumerate(platforms)
        )

    cover = image_url(_bucket(product.images, "mainImage"), base_url)
    card = image_url(_bucket(product.images, "cardImage"), base_url)

    video = product.video
    video_url = (video.embed_url or video.manifest_url) if video else None
    video_title = (video.video_title or video.title) if video else None

    detail = GameDetail(
        slug=product.slug,
        title=product.title,
        title_norm=normalize_title(product.title),
        mc_url=game_url(product.slug, base_url),
        mc_title_id=product.id,
        mc_family_id=taxonomy.family.id if taxonomy and taxonomy.family else None,
        description=clean_body(product.description),
        cover_url=cover,
        card_url=card,
        video_url=video_url,
        video_title=video_title,
        video_duration_s=video.duration if video else None,
        release_date=product.release_date,
        premiere_year=product.premiere_year,
        esrb_rating=product.rating,
        must_play=product.must_play,
        genres=genres,
        companies=tuple(companies),
        franchise=franchise,
        platforms=platforms,
    )
    return _with_fingerprint(detail)


def _with_fingerprint(detail: GameDetail) -> GameDetail:
    """Fingerprint the fields whose change should trigger downstream work.

    Timestamps and review counts are excluded on purpose: they move constantly and would
    make every hourly pass look like a change, which is precisely the expensive mistake
    the differential update exists to avoid.
    """
    payload = {
        "slug": detail.slug,
        "title": detail.title,
        "description": detail.description,
        "cover": detail.cover_url,
        "video": detail.video_url,
        "release_date": detail.release_date.isoformat() if detail.release_date else None,
        "rating": detail.esrb_rating,
        "genres": sorted(g.slug for g in detail.genres),
        "companies": sorted(f"{c.role}:{c.slug}" for c in detail.companies),
        "franchise": detail.franchise.slug if detail.franchise else None,
        "platforms": sorted(
            f"{p.slug}:{p.metascore}:{p.metascore_count}" for p in detail.platforms
        ),
    }
    return replace(detail, source_fingerprint=stable_fingerprint(payload))


def _platform_scores(platform) -> PlatformScores:
    summary = platform.critic_score_summary
    slug = platform.slug or slugify(platform.name)
    return PlatformScores(
        slug=slug,
        name=platform.name,
        mc_platform_id=platform.id,
        is_lead=platform.is_lead_platform,
        release_date=platform.release_date,
        metascore=_int_or_none(summary.score if summary else None),
        metascore_count=int(summary.review_count or 0) if summary else 0,
        metascore_positive=int(summary.positive_count or 0) if summary else 0,
        metascore_neutral=int(summary.neutral_count or 0) if summary else 0,
        metascore_negative=int(summary.negative_count or 0) if summary else 0,
        critic_sentiment=summary.sentiment if summary else None,
        mc_related_game_id=platform.related_game_id,
    )


def _bucket(images, type_name: str) -> str | None:
    for image in images:
        if image.type_name == type_name and image.bucket_path:
            return image.bucket_path
    return images[0].bucket_path if images else None


def _int_or_none(value: float | None) -> int | None:
    return None if value is None else round(value)


# --------------------------------------------------------------------------- stats


def parse_score_stats(payload: dict[str, Any], *, scale_max: int) -> ScoreStats:
    item = ((payload.get("data") or {}).get("item")) or {}
    summary = validate(McScoreSummary, item, context="stats.item")
    return ScoreStats(
        score=summary.score,
        scale_max=summary.max or scale_max,
        review_count=int(summary.review_count or 0),
        positive=int(summary.positive_count or 0),
        neutral=int(summary.neutral_count or 0),
        negative=int(summary.negative_count or 0),
        sentiment=summary.sentiment,
    )


# --------------------------------------------------------------------------- reviews


def parse_reviews(
    payload: dict[str, Any], *, kind: ReviewKind, offset: int, sentiment_bucket: str | None = None
) -> ReviewPage:
    data = (payload.get("data") or {})
    raw_items = data.get("items") or []
    total = int(data.get("totalResults") or 0)

    records: list[ReviewRecord] = []
    for raw in raw_items:
        record = (
            _critic_record(raw, sentiment_bucket)
            if kind is ReviewKind.CRITIC
            else _user_record(raw, sentiment_bucket)
        )
        if record is not None:
            records.append(record)

    return ReviewPage(items=tuple(records), total_results=total, offset=offset)


def _critic_record(raw: dict[str, Any], bucket: str | None) -> ReviewRecord | None:
    review = validate(McCriticReview, raw, context="critic review")
    body = clean_body(review.quote)
    publication = review.publication_slug or slugify(review.publication_name or "")
    if not publication and not body:
        return None
    hashed = body_hash(body)
    # Critic reviews carry NO id, and url/author are frequently empty strings, so the
    # natural key is publication + date + text. A republished, edited review becomes a
    # new row: acceptable, and better than losing it.
    key = sha256_text(
        f"{publication}|{review.date.isoformat() if review.date else ''}|{hashed}"
    )[:64]
    score = review.score
    return ReviewRecord(
        kind=ReviewKind.CRITIC,
        dedupe_key=key,
        body=body,
        body_hash=hashed,
        score=score,
        score_max=100,
        score_normalized=score,
        published_on=review.date,
        author=review.author or None,
        publication_name=review.publication_name or None,
        publication_slug=publication or None,
        url=review.url or None,
        char_count=len(body or ""),
        sentiment_bucket=bucket,
    )


def _user_record(raw: dict[str, Any], bucket: str | None) -> ReviewRecord | None:
    review = validate(McUserReview, raw, context="user review")
    body = clean_body(review.quote)
    if not review.id and not body:
        return None
    hashed = body_hash(body)
    key = (review.id or sha256_text(f"{review.author or ''}|{hashed}"))[:64]
    score = review.score
    return ReviewRecord(
        kind=ReviewKind.USER,
        dedupe_key=key,
        source_review_id=review.id,
        body=body,
        body_hash=hashed,
        score=score,
        score_max=10,
        score_normalized=None if score is None else score * 10,
        published_on=review.date,
        author=review.author or None,
        is_spoiler=bool(review.spoiler),
        source_version=review.version,
        thumbs_up=review.thumbs_up,
        thumbs_down=review.thumbs_down,
        char_count=len(body or ""),
        sentiment_bucket=bucket,
    )


# --------------------------------------------------------------------------- sitemap


def parse_sitemap_index(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [
        loc.text.strip()
        for loc in root.iter()
        if loc.tag.endswith("loc") and loc.text and loc.text.strip()
    ]


def parse_sitemap_shard(xml: str) -> list[str]:
    """Slugs from a sitemap shard — the independent completeness check (ADR-004)."""
    slugs: list[str] = []
    for url in parse_sitemap_index(xml):
        match = _SLUG_FROM_URL.search(url.split("?")[0])
        if match:
            slugs.append(match.group(1))
    return slugs
