"""Architectural rules that must hold regardless of who is editing.

Each one encodes a decision that is cheap to state and expensive to rediscover.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def module_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def modules_under(*parts: str) -> list[pathlib.Path]:
    return sorted((APP.joinpath(*parts)).rglob("*.py"))


class TestLayerBoundaries:
    """The business layer must not know how anything reaches it."""

    @pytest.mark.parametrize("path", modules_under("services"), ids=lambda p: p.name)
    def test_services_do_not_speak_http(self, path: pathlib.Path):
        assert not {i for i in module_imports(path) if i.split(".")[0] == "httpx"}

    @pytest.mark.parametrize("path", modules_under("services"), ids=lambda p: p.name)
    def test_services_do_not_write_sql(self, path: pathlib.Path):
        offenders = {i for i in module_imports(path) if i.split(".")[0] == "sqlalchemy"}
        assert not offenders, f"{path.name} imports SQLAlchemy; use a repository"

    @pytest.mark.parametrize("path", modules_under("domain"), ids=lambda p: p.name)
    def test_domain_is_dependency_free(self, path: pathlib.Path):
        allowed_prefixes = ("app.domain", "app.normalizers")
        for imported in module_imports(path):
            root = imported.split(".")[0]
            assert root not in {"httpx", "sqlalchemy", "fastapi"}, path.name
            if root == "app":
                assert imported.startswith(allowed_prefixes), f"{path.name} -> {imported}"

    @pytest.mark.parametrize("path", modules_under("parsers"), ids=lambda p: p.name)
    def test_parsers_do_no_io(self, path: pathlib.Path):
        for imported in module_imports(path):
            root = imported.split(".")[0]
            assert root not in {"httpx", "sqlalchemy"}, f"{path.name} performs IO"

    @pytest.mark.parametrize("path", modules_under("normalizers"), ids=lambda p: p.name)
    def test_normalizers_are_pure(self, path: pathlib.Path):
        for imported in module_imports(path):
            assert imported.split(".")[0] not in {"httpx", "sqlalchemy", "app"}


class TestSourceIsolation:
    """ADR-001: no dependency on the unofficial API may leak out of its adapter."""

    def test_only_the_adapter_mentions_the_internal_host(self):
        offenders = [
            path.relative_to(APP).as_posix()
            for path in APP.rglob("*.py")
            if "backend.metacritic" in path.read_text(encoding="utf-8")
            and not path.as_posix().endswith(("config.py",))
            and "adapters/metacritic" not in path.as_posix()
        ]
        assert not offenders, f"internal API host leaked into {offenders}"

    def test_no_tailwind_class_selectors_in_html_parsing(self):
        """Generated utility classes are not a contract and must never be selected on."""
        watched = list(modules_under("parsers")) + list(modules_under("adapters", "metacritic"))
        banned = ("class_=", "select_one(", ".select(", "find_all(", "css=", "shrink-0", "grow-0")
        offenders = []
        for path in watched:
            text = path.read_text(encoding="utf-8")
            offenders += [f"{path.name}:{needle}" for needle in banned if needle in text]
        assert not offenders, f"CSS-class based extraction found: {offenders}"
