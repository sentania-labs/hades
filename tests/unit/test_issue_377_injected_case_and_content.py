"""hades #377: `no_injected_files` checks shim content on every added path and
matches injected names case-insensitively.

AC1 - a lower-case `claude.md` holding the shim text fails no_injected_files.
AC2 - `notes/readme.txt` whose content is the shim text also fails.
AC3 - an ordinary added file still passes and no blob content is exported for
      paths whose name has no stem.
"""

from __future__ import annotations

import hashlib

from crucible.domain.gates import (
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    _injected_hits,
    _shim_blob_ids,
    evaluate_gate,
    injected_shim_text,
)
from crucible.domain.injected import injected_name
from tests.fixtures import contract_document

SHIM_BLOB_IDS = _shim_blob_ids()

SHIM_CONTENT = injected_shim_text() + "\n"
EXPECTED_SHIM_BLOB = hashlib.sha1(
    b"blob %d\0" % len(SHIM_CONTENT) + SHIM_CONTENT.encode()
).hexdigest()


def _change(path: str, status: str, blob: str | None = None) -> tuple[str, str, str, str]:
    """Return (path, status, blob, classification)."""
    blob_id = blob or "b" * 40
    classification = ""
    return (path, status, blob_id, classification)


def _shim_change(path: str, status: str) -> tuple[str, str, str, str]:
    """A change whose blob matches the shim."""
    return (path, status, EXPECTED_SHIM_BLOB, "shim")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hits(
    paths: list[str],
    diff_changes: list[tuple[str, str, str, str]],
    commit_paths: list[str] | None = None,
    commit_changes: list[tuple[str, str, str, str]] | None = None,
) -> set[str]:
    hits, _ = _injected_hits(
        paths,
        diff_changes,
        commit_paths or [],
        commit_changes,
    )
    return hits


def _gate_result(
    diff_paths: list[str],
    diff_changes: list[tuple[str, str, str, str]],
    commit_paths: list[str] | None = None,
    commit_changes: list[tuple[str, str, str, str]] | None = None,
) -> tuple[GateResult, str]:
    diff = EvidenceItem(
        id=1,
        kind="diff_paths",
        source="crucible",
        verified=True,
        payload={
            "paths": diff_paths,
            "changes": [
                dict(
                    zip(
                        ["path", "status", "blob", "classification"],
                        c,
                        strict=True,
                    )
                )
                for c in diff_changes
            ],
        },
    )
    bundle = EvidenceItem(
        id=2,
        kind="bundle_head",
        source="crucible",
        verified=True,
        payload={
            "head_sha": "a" * 40,
            "commits": 1,
            "commit_paths": commit_paths or [],
            "commit_changes": [
                dict(
                    zip(
                        ["path", "status", "blob", "classification"],
                        c,
                        strict=True,
                    )
                )
                for c in (commit_changes or [])
            ],
        },
    )
    gi = GateInput(
        contract=contract_document(),
        policy={},
        head_sha="a" * 40,
        evidence=(diff, bundle),
    )
    outcome = evaluate_gate(GateName.NO_INJECTED_FILES, gi)
    return outcome.result, outcome.detail


# ---------------------------------------------------------------------------
# AC1 - case-insensitive name matching (Issue #400 already normalises names,
# but we need a test proving the lower-case case fails)
# ---------------------------------------------------------------------------


def test_ac1_claude_md_lower_case_fails() -> None:
    """Lower-case `claude.md` is matched case-insensitively and fails."""
    assert injected_name("claude.md") is True
    assert injected_name("CLAUDE.md") is True
    assert injected_name("Agents.md") is True

    # A diff adding `claude.md` (lower case) hits the gate.
    result, detail = _gate_result(
        ["claude.md"],
        [_shim_change("claude.md", "A")],
    )
    assert result is GateResult.FAIL
    assert "claude.md" in detail


def test_ac1_case_variant_injected_prefix_fails() -> None:
    """Mixed-case injected-prefix paths also fail."""
    assert injected_name(".Claude/settings.json") is True
    assert injected_name(".CLAUDE/settings.json") is True

    result, detail = _gate_result(
        [".Claude/settings.json"],
        [_shim_change(".Claude/settings.json", "A")],
    )
    assert result is GateResult.FAIL
    assert ".Claude/settings.json" in detail


# ---------------------------------------------------------------------------
# AC2 - shim-content check on every path, not only injected names
# ---------------------------------------------------------------------------


