"""Every setting must actually be read somewhere.

Two settings in this project were declared, documented, shown on the dashboard and never
read: `AI_DAILY_COST_LIMIT_USD` and `IMAGE_CACHE_MAX_MB`. A scan afterwards found ten
more. A knob that changes nothing is worse than no knob — during an incident someone will
turn it, see no effect, and conclude the system is broken.

This test is the thing that stops the eleventh.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]

#: Read by the framework or by a person, not by our code. Each needs a stated reason.
NOT_READ_BY_CODE = {
    # pydantic-settings itself selects the environment; nothing branches on it in app/.
    "app_env": "read by pydantic-settings and by the deployer, not branched on",
    "app_name": "presentation only, rendered in templates",
    "app_version": "presentation only, rendered in the OpenAPI document and /health",
}


def sources() -> str:
    parts = []
    for directory in ("app", "scripts", "migrations"):
        for path in (ROOT / directory).rglob("*"):
            if path.suffix in {".py", ".html"} and path.name != "config.py":
                parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def code() -> str:
    return sources()


@pytest.fixture(scope="module")
def config_source() -> str:
    return (ROOT / "app" / "config.py").read_text(encoding="utf-8")


def test_no_setting_is_declared_and_never_read(code: str, config_source: str):
    dead = []
    for name in Settings.model_fields:
        if name in NOT_READ_BY_CODE:
            continue
        used_outside = re.search(rf"\b{name}\b", code) is not None
        # A field may legitimately be read only by a derived property in config.py; that
        # shows up as more than one mention of the name in that file.
        used_by_property = len(re.findall(rf"\b{name}\b", config_source)) > 1
        if not (used_outside or used_by_property):
            dead.append(name)
    assert dead == [], (
        f"{len(dead)} settings are declared but never read: {dead}. "
        "Implement them, or delete them and say why in app/config.py."
    )


def test_the_exemptions_are_justified():
    """An exemption list with no reasons becomes a place to hide things."""
    for name, reason in NOT_READ_BY_CODE.items():
        assert name in Settings.model_fields, f"{name} no longer exists; drop the exemption"
        assert len(reason) > 20, f"{name} needs a real reason, not a label"


def test_removed_settings_are_documented_where_they_were(config_source: str):
    """Deleting a setting silently is its own trap: someone will put it back."""
    from app.config import _REMOVED_SETTINGS

    assert _REMOVED_SETTINGS
    for name in _REMOVED_SETTINGS:
        assert name.lower() not in Settings.model_fields, f"{name} is declared again"
    assert "Removed rather than left declared-and-unread" in config_source


def test_no_removed_setting_lingers_in_the_example_file():
    from app.config import _REMOVED_SETTINGS

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in _REMOVED_SETTINGS:
        assert f"\n{name}=" not in f"\n{example}", f"{name} still in .env.example"


def test_no_removed_setting_lingers_in_compose():
    from app.config import _REMOVED_SETTINGS

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in _REMOVED_SETTINGS:
        assert f"{name}:" not in compose, f"{name} still passed by docker-compose"
