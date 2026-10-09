"""Regression tests for FDY-0599: fix red main after PR #611.

AC1: CONTRIBUTING.md carries the 'Rules for workers' section with the
     six rules and reasons, under 60 lines.
AC2: the generated worker instructions reference the section; a unit test
     asserts the section and the six key phrases.
"""

from __future__ import annotations

from pathlib import Path

from crucible.domain.gates import injected_shim_text

REPO = Path(__file__).resolve().parents[2]


def test_ac1_contributing_rules_section_under_sixty_lines() -> None:
    """AC1: the section is under 60 lines."""
    content = (REPO / "CONTRIBUTING.md").read_text()
    start = content.index("## Rules for workers")
    next_heading = content.index("## Kubernetes manifests", start)
    section_text = content[start:next_heading]
    line_count = section_text.count("\n") + 1
    assert line_count < 60, f"Rules for workers section is {line_count} lines, must be under 60"


def test_ac2_shim_references_contributing_and_rules_section() -> None:
    """AC2: the shim points at CONTRIBUTING.md and the Rules for workers section."""
    shim = injected_shim_text()
    assert "CONTRIBUTING.md" in shim, "shim must reference CONTRIBUTING.md"
    assert "Rules-for-workers" in shim or "Rules for workers" in shim, (
        "shim must reference the Rules for workers section"
    )


def test_ac2_contributing_has_all_key_phrases() -> None:
    """AC2: the section and the six key phrases are present."""
    content = (REPO / "CONTRIBUTING.md").read_text()
    start = content.index("## Rules for workers")
    next_heading = content.index("## Kubernetes manifests", start)
    section = content[start:next_heading]
    normalised = " ".join(section.split())

    key_phrases = [
        "shaped like a credential",
        "angle-bracket",
        "after the final commit",
        "test-integration",
        "CRUCIBLE_TEST_DATABASE_URL",
        "compose smoke",
        "kind e2e",
        "helm lint",
        "/crucible/repo",
        "em-dashes",
        "America/Chicago",
    ]

    for phrase in key_phrases:
        assert phrase in normalised, f'Missing key phrase: "{phrase}"'
