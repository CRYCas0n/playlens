"""Smoke a deployed Playlens over HTTP, against the real public name.

`scripts/smoke.py` boots the app in-process and asks whether the wiring holds together.
This one asks a different question, and the only one that matters after a deploy: does
the thing on the internet work. It goes through DNS, the certificate, the reverse proxy
and the container, and it reads the bodies rather than trusting the status line -- a
proxy returning its own cheerful 200 while the app is down is exactly the failure a
status-code-only smoke cannot see.

    python -m scripts.prod_smoke https://playlens.example.com
"""

from __future__ import annotations

import re
import sys

import httpx

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Things that must never appear in a public response body.
SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"'<]+:[^\s\"'<@]+@"),
)


class Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        self.checks += 1
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {what}" + (f" -- {detail}" if detail and not ok else ""))
        if not ok:
            self.failures.append(what)
        return ok


def main(base: str) -> int:
    base = base.rstrip("/")
    r = Result()
    print(f"\nsmoking {base}\n")

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        # -------------------------------------------------- transport
        print("transport")
        if base.startswith("https://"):
            try:
                plain = client.get(base.replace("https://", "http://", 1), follow_redirects=False)
                r.check(
                    plain.status_code in (301, 302, 307, 308),
                    "http redirects to https",
                    f"got {plain.status_code}",
                )
            except httpx.HTTPError as exc:
                r.check(False, "http redirects to https", str(exc)[:80])
        else:
            r.check(False, "the public URL is https", base)

        # -------------------------------------------------- health
        print("\nhealth")
        try:
            health = client.get(f"{base}/api/v1/health")
        except httpx.HTTPError as exc:
            print(f"  FAIL cannot reach the service -- {exc}")
            return 1
        r.check(health.status_code == 200, "GET /api/v1/health is 200", str(health.status_code))
        ctype = health.headers.get("content-type", "")
        body = health.json() if ctype.startswith("application/json") else {}
        r.check(body.get("status") == "ok", "health says ok", repr(body.get("status")))
        r.check(body.get("database") in ("ok", True, None) or "not_migrated" not in str(body),
                "the database is migrated", repr(body))

        # -------------------------------------------------- api
        print("\napi")
        games = client.get(f"{base}/api/v1/games")
        r.check(games.status_code == 200, "GET /api/v1/games is 200", str(games.status_code))
        items = games.json().get("items", []) if games.status_code == 200 else []
        r.check(bool(items), "the catalogue has games", f"{len(items)} returned")

        stats = client.get(f"{base}/api/v1/stats")
        r.check(stats.status_code == 200, "GET /api/v1/stats is 200")

        mon = client.get(f"{base}/api/v1/monitoring/status")
        r.check(mon.status_code == 200, "GET /api/v1/monitoring/status is 200")
        if mon.status_code == 200:
            status = mon.json()
            r.check("status" in status, "monitoring reports a system status", repr(status)[:100])

        # -------------------------------------------------- pages
        print("\npages")
        home = client.get(f"{base}/")
        r.check(home.status_code == 200, "GET / is 200", str(home.status_code))
        r.check("<html" in home.text.lower(), "/ returns HTML")
        r.check("playlens" in home.text.lower(), "/ is actually Playlens and not a proxy page")

        for path in ("/games", "/about"):
            page = client.get(f"{base}{path}")
            r.check(page.status_code == 200, f"GET {path} is 200", str(page.status_code))

        slug = items[0].get("slug") if items else None
        if slug:
            game = client.get(f"{base}/games/{slug}")
            r.check(game.status_code == 200, f"GET /games/{slug} is 200", str(game.status_code))
            r.check(len(game.text) > 2000, "the game page has content", f"{len(game.text)} bytes")

        missing = client.get(f"{base}/games/definitely-not-a-game")
        r.check(missing.status_code == 404, "an unknown game is 404", str(missing.status_code))

        # -------------------------------------------------- security
        print("\nsecurity")
        admin = client.post(f"{base}/api/v1/admin/crawl/run", json={})
        r.check(admin.status_code == 401, "the admin API refuses an unauthenticated write",
                str(admin.status_code))

        for name in ("content-security-policy", "x-content-type-options", "referrer-policy"):
            r.check(name in {k.lower() for k in home.headers}, f"{name} is set")

        leaked = [
            p.pattern for p in SECRET_SHAPES
            for text in (home.text, games.text, health.text)
            if p.search(text)
        ]
        r.check(not leaked, "no secret-shaped string in any response", str(leaked))

    print(f"\n{r.checks - len(r.failures)}/{r.checks} passed")
    if r.failures:
        print("\nfailed:")
        for f in r.failures:
            print(f"  - {f}")
        return 1
    print("production smoke: green")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
