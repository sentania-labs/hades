"""Test that CONTRIBUTING.md carries the 'Rules for workers' section.

AC2: a unit test asserts the section and the six key phrases.
"""

from __future__ import annotations

from pathlib import Path

from crucible.domain.gates import injected_shim_text

REPO = Path(__file__).resolve().parents[2]


def test_contributing_has_rules_for_workers_section() -> None:
    content = (REPO / "CONTRIBUTING.md").read_text()
    assert "## Rules for workers" in content


def test_contributing_rules_has_all_six_key_phrases() -> None:
    """The section lists all six rules by their key phrases."""
    content = (REPO / "CONTRIBUTING.md").read_text()

    # Extract just the Rules for workers section
    start = content.index("## Rules for workers")
    next_heading = content.index("## Kubernetes manifests", start)
    section = content[start:next_heading]
    # Normalise whitespace so phrases spanning line boundaries match
    normalised = " ".join(section.split())

    key_phrases = [
        "shaped like a credential",  # rule 1
        "angle-bracket",  # rule 1
        "after the final commit",  # rule 2
        "test-integration",  # rule 3
        "CRUCIBLE_TEST_DATABASE_URL",  # rule 3
        "compose smoke",  # rule 4
        "kind e2e",  # rule 4
        "helm lint",  # rule 4
        "/crucible/repo",  # rule 5
        "em-dashes",  # rule 6
        "America/Chicago",  # rule 6
    ]

    for phrase in key_phrases:
        assert phrase in normalised, f'Missing key phrase: "{phrase}"'


def test_shim_references_contributing_rules_section() -> None:
    """AC2: the generated worker instructions reference the rules section."""
    shim = injected_shim_text()
    assert "CONTRIBUTING.md" in shim
    assert "Rules-for-workers" in shim or "Rules for workers" in shim


def test_shim_references_identity_md() -> None:
    """The shim still points at IDENTITY.md."""
    shim = injected_shim_text("/crucible/identity")
    assert "IDENTITY.md" in shim
    assert "task contract" in shim
