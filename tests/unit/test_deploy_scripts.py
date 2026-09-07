"""The deployment scripts, checked as far as text allows.

They will run on a server that already hosts a live site and an n8n beside it, and I
cannot run them here to find out what they do. So the assertions below are about the two
properties that matter when you cannot execute: that nothing destructive is written down
at all, and that every edit to a file belonging to something else is backed up and
validated before it is applied.

This is the same approach that found the $PORT and ROLE=cron bugs in the compose files
before any deploy. It proves the scripts say the right thing, not that they work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))


def body(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_the_deploy_directory_has_scripts():
    assert SCRIPTS, "deploy/ has no scripts"


class TestNothingDestructiveIsWrittenDown:
    """Not a sandbox, so this is the whole defence: the dangerous forms are absent."""

    FORBIDDEN = (
        "rm -rf /",
        "rm -fr /",
        "mkfs",
        "dd if=",
        "chmod 777",
        "docker system prune",
        "docker volume prune",
        "docker volume rm",
        "docker rm -f $(",
        "docker stop $(",
        "iptables -F",
        "ufw --force reset",
        "> /etc/",
        "PermitRootLogin",
        "PasswordAuthentication",
    )

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_no_forbidden_command(self, script: Path):
        text = script.read_text(encoding="utf-8")
        for phrase in self.FORBIDDEN:
            assert phrase not in text, f"{script.name} contains {phrase!r}"

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_deletions_are_scoped_and_named(self, script: Path):
        """A -delete is allowed only inside the backup directory, and only for files this
        project's own naming scheme produces."""
        for line in script.read_text(encoding="utf-8").splitlines():
            if "-delete" in line:
                assert "BACKUP_DIR" in line and "playlens-" in line, line


class TestTheyFailLoudly:
    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_shebang(self, script: Path):
        assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")

    @pytest.mark.parametrize(
        "script", [s for s in SCRIPTS if s.name != "preflight.sh"], ids=lambda p: p.name
    )
    def test_errexit(self, script: Path):
        """Every script but the diagnostic one stops at the first failure. preflight is
        the deliberate exception: it is read-only and should report everything it can
        even when half the commands it looks for are missing."""
        assert "set -euo pipefail" in script.read_text(encoding="utf-8")

    def test_preflight_is_read_only(self):
        """The script that runs first, on a server in use, before any trust exists.

        Comments are stripped: the file explains what it does not do, and matching that
        prose would be checking the documentation rather than the commands."""
        lines = body("preflight.sh").splitlines()
        text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        for writer in (
            "docker run",
            "docker rm",
            "docker exec",
            "docker network connect",
            "docker compose up",
            "docker compose down",
            "docker compose build",
            "docker compose restart",
            "apt-get",
            "systemctl restart",
            "systemctl start",
            "systemctl stop",
            "cp ",
            "mv ",
            "rm ",
            "> /",
        ):
            assert writer not in text, f"preflight.sh writes: {writer!r}"


class TestTheCaddyEditIsReversible:
    """The one script that touches a file belonging to a live site."""

    def test_it_backs_up_before_writing(self):
        text = body("caddy-site.sh")
        assert text.index('cp -p "$CADDYFILE" "$BACKUP"') < text.index('cat >> "$CADDYFILE"')

    def test_it_validates_before_reloading(self):
        text = body("caddy-site.sh")
        assert text.index("caddy validate") < text.index('log "reloading caddy"')

    def test_a_failed_validation_restores_the_backup(self):
        assert 'cp -p "$BACKUP" "$CADDYFILE"' in body("caddy-site.sh")

    def test_it_is_idempotent_on_the_site_name(self):
        assert 'grep -qF "$SITE" "$CADDYFILE"' in body("caddy-site.sh")

    def test_it_opens_no_new_port(self):
        """Caddy already holds 80 and 443. Needing a third port would mean a firewall
        change on a server that is not ours to reconfigure."""
        text = body("caddy-site.sh")
        assert "ufw allow" not in text
        assert "firewall-cmd" not in text


class TestBackups:
    def test_the_dump_is_named_only_after_it_succeeds(self):
        """A truncated file with the right name is worse than no file: it looks like a
        backup right up to the day you need it."""
        text = body("backup.sh")
        assert ".partial" in text
        assert 'mv "$OUT.partial" "$OUT"' in text

    def test_retention_will_not_delete_the_last_copy(self):
        assert "-mtime -1" in body("backup.sh"), "no recency guard before pruning"

    def test_restore_requires_a_typed_confirmation(self):
        assert '[ "$answer" = "restore" ]' in body("restore.sh")

    def test_restore_stops_the_writers_first(self):
        text = body("restore.sh")
        assert text.index("stop worker scheduler api") < text.index("gunzip -c")

    def test_update_backs_up_before_migrating(self):
        text = body("update.sh")
        assert text.index("deploy/backup.sh") < text.index("up -d api")

    def test_update_rolls_back_when_the_health_check_fails(self):
        assert 'git reset --hard --quiet "$PREVIOUS"' in body("update.sh")


