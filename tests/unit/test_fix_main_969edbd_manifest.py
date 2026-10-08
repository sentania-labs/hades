"""Regression coverage for the one-click GitHub App manifest on main."""

from crucible.application.admin.github_manifest import build_manifest


def test_one_click_manifest_matches_the_integration_contract() -> None:
    external_url = "http://hades.test"

    assert build_manifest("Hades-test", external_url) == {
        "name": "Hades-test",
        "url": external_url,
        "description": "Crucible delivery: opens and follows pull requests.",
        "public": False,
        "redirect_url": f"{external_url}/ui/github/callback",
        "setup_url": f"{external_url}/ui/github/installed",
        "setup_on_update": True,
        "hook_attributes": {"url": f"{external_url}/v1/github/webhook", "active": False},
        "default_permissions": {
            "metadata": "read",
            "contents": "write",
            "pull_requests": "write",
            "checks": "read",
            "actions": "write",
            "issues": "read",
        },
        "default_events": [],
    }
