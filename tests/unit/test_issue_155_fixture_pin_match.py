"""Issue 155: transcript fixtures must stay in sync with harness version pins.

When the worker image Dockerfile bumps a harness version, its transcript
fixtures under tests/fixtures_data/transcripts must be renamed to match.
This test compares the version pins declared in images/worker/Dockerfile
against the versions baked into the transcript fixture filenames and fails
with a clear message telling the operator to refresh the fixtures.

The known harnesses that produce transcript fixtures are those whose Dockerfile
ARGs correspond to transcript file prefixes.  The mapping is:

    HARNESS_CLAUDE_CODE_VERSION -> claude-code-<version>-...
    HARNESS_CODEX_VERSION       -> codex-<version>-...

AGY and Hermes do not produce transcript fixtures in this directory so they
are ignored here.
"""

from __future__ import annotations

import re
from pathlib import Path

# Where the pinned harness versions live.
DOCKERFILE = Path(__file__).resolve().parent.parent.parent / "images" / "worker" / "Dockerfile"

# Where the transcript fixtures live.
TRANSCRIPTS = Path(__file__).resolve().parent.parent / "fixtures_data" / "transcripts"

# Maps the harness name to the fixture filename prefix and the ARG name
# in the Dockerfile.
KNOWN_HARNESSES: dict[str, tuple[str, str]] = {
    "claude_code": ("claude-code", "HARNESS_CLAUDE_CODE_VERSION"),
    "codex": ("codex", "HARNESS_CODEX_VERSION"),
}


def _extract_pins(dockerfile_path: Path) -> dict[str, str]:
    """Return {harness_name: version_string} from the Dockerfile."""
    content = dockerfile_path.read_text(encoding="utf-8")
    pins: dict[str, str] = {}
    for name, (_, arg) in KNOWN_HARNESSES.items():
        pattern = rf"^ARG {arg}=(.+)$"
        m = re.search(pattern, content, re.MULTILINE)
        if m:
            pins[name] = m.group(1).strip()
    return pins


def _fixture_versions(transcripts_path: Path) -> dict[str, set[str]]:
    """Return {harness_name: set_of_versions} found in fixture filenames."""
    versions: dict[str, set[str]] = {}
    for name, (prefix, _) in KNOWN_HARNESSES.items():
        found: set[str] = set()
        for fpath in transcripts_path.glob(f"{prefix}-*.jsonl"):
            m = re.match(
                rf"^{re.escape(prefix)}-(\d+\.\d+\.\d+)-.*\.jsonl$",
                fpath.name,
            )
            if m:
                found.add(m.group(1))
        versions[name] = found
    return versions


def test_transcript_fixture_versions_match_dockerfile_pins() -> None:
    """Every harness that has fixtures must have those fixtures at the
    pinned version.  If a pin was bumped the operator must rename or
    regenerate the fixtures (issue 155)."""
    pins = _extract_pins(DOCKERFILE)
    fixtures = _fixture_versions(TRANSCRIPTS)

    errors: list[str] = []

    for harness_name, pin_version in sorted(pins.items()):
        fixture_prefix, _ = KNOWN_HARNESSES[harness_name]
        fixture_versions = fixtures.get(harness_name, set())

        if not fixture_versions:
            # No fixtures exist for this harness; nothing to match.
            continue

        if fixture_versions != {pin_version}:
            errors.append(
                f"harness {fixture_prefix}: pinned version "
                f"{pin_version!r} in images/worker/Dockerfile "
                f"does not match fixture versions "
                f"{sorted(fixture_versions)} "
                f"in tests/fixtures_data/transcripts/; "
                f"run `make e2e-command-timeout` and refresh the fixtures"
            )

    if errors:
        combined = "\n".join(f"  - {e}" for e in errors)
        raise AssertionError(
            "Transcript fixtures must match harness version pins.\n"
            "Bump the pin without renaming fixtures is a mismatch:\n"
            f"{combined}\n"
            "Run `make e2e-command-timeout` to regenerate fixtures."
        )
