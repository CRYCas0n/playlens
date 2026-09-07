"""`.env.example` is documentation that rots silently.

A setting added to ``Settings`` and forgotten here is invisible to whoever deploys the
service: they cannot configure what they do not know exists. This test is the only thing
that notices.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

#: Settings deliberately absent from the example file, each for a stated reason.
INTENTIONALLY_ABSENT = {
    # Version and name are build identity, not deployment configuration.
    "app_name",
    "app_version",
    # Public value from Metacritic's own frontend; documented in prose in the file
    # rather than as a variable, so nobody copies it around as if it were a credential.
    "metacritic_api_key",
    # Internal tuning with no reason to differ per deployment.
    "corpus_ordering_version",
    "corpus_sampling_version",
    "params_version",
    "db_echo",
}


@pytest.fixture(scope="module")
def env_example() -> str:
    path = Path(__file__).resolve().parents[2] / ".env.example"
    assert path.exists(), ".env.example is part of the deliverable"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def documented(env_example: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=", env_example, re.M)
    }


def test_every_setting_is_documented(documented: set[str]):
    missing = sorted(set(Settings.model_fields) - documented - INTENTIONALLY_ABSENT)
    assert missing == [], f"add these to .env.example: {missing}"


def test_nothing_documented_has_been_removed_from_settings(documented: set[str]):
    """The other direction: a variable nobody reads is worse than an undocumented one."""
    stale = sorted(documented - set(Settings.model_fields))
    assert stale == [], f"these are in .env.example but no longer exist: {stale}"


def test_no_real_secret_is_committed_in_the_example(env_example: str):
    """Driven by the declared SecretStr fields, not by name matching.

    A substring rule flags LLM_MAX_OUTPUT_TOKENS and misses anything named without the
    word; the type declaration is the fact.
    """
    from pydantic import SecretStr

    secrets = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if field.annotation is SecretStr
    }
    assert secrets, "expected at least one SecretStr setting"

    for line in env_example.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        if name.strip() in secrets:
            assert value.split("#")[0].strip() == "", f"{name} must ship empty"


def test_the_required_variable_is_marked_required(env_example: str):
    """ADMIN_TOKEN is the one setting with no working default."""
    assert "ADMIN_TOKEN=" in env_example
    assert "REQUIRED" in env_example


def test_the_dangerous_defaults_are_the_safe_ones(env_example: str):
    """Anything that spends money or touches a third party ships off."""
    for line in ("LLM_ENABLED=false", "YOUTUBE_ENABLED=false", "YT_ASR_ENABLED=false"):
        assert line in env_example
