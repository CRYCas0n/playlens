"""Boot the application and request every route once.

Not a substitute for the test suite: this is the check that the *assembled* application
starts -- migrations, container, routers, templates and static files together. A unit test
can pass against a wiring that never boots.
"""

from __future__ import annotations

import sys

ROUTES = [
    ("GET", "/api/v1/health", 200),
    ("GET", "/api/v1/openapi.json", 200),
    ("GET", "/api/v1/games", 200),
    ("GET", "/api/v1/platforms", 200),
    ("GET", "/api/v1/genres", 200),
    ("GET", "/api/v1/stats", 200),
    ("GET", "/api/v1/monitoring/status", 200),
    ("GET", "/api/v1/monitoring/runs", 200),
    ("GET", "/api/v1/metrics", 200),
    ("GET", "/", 200),
    ("GET", "/games", 200),
    ("GET", "/about", 200),
    ("GET", "/admin/monitoring", 200),
    ("GET", "/games/definitely-not-a-game", 404),
    ("POST", "/api/v1/admin/crawl/run", 401),
]


def main() -> int:
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    settings = get_settings()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")

    failures = []
    with TestClient(create_app()) as client:
        for method, path, expected in ROUTES:
            response = client.request(method, path)
            ok = response.status_code == expected
            print(
                f"{'ok  ' if ok else 'FAIL'} {response.status_code:>3} "
                f"{method:<4} {path}  ({len(response.content)} bytes)"
            )
            if not ok:
                failures.append(f"{method} {path}: expected {expected}, got {response.status_code}")

    if failures:
        print("\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print(f"\n{len(ROUTES)} routes ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
