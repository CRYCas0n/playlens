"""The compose files, checked as far as they can be without a Docker daemon.

No daemon was available, so nothing here proves the stack runs. What it does prove is
that the files parse, that the services and roles line up, and — the one that matters —
that no file publishes the database to the host by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
PROD = ROOT / "docker-compose.prod.yml"
DEV = ROOT / "docker-compose.dev.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestTheyParse:
    @pytest.mark.parametrize("path", [BASE, PROD, DEV])
    def test_valid_yaml(self, path: Path):
        assert isinstance(load(path), dict)

    def test_no_compose_specific_tags_that_plain_yaml_cannot_check(self):
        """An extension tag would make these files unverifiable in this environment, and
        an unverifiable security control is not a control."""
        for path in (BASE, PROD, DEV):
            body = path.read_text(encoding="utf-8")
            assert "!override" not in body
            assert "!reset" not in body


class TestTheDatabaseIsNotExposed:
    def test_the_base_file_does_not_publish_postgres(self):
        """The one that would matter on a public host: an open PostgreSQL."""
        assert "ports" not in load(BASE)["services"]["postgres"]

    def test_the_production_overlay_does_not_either(self):
        assert "ports" not in load(PROD)["services"]["postgres"]

    def test_only_the_dev_overlay_publishes_it_and_only_to_loopback(self):
        ports = load(DEV)["services"]["postgres"]["ports"]
        assert ports == ["127.0.0.1:5432:5432"], (
            "publishing to 0.0.0.0 would put the database on every interface"
        )


class TestTheServicesLineUp:
    def test_the_base_file_has_all_four_roles(self):
        services = load(BASE)["services"]
        assert set(services) == {"postgres", "api", "worker", "scheduler"}

    def test_each_role_is_one_the_entrypoint_understands(self):
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        for name, service in load(BASE)["services"].items():
            role = (service.get("environment") or {}).get("ROLE")
            if role is None:
                continue
            assert f"  {role})" in entrypoint, f"{name} uses ROLE={role}, unhandled"

    def test_the_overlay_only_names_services_the_base_defines(self):
        base = set(load(BASE)["services"])
        for path in (PROD, DEV):
            assert set(load(path)["services"]) <= base, f"{path.name} invents a service"

    def test_exactly_one_scheduler_in_production(self):
        """Two would enqueue every tick twice. The duplicate is skipped by the idempotency
        key rather than run, but the second process only adds a way to be confused."""
        assert load(PROD)["services"]["scheduler"]["deploy"]["replicas"] == 1


class TestSecrets:
    def test_no_compose_file_contains_a_literal_secret(self):
        for path in (BASE, PROD, DEV):
            body = path.read_text(encoding="utf-8")
            assert "sk-ant-" not in body
            assert "AIza" not in body

    def test_production_requires_its_credentials_rather_than_defaulting_them(self):
        """`${VAR:?...}` fails the command; `${VAR:-default}` silently ships a default
        password, which is how demo credentials reach production."""
        body = PROD.read_text(encoding="utf-8")
        for name in ("POSTGRES_USER", "POSTGRES_PASSWORD"):
            assert f"${{{name}:?" in body, f"{name} must be required in production"

    def test_the_base_file_requires_an_admin_token(self):
        body = BASE.read_text(encoding="utf-8")
        assert "${ADMIN_TOKEN:?" in body


class TestTheDockerfile:
    @pytest.fixture(scope="class")
    def dockerfile(self) -> str:
        return (ROOT / "docker" / "backend.Dockerfile").read_text(encoding="utf-8")

    def test_it_runs_as_a_non_root_user(self, dockerfile: str):
        assert "USER playlens" in dockerfile
        assert dockerfile.index("USER playlens") > dockerfile.index("useradd")

    def test_it_pins_a_python_version(self, dockerfile: str):
        assert "FROM python:3.11" in dockerfile

    def test_it_has_a_healthcheck_pointing_at_the_health_endpoint(self, dockerfile: str):
        assert "HEALTHCHECK" in dockerfile
        assert "/api/v1/health" in dockerfile

    def test_dependencies_are_installed_before_the_code_is_copied(self, dockerfile: str):
        """Otherwise every one-line change re-downloads the whole dependency tree."""
        assert dockerfile.index("COPY pyproject.toml") < dockerfile.index("COPY app ")

    def test_the_entrypoint_it_names_exists_and_is_a_shell_script(self, dockerfile: str):
        assert "entrypoint.sh" in dockerfile
        script = ROOT / "docker" / "entrypoint.sh"
        assert script.exists()
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh")

    def test_migrations_run_in_exactly_one_role(self):
        """Three containers racing for Alembic's lock on a cold start is how a worker
        ends up crash-looping against a half-built schema."""
        script = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert script.count("alembic upgrade head") == 1
        api_block = script.split("api)")[1].split(";;")[0]
        assert "alembic upgrade head" in api_block


def test_the_dockerignore_keeps_the_build_context_small():
    body = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for entry in (".venv/", ".git/", "tests/", ".env"):
        assert entry in body, f"{entry} would be copied into the image"
