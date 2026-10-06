from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from tools.images import harness_pins


def codex_release() -> harness_pins.Release:
    payload = {
        "tag_name": "rust-v0.156.1",
        "html_url": "https://github.com/openai/codex/releases/tag/rust-v0.156.1",
        "assets": [
            {
                "name": "codex-x86_64-unknown-linux-musl.tar.gz",
                "browser_download_url": "https://fixtures/codex",
            },
            {
                "name": "codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz",
                "browser_download_url": "https://fixtures/host",
            },
        ],
    }
    release = harness_pins.discover(
        "codex",
        get_json=lambda _url: payload,
        get_bytes=lambda url: {
            "https://fixtures/codex": b"codex",
            "https://fixtures/host": b"host",
        }[url],
    )
    assert release.sha256 == hashlib.sha256(b"codex").hexdigest()
    return release


def test_new_codex_release_rewrites_only_codex() -> None:
    dockerfile = """ARG HARNESS_CLAUDE_CODE_VERSION=2.1.280
ARG HARNESS_CLAUDE_CODE_SHA256=claude
ARG HARNESS_CODEX_VERSION=0.156.0
ARG HARNESS_CODEX_SHA256=old-codex
ARG CODE_MODE_HOST_SHA256=old-host
ARG HARNESS_AGY_VERSION=1.2.8
ARG HARNESS_HERMES_VERSION=0.19.0
"""
    manifest = "WORKER_HARNESSES=agy:1.2.8,claude_code:2.1.280,codex:0.156.0,hermes:0.19.0\n"
    update = harness_pins.inspect_update(codex_release(), dockerfile, ">=0.153.0,<0.157.0")

    rewritten, rewritten_manifest = harness_pins.rewrite(update, dockerfile, manifest)

    assert "ARG HARNESS_CODEX_VERSION=0.156.1" in rewritten
    assert f"ARG HARNESS_CODEX_SHA256={hashlib.sha256(b'codex').hexdigest()}" in rewritten
    assert f"ARG CODE_MODE_HOST_SHA256={hashlib.sha256(b'host').hexdigest()}" in rewritten
    assert "ARG HARNESS_CLAUDE_CODE_SHA256=claude" in rewritten
    assert "agy:1.2.8,claude_code:2.1.280,codex:0.156.1,hermes:0.19.0" in rewritten_manifest


def test_out_of_range_release_is_reported_but_not_pinned() -> None:
    dockerfile = "ARG HARNESS_CODEX_VERSION=0.156.0\nARG HARNESS_CODEX_SHA256=old\n"
    release = harness_pins.Release("codex", "0.157.0", "artifact", "changes", "new")
    update = harness_pins.inspect_update(release, dockerfile, ">=0.153.0,<0.157.0")

    assert not update.supported
    assert harness_pins.rewrite(update, dockerfile, "WORKER_HARNESSES=codex:0.156.0\n") == (
        dockerfile,
        "WORKER_HARNESSES=codex:0.156.0\n",
    )


def test_agy_manifest_supplies_version_url_and_build() -> None:
    artifact = (
        "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
        "1.2.9-123456/linux-x64/cli_linux_x64.tar.gz"
    )
    release = harness_pins.discover(
        "agy",
        get_json=lambda _url: {"version": "1.2.9", "url": artifact, "sha512": "ignored"},
        get_bytes=lambda _url: b"agy",
    )

    assert (release.version, release.build, release.artifact_url) == ("1.2.9", "123456", artifact)


def test_dry_run_writes_body_without_changing_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    manifest = tmp_path / "manifest.env"
    body = tmp_path / "body.md"
    dockerfile.write_text(
        "ARG HARNESS_CODEX_VERSION=0.156.0\nARG HARNESS_CODEX_SHA256=old\n", encoding="utf-8"
    )
    manifest.write_text("WORKER_HARNESSES=codex:0.156.0\n", encoding="utf-8")
    original = dockerfile.read_text(encoding="utf-8")
    release = codex_release()
    monkeypatch.setattr(harness_pins, "discover", lambda _name: release)
    monkeypatch.setattr(harness_pins, "supported_range", lambda _name: ">=0.153.0,<0.157.0")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harness_pins.py",
            "--harness",
            "codex",
            "--dockerfile",
            str(dockerfile),
            "--manifest",
            str(manifest),
            "--body",
            str(body),
            "--dry-run",
        ],
    )

    assert harness_pins.main() == 0
    assert dockerfile.read_text(encoding="utf-8") == original
    assert "Promotion stays an explicit admin action per harness" in body.read_text(
        encoding="utf-8"
    )
