"""Task registry.

Each entry declares what the assignment asks to be declared (section 11): which queue it
runs on, whether it is retryable, whether it is idempotent, and what triggers it. Handlers
are thin — all the logic they call lives in ``app/services``, which is why the services can
be tested without a queue at all.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.ai.prompts import build_translation_prompt
from app.ai.schemas import TranslationOut
from app.container import Container
from app.db.uow import UnitOfWork
from app.domain.enums import Audience, CrawlItemStatus, RunTrigger
from app.domain.errors import PermanentError, SchemaDriftError
from app.logging import get_logger
from app.services.drift import SCHEMA_DRIFT_EVENT, SCHEMA_OK_EVENT
from app.tasks.contracts import max_attempts_for

log = get_logger(__name__)

Handler = Callable[[Container, UnitOfWork, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    queue: str
    handler: Handler
    retryable: bool
    idempotent: bool
    description: str

    @property
    def max_attempts(self) -> int:
        """Read from ``contracts.MAX_ATTEMPTS``, never stored here.

        One number per task, in one place. A spec that carried its own copy could
        disagree with the one an enqueue site looks up, and the disagreement would show
        up as a job that quietly retries the wrong number of times.
        """
        return max_attempts_for(self.name)


# --------------------------------------------------------------------------- handlers


def crawl_tick(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    result = c.crawl.tick(
        uow,
        trigger=RunTrigger(payload.get("trigger", RunTrigger.SCHEDULE.value)),
        triggered_by=payload.get("triggered_by", "scheduler"),
        worker_id=payload.get("worker_id"),
        max_games=payload.get("max_games"),
    )
    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "claimed": result.claimed,
        "discovered": result.discovered,
        "skipped_duplicates": result.skipped_duplicates,
        "reason": result.reason,
    }


def crawl_reap(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    return c.crawl.reap(uow)


def crawl_reconcile(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    return {"added": c.crawl.reconcile_sitemap(uow, max_new=payload.get("max_new"))}


def game_sync(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    slug = payload["slug"]
    item_id = payload.get("crawl_item_id")
    try:
        outcome = c.game_sync.sync(
            uow, slug, crawl_item_id=item_id, crawl_run_id=payload.get("crawl_run_id")
        )
    except SchemaDriftError as exc:
        # Recorded, not just raised. One game with an odd shape is noise; ten in a row is
        # the source having changed, and the difference between the two is a count that
        # has to survive worker restarts -- so it lives in the event log (ADR-005).
        uow.events.emit(
            SCHEMA_DRIFT_EVENT,
            level="error",
            game_id=None,
            message=str(exc)[:1000],
            data={"slug": slug},
        )
        if item_id:
            uow.crawl.mark_item(item_id, status=CrawlItemStatus.FAILED, error="schema drift")
        raise
    except PermanentError:
        if item_id:
            uow.crawl.mark_item(
                item_id, status=CrawlItemStatus.FAILED, error="permanent failure"
            )
        raise
    # A parse that worked resets the counter: the threshold is about CONSECUTIVE failures.
    uow.events.emit(SCHEMA_OK_EVENT, game_id=outcome.game_id, data={"slug": slug})
    return {
        "game_id": outcome.game_id,
        "created": outcome.created,
        "changed": outcome.changed,
        "follow_ups": list(outcome.follow_ups),
    }


def reviews_sync(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    game_id = payload["game_id"]
    result = c.review_sync.sync(uow, game_id=game_id)

    # Only queue summaries for platforms that actually gained material.
    today = dt.datetime.now(dt.UTC).date().isoformat()
    queued = 0
    for game_platform in uow.games.platforms_for(game_id):
        for audience in (Audience.CRITIC, Audience.USER):
            if uow.jobs.enqueue(
                job_type="summary.generate",
                idempotency_key=(
                    f"summary:{game_platform.id}:{audience.value}:{today}"
                ),
                queue="ai",
                game_id=game_id,
                payload={
                    "game_id": game_id,
                    "game_platform_id": game_platform.id,
                    "audience": audience.value,
                },
            ).created:
                queued += 1

    return {
        "inserted": result.inserted,
        "updated": result.updated,
        "platforms": result.platforms_considered,
        "summaries_queued": queued,
    }


def summary_generate(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    outcome = c.summaries.generate(
        uow,
        game_id=payload["game_id"],
        game_platform_id=payload["game_platform_id"],
        audience=Audience(payload["audience"]),
    )
    # The gap explanation depends on BOTH sides existing, so it is attempted after the
    # player summary rather than as its own scheduled job.
    if outcome.status.value == "fresh" and payload["audience"] == Audience.USER.value:
        uow.jobs.enqueue(
            job_type="summary.gap",
            idempotency_key=f"gap:{payload['game_platform_id']}:{outcome.summary_id}",
            queue="ai",
            game_id=payload["game_id"],
            payload={
                "game_id": payload["game_id"],
                "game_platform_id": payload["game_platform_id"],
            },
        )
    return {
        "status": outcome.status.value,
        "reason": outcome.reason,
        "accepted": outcome.accepted_claims,
        "rejected": outcome.rejected_claims,
        "cost_usd": outcome.cost_usd,
    }


def summary_gap(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    explanation = c.summaries.explain_gap(
        uow, game_id=payload["game_id"], game_platform_id=payload["game_platform_id"]
    )
    return {"explained": explanation is not None}


def game_translate(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    """Render the source's own description in Russian.

    Source text, not evidence: it is a paragraph about the game rather than a claim about
    what reviewers said, so nothing here goes through claim validation -- there is no
    corpus for it to be validated against. The English original is kept and the page says
    which one it is showing.
    """
    game = uow.games.get(payload["game_id"])
    if game is None or not game.description:
        return {"status": "skipped", "reason": "no description"}
    if game.description_ru:
        return {"status": "skipped", "reason": "already translated"}
    if not c.llm.enabled:
        return {"status": "skipped", "reason": "llm disabled"}

    result = c.llm.complete_structured(
        prompt=build_translation_prompt(title=game.title, text=game.description),
        schema=TranslationOut,
        max_tokens=1500,
    )
    text = (result.value.text or "").strip()
    if not text:
        return {"status": "skipped", "reason": "empty translation"}
    game.description_ru = text
    return {"status": "ok", "chars": len(text)}


def similarity_recompute(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    return {"published": c.similarity.recompute(uow, payload["game_id"])}


def similarity_refresh_stale(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
        days=c.settings.similarity_refresh_stale_days
    )
    game_ids = uow.games.needs_similarity(older_than=cutoff, limit=payload.get("limit", 500))
    for game_id in game_ids:
        uow.jobs.enqueue(
            job_type="similarity.recompute",
            idempotency_key=f"similarity:{game_id}:stale:{dt.date.today().isoformat()}",
            queue="enrich",
            game_id=game_id,
            payload={"game_id": game_id},
            priority=2,
        )
    return {"queued": len(game_ids)}


def _queue_next(uow: UnitOfWork, *, job_type: str, game_id: int, key: str) -> bool:
    """Hand the game to the next step of the Let's Play chain.

    The three tasks existed, were registered, and were never linked: `discover` selected
    a video and stopped. In production that meant 180 successful discoveries, 2,285
    videos ranked, and not one transcript ever attempted -- the feature looked alive from
    every angle except the only one that matters.
    """
    return uow.jobs.enqueue(
        job_type=job_type,
        idempotency_key=key,
        queue="enrich" if job_type == "youtube.transcript" else "ai",
        game_id=game_id,
        payload={"game_id": game_id},
        priority=4,
    ).created


def youtube_discover(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    game_id = payload["game_id"]
    result = c.letsplay.discover(uow, game_id=game_id)
    if result.get("status") == "selected":
        # Keyed by the video, so re-discovering the same one does not re-fetch it, and
        # picking a different video does queue a fresh attempt.
        result["queued_transcript"] = _queue_next(
            uow,
            job_type="youtube.transcript",
            game_id=game_id,
            key=f"youtube.transcript:{result['video_id']}",
        )
    return result


def youtube_transcript(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    game_id = payload["game_id"]
    result = c.letsplay.fetch_transcript(uow, game_id=game_id)
    # "cached" counts: a transcript we already hold still has no summary written from it
    # if this is the first time the chain has run to the end.
    if result.get("status") in ("available", "cached"):
        result["queued_summary"] = _queue_next(
            uow,
            job_type="youtube.summarise",
            game_id=game_id,
            key=f"youtube.summarise:{game_id}:{dt.date.today().isoformat()}",
        )
    return result


def youtube_summarise(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    return c.letsplay.summarise(uow, game_id=payload["game_id"])


def maintenance_cleanup(c: Container, uow: UnitOfWork, payload: dict) -> dict:
    now = dt.datetime.now(dt.UTC)
    settings = c.settings
    return {
        "events": uow.events.purge_old(
            now - dt.timedelta(days=settings.events_retention_days)
        ),
        "jobs": uow.jobs.purge_old(now - dt.timedelta(days=settings.jobs_retention_days)),
        "crawl_items": uow.crawl.purge_old_items(
            (now - dt.timedelta(days=settings.crawl_items_retention_days)).date()
        ),
        "snapshots": uow.snapshots.purge_unreferenced(
            now - dt.timedelta(days=settings.snapshot_retention_days)
        ),
        # Worker ids are hostname:pid, so a restart leaves the old row behind forever.
        "workers": uow.workers.purge_old(now - dt.timedelta(hours=6)),
    }


# --------------------------------------------------------------------------- registry

TASKS: dict[str, TaskSpec] = {
    spec.name: spec
    for spec in [
        TaskSpec(
            "crawl.tick", "crawl", crawl_tick,
            retryable=False,  # the next hour will do it anyway
            idempotent=True,
            description="Hourly discovery: New Releases, then browse pages.",
        ),
        TaskSpec(
            "crawl.reap", "crawl", crawl_reap,
            retryable=True, idempotent=True,
            description="Return abandoned leases to the queue.",
        ),
        TaskSpec(
            "crawl.reconcile", "crawl", crawl_reconcile,
            retryable=True, idempotent=True,
            description="Weekly sitemap completeness check.",
        ),
        TaskSpec(
            "game.sync", "enrich", game_sync,
            retryable=True, idempotent=True,
            description="Fetch and upsert one game, then dispatch enrichment.",
        ),
        TaskSpec(
            "reviews.sync", "enrich", reviews_sync,
            retryable=True, idempotent=True,
            description="Fetch reviews for the platforms in budget.",
        ),
        TaskSpec(
            "summary.generate", "ai", summary_generate,
            retryable=True, idempotent=True,
            description="Build a snapshot and, if warranted, a validated summary.",
        ),
        TaskSpec(
            "summary.gap", "ai", summary_gap,
            retryable=True, idempotent=True,
            description="Explain a critic/player gap, only when the data supports it.",
        ),
        TaskSpec(
            "game.translate", "ai", game_translate,
            retryable=True, idempotent=True,
            description="Render the source's description in Russian.",
        ),
        TaskSpec(
            "similarity.recompute", "enrich", similarity_recompute,
            retryable=True, idempotent=True,
            description="Recompute similar games for one game.",
        ),
        TaskSpec(
            "similarity.refresh_stale", "enrich", similarity_refresh_stale,
            retryable=True, idempotent=True,
            description="Queue recomputation for games whose neighbours moved on.",
        ),
        TaskSpec(
            "youtube.discover", "enrich", youtube_discover,
            retryable=True, idempotent=True,
            description="Find and rank a Let's Play. Never blocks the game.",
        ),
        TaskSpec(
            "youtube.transcript", "enrich", youtube_transcript,
            retryable=True, idempotent=True,
            description="Run the transcript provider cascade.",
        ),
        TaskSpec(
            "youtube.summarise", "ai", youtube_summarise,
            retryable=True, idempotent=True,
            description="Spoiler-free impression from a transcript.",
        ),
        TaskSpec(
            "maintenance.cleanup", "crawl", maintenance_cleanup,
            retryable=True, idempotent=True,
            description="Apply retention windows.",
        ),
    ]
}


def get_task(name: str) -> TaskSpec:
    spec = TASKS.get(name)
    if spec is None:
        raise PermanentError(f"unknown job type: {name}")
    return spec
