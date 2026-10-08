"""Release workflow: policy-programs check runs only when worker image was built (hades #570).

The policy check step must carry the same ``if:`` condition as the build step,
because a reused worker image was never built on the runner and the check would
fail with docker exit 125.
"""

from __future__ import annotations

import pathlib
import typing

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"


def _build_step_condition(steps: list[dict[str, typing.Any]]) -> str | None:
    """Return the ``if:`` expression of the build step, or None."""
    for step in steps:
        name: object = step.get("name")
        if name and "build and prove the worker images" in str(name):
            return str(step.get("if", ""))
    return None


def _policy_check_condition(steps: list[dict[str, typing.Any]]) -> str | None:
    """Return the ``if:`` expression of the policy-check step, or None."""
    for step in steps:
        name: object = step.get("name")
        if name and "check the worker image carries the policy programs" in str(name):
            return str(step.get("if", ""))
    return None


def test_policy_check_has_build_condition() -> None:
    """AC1: the policy-programs check step carries exactly the build step's condition."""
    text = RELEASE_YML.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    steps = typing.cast(
        list[dict[str, typing.Any]],
        workflow["jobs"]["release"]["steps"],
    )

    build_cond = _build_step_condition(steps)
    policy_cond = _policy_check_condition(steps)

    assert build_cond is not None, (
        "release.yml must contain the 'build and prove the worker images' step"
    )
    assert policy_cond is not None, (
        "release.yml must contain the 'check the worker image carries the policy programs' step"
    )
    assert policy_cond == build_cond, (
        f"policy-programs check condition ({policy_cond!r}) must match "
        f"the build step condition ({build_cond!r})"
    )
