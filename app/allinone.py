"""Everything in one process: the web server, a worker and the scheduler.

The deployment shape this exists for is the free tier of anywhere — Hugging Face Spaces,
Koyeb, a 512 MB VPS. They give you one container, and the service needs three things
running.

**This is a compromise and worth naming as one.** Three processes exist separately for
good reasons: a crash in the worker should not take the site down, and the scheduler
should be the only one of its kind while workers scale out. One process gives up both.
Use `docker-compose.prod.yml` wherever you can run three.

What makes it acceptable here:

* The worker is entirely I/O-bound — HTTP to Metacritic, HTTP to a model, SQL — so the
  GIL costs nothing. It sleeps between polls.
* The scheduler wakes every twenty seconds to ask whether a slot is due, and enqueues by
  idempotency key. Running it twice would be harmless; running it in a thread is dull.
* A crash in either thread is logged and the thread restarts, rather than being allowed
  to take the request loop with it.

Concurrency is one job at a time, exactly as with a separate worker process. The unit of
concurrency is still the process; there is simply only one of them.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from typing import Any

from app.config import get_settings
from app.logging import configure_logging, get_logger

log = get_logger(__name__)

#: How long a crashed loop waits before restarting. Long enough not to spin on a
#: permanent failure, short enough that a transient one costs one cycle.
RESTART_DELAY_S = 15.0

_stop = threading.Event()


def _supervise(name: str, run: Any) -> None:
    """Run a loop forever, restarting it if it dies.

    A background thread that raises silently kills the capability and leaves the process
    looking healthy — the site keeps serving and nothing is ever crawled again. So the
    exception is logged loudly and the loop comes back.
    """
    while not _stop.is_set():
        try:
            log.info("allinone.thread_started", extra={"thread": name})
            run()
            # A clean return means the loop decided to stop; do not restart it.
            log.info("allinone.thread_finished", extra={"thread": name})
            return
        except Exception as exc:
            log.exception(
                "allinone.thread_crashed",
                extra={"thread": name, "error": str(exc)[:300], "restart_in_s": RESTART_DELAY_S},
            )
            if _stop.wait(RESTART_DELAY_S):
                return


def _worker_loop() -> None:
    from app.container import get_container
    from app.queue.worker import Worker

    Worker(get_container()).run()


def _scheduler_loop() -> None:
    from app.container import get_container
    from app.queue.scheduler import Scheduler

    Scheduler(get_container()).run()


def start_background_threads() -> list[threading.Thread]:
    """Start the worker and the scheduler. Returns them so a caller can join."""
    settings = get_settings()
    threads: list[threading.Thread] = []

    for name, target in (("worker", _worker_loop), ("scheduler", _scheduler_loop)):
        if name == "scheduler" and not settings.crawl_enabled:
            log.info("allinone.scheduler_disabled", extra={"reason": "CRAWL_ENABLED=false"})
            continue
        thread = threading.Thread(
            target=_supervise, args=(name, target), name=f"playlens-{name}", daemon=True
        )
        thread.start()
        threads.append(thread)

    return threads


def main() -> int:
    settings = get_settings()
    configure_logging(settings)

    # Migrations first, and in this process only: there is no other process to race with,
    # which is the one simplification this mode genuinely buys.
    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    log.info("allinone.migrating")
    command.upgrade(cfg, "head")

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("allinone.stopping", extra={"signal": signum})
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not every host lets a container install handlers, and this may not be the main
        # thread. Failing to register one is not a reason to refuse to start.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _shutdown)

    start_background_threads()

    # The port comes from the platform. Hugging Face Spaces uses 7860, Koyeb and Render
    # set $PORT, a plain container gets 8000.
    port = int(os.environ.get("PORT") or os.environ.get("SPACE_PORT") or 8000)

    import uvicorn

    from app.main import create_app

    log.info(
        "allinone.serving",
        extra={"port": port, "queues": settings.queues, "crawl": settings.crawl_enabled},
    )
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_config=None)
    _stop.set()
    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
