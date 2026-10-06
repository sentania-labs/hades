"""crucible#169: the first-run setup path on the Status page. Five numbered steps,
each linking to its page, shown as not done or done from the readiness state the
Status page already computes. The path disappears once every step is done."""

from __future__ import annotations

from typing import Any

from crucible.adapters.ui.pages.dashboard import _first_run_path


def _readiness_with_harnesses(
    harness_states: list[dict[str, Any]],
    readiness_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    steps = readiness_steps or []
    return {
        "ready": False,
        "ready_harnesses": [h["name"] for h in harness_states if h.get("state") == "ready"],
        "steps": steps,
        "harnesses": [
            {
                "name": hs.get("name", f"harness-{i}"),
                "state": hs.get("state", "not_ready"),
                "note": "",
                "steps": hs.get("steps", []),
                "default_image": hs.get("default_image"),
                "enabled_by_administrator": True,
                "images": hs.get("images"),
            }
            for i, hs in enumerate(harness_states)
        ],
    }


def test_first_run_path_has_five_steps() -> None:
    readiness = _readiness_with_harnesses([])
    path = _first_run_path(readiness)
    assert len(path) == 5


def test_step_order_gateway_images_credentials_github_harnesses() -> None:
    readiness = _readiness_with_harnesses([])
    path = _first_run_path(readiness)
    labels = [s["label"] for s in path]
    assert labels == [
        "Local gateway",
        "Images",
        "Credentials",
        "GitHub",
        "Test each harness",
    ]


def test_step_links() -> None:
    readiness = _readiness_with_harnesses([])
    path = _first_run_path(readiness)
    links = [s["link"] for s in path]
    assert links == [
        "/ui/gateway",
        "/ui/images",
        "/ui/credentials",
        "/ui/github",
        "/ui/harnesses",
    ]


def test_fresh_deployment_all_not_done() -> None:
    """AC1: On a fresh deployment all five steps show not done, with links.
    A fresh deployment has configured harnesses but none ready."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "not_ready",
                "steps": [
                    {"code": "credential_missing", "text": "missing"},
                    {"code": "endpoint_not_configured", "text": "no endpoint"},
                ],
            },
            {
                "name": "claude_code",
                "state": "not_ready",
                "steps": [{"code": "credential_missing", "text": "missing"}],
            },
        ],
        readiness_steps=[
            {"code": "no_ready_harness", "text": "no ready harnesses", "fix": "/ui/harnesses"},
            {"code": "no_repository", "text": "no repository", "fix": "/ui/github"},
            {"code": "github_app_not_connected", "text": "app not connected", "fix": "/ui/github"},
        ],
    )
    path = _first_run_path(readiness)
    for step in path:
        assert step["done"] is False, f"Step {step['number']} {step['label']} should be not done"


def test_local_gateway_done_when_hermes_credential_valid() -> None:
    """AC2: Completing a step flips it to done from readiness state with no separate flag."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            }
        ]
    )
    path = _first_run_path(readiness)
    assert path[0]["done"] is True


def test_images_done_when_promoted_image_exists() -> None:
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "claude_code",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1", "digest": "sha256:abc"},
            }
        ]
    )
    path = _first_run_path(readiness)
    assert path[1]["done"] is True


def test_credentials_done_when_no_missing_creds() -> None:
    """Non-Hermes harnesses with valid credentials make this step done."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "codex",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            }
        ]
    )
    path = _first_run_path(readiness)
    assert path[2]["done"] is True


def test_credentials_not_done_when_credential_missing() -> None:
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "codex",
                "state": "not_ready",
                "steps": [{"code": "credential_missing", "text": "missing"}],
                "default_image": {"reference": "w:1"},
            }
        ]
    )
    path = _first_run_path(readiness)
    assert path[2]["done"] is False


def test_github_done_when_no_github_steps_in_readiness() -> None:
    """No no_repository or github_app_not_connected steps means done."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            }
        ],
        readiness_steps=[],
    )
    path = _first_run_path(readiness)
    assert path[3]["done"] is True


def test_github_not_done_when_no_repository() -> None:
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            }
        ],
        readiness_steps=[{"code": "no_repository", "text": "no repo", "fix": "/ui/github"}],
    )
    path = _first_run_path(readiness)
    assert path[3]["done"] is False


def test_github_not_done_when_app_not_connected() -> None:
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            }
        ],
        readiness_steps=[
            {"code": "github_app_not_connected", "text": "no app", "fix": "/ui/github"}
        ],
    )
    path = _first_run_path(readiness)
    assert path[3]["done"] is False


def test_harness_test_done_when_all_harnesses_ready() -> None:
    """Step 5 is done when all non-off harnesses are ready."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            },
            {
                "name": "codex",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            },
        ]
    )
    path = _first_run_path(readiness)
    assert path[4]["done"] is True


def test_harness_test_not_done_when_one_harness_not_ready() -> None:
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            },
            {
                "name": "codex",
                "state": "not_ready",
                "steps": [{"code": "credential_missing", "text": "missing"}],
                "default_image": {"reference": "w:1"},
            },
        ]
    )
    path = _first_run_path(readiness)
    assert path[4]["done"] is False


def test_off_harnesses_ignored_for_harness_test() -> None:
    """Harnesses with state 'off' do not block the harness test step."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
            },
            {
                "name": "script-harness",
                "state": "off",
                "steps": [],
                "note": "off by default",
            },
        ]
    )
    path = _first_run_path(readiness)
    assert path[4]["done"] is True


def test_image_state_shows_when_missing() -> None:
    """Image state is preserved in the readiness payload so the first-run path
    can see when a harness has no promoted image. The test constructs a readiness
    payload that mimics what harness_readiness() now returns (with default_image
    and images preserved from the source), then renders the first-run path and
    asserts that the missing image state surfaces."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "codex",
                "state": "not_ready",
                "steps": [{"code": "no_promoted_image", "text": "no image", "fix": "/ui/images"}],
                "default_image": None,
                "images": [
                    {
                        "reference": "w:2",
                        "harness_version": "0.24.0",
                        "digest": "sha256:def",
                        "promotion_state": "candidate",
                    }
                ],
            }
        ]
    )
    path = _first_run_path(readiness)
    # Step 2 (Images) should be not done because codex has no promoted default image.
    assert path[1]["label"] == "Images"
    assert path[1]["done"] is False
    # The harness entry in readiness should still carry images.
    harness = next(h for h in readiness["harnesses"] if h["name"] == "codex")
    assert harness["images"] is not None
    assert len(harness["images"]) == 1
    assert harness["images"][0]["promotion_state"] == "candidate"


def test_images_done_via_images_list() -> None:
    """Images step is done when an image has promotion_state == 'default'."""
    readiness = _readiness_with_harnesses(
        [
            {
                "name": "hermes",
                "state": "ready",
                "steps": [],
                "default_image": {"reference": "w:1"},
                "images": [
                    {
                        "reference": "w:1",
                        "harness_version": "0.25.0",
                        "digest": "sha256:abc",
                        "promotion_state": "default",
                    }
                ],
            }
        ]
    )
    path = _first_run_path(readiness)
    assert path[1]["done"] is True
