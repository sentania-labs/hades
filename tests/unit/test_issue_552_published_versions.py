"""Unit tests for ``tools/release/published_versions.py`` (hades #573).

Covers the tag-listing parser (via the ``published_versions`` function
accepting a pre-built list so no network is needed) and the end-to-end
``--previous`` chain between ``published_versions.py`` output and
``version.py --previous`` by fixture-ing the GHCR API response shape.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]

_PUBLISHED_VERSIONS = REPOSITORY / "tools" / "release" / "published_versions.py"
_VERSION_PY = REPOSITORY / "tools" / "release" / "version.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pv = _load("published_versions", _PUBLISHED_VERSIONS)
v = _load("version", _VERSION_PY)


# -- published_versions filtering ---------------------------------------------


class TestPublishedVersionsFilter:
    """Filter tags from a raw API list to bare version strings (N.N.N)."""

    def test_rejects_plain_versions_when_no_harness_tags_present(self) -> None:
        """A first release can fail before publishing any harness tag."""
        tags = ["0.11.0", "0.11.1", "0.11.2", "0.11.4"]
        assert pv.published_versions(tags) == []

    def test_rejects_script_harness_prefix(self) -> None:
        tags = ["script-harness-0.11.3", "0.11.3", "script-harness-0.11.4"]
        assert pv.published_versions(tags) == ["0.11.3"]

    def test_rejects_latest(self) -> None:
        tags = ["latest", "0.11.3", "script-harness-0.11.3"]
        assert pv.published_versions(tags) == ["0.11.3"]

    def test_rejects_build_id_tags(self) -> None:
        tags = ["20260916-aaaaaaaaaaaa", "0.11.2", "script-harness-0.11.2"]
        assert pv.published_versions(tags) == ["0.11.2"]

    def test_rejects_partial_versions(self) -> None:
        tags = ["0.11", "0.11.3", "0.11.3.1", "script-harness-0.11.3"]
        assert pv.published_versions(tags) == ["0.11.3"]

    def test_empty_list_returns_empty(self) -> None:
        assert pv.published_versions([]) == []

    def test_no_version_tags_returns_empty(self) -> None:
        tags = ["latest", "script-harness-0.11.0", "20260916-aaa"]
        assert pv.published_versions(tags) == []

    def test_half_published_version_is_excluded(self) -> None:
        """Finding 01M4E3NSPW77SVH0YYRDZV1K5T: a release that pushed <version>
        but failed before script-harness-<version> must not be considered
        published."""
        tags = [
            "0.11.0",
            "script-harness-0.11.0",
            "0.11.3",  # bare but no script-harness-0.11.3
            "0.11.2",
            "script-harness-0.11.2",
        ]
        assert pv.published_versions(tags) == ["0.11.0", "0.11.2"]

    def test_half_published_version_skipped_with_multiple_full(self) -> None:
        """When there are multiple full publishes, the half-published one
        is excluded but others are included."""
        tags = [
            "0.11.0",
            "script-harness-0.11.0",
            "0.11.1",
            "script-harness-0.11.1",
            "0.11.3",  # bare only — half-published
            "0.11.4",
            "script-harness-0.11.4",
        ]
        assert pv.published_versions(tags) == ["0.11.0", "0.11.1", "0.11.4"]

    def test_no_bare_version_with_harness_returns_empty(self) -> None:
        """When there are only harness tags and no bare version tags, return empty."""
        tags = ["script-harness-0.11.0", "script-harness-0.11.1"]
        assert pv.published_versions(tags) == []

    def test_mixed_harness_and_bare_with_no_match(self) -> None:
        """Harness tags exist but no bare version matches any harness."""
        tags = [
            "0.9.9",  # bare but no script-harness-0.9.9
            "script-harness-0.11.0",
            "0.11.0",
            "script-harness-0.11.0",
        ]
        assert pv.published_versions(tags) == ["0.11.0"]


# -- integration with version.py --previous -----------------------------------


class TestPublishedVersionsPrevious:
    """Verify that the published-versions list feeds version.py --previous
    correctly — specifically that a failed git tag (0.11.3) is skipped.

    We exercise the *function* chain (published_versions -> version.py
    --previous) with a fixture list, so no network is needed.
    """

    def test_previous_release_skips_failed_tag(self) -> None:
        """Published versions (with harness): 0.11.0, 0.11.1, 0.11.2, 0.11.4
        (0.11.3 has bare tag but no script-harness).  For candidate 0.11.4,
        --previous must return 0.11.2, not 0.11.3 (which is a git tag but not
        fully published)."""
        # Simulate the full tag list from GHCR, including harness tags.
        all_tags = [
            "0.11.0",
            "script-harness-0.11.0",
            "0.11.1",
            "script-harness-0.11.1",
            "0.11.2",
            "script-harness-0.11.2",
            "0.11.3",  # git tag exists but no harness — half-published
            "0.11.4",
            "script-harness-0.11.4",
        ]
        published = pv.published_versions(all_tags)
        # published is [0.11.0, 0.11.1, 0.11.2, 0.11.4]
        versions_output = "\n".join(published) + "\n"
        ver_stdin = StringIO(versions_output)
        ver_old_stdin = sys.stdin
        sys.stdin = ver_stdin
        try:
            ver_out = StringIO()
            with redirect_stdout(ver_out):
                v.main(["--previous", "0.11.4"])
        finally:
            sys.stdin = ver_old_stdin
        # 0.11.4 is excluded by --previous; highest below is 0.11.2.
        assert ver_out.getvalue().strip() == "0.11.2"

    def test_previous_release_skips_failed_tag_explicit(self) -> None:
        """The real scenario: published versions (with harness) are 0.11.0,
        0.11.1, 0.11.2 (0.11.3 never published).  For 0.11.4, the previous
        published is 0.11.2, not 0.11.3."""
        published = ["0.11.0", "0.11.1", "0.11.2"]
        versions_output = "\n".join(published) + "\n"
        ver_stdin = StringIO(versions_output)
        ver_old_stdin = sys.stdin
        sys.stdin = ver_stdin
        try:
            ver_out = StringIO()
            with redirect_stdout(ver_out):
                v.main(["--previous", "0.11.4"])
        finally:
            sys.stdin = ver_old_stdin
        assert ver_out.getvalue().strip() == "0.11.2"

    def test_no_published_versions_produces_empty(self) -> None:
        """When published_versions prints nothing, version.py --previous
        receives an empty list and should return empty."""
        versions_output = ""
        ver_stdin = StringIO(versions_output + "\n")
        ver_old_stdin = sys.stdin
        sys.stdin = ver_stdin
        try:
            ver_out = StringIO()
            with redirect_stdout(ver_out):
                v.main(["--previous", "0.11.4"])
        finally:
            sys.stdin = ver_old_stdin
        assert ver_out.getvalue().strip() == ""


# -- tag-listing parser: fixture-ing GHCR response shape ----------------------


class TestGhcrTagParsing:
    """Test the published_versions filter against the real GHCR response shape
    (a JSON array called ``tags`` inside the response object)."""

    def test_parses_realistic_ghcr_response(self) -> None:
        """Simulate the GHCR response body that the release workflow's
        'move latest' step paginates.  It includes version tags,
        script-harness tags, and latest."""
        response_body = json.dumps(
            {
                "tags": [
                    "0.11.0",
                    "0.11.1",
                    "0.11.2",
                    "0.11.3",  # published but git tag later failed
                    "0.11.4",
                    "latest",
                    "script-harness-0.11.0",
                    "script-harness-0.11.1",
                    "script-harness-0.11.2",
                    "script-harness-0.11.4",
                ]
            }
        )
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        versions = pv.published_versions(tags)
        # All bare versions that have matching harness tags, excluding latest.
        assert versions == ["0.11.0", "0.11.1", "0.11.2", "0.11.4"]

    def test_parses_realistic_ghcr_response_no_harness(self) -> None:
        """Bare tags without harness tags cannot be reused."""
        response_body = json.dumps(
            {
                "tags": [
                    "0.1.0",
                    "0.2.0",
                    "0.3.0",
                    "latest",
                ]
            }
        )
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        versions = pv.published_versions(tags)
        assert versions == []

    def test_empty_tags_array(self) -> None:
        response_body = json.dumps({"tags": []})
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        assert pv.published_versions(tags) == []

    def test_missing_tags_key(self) -> None:
        response_body = json.dumps({})
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        assert pv.published_versions(tags) == []


# -- CLI smoke test -----------------------------------------------------------


class TestCli:
    """Smoke test the CLI entry point (no network)."""

    def test_cli_rejects_missing_registry(self) -> None:
        """The CLI requires --registry; without it it exits with usage."""
        result = subprocess.run(
            [sys.executable, str(_PUBLISHED_VERSIONS)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "usage" in result.stderr.lower() or "required" in result.stderr.lower()


@pytest.mark.parametrize(
    "registry",
    ["ghcr.io/sentania-labs/crucible-worker", "https://ghcr.io/sentania-labs/crucible-worker"],
)
def test_published_versions_cli_parses_registry_url(registry: str) -> None:
    with patch.object(pv, "fetch_tags", return_value=[]) as fetch:
        assert pv.main(["--registry", registry]) == 0
    fetch.assert_called_once_with("ghcr.io", "sentania-labs/crucible-worker")


def test_published_versions_fetches_authenticated_paginated_ghcr_fixture() -> None:
    responses = []
    for body, headers in [
        ({"token": "anonymous-token"}, {}),
        (
            {"name": "sentania-labs/crucible-worker", "tags": ["0.11.2", "0.11.3"]},
            {"Link": '</v2/sentania-labs/crucible-worker/tags/list?n=100&last=0.11.3>; rel="next"'},
        ),
        ({"name": "sentania-labs/crucible-worker", "tags": ["script-harness-0.11.2"]}, {}),
    ]:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(body).encode()
        response.headers = headers
        responses.append(response)
    with patch.object(pv.urllib.request, "urlopen", side_effect=responses) as urlopen:
        tags = pv.fetch_tags("ghcr.io", "sentania-labs/crucible-worker")
    requests = [call.args[0] for call in urlopen.call_args_list]
    assert requests[0].full_url == (
        "https://ghcr.io/token?scope=repository:sentania-labs/crucible-worker:pull"
    )
    assert requests[0].get_header("Authorization") is None
    assert requests[1].full_url == (
        "https://ghcr.io/v2/sentania-labs/crucible-worker/tags/list?n=100"
    )
    assert requests[2].full_url == (
        "https://ghcr.io/v2/sentania-labs/crucible-worker/tags/list?n=100&last=0.11.3"
    )
    assert all(req.get_header("Authorization") == "Bearer anonymous-token" for req in requests[1:])
    assert pv.published_versions(tags) == ["0.11.2"]


@pytest.mark.parametrize("body", [{"tags": []}, {"tags": None}, {}])
def test_published_versions_empty_ghcr_response(body: dict[str, Any]) -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(body).encode()
    response.headers = {}
    with (
        patch.object(pv, "_anonymous_token", return_value="anonymous-token"),
        patch.object(pv.urllib.request, "urlopen", return_value=response),
    ):
        assert pv.fetch_tags("ghcr.io", "sentania-labs/crucible-worker") == []


def test_previous_release_workflow_uses_published_versions() -> None:
    workflow = (REPOSITORY / ".github/workflows/release.yml").read_text()
    step = workflow.split("- name: find the previous release", 1)[1].split("- name:", 1)[0]
    assert 'published_versions.py --registry "$WORKER_REGISTRY"' in step
    assert '| python3 tools/release/version.py --previous "$VERSION"' in step
    assert "git tag" not in step
