"""The shipped sample config must not undo an adapter's read-only credential (12).

`mount_mode` may raise an adapter's minimum to rw-narrow and never lowers it, so a
sample that says rw-narrow for Claude Code would mount every copy writable on any
deployment built from it (Codex review of PR 307)."""

import tomllib
from pathlib import Path

from crucible.adapters.harness.registry import default_registry
from crucible.ports.harness import MountMode

SAMPLE = Path(__file__).resolve().parents[2] / "examples" / "config" / "crucible.example.toml"


def test_the_sample_config_keeps_read_only_adapters_read_only() -> None:
    document = tomllib.loads(SAMPLE.read_text())
    credentials = document["credentials"]
    checked: set[str] = set()
    for adapter in default_registry():
        spec = adapter.credential_spec()
        entry = credentials.get(adapter.name)
        if spec is None or entry is None:
            continue
        if spec.minimum_mode is MountMode.RO:
            checked.add(adapter.name)
            assert "mount_mode" not in entry
    # Claude Code's minimum became read-only in PR 307; the test must not pass by
    # finding nothing to check (Codex review of PR 323).
    assert "claude_code" in checked
