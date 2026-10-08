"""The one-click GitHub App's pure parts (crucible#168): the manifest, GitHub's web origin,
the target page, the external URL, and the unauthenticated conversion call.

Every secret-shaped value here is built at run time."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from crucible.adapters.github.apps import RestGitHubApps
from crucible.adapters.github.transport import RestTransport
from crucible.application.admin.github import _github_refusal, _install_url, web_base
from crucible.application.admin.github_manifest import (
    PERMISSIONS,
    binding_cookie_path,
    build_manifest,
    default_app_name,
    manifest_target_url,
    normalize_external_url,
)
from crucible.application.errors import ContractValidationError


def test_the_manifest_asks_for_spec_23s_permissions_no_events_and_no_webhook() -> None:
    manifest = build_manifest("Hades-abc123", "https://hades.int.example")
    assert manifest["default_permissions"] == {
        "metadata": "read",
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "actions": "write",
        "issues": "read",
    }
    assert manifest["default_permissions"] == PERMISSIONS
    assert manifest["default_events"] == []
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["public"] is False
    assert manifest["url"] == "https://hades.int.example"
    assert manifest["redirect_url"] == "https://hades.int.example/ui/github/callback"
    assert manifest["setup_url"] == "https://hades.int.example/ui/github/installed"
    assert "issues" in manifest["default_permissions"]
    assert "write" not in manifest["default_permissions"]["issues"]


def test_the_default_name_is_hades_and_a_short_suffix() -> None:
    names = {default_app_name() for _ in range(5)}
    assert all(re.fullmatch(r"Hades-[0-9a-f]{6}", n) for n in names)
    assert len(names) > 1


@pytest.mark.parametrize(
    ("api_base", "web"),
    [
        ("https://api.github.com", "https://github.com"),
        ("https://api.github.com/", "https://github.com"),
        ("https://ghe.example.internal/api/v3", "https://ghe.example.internal"),
        ("https://api.corp.example/api/v3", "https://api.corp.example"),
        (
            "http://crucible-stubs.ns.svc.cluster.local:8080",
            "http://crucible-stubs.ns.svc.cluster.local:8080",
        ),
    ],
)
def test_githubs_web_origin_follows_the_api_base(api_base: str, web: str) -> None:
    assert web_base(api_base) == web


def test_the_target_is_the_accounts_or_the_organizations_create_page() -> None:
    assert (
        manifest_target_url("https://github.com", "S1")
        == "https://github.com/settings/apps/new?state=S1"
    )
    assert (
        manifest_target_url("https://github.com", "S1", "octo-lab")
        == "https://github.com/organizations/octo-lab/settings/apps/new?state=S1"
    )


def test_the_install_link_is_https_or_githubs_own_origin() -> None:
    app = {"html_url": "https://github.com/apps/hades-x"}
    assert _install_url(app) == "https://github.com/apps/hades-x/installations/new"
    stub = {"html_url": "http://stub:8080/apps/hades-x"}
    assert (
        _install_url(stub, "http://stub:8080") == "http://stub:8080/apps/hades-x/installations/new"
    )
    assert _install_url(stub) is None
    assert _install_url({"html_url": "javascript:alert(1)"}, "http://stub:8080") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://hades.apps.int.example/", "https://hades.apps.int.example"),
        (" http://127.0.0.1:8080 ", "http://127.0.0.1:8080"),
        ("https://lab.example/crucible/", "https://lab.example/crucible"),
    ],
)
def test_an_external_url_is_an_origin_and_an_optional_prefix(value: str, expected: str) -> None:
    assert normalize_external_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["ftp://x", "hades.example", "https://user:pw@x.example", "https://x.example/?a=1", "https://"],
)
def test_an_external_url_that_is_not_one_is_refused(value: str) -> None:
    with pytest.raises(ContractValidationError):
        normalize_external_url(value)


@pytest.mark.parametrize(
    ("external_url", "path"),
    [
        ("https://hades.apps.int.example", "/ui/github"),
        ("https://lab.example/crucible", "/crucible/ui/github"),
        ("https://lab.example/crucible/hades", "/crucible/hades/ui/github"),
    ],
)
def test_the_binding_cookie_is_scoped_under_the_external_urls_prefix(
    external_url: str, path: str
) -> None:
    """A deployment behind a reverse-proxy path prefix (crucible#168 Codex correction) gets
    the callback at that prefix in the browser's address bar, so the cookie must cover it."""
    assert binding_cookie_path(external_url) == path


class _Connection:
    """One recorded request and a canned answer, in place of http.client."""

    def __init__(self, seen: list[dict[str, Any]], status: int, body: Any) -> None:
        self._seen = seen
        self._status = status
        self._body = body

    def request(self, method: str, url: str, body: Any = None, headers: Any = None) -> None:
        self._seen.append({"method": method, "url": url, "headers": dict(headers or {})})

    def getresponse(self) -> _Connection:
        return self

    @property
    def status(self) -> int:
        return self._status

    def read(self) -> bytes:
        return json.dumps(self._body).encode()

    def getheaders(self) -> list[tuple[str, str]]:
        return [("Content-Type", "application/json")]

    def close(self) -> None:
        return


def _apps(status: int, body: Any) -> tuple[RestGitHubApps, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    transport = RestTransport(
        "https://api.github.com",
        connection_factory=lambda host, port, timeout: _Connection(seen, status, body),
    )
    return RestGitHubApps(authenticator=None, transport=transport), seen  # type: ignore[arg-type]


def test_the_conversion_is_unauthenticated_and_keeps_no_oauth_secret() -> None:
    label = " ".join(["RSA", "PRIVATE", "KEY"])
    pem = f"-----BEGIN {label}-----\nMIIfake\n-----END {label}-----\n"
    hook = "hook-" + "h" * 20
    oauth = "oauth-" + "o" * 20
    apps, seen = _apps(
        201,
        {
            "id": 5151,
            "slug": "hades-x",
            "name": "Hades-x",
            "owner": {"login": "octo-lab"},
            "html_url": "https://github.com/apps/hades-x",
            "pem": pem,
            "webhook_secret": hook,
            "client_secret": oauth,
        },
    )
    made = apps.convert_manifest("abc123")
    assert seen[0]["method"] == "POST" and seen[0]["url"] == "/app-manifests/abc123/conversions"
    assert "Authorization" not in seen[0]["headers"]
    assert made.app_id == 5151 and made.slug == "hades-x" and made.owner == "octo-lab"
    assert made.private_key == pem.encode() and made.webhook_secret == hook.encode()
    assert "MIIfake" not in repr(made) and hook not in repr(made)
    assert oauth not in repr(made) and not hasattr(made, "client_secret")


def test_a_spent_code_is_githubs_404() -> None:
    from crucible.ports.github import GitHubError  # noqa: PLC0415

    apps, _ = _apps(404, {"message": "Not Found"})
    with pytest.raises(GitHubError) as caught:
        apps.convert_manifest("spent")
    assert caught.value.status == 404


def test_a_manifest_code_never_reaches_a_log_or_an_error() -> None:
    from crucible.adapters.github.transport import _loggable  # noqa: PLC0415

    assert _loggable("/app-manifests/mc_live/conversions") == "/app-manifests/[code]/conversions"
    assert _loggable("/app/installations") == "/app/installations"


def test_a_refused_stored_key_points_at_create_not_at_pasting_one() -> None:
    """With the paste path gone (the operator, 2026-09-27), a refused key's remedy is a new
    App from the GitHub page."""
    from crucible.ports.github import GitHubError  # noqa: PLC0415

    detail = _github_refusal(
        GitHubError(401, "Bad credentials"), 5, "https://api.github.com"
    ).detail
    assert detail.startswith("GitHub refused the stored key for App 5 (HTTP 401)")
    assert "create a new App on the GitHub page" in detail and "App ID" not in detail
