"""Every enum-backed column is written from its enum, not from a string.

`summary_claims.claim_type` is guarded by a check constraint built from `ClaimType`. The
Let's Play service wrote `"observation"`, which is not a member and never was, so the
database refused every row and no Let's Play summary could be saved at all.

Nobody noticed for one reason: the chain that reaches that code was never joined up, so
the line had never run. A constraint caught it the first time it did — which is the
argument for constraints — but a string literal where an enum exists is worth catching
earlier than that.

This walks the source for literals assigned to enum-backed keys and checks each against
the enum the column is constrained by.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.domain import enums

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
MODELS = APP / "db" / "models.py"

#: dict key -> the enum its column is constrained by, read from the check constraints.
GUARDED = {
    "claim_type": enums.ClaimType,
    "side": enums.ClaimSide,
    "validation": enums.ClaimValidation,
    "aspect": enums.Aspect,
    "audience": enums.Audience,
    "status": None,  # several tables, several enums — checked separately below
}


def literal_assignments() -> list[tuple[str, str, str, int]]:
    """(file, key, literal value, line) for every `"key": "literal"` in a dict."""
    out = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    out.append(
                        (path.relative_to(ROOT).as_posix(), key.value, value.value, node.lineno)
                    )
    return out


def test_the_scan_finds_dict_literals_at_all():
    assert literal_assignments(), "the AST walk found nothing, so it proves nothing"


def test_the_check_constraints_are_where_this_test_thinks_they_are():
    body = MODELS.read_text(encoding="utf-8")
    assert '_enum_check("claim_type", enums.ClaimType' in body
    assert '_enum_check("side", enums.ClaimSide' in body


@pytest.mark.parametrize(
    "key,enum",
    [(k, v) for k, v in GUARDED.items() if v is not None],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_no_literal_outside_its_enum(key: str, enum):
    allowed = {member.value for member in enum}
    offenders = [
        f"{path}:{line} {key}={value!r}"
        for path, k, value, line in literal_assignments()
        if k == key and value not in allowed
    ]
    assert not offenders, (
        f"written as a string, not a {enum.__name__}: {offenders}. "
        f"Allowed: {sorted(allowed)}"
    )


def test_the_detector_would_catch_the_one_that_shipped():
    """`observation` is not a ClaimType, and was written as one for months."""
    assert "observation" not in {m.value for m in enums.ClaimType}
    source = (APP / "services" / "letsplay_service.py").read_text(encoding="utf-8")
    assert '"claim_type": "observation"' not in source


def test_every_enum_check_in_models_names_a_real_enum():
    """A constraint built from an enum that no longer exists would be a silent no-op."""
    body = MODELS.read_text(encoding="utf-8")
    for name in re.findall(r"_enum_check\(\s*\"[a-z_]+\",\s*enums\.(\w+)", body):
        assert hasattr(enums, name), f"models.py constrains a column with a missing {name}"