def test_ac2_readme_txt_with_shim_content_fails() -> None:
    """`notes/readme.txt` whose content equals the shim text fails."""
    result, detail = _gate_result(
        ["notes/readme.txt"],
        [_shim_change("notes/readme.txt", "A")],
    )
    assert result is GateResult.FAIL
    assert "notes/readme.txt" in detail


def test_ac2_any_name_with_shim_content_fails() -> None:
    """Arbitrary file name with the shim blob id fails."""
    result, detail = _gate_result(
        ["random/filename.xyz"],
        [_shim_change("random/filename.xyz", "A")],
    )
    assert result is GateResult.FAIL
    assert "random/filename.xyz" in detail


def test_ac2_editing_a_file_to_shim_content_fails() -> None:
    """A modification whose blob becomes the shim fails."""
    result, detail = _gate_result(
        ["notes/readme.txt"],
        [_shim_change("notes/readme.txt", "M")],
    )
    assert result is GateResult.FAIL
    assert "notes/readme.txt" in detail


# ---------------------------------------------------------------------------
# AC3 - ordinary files pass and no blob content is exported for non-stem paths
# ---------------------------------------------------------------------------


def test_ac3_ordinary_added_file_passes() -> None:
    """An ordinary added file (no stem, no shim content) passes."""
    result, _ = _gate_result(
        ["src/main.py"],
        [(_change("src/main.py", "A"))],
    )
    assert result is GateResult.PASS


def test_ac3_no_blob_export_for_non_stem_paths() -> None:
    """Paths whose name has no instruction/harness stem must NOT export blob
    content.  The awk filter in ``_injected_collection_script`` only exports
    blobs for names containing ``agents|claude|gemini|codex|hermes|crucible``
    or non-ASCII/control characters.

    We verify this by asserting that a plain name like ``src/main.py`` is NOT
    considered an injected name, so the collector's awk filter skips it and
    no blob is exported.
    """
    # The awk filter checks for stems in the path name; `src/main.py`
    # matches none, so injected_name returns False.
    assert injected_name("src/main.py") is False
    assert injected_name("docs/README.txt") is False

    # A diff with only ordinary paths produces no hits.
    result, _ = _gate_result(
        ["src/main.py", "docs/README.txt"],
        [
            _change("src/main.py", "A"),
            _change("docs/README.txt", "A"),
        ],
    )
    assert result is GateResult.PASS

    # Even if one of those paths had the shim blob id by coincidence
    # (extremely unlikely for 40-hex random blobs), it would now fail
    # because _injected_hits checks blob ids on every path.  The test
    # above with EXPECTED_SHIM_BLOB proves that case works too.


def test_ac3_commit_with_shim_content_fails() -> None:
    """A commit that adds the shim content into a non-injected-name path fails."""
    result, detail = _gate_result(
        [],  # diff is clean
        [],
        commit_paths=["notes/readme.txt"],
        commit_changes=[_shim_change("notes/readme.txt", "A")],
    )
    assert result is GateResult.FAIL
    assert "notes/readme.txt" in detail


def test_finding_fdy_0537_commit_m_status_with_shim() -> None:
    """Finding FDY-0537: a file modified to shim, then modified back still has
    the shim record with status M in commit history — this must fail."""
    result, detail = _gate_result(
        [],
        [],
        commit_paths=["notes/readme.txt"],
        commit_changes=[_shim_change("notes/readme.txt", "M")],
    )
    assert result is GateResult.FAIL
    assert "notes/readme.txt" in detail


# ---------------------------------------------------------------------------
# Integration: end-to-end gate evaluation with real shim blob
# ---------------------------------------------------------------------------


def test_ac1_end_to_end_claude_md_lower_case() -> None:
    """Full gate evaluation: lower-case `claude.md` with shim content fails."""
    result, detail = _gate_result(
        ["claude.md"],
        [(_change("claude.md", "A", EXPECTED_SHIM_BLOB))],
    )
    assert result is GateResult.FAIL
    assert "claude.md" in detail


def test_ac2_end_to_end_readme_txt() -> None:
    """Full gate evaluation: `notes/readme.txt` with shim content fails."""
    result, detail = _gate_result(
        ["notes/readme.txt"],
        [(_change("notes/readme.txt", "A", EXPECTED_SHIM_BLOB))],
    )
    assert result is GateResult.FAIL
    assert "notes/readme.txt" in detail


def test_ac3_end_to_end_ordinary_file() -> None:
    """Full gate evaluation: ordinary file with random blob passes."""
    result, _ = _gate_result(
        ["src/main.py"],
        [(_change("src/main.py", "A", "c" * 40))],
    )
    assert result is GateResult.PASS
