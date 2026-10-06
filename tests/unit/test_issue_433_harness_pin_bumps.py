from __future__ import annotations

import hashlib
import subprocess
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


@pytest.mark.parametrize(
    "mode", ["update", "resolver_failure", "wrong_hash", "dry_run", "unsupported"]
)
def test_hermes_update_keeps_lock_and_pins_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    dockerfile, manifest, lock = (tmp_path / name for name in ("Dockerfile", "manifest", "lock"))
    original = (
        "ARG HARNESS_HERMES_VERSION=0.19.0\nARG HARNESS_HERMES_SHA256=old\n"
        "    https://files.pythonhosted.org/packages/old/"
        "hermes_agent-${HARNESS_HERMES_VERSION}-py3-none-any.whl \\\n"
    )
    dockerfile.write_text(original)
    manifest.write_text("WORKER_HARNESSES=codex:0.156.0,hermes:0.19.0\n")
    lock.write_text("hermes-agent==0.19.0\nold-dependency==1.0\n")
    before = [path.read_text() for path in (dockerfile, manifest, lock)]
    artifact = "https://files.pythonhosted.org/packages/new/hermes_agent-0.19.1-py3-none-any.whl"
    release = harness_pins.discover(
        "hermes",
        get_json=lambda _: {
            "info": {"version": "0.19.1"},
            "urls": [{"filename": "hermes_agent-0.19.1-py3-none-any.whl", "url": artifact}],
        },
        get_bytes=lambda _: b"wheel",
    )
    monkeypatch.setattr(harness_pins, "discover", lambda _: release)
    monkeypatch.setattr(
        harness_pins, "supported_range", lambda _: "<0.19.1" if mode == "unsupported" else "<0.20"
    )
    calls = []

    def compile_lock(command: list[str], *, input: str, text: bool, check: bool) -> None:
        calls.append(command)
        assert mode not in ("dry_run", "unsupported")
        assert input == "hermes-agent==0.19.1\n" and text and check
        for flag, value in (
            ("--python-version", "3.11"),
            ("--python-platform", "x86_64-manylinux_2_36"),
            ("--upgrade-package", "hermes-agent"),
            ("--only-binary", ":all:"),
        ):
            assert command[command.index(flag) + 1] == value
        assert "--generate-hashes" in command
        candidate = Path(command[-1])
        assert candidate.read_text() == before[2]
        digest = "wrong" if mode == "wrong_hash" else release.sha256
        candidate.write_text(
            f"hermes-agent==0.19.1 \\\n    --hash=sha256:{digest}\n"
            "new-dependency==2.0 \\\n    --hash=sha256:dependency\n"
        )
        if mode == "resolver_failure":
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(harness_pins.subprocess, "run", compile_lock)
    argv = [
        "harness_pins.py",
        "--harness",
        "hermes",
        "--dockerfile",
        str(dockerfile),
        "--manifest",
        str(manifest),
        "--lock",
        str(lock),
    ]
    if mode == "dry_run":
        argv.append("--dry-run")
    monkeypatch.setattr(sys, "argv", argv)
    if mode in ("resolver_failure", "wrong_hash"):
        with pytest.raises((subprocess.CalledProcessError, ValueError)):
            harness_pins.main()
    else:
        assert harness_pins.main() == 0
    if mode == "update":
        assert "HARNESS_HERMES_VERSION=0.19.1" in dockerfile.read_text()
        assert f"HARNESS_HERMES_SHA256={release.sha256}" in dockerfile.read_text()
        assert artifact in dockerfile.read_text()
        assert manifest.read_text() == "WORKER_HARNESSES=codex:0.156.0,hermes:0.19.1\n"
        assert "hermes-agent==0.19.1" in lock.read_text()
        assert "new-dependency==2.0" in lock.read_text()
        assert "old-dependency" not in lock.read_text()
    else:
        assert [path.read_text() for path in (dockerfile, manifest, lock)] == before
    assert len(calls) == (0 if mode in ("dry_run", "unsupported") else 1)
