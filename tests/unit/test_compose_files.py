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


class TestTheRenderBlueprint:
    """`render.yaml` describes the deployment. NOT VERIFIED: never applied — no account
    exists and creating one is the owner's action. What is checked is everything that
    can be checked without one."""

    @pytest.fixture(scope="class")
    def blueprint(self) -> dict:
        path = ROOT / "render.yaml"
        assert path.exists(), "the deployment blueprint is part of the deliverable"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_it_declares_a_database_a_web_service_a_worker_and_a_schedule(self, blueprint):
        assert [d["name"] for d in blueprint["databases"]] == ["playlens-db"]
        kinds = {s["type"] for s in blueprint["services"]}
        assert kinds == {"web", "worker", "cron"}

    def test_no_secret_is_written_into_the_file(self, blueprint):
        """`sync: false` means Render prompts for it; a literal would be a committed key."""
        for service in blueprint["services"]:
            for var in service["envVars"]:
                if var["key"] in {"LLM_API_KEY", "YOUTUBE_API_KEY"}:
                    assert var.get("sync") is False, f"{var['key']} must not be committed"
                    assert "value" not in var

    def test_the_admin_token_is_generated_rather_than_defaulted(self, blueprint):
        web = next(s for s in blueprint["services"] if s["type"] == "web")
        token = next(v for v in web["envVars"] if v["key"] == "ADMIN_TOKEN")
        assert token.get("generateValue") is True

    def test_every_service_gets_the_same_database(self, blueprint):
        for service in blueprint["services"]:
            url = next(v for v in service["envVars"] if v["key"] == "DATABASE_URL")
            assert url["fromDatabase"]["name"] == "playlens-db"

    def test_production_is_the_declared_environment(self, blueprint):
        for service in blueprint["services"]:
            env = next(v for v in service["envVars"] if v["key"] == "APP_ENV")
            assert env["value"] == "production"

    def test_the_health_check_points_at_the_health_endpoint(self, blueprint):
        web = next(s for s in blueprint["services"] if s["type"] == "web")
        assert web["healthCheckPath"] == "/api/v1/health"

    def test_the_cron_command_is_one_the_cli_actually_has(self, blueprint):
        """A schedule calling a command that does not exist fails once an hour, quietly."""
        from app.cli import build_parser

        cron = next(s for s in blueprint["services"] if s["type"] == "cron")
        command = cron["dockerCommand"].split()
        assert command[:3] == ["python", "-m", "app.cli"]
        build_parser().parse_args(command[3:])  # raises SystemExit if unknown

    def test_the_entrypoint_runs_an_explicit_command_instead_of_switching_on_role(self):
        """Render's cron passes a command. Without this branch it would hit the
        unknown-role case and exit 64 every hour."""
        script = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert 'if [ "$#" -gt 0 ]' in script
        assert 'exec "$@"' in script
        assert script.index('exec "$@"') < script.index('case "${ROLE')

    def test_the_web_port_comes_from_the_platform(self):
        """Render, Railway, Heroku and Cloud Run all set $PORT and route to it. A
        hardcoded port means the health check never connects and the deploy rolls back
        with "no open ports detected"."""
        script = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "${PORT:-8000}" in script
        assert "--port 8000" not in script


class TestTheSingleContainerImage:
    """The root `Dockerfile` runs everything in one container, for free hosts.

    Every genuinely free platform gives you exactly one container, and this service needs
    three roles running. The image is a compromise with a stated cost — one failure domain
    instead of three — and these tests check that the compromise is at least built right.
    """

    @pytest.fixture(scope="class")
    def dockerfile(self) -> str:
        path = ROOT / "Dockerfile"
        assert path.exists(), "free-tier hosts look for a Dockerfile at the repo root"
        return path.read_text(encoding="utf-8")

    def test_it_runs_the_all_in_one_entry_point(self, dockerfile: str):
        assert "app.allinone" in dockerfile

    def test_it_runs_as_uid_1000(self, dockerfile: str):
        """Hugging Face Spaces refuses to run a container as root and expects uid 1000."""
        assert "useradd -m -u 1000" in dockerfile
        assert "USER playlens" in dockerfile
        assert dockerfile.index("USER playlens") < dockerfile.index("CMD ")

    def test_the_port_is_overridable(self, dockerfile: str):
        """7860 is what Spaces routes to; anywhere setting $PORT must win."""
        assert "ENV PORT=7860" in dockerfile
        assert "${PORT}" in dockerfile

    def test_dependencies_are_installed_before_the_code(self, dockerfile: str):
        assert dockerfile.index("COPY --chown=playlens:playlens pyproject.toml") < dockerfile.index(
            "COPY --chown=playlens:playlens app "
        )

    def test_it_installs_the_drivers_the_free_stack_needs(self, dockerfile: str):
        """psycopg for Neon, Pillow so the image proxy actually resizes rather than
        passing 2.3 MB originals through."""
        assert "psycopg[binary]" in dockerfile
        assert "Pillow" in dockerfile

    def test_the_healthcheck_points_at_the_health_endpoint(self, dockerfile: str):
        assert "HEALTHCHECK" in dockerfile
        assert "/api/v1/health" in dockerfile

    def test_it_does_not_replace_the_multi_container_image(self):
        """The three-role image stays the real one; this is the fallback."""
        assert (ROOT / "docker" / "backend.Dockerfile").exists()


class TestTheAllInOneModule:
    def test_it_reads_the_platform_port(self):
        source = (ROOT / "app" / "allinone.py").read_text(encoding="utf-8")
        assert 'os.environ.get("PORT")' in source
        assert "SPACE_PORT" in source

    def test_a_crashed_thread_is_logged_and_restarted(self):
        """A background thread that dies silently kills the capability while leaving the
        process looking healthy: the site serves and nothing is ever crawled again."""
        source = (ROOT / "app" / "allinone.py").read_text(encoding="utf-8")
        assert "allinone.thread_crashed" in source
        assert "RESTART_DELAY_S" in source

    def test_the_compromise_is_documented_in_the_module_itself(self):
        source = (ROOT / "app" / "allinone.py").read_text(encoding="utf-8")
        assert "compromise" in source.lower()
        assert "docker-compose.prod.yml" in source


def test_the_images_carry_the_transcript_provider():
    """A shipped feature has to have its dependency in the image that runs it.

    yt-dlp is the first provider in the transcript cascade, and it was in no image. The
    cascade behaved correctly -- it reported the provider "disabled" and fell through --
    so nothing failed and nothing said why: the Let's Play feature ran its search, ranked
    2,285 videos in production and could never read one of them.

    Both images, because the single-container mode runs the same worker.
    """
    for name in ("docker/backend.Dockerfile", "Dockerfile"):
        body = (ROOT / name).read_text(encoding="utf-8")
        assert "yt-dlp" in body, f"{name} cannot fetch a transcript"


def test_every_optional_dependency_a_default_feature_needs_is_installed():
    """`YT_TRANSCRIPT_PROVIDERS` lists ytdlp first by default, so it is not optional in
    practice however it is packaged."""
    from app.config import Settings

    defaults = Settings(admin_token="t" * 32).yt_transcript_providers
    if "ytdlp" in defaults:
        for name in ("docker/backend.Dockerfile", "Dockerfile"):
            assert "yt-dlp" in (ROOT / name).read_text(encoding="utf-8"), name
