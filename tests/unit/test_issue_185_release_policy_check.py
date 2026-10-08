"""Release workflow includes a policy check after build and before push (hades #185).

The release workflow must run ``make images-policy-check`` after the worker image
build step and before the first push/publish step, so that a release whose worker
image and shipped policies disagree stops before anything is published.
"""

from __future__ import annotations

import pathlib
import re
import typing

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"


def _load_steps() -> list[dict[str, typing.Any]]:
    """Return the ordered list of top-level steps from the release job."""
    text = RELEASE_YML.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    steps = typing.cast(list[dict[str, typing.Any]], workflow["jobs"]["release"]["steps"])
    return steps


def _step_names(steps: list[dict[str, typing.Any]]) -> list[tuple[str, str]]:
    """Return pairs of (name, index) for every step that has a name."""
    result: list[tuple[str, str]] = []
    for idx, step in enumerate(steps):
        name: object = step.get("name")
        if name and isinstance(name, str):
            result.append((name, str(idx)))
    return result


def _first_push_step_index(steps: list[dict[str, typing.Any]]) -> int | None:
    """Return the index of the first step that pushes/publishes to a registry.

    A push step either references ``docker push``, ``ghcr``, uses a script
    whose name contains ``push_worker``, or otherwise performs a publish.
    """
    push_patterns = [
        r"docker\s+push",
        r"push_worker_images",
        r"buildx\s+imagetools\s+create",
    ]
    for idx, step in enumerate(steps):
        run_val: object = step.get("run", "")
        if isinstance(run_val, str):
            for pattern in push_patterns:
                if re.search(pattern, run_val):
                    return idx
    return None


def test_policy_check_after_build_and_before_push() -> None:
    """AC1: the policy check step exists after the image build and before the first push."""
    steps = _load_steps()
    names_with_idx = _step_names(steps)

    build_idx: int | None = None
    for name, idx in names_with_idx:
        if "build and prove the worker images" in name:
            build_idx = int(idx)
            break

    assert build_idx is not None, (
        "release.yml must contain the 'build and prove the worker images' step"
    )

    policy_check_idx: int | None = None
    for name, idx in names_with_idx:
        if "check the worker image carries the policy programs" in name:
            policy_check_idx = int(idx)
            break

    assert policy_check_idx is not None, (
        "release.yml must contain the 'check the worker image carries the policy programs' step"
    )

    assert policy_check_idx > build_idx, (
        f"policy check step (index {policy_check_idx}) must come after "
        f"the build step (index {build_idx})"
    )

    first_push = _first_push_step_index(steps)
    assert first_push is not None, "release.yml must contain a push step"
    assert policy_check_idx < first_push, (
        f"policy check step (index {policy_check_idx}) must come before "
        f"the first push step (index {first_push})"
    )