class TestSecretsStayOut:
    KEY_SHAPES = (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"AIza[A-Za-z0-9_-]{30,}"))

    def test_install_generates_them_and_writes_them_to_a_600_file(self):
        text = body("install.sh")
        assert "openssl rand" in text
        assert "chmod 600 .env" in text
        assert "umask 077" in text

    def test_no_script_contains_a_key_shaped_string(self):
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            for shape in self.KEY_SHAPES:
                assert not shape.search(text), f"{script.name} contains a key-shaped string"

    def test_no_script_takes_a_secret_as_an_argument(self):
        """Arguments land in the shell history and in ps output."""
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            assert "LLM_API_KEY=$1" not in text
            assert "ADMIN_TOKEN=$1" not in text


class TestTheyAgreeWithTheRepository:
    def test_the_compose_pair_matches_the_runbook(self):
        for name in ("install.sh", "backup.sh", "restore.sh", "update.sh"):
            assert "-f docker-compose.yml -f docker-compose.prod.yml" in body(name), name

    def test_install_waits_on_the_real_health_route(self):
        assert "/api/v1/health" in body("install.sh")

    def test_the_upstream_matches_what_the_container_serves(self):
        assert "api:8000" in body("caddy-site.sh")
        assert "EXPOSE 8000" in (ROOT / "docker" / "backend.Dockerfile").read_text(encoding="utf-8")

    def test_the_app_is_bound_to_loopback_by_default(self):
        """Behind a proxy the app must be reachable from the host and nowhere else.
        Published on 0.0.0.0 it also answers on http://<ip>:8000 -- the same site without
        the certificate, without the proxy, and without its rate limits."""
        yaml = pytest.importorskip("yaml")
        base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        ports = base["services"]["api"]["ports"]
        assert ports == ["${API_BIND_HOST:-127.0.0.1}:${API_BIND_PORT:-8000}:8000"], ports

    def test_no_overlay_adds_a_second_binding_for_the_same_port(self):
        """`docker compose config` on the server proved this the hard way: compose
        CONCATENATES `ports` across files instead of replacing them, so an entry in the
        overlay is added to the base file's rather than substituted for it. The result
        was two bindings for port 8000, one of them on 0.0.0.0 -- exactly the exposure
        the loopback bind exists to prevent."""
        yaml = pytest.importorskip("yaml")
        for name in ("docker-compose.prod.yml", "docker-compose.dev.yml"):
            overlay = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
            api = overlay.get("services", {}).get("api", {})
            assert "ports" not in api, f"{name} adds a second binding for the api"

    def test_no_cpu_limit_exceeds_a_single_core(self):
        """Docker refuses outright: "range of CPUs is from 0.01 to 1.00, as there are
        only 1 CPUs available". The overlay asked for 1.5 and the api would not start on
        a one-core host, which is the size of host this is most likely to meet. Defaults
        must fit the smallest machine; a bigger one raises them with the variables."""
        import re as _re

        text = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        for default in _re.findall(r'cpus:\s*"\$\{[A-Z_]+:-([0-9.]+)\}"', text):
            assert float(default) <= 1.0, f"default cpus {default} will not start on 1 core"
        assert not _re.search(r'cpus:\s*"[0-9.]+"', text), "a cpus limit is hardcoded"

    def test_memory_limits_fit_a_small_host(self):
        """Limits are ceilings, not reservations, but four containers whose ceilings sum
        past the machine's RAM will still OOM together under load -- on a box that is
        also running someone else's containers."""
        import re as _re

        text = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        megabytes = [
            int(v) * (1024 if unit.upper() == "G" else 1)
            for v, unit in _re.findall(r"memory:\s*\$\{[A-Z_]+:-([0-9]+)([MG])\}", text)
        ]
        assert megabytes, "no parameterised memory limits found"
        assert sum(megabytes) <= 2560, f"defaults sum to {sum(megabytes)} MB"

    def test_the_healthcheck_probes_the_port_the_entrypoint_binds(self):
        """These disagreed: a $PORT the entrypoint honoured, a literal 8000 the
        healthcheck probed. The api would never report healthy, and both the worker and
        the scheduler wait on exactly that condition."""
        dockerfile = (ROOT / "docker" / "backend.Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "${PORT:-8000}" in dockerfile
        assert "${PORT:-8000}" in entrypoint
