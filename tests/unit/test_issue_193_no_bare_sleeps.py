"""Regression coverage for shared deadline-based test waits."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.wait import wait_until

TESTS = Path(__file__).parents[1]
SLEEP_ALLOWLIST = {
    Path("e2e/conftest.py"),
    Path("e2e/daemon.py"),
    Path("e2e/stub_model.py"),
    Path("e2e/test_admin_live.py"),
    Path("e2e/test_class_routing.py"),
    Path("e2e/test_command_timeout.py"),
    Path("e2e/test_failures.py"),
    Path("e2e/test_github_live.py"),
    Path("e2e/test_kind.py"),
    Path("e2e/test_kind_self_hosting.py"),
    Path("e2e/test_live_harness.py"),
}


def _bare_sleeps(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"time", "asyncio"}
        and node.func.attr == "sleep"
    ]


def test_tests_use_shared_wait_helpers_instead_of_bare_sleeps() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        relative = path.relative_to(TESTS)
        if relative == Path("wait.py") or relative in SLEEP_ALLOWLIST:
            continue
        offenders.extend(f"{relative}:{line}" for line in _bare_sleeps(path))
    assert not offenders, "bare sleeps must use tests.wait helpers:\n" + "\n".join(offenders)


def test_wait_until_failure_names_condition_and_last_state() -> None:
    with pytest.raises(AssertionError, match=r"widgets to arrive; last observed state: 0"):
        wait_until(lambda: 0, timeout=0, describe="widgets to arrive")
