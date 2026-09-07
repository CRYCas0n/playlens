"""Re-run the Release 0 validation against a live model.

Release 0 measured how good the AI summaries actually are. Its numbers were produced
interactively, and the report itself calls them a **ceiling** rather than a measurement of
this system. This script is what turns them into a measurement: it runs the real pipeline
over the same twenty games, on whatever model is configured, and reports the two figures
the report defined.

    LLM_ENABLED=true LLM_API_KEY=sk-ant-... make release0

What it prints, and what the numbers mean:

* **PV1 — claim-level accuracy.** Of the claims the model produced, what share survived
  the validator: enough cited reviews, aspect vocabulary actually present in them, no
  invented temporal or comparative statement. Release 0 set the bar at **80%**.
* **PV2 — block-level accuracy.** Of the summaries produced, what share came out
  publishable at all rather than rejected. Release 0 set the bar at **85%**.

Neither number says the summaries are *useful*. That question needs a person, and
``docs/HUMAN_EVALUATION.md`` is the twenty-minute procedure for answering it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARVEST = REPO_ROOT / "docs" / "research-fixtures" / "release0" / "harvest"

PV1_TARGET = 80.0
PV2_TARGET = 85.0

#: Matches what the worker gives summary.generate, so the measurement reflects production
#: rather than a stricter version of it.
RETRIES = 6


def games() -> list[str]:
    """The Release 0 sample, in a fixed order so two runs are comparable."""
    return sorted(p.stem for p in HARVEST.glob("*.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "research-fixtures" / "release0" / "rerun"),
        help="where to write the report (default: docs/research-fixtures/release0/rerun)",
    )
    parser.add_argument("--limit", type=int, default=0, help="stop after N games")
    parser.add_argument(
        "--load-fixtures",
        action="store_true",
        help=(
            "load the Release 0 harvest into the database first. Reproduces the original "
            "measurement byte for byte, and needs no crawl."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use the fixture model instead of a real one; proves the wiring, costs nothing",
    )
    args = parser.parse_args()

    from app.config import get_settings

    settings = get_settings()
    if not args.dry_run and not settings.llm_enabled:
        print(
            "LLM_ENABLED is false, so this would measure nothing.\n"
            "Either set LLM_ENABLED=true with an LLM_API_KEY, or pass --dry-run to "
            "check the plumbing with the fixture model.",
            file=sys.stderr,
        )
        return 2

    from sqlalchemy import create_engine

    from app.adapters.llm.base import NullLLMProvider
    from app.adapters.llm.fixture import FixtureLLMProvider
    from app.db.base import make_session_factory
    from app.db.uow import UnitOfWork
    from app.domain.enums import Audience, SummaryStatus
    from app.domain.errors import RetryableError
    from app.services.summary_service import SummaryService

    if args.dry_run:
        llm = FixtureLLMProvider()
    else:
        from app.container import get_container

        llm = get_container().llm
        if isinstance(llm, NullLLMProvider):
            print("no usable LLM provider; check LLM_API_KEY", file=sys.stderr)
            return 2

    engine = create_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    slugs = games()
    if args.load_fixtures:
        from app.db.seed import seed_platforms
        from scripts.harvest import load_harvest

        session = session_factory()
        seed_platforms(session)
        session.commit()
        loader = load_harvest(session)
        loaded = 0
        for slug in slugs:
            try:
                loader(slug)
                loaded += 1
            except Exception as exc:  # a malformed harvest file is not a run failure
                print(f"  could not load {slug}: {exc}")
        session.close()
        print(f"loaded {loaded} harvest games\n")

    if args.limit:
        slugs = slugs[: args.limit]
    if not slugs:
        print(f"no harvest files in {HARVEST}", file=sys.stderr)
        return 2

    service = SummaryService(llm, settings)
    rows: list[dict] = []

    print(f"running {len(slugs)} games through {llm.name}/{llm.model}\n")
    for slug in slugs:
        with UnitOfWork(session_factory) as uow:
            game = uow.games.get_by_slug(slug)
            if game is None:
                rows.append({"slug": slug, "skipped": "not in the database"})
                print(f"  {slug:<34} not indexed -- crawl it first")
                continue
            lead = next((gp for gp in game.platforms if gp.is_lead), None)
            if lead is None:
                rows.append({"slug": slug, "skipped": "no lead platform"})
                continue

            for audience in (Audience.CRITIC, Audience.USER):
                # The worker reschedules a retryable failure; this script has no worker,
                # so it does the same thing inline. Without it a single transient model
                # hiccup ends the whole measurement run.
                outcome = None
                for attempt in range(1, RETRIES + 1):
                    try:
                        outcome = service.generate(
                            uow,
                            game_id=game.id,
                            game_platform_id=lead.id,
                            audience=audience,
                        )
                        break
                    except RetryableError as exc:
                        if attempt == RETRIES:
                            rows.append(
                                {
                                    "slug": slug,
                                    "audience": audience.value,
                                    "status": "failed",
                                    "reason": f"retries exhausted: {exc}"[:200],
                                    "claims_total": 0,
                                    "claims_accepted": 0,
                                    "cost_usd": 0.0,
                                }
                            )
                            print(f"  {slug:<34} {audience.value:<7} failed after {RETRIES}")
                        else:
                            print(f"  {slug:<34} {audience.value:<7} retry {attempt}")
                if outcome is None:
                    continue
                summary = uow.summaries.current(
                    game_platform_id=lead.id, audience=audience
                )
                claims = uow.summaries.accepted_claims(summary.id) if summary else []
                total_claims = len(summary.claims) if summary else 0
                rows.append(
                    {
                        "slug": slug,
                        "audience": audience.value,
                        "status": outcome.status.value,
                        "reason": outcome.reason,
                        "claims_total": total_claims,
                        "claims_accepted": len(claims),
                        "cost_usd": float(summary.cost_usd or 0) if summary else 0.0,
                    }
                )
                print(
                    f"  {slug:<34} {audience.value:<7} {outcome.status.value:<18} "
                    f"{len(claims)}/{total_claims} claims"
                )

    engine.dispose()

    generated = [r for r in rows if r.get("status") in {"fresh", "rejected"}]
    claims_total = sum(r.get("claims_total", 0) for r in generated)
    claims_ok = sum(r.get("claims_accepted", 0) for r in generated)
    published = [r for r in generated if r["status"] == SummaryStatus.FRESH.value]

    pv1 = 100 * claims_ok / claims_total if claims_total else 0.0
    pv2 = 100 * len(published) / len(generated) if generated else 0.0
    cost = sum(r.get("cost_usd", 0.0) for r in rows)

    report = {
        "ran_at": dt.datetime.now(dt.UTC).isoformat(),
        "model": f"{llm.name}/{llm.model}",
        "dry_run": bool(args.dry_run),
        "prompt_versions": {
            "critic": settings.prompt_version_critic,
            "user": settings.prompt_version_user,
        },
        "params_version": settings.params_version,
        "games": len(slugs),
        "summaries_attempted": len(generated),
        "summaries_published": len(published),
        "claims_total": claims_total,
        "claims_accepted": claims_ok,
        "pv1_claim_level_pct": round(pv1, 1),
        "pv2_block_level_pct": round(pv2, 1),
        "pv1_target_pct": PV1_TARGET,
        "pv2_target_pct": PV2_TARGET,
        "cost_usd": round(cost, 4),
        "rows": rows,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"release0-{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 62)
    print(f"PV1 claim-level   {pv1:5.1f}%   target {PV1_TARGET}%   "
          f"{'PASS' if pv1 >= PV1_TARGET else 'BELOW TARGET'}")
    print(f"PV2 block-level   {pv2:5.1f}%   target {PV2_TARGET}%   "
          f"{'PASS' if pv2 >= PV2_TARGET else 'BELOW TARGET'}")
    print(f"cost              ${cost:.4f}")
    print("=" * 62)
    print(f"\nwritten to {path}")
    print(
        "\nThese two numbers say the summaries are CHECKABLE, not that they are USEFUL.\n"
        "For the second question see docs/HUMAN_EVALUATION.md -- twenty minutes with a\n"
        "person who did not write them."
    )
    # Deliberately 0 even below target: this is a measurement, and a build that fails
    # because a model had a bad day teaches nothing.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
