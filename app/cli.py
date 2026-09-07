"""Operator commands.

``ADR-014`` specifies the seed catalogue as ``python -m app.cli seed --limit 500``,
``FINAL_SPEC §20`` uses the same entry point, and ``IMPLEMENTATION_PLAN`` phase 5 makes
``crawl --once`` its definition of done. None of it existed: the documents described a
command line that had never been written, and the only way to trigger anything was curl
with an admin token.

This is that command line. It is thin on purpose — every subcommand calls the same
service the worker calls, so nothing here can behave differently from production.

    python -m app.cli status
    python -m app.cli crawl --once
    python -m app.cli seed --limit 500
    python -m app.cli summarise --slug ashen-veil
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any

from app.config import get_settings
from app.container import get_container
from app.db.readiness import MIGRATE_COMMAND, schema_exists

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_READY = 3


def _require_schema() -> int | None:
    """Every subcommand needs tables. Say so once, in words, rather than per query."""
    container = get_container()
    if schema_exists(container.engine):
        return None
    print(
        f"\n  The database has no tables yet.\n  Create them with:  {MIGRATE_COMMAND}\n",
        file=sys.stderr,
    )
    return EXIT_NOT_READY


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    width = max((len(k) for k in payload), default=0)
    for key, value in payload.items():
        print(f"  {key:<{width}}  {value}")


# --------------------------------------------------------------------------- commands


def cmd_status(args: argparse.Namespace) -> int:
    """What an operator asks first: is anything working, and when did it last work."""
    if (code := _require_schema()) is not None:
        return code

    from app.services.monitoring_service import MonitoringService

    container = get_container()
    settings = get_settings()
    with container.uow() as uow:
        status = MonitoringService(settings).status(uow)
        stats = uow.games.stats()

    payload = {
        "health": status.system["status"],
        "message": status.system["message"],
        "games": stats["games_total"],
        "with_metascore": stats["games_with_metascore"],
        "with_summaries": stats["games_with_summaries"],
        "last_crawl": stats["last_crawl_at"],
        "next_run": status.schedule["next_run_at"],
        "queues": status.queues or "empty",
        "workers": len(status.workers),
        "ai_cost_24h_usd": round(status.ai.get("cost_usd", 0.0), 4),
        "ai_provider": f"{settings.llm_provider}/{settings.llm_model}"
        if settings.llm_enabled
        else "disabled",
        "youtube": "enabled" if settings.youtube_enabled else "disabled",
        "schema_drift_streak": status.system.get("schema_drift_streak", 0),
    }
    _emit(payload, as_json=args.json)
    return EXIT_OK


def cmd_crawl(args: argparse.Namespace) -> int:
    """One crawl tick, inline. The same code the scheduled job runs."""
    if (code := _require_schema()) is not None:
        return code

    from app.domain.enums import RunTrigger

    container = get_container()
    with container.uow() as uow:
        result = container.crawl.tick(
            uow,
            trigger=RunTrigger.MANUAL,
            triggered_by="cli",
            max_games=args.max_games,
        )

    _emit(
        {
            "run_id": result.run_id,
            "status": result.status.value,
            "phase": result.phase.value,
            "discovered": result.discovered,
            "claimed": result.claimed,
            "duplicates": result.skipped_duplicates,
            "pages_fetched": result.pages_fetched,
            "reason": result.reason or "-",
        },
        as_json=args.json,
    )
    if result.claimed:
        print(
            f"\n  {result.claimed} games queued for enrichment. "
            "Run `python -m app.queue.worker` to process them."
        )
    return EXIT_OK


def cmd_seed(args: argparse.Namespace) -> int:
    """Fill the catalogue from the top of the score ranking (ADR-014).

    The same discovery pipeline as an ordinary crawl, pointed at a different sort order.
    Nothing is hardcoded, and re-running it adds only what is missing.
    """
    if (code := _require_schema()) is not None:
        return code

    container = get_container()
    with container.uow() as uow:
        added = container.crawl.seed(uow, limit=args.limit)

    _emit({"queued": added, "limit": args.limit}, as_json=args.json)
    if added:
        print(
            f"\n  {added} games queued. This is discovery only: run the worker to fetch\n"
            "  their details and reviews, at 2 requests per second."
        )
    return EXIT_OK


def _summarise_one(container, uow, game) -> dict[str, str]:
    from app.domain.enums import Audience

    lead = next((gp for gp in game.platforms if gp.is_lead), None)
    if lead is None:
        return {"error": "no lead platform"}
    out = {}
    for audience in (Audience.CRITIC, Audience.USER):
        outcome = container.summaries.generate(
            uow, game_id=game.id, game_platform_id=lead.id, audience=audience
        )
        out[audience.value] = f"{outcome.status.value} ({outcome.reason or '-'})"
    return out


def cmd_summarise(args: argparse.Namespace) -> int:
    """Generate summaries now, without waiting for the queue.

    `--slug` generates inline, for one game, with an operator watching. `--all` queues
    the work instead -- see the comment where it does, which is about a race it lost.

    `--all` exists because of a gap the prompt-version bump exposed. A summary's
    fingerprint includes the prompt version, so changing the prompt makes every existing
    summary stale -- but nothing *enqueues* the rewrite. `summary.generate` is queued by
    `reviews.sync`, which only runs when the reviews themselves change. A game nobody
    reviews again would have kept its old summary indefinitely.
    """
    if (code := _require_schema()) is not None:
        return code

    settings = get_settings()
    if not settings.llm_enabled:
        print("LLM_ENABLED is false; nothing to do.", file=sys.stderr)
        return EXIT_USAGE

    if not args.all and not args.slug:
        print("pass --slug SLUG or --all", file=sys.stderr)
        return EXIT_USAGE

    container = get_container()

    if args.slug:
        with container.uow() as uow:
            game = uow.games.get_by_slug(args.slug)
            if game is None:
                print(f"no game with slug {args.slug!r}", file=sys.stderr)
                return EXIT_USAGE
            results = _summarise_one(container, uow, game)
        _emit({"game": args.slug, **results}, as_json=args.json)
        return EXIT_OK

    # --all ENQUEUES; it does not generate. Generating here raced the worker, which was
    # regenerating the same summaries at the same time: both computed the same
    # fingerprint, both wrote, and the second hit uq_summary_fingerprint. 29 games out of
    # 40 failed that way.
    #
    # The queue already solves this. One worker takes one job at a time, the enqueue is
    # idempotent by key, and the daily cost ceiling applies where it always has. The key
    # carries the prompt version, so a refresh happens once per prompt change rather than
    # once per invocation -- running this twice is free.
    from app.domain.enums import Audience

    # The key carries the prompt version AND the date, and it needs both.
    #
    # Version alone was spent permanently by a budget stop: a job that reaches the daily
    # ceiling is marked succeeded having done nothing, so the pairs it skipped could never
    # be asked again for that prompt.
    #
    # Date alone deduplicated against the same day's earlier run. Those jobs had already
    # executed under the code as it was before `recipe_changed` existed, decided not to
    # regenerate, and succeeded -- so re-running the command after fixing the code queued
    # nothing for them and the fix could not land until tomorrow.
    #
    # Together: idempotent within a day for a given recipe, immediately re-queueable when
    # the recipe changes, and resumable the next day when the money runs out.
    queued = 0
    with container.uow() as uow:
        today = dt.date.today().isoformat()
        recipe = f"{container.settings.prompt_version_critic}"
        for slug in sorted(uow.games.known_slugs())[: args.limit]:
            game = uow.games.get_by_slug(slug)
            if game is None:
                continue

            # EVERY platform that already has a summary, not just the lead. Only the lead
            # was covered, so a game's other platforms kept summaries written by the old
            # prompt -- and until the presenter was fixed, one of those was what the page
            # showed.
            platforms = [
                gp
                for gp in game.platforms
                if gp.is_lead
                or any(
                    uow.summaries.current(game_platform_id=gp.id, audience=a)
                    for a in (Audience.CRITIC, Audience.USER)
                )
            ]

            for platform in platforms:
                for audience in (Audience.CRITIC, Audience.USER):
                    if uow.jobs.enqueue(
                        job_type="summary.generate",
                        idempotency_key=(
                            f"summary:{platform.id}:{audience.value}"
                            f":refresh:{recipe}:{today}"
                        ),
                        queue="ai",
                        game_id=game.id,
                        payload={
                            "game_id": game.id,
                            "game_platform_id": platform.id,
                            "audience": audience.value,
                        },
                        priority=3,
                    ).created:
                        queued += 1

    _emit({"queued": queued}, as_json=args.json)
    if queued:
        print(
            f"\n  {queued} summaries queued for rewriting. The worker takes them one at "
            "a time,\n  and stops for the day when AI_DAILY_COST_LIMIT_USD is reached."
        )
    return EXIT_OK


def cmd_translate(args: argparse.Namespace) -> int:
    """Queue a Russian rendering of the source description for every game that lacks one.

    `game.sync` queues this for a game it has just written, which covers new games and
    changed descriptions. It does not cover the catalogue that already existed when the
    site became Russian — the same gap the prompt-version bump had, for the same reason:
    the work is queued by the thing that changes the data, and nothing changed.

    Queues rather than generates, so it cannot race the worker. The idempotency key is
    per game, so running it twice is free.
    """
    if (code := _require_schema()) is not None:
        return code

    settings = get_settings()
    if not settings.llm_enabled:
        print("LLM_ENABLED is false; nothing to do.", file=sys.stderr)
        return EXIT_USAGE

    container = get_container()
    queued = 0
    with container.uow() as uow:
        for slug in sorted(uow.games.known_slugs())[: args.limit]:
            game = uow.games.get_by_slug(slug)
            if game is None or not game.description or game.description_ru:
                continue
            if uow.jobs.enqueue(
                job_type="game.translate",
                idempotency_key=f"game.translate:{game.id}",
                queue="ai",
                game_id=game.id,
                payload={"game_id": game.id},
                priority=4,
            ).created:
                queued += 1

    _emit({"queued": queued}, as_json=args.json)
    if queued:
        print(
            f"\n  {queued} descriptions queued for translation. One model call each, "
            "behind\n  the same daily cost ceiling as the summaries."
        )
    return EXIT_OK


def cmd_recompute(args: argparse.Namespace) -> int:
    """Recompute similarity for every game.

    The reason on a similar-game chip is *stored* when similarity is computed, not derived
    when the page renders, so changing `reason_for` reaches new rows only. Recomputing is
    arithmetic -- no model calls, no source requests -- so this is cheap and safe to run
    whenever the rule changes.

    The third command of its shape, after `summarise --all` and `translate`, and for the
    third time the reason is the same: work is queued by whatever changes the data, and a
    change to the *code* changes no data. The first attempt at this was an INSERT into the
    jobs table by hand, which silently enqueued nothing.
    """
    if (code := _require_schema()) is not None:
        return code

    container = get_container()
    queued = 0
    today = dt.date.today().isoformat()
    with container.uow() as uow:
        for slug in sorted(uow.games.known_slugs())[: args.limit]:
            game = uow.games.get_by_slug(slug)
            if game is None:
                continue
            if uow.jobs.enqueue(
                job_type="similarity.recompute",
                idempotency_key=f"similarity:{game.id}:recompute:{today}",
                queue="enrich",
                game_id=game.id,
                payload={"game_id": game.id},
                priority=4,
            ).created:
                queued += 1

    _emit({"queued": queued}, as_json=args.json)
    if queued:
        print(f"\n  {queued} games queued for similarity recompute. No model calls.")
    return EXIT_OK


def cmd_purge(args: argparse.Namespace) -> int:
    """Retention, run by hand. The scheduler does this hourly; this is for a one-off."""
    if (code := _require_schema()) is not None:
        return code

    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    container = get_container()
    with container.uow() as uow:
        removed = {
            "events": uow.events.purge_old(
                now - dt.timedelta(days=settings.events_retention_days)
            ),
            "jobs": uow.jobs.purge_old(
                now - dt.timedelta(days=settings.jobs_retention_days)
            ),
            "snapshots": uow.snapshots.purge_unreferenced(
                now - dt.timedelta(days=settings.snapshot_retention_days)
            ),
        }
    _emit(removed, as_json=args.json)
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="health, counts, cost, queue depth").set_defaults(
        func=cmd_status
    )

    crawl = sub.add_parser("crawl", help="run one crawl tick inline")
    crawl.add_argument(
        "--once", action="store_true", help="accepted for symmetry; a tick is always one"
    )
    crawl.add_argument(
        "--max-games",
        type=int,
        default=None,
        help="lower the per-run ceiling for this run. It can never raise it.",
    )
    crawl.set_defaults(func=cmd_crawl)

    seed = sub.add_parser("seed", help="queue the top-scoring games (ADR-014)")
    seed.add_argument("--limit", type=int, default=500)
    seed.set_defaults(func=cmd_seed)

    summarise = sub.add_parser("summarise", help="generate summaries, one game or all")
    summarise.add_argument("--slug")
    summarise.add_argument(
        "--all",
        action="store_true",
        help="every game; skips summaries whose fingerprint still matches",
    )
    summarise.add_argument("--limit", type=int, default=1000)
    summarise.set_defaults(func=cmd_summarise)

    translate = sub.add_parser(
        "translate", help="queue Russian descriptions for games that have none"
    )
    translate.add_argument("--limit", type=int, default=1000)
    translate.set_defaults(func=cmd_translate)

    recompute = sub.add_parser(
        "recompute", help="recompute similarity for every game (no model calls)"
    )
    recompute.add_argument("--limit", type=int, default=5000)
    recompute.set_defaults(func=cmd_recompute)

    sub.add_parser("purge", help="apply retention now").set_defaults(func=cmd_purge)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
