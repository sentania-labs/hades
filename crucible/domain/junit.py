"""JUnit XML as pytest node ids (hades #558, #85).

The `test`, `e2e` and `e2e-kind` jobs of the repository's CI keep their JUnit XML as
artifacts (hades #196). A CI failure wake names the failing tests from that report, so
a correction knows what failed without reading the job log. The reading is the one
`tools/ci/flakes.py` makes for the weekly flake scan, kept in step by its tests: this
module is the domain's copy, since the application layer never imports a tool.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree

# ci.yml's `name:` for the uploaded JUnit XML, per job, with `github.run_attempt`
# appended so a re-run's upload never collides with attempt 1's.
_JUNIT_JOBS = frozenset({"test", "e2e"})
_KIND_SHARD = re.compile(r"e2e-kind \((\d+)\)")


class JUnitParseError(ValueError):
    """The bytes are not a JUnit report."""


def failing_test_ids(xml_bytes: bytes) -> list[str]:
    """The pytest node ids of every failing or erroring `<testcase>` in a JUnit report,
    in document order and each once. Raises `JUnitParseError` for bytes that are not
    XML, so a caller says the report was unreadable instead of reporting no failures."""
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise JUnitParseError(str(exc)) from exc
    failing: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            node = node_id(case.get("classname") or "", case.get("name") or "")
            if node not in failing:
                failing.append(node)
    return failing


def node_id(classname: str, name: str) -> str:
    """`tests.unit.test_foo`, `test_bar` -> `tests/unit/test_foo.py::test_bar`.

    pytest's default (xunit2) JUnit report has no `file` attribute, only the dotted
    `classname`, which for a class-based test is the module path followed by one or
    more test classes with no marker between them. This repository's convention is
    snake_case modules and CapWords classes, so the first dotted component that starts
    with an uppercase letter is the first class component; everything before it is the
    module path, everything from it on (plus `name`) is the `::`-joined node suffix."""
    if not classname:
        return name
    parts = classname.split(".")
    split_at = next((i for i, part in enumerate(parts) if part[:1].isupper()), len(parts))
    module_path = "/".join(parts[:split_at]) + ".py"
    suffix = "::".join([*parts[split_at:], name])
    return f"{module_path}::{suffix}"


def junit_artifact_name(job_name: str, run_attempt: int) -> str | None:
    """The JUnit artifact ci.yml uploads for a job, or None for a job that uploads none
    (`lint`, `images`, `registry`, `classify`): `junit-test-1`, `junit-e2e-2`,
    `junit-e2e-kind-3-1`."""
    attempt = max(1, int(run_attempt or 1))
    if job_name in _JUNIT_JOBS:
        return f"junit-{job_name}-{attempt}"
    match = _KIND_SHARD.fullmatch(job_name)
    return f"junit-e2e-kind-{match.group(1)}-{attempt}" if match else None


__all__ = ["JUnitParseError", "failing_test_ids", "junit_artifact_name", "node_id"]
