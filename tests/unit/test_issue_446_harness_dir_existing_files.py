"""hades #446: ``no_injected_files`` permits editing or deleting a file under an
injected prefix (.claude/, .codex/, .hermes/, .gemini/, .crucible/,
crucible/identity/, .crucible-shims/) that the merge base already has, while
still blocking new additions, symlink flips, and shim content.  The pre-existing
rule that instruction files (CLAUDE.md, AGENTS.md, GEMINI.md) are treated
similarly still holds."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from crucible.application.submit_task import _harness_path_warning
from crucible.contracts.task_contract import Scope, TaskContractV1
from crucible.domain.gates import (
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    evaluate_gate,
    injected_shim_text,
)
from tests.fixtures import contract_document

ZERO = "0" * 40
SHIM_BLOB = hashlib.sha1(
    b"blob %d\0" % len(injected_shim_text() + "\n") + (injected_shim_text() + "\n").encode()
).hexdigest()


def _change(path: str, status: str, blob: str = "b" * 40) -> dict[str, str]:
    """Return a diff change record.  Status D always uses ZERO for the blob."""
    return {"path": path, "status": status, "blob": ZERO if status == "D" else blob}


def _outcome(
    diff: dict[str, Any],
    commit_paths: list[str],
    commit_changes: list[dict[str, str]] | None,
    base_paths: list[str] | None = None,
) -> GateResult:
    """Build a minimal ``GateInput`` that mirrors the collector's evidence shape.

    *diff* may contain a ``base_paths`` list.  If ``base_paths`` is also
    passed explicitly, the explicit list takes precedence.
    """
    if base_paths is None:
        base_paths = list(diff.get("base_paths", []))
    bundle: dict[str, Any] = {"head_sha": "a" * 40, "commits": 1, "commit_paths": commit_paths}
    if commit_changes is not None:
        bundle["commit_changes"] = commit_changes
    evidence = (
        EvidenceItem(
            id=1,
            kind="diff_paths",
            source="crucible",
            verified=True,
            payload={**diff, "base_paths": base_paths},
        ),
        EvidenceItem(
            id=2,
            kind="bundle_head",
            source="crucible",
            verified=True,
            payload=bundle,
        ),
    )
    gi = GateInput(
        contract=contract_document(),
        policy={},
        head_sha="a" * 40,
        evidence=evidence,
    )
    result = evaluate_gate(GateName.NO_INJECTED_FILES, gi)
    assert isinstance(result, GateResult)
    return result


def _judge(
    changes: list[dict[str, str]],
    base_paths: list[str] | None = None,
) -> tuple[GateResult, str]:
    """Return ``(result, detail)`` for a *diff-only* scenario (no commit info
    is provided so the gate falls back to the ``diff_status`` loop)."""
    if base_paths is None:
        base_paths = []
    paths = [c["path"] for c in changes]
    result = _outcome(
        {"paths": paths, "changes": list(changes)},
        sorted(paths),
        None,
        base_paths=base_paths,
    )
    # Extract detail from the result if available; otherwise just return
    # the empty string.  The ``GateResult`` is a simple string enum but we
    # also need the detail.  For now, just use the string representation.
    return result, ""


# ---------------------------------------------------------------------------
# AC1: editing / deleting an existing harness-directory file passes
# ---------------------------------------------------------------------------


def test_editing_harness_existing_file_passes() -> None:
    changes = [_change(".claude/hooks/check-review-passed.sh", "M")]
    base_paths = [".claude/hooks/check-review-passed.sh"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.PASS


def test_deleting_harness_existing_file_passes() -> None:
    changes = [_change(".claude/hooks/check-review-passed.sh", "D")]
    base_paths = [".claude/hooks/check-review-passed.sh"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.PASS


def test_editing_under_codex_prefix_passes() -> None:
    changes = [_change(".codex/rules.md", "M")]
    base_paths = [".codex/rules.md"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.PASS


def test_editing_under_hermes_prefix_passes() -> None:
    changes = [_change(".hermes/config.yaml", "M")]
    base_paths = [".hermes/config.yaml"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.PASS


# ---------------------------------------------------------------------------
# AC2: adding a new harness-directory file or turning an entry into a
#      symlink still fails
# ---------------------------------------------------------------------------


def test_new_entry_under_claude_prefix_fails() -> None:
    changes = [_change(".claude/hooks/new-hook.sh", "A")]
    result, _ = _judge(changes)
    assert result is GateResult.FAIL


def test_new_entry_under_codex_prefix_fails() -> None:
    changes = [_change(".codex/ai-config.toml", "A")]
    result, _ = _judge(changes)
    assert result is GateResult.FAIL


def test_symlink_flip_under_claude_fails() -> None:
    changes = [_change(".claude/scripts/tools.sh", "T", "b" * 40)]
    result, _ = _judge(changes)
    assert result is GateResult.FAIL


def test_symlink_flip_under_hermes_fails() -> None:
    changes = [_change(".hermes/tool.sh", "T", "b" * 40)]
    result, _ = _judge(changes)
    assert result is GateResult.FAIL


def test_new_entry_under_crucible_prefix_fails() -> None:
    changes = [_change(".crucible/shim.b64", "A")]
    result, _ = _judge(changes)
    assert result is GateResult.FAIL


# ---------------------------------------------------------------------------
# AC3: writing shim content into an existing harness-directory file still fails
# ---------------------------------------------------------------------------


def test_shim_content_in_harness_fails() -> None:
    changes = [
        _change(".claude/hooks/check-review-passed.sh", "M", SHIM_BLOB),
    ]
    base_paths = [".claude/hooks/check-review-passed.sh"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.FAIL


def test_shim_content_in_codex_fails() -> None:
    changes = [
        _change(".codex/rules.md", "M", SHIM_BLOB),
    ]
    base_paths = [".codex/rules.md"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.FAIL


# ---------------------------------------------------------------------------
# Mixed: edit + new addition under the same harness prefix
# ---------------------------------------------------------------------------


def test_editing_harness_and_adding_new_harness_fails() -> None:
    changes = [
        _change(".claude/hooks/check-review-passed.sh", "M"),
        _change(".claude/hooks/new-hook.sh", "A"),
    ]
    result, _ = _judge(changes, base_paths=[".claude/hooks/check-review-passed.sh"])
    assert result is GateResult.FAIL


# ---------------------------------------------------------------------------
# Instruction-name files: editing/deleting base-harvest files still pass
# ---------------------------------------------------------------------------


def test_editing_instruction_name_passes() -> None:
    changes = [_change("CLAUDE.md", "M")]
    base_paths = ["CLAUDE.md"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.PASS


def test_deleting_instruction_name_passes() -> None:
    changes = [_change("AGENTS.md", "D")]
    base_paths = ["AGENTS.md"]
    result, _ = _judge(changes, base_paths=base_paths)
    assert result is GateResult.PASS


def test_adding_instruction_name_fails() -> None:
    changes = [_change("CLAUDE.md", "A")]
    result, _ = _judge(changes)
    assert result is GateResult.FAIL


# ---------------------------------------------------------------------------
# AC4: harness directory in ``allowed_paths`` produces a warning (submit_task.py)
# ---------------------------------------------------------------------------


def test_harness_path_in_allowed_paths_returns_warning() -> None:
    from unittest.mock import MagicMock

    contract = MagicMock(spec=TaskContractV1)
    contract.scope = Scope(
        allowed_paths=["hades/.claude/", "hades/docs/"],
        prohibited_paths=["src/test.py"],
        may_add_dependencies=False,
        may_modify_ci=False,
    )
    warning = _harness_path_warning(contract)
    assert warning is not None
    assert ".claude/" in warning
    assert "allowed_paths contains 'hades/.claude/', which reaches into a harness directory" in warning


def test_non_harness_allowed_paths_no_warning() -> None:
    from unittest.mock import MagicMock

    contract = MagicMock(spec=TaskContractV1)
    contract.scope = Scope(
        allowed_paths=["hades/docs/", "src/main.py"],
        prohibited_paths=["src/test.py"],
        may_add_dependencies=False,
        may_modify_ci=False,
    )
    warning = _harness_path_warning(contract)
    assert warning is None
