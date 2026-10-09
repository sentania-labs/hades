"""Task checks beyond the repository checks required by policy."""

import json
import re
import shlex
from collections.abc import Collection, Sequence
from pathlib import PurePosixPath
from typing import Any

_GENERIC_CHECKS = {"make lint", "make test", "make test-unit", "make scan"}


def task_specific_checks(contract: dict[str, Any], policy_document: dict[str, Any]) -> list[str]:
    """Return normalized command checks not named in repository.required_checks.

    Those policy checks are mandatory in every contract and are re-run by the
    verification_ran gate. Older policies without commands use the standard set.
    Artifact requirements and empty commands are not executable checks.
    """
    named = policy_document.get("repository", {}).get("required_checks", [])
    generic = {" ".join(command.split()) for command in named if command.strip()}
    generic = generic or _GENERIC_CHECKS
    commands = (
        " ".join(check.get("command", "").split())
        for check in contract.get("required_verification", [])
        if check.get("kind", "command") == "command"
    )
    return [command for command in commands if command and command not in generic]


# hades #517, #608: what the gate probe records for one check on the unchanged tree.
PROOF_NEW_FILE = "new file named"
PROOF_FAILS = "fails on the unchanged tree"
PROOF_NONE = "passes on the unchanged tree"

# A token that holds any of these is shell syntax or a pattern, never one literal path.
_NOT_A_PATH = frozenset("$`*?[]{}~<>|;&()'\"\\\n\t ")


def named_paths(command: str) -> tuple[str, ...]:
    """The repository paths a check command names: each word that is a relative path
    inside the tree (it has a `/` or ends in `.py`), with a pytest node id's `::name`
    cut off. Options, assignments, URLs, absolute paths, `..` and shell syntax are not
    paths here. The gate probe asks the unchanged tree which of them exist (hades #517,
    #608): a check naming a file the attempt is to add fails there by definition."""
    try:
        words = shlex.split(command)
    except ValueError:
        return ()
    paths: list[str] = []
    for word in words:
        path = word.split("::", 1)[0]
        if (
            not path
            or path.startswith(("-", "/"))
            or "=" in path
            or "://" in path
            or any(char in _NOT_A_PATH for char in path)
            or ".." in PurePosixPath(path).parts
        ):
            continue
        if "/" in path or path.endswith(".py"):
            paths.append(path.rstrip("/") or path)
    return tuple(dict.fromkeys(paths))


def probe_proof(exit_code: int, expect_exit: int, missing_paths: Sequence[str]) -> str:
    """What one probed check proves on the unchanged tree (hades #517, #608).

    A check that names a file the tree does not have fails there by definition,
    whatever its runner made of it (pytest exits 4, or 2, for a path it cannot find):
    that is `new file named`, the proof a new test file gives. A check that exits other
    than its expectation fails on its own. A check that passes proves nothing."""
    if missing_paths:
        return PROOF_NEW_FILE
    if exit_code != expect_exit:
        return PROOF_FAILS
    return PROOF_NONE


def proves_nothing_detail(
    passing: Sequence[tuple[str, str, int]], taken_ids: Collection[str]
) -> str:
    """The gate_proves_nothing refusal (hades #412): which checks pass on the unchanged
    repo, and the exact amendment that fixes it. `passing` is each probed check's id,
    command and exit; `taken_ids` every id the contract already uses, so the suggested
    check's id is the next free `V<n>`."""
    number = 1 + max(
        (int(taken[1:]) for taken in taken_ids if re.fullmatch(r"V[0-9]{1,6}", taken)),
        default=len(taken_ids),
    )
    while f"V{number}" in taken_ids:
        number += 1
    example = {
        "id": f"V{number}",
        "kind": "command",
        "command": "uv run pytest -q tests/unit/test_issue_<n>_<slug>.py",
        "expect_exit": 0,
    }
    named = "; ".join(
        f"{check_id} passes on the unchanged repo (`{command}` exits {code})"
        for check_id, command, code in passing
    )
    return (
        f"{named}. No probed check fails before the work, so passing them after it proves "
        f"nothing. Fix: amend the contract to add a required_verification check that fails "
        f"on the unchanged repo and passes once the work is done, for example "
        f"{json.dumps(example)} naming the new test file the attempt adds (a file absent "
        f"on the unchanged repo counts as {PROOF_NEW_FILE!r}), then reschedule the blocked "
        f"task: the gate probe runs again before any worker, and this refusal spent no "
        f"attempt."
    )
