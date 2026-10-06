"""Discover and rewrite worker harness pins (hades #433)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from packaging.specifiers import SpecifierSet

from crucible.adapters.harness.registry import default_registry

JsonGet = Callable[[str], dict[str, Any]]
BytesGet = Callable[[str], bytes]


@dataclass(frozen=True)
class Release:
    name: str
    version: str
    artifact_url: str
    changelog_url: str
    sha256: str
    build: str | None = None
    code_mode_host_sha256: str | None = None


@dataclass(frozen=True)
class Update:
    release: Release
    current_version: str
    supported_range: str
    supported: bool


def _json_get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "hades-harness-pins"})
    with urllib.request.urlopen(request, timeout=30) as response:
        value: Any = json.load(response)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object from {url}")
    return value


def _bytes_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hades-harness-pins"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return cast(bytes, response.read())


def _sha256(url: str, get_bytes: BytesGet) -> str:
    return hashlib.sha256(get_bytes(url)).hexdigest()


def discover(name: str, get_json: JsonGet = _json_get, get_bytes: BytesGet = _bytes_get) -> Release:
    """Look up one release through the same publisher endpoint used by the image."""
    if name == "claude_code":
        data = get_json("https://registry.npmjs.org/@anthropic-ai/claude-code/latest")
        version = str(data["version"])
        artifact = f"https://downloads.claude.ai/claude-code-releases/{version}/linux-x64/claude"
        return Release(
            name,
            version,
            artifact,
            "https://www.npmjs.com/package/@anthropic-ai/claude-code",
            _sha256(artifact, get_bytes),
        )
    if name == "codex":
        data = get_json("https://api.github.com/repos/openai/codex/releases/latest")
        version = str(data["tag_name"]).removeprefix("rust-v")
        assets = {
            str(asset["name"]): str(asset["browser_download_url"]) for asset in data["assets"]
        }
        binary = assets["codex-x86_64-unknown-linux-musl.tar.gz"]
        host = assets["codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz"]
        return Release(
            name,
            version,
            binary,
            str(data["html_url"]),
            _sha256(binary, get_bytes),
            code_mode_host_sha256=_sha256(host, get_bytes),
        )
    if name == "agy":
        data = get_json(
            "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/"
            "manifests/linux_amd64.json"
        )
        version, artifact = str(data["version"]), str(data["url"])
        build_match = re.search(rf"/{re.escape(version)}-(\d+)/", artifact)
        if build_match is None:
            raise ValueError("AGY manifest URL does not contain its build number")
        build = build_match.group(1)
        return Release(
            name,
            version,
            artifact,
            "https://antigravity.google/changelog",
            _sha256(artifact, get_bytes),
            build=build,
        )
    if name == "hermes":
        data = get_json("https://pypi.org/pypi/hermes-agent/json")
        version = str(data["info"]["version"])
        wheel = next(
            item for item in data["urls"] if str(item["filename"]).endswith("-py3-none-any.whl")
        )
        artifact = str(wheel["url"])
        return Release(
            name,
            version,
            artifact,
            str(data["info"].get("project_url") or "https://pypi.org/project/hermes-agent/"),
            _sha256(artifact, get_bytes),
        )
    raise ValueError(f"unknown harness: {name}")


def current_version(dockerfile: str, name: str) -> str:
    match = re.search(rf"^ARG HARNESS_{name.upper()}_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    if match is None:
        raise ValueError(f"missing {name} version ARG")
    return match.group(1)


def inspect_update(release: Release, dockerfile: str, supported_range: str) -> Update:
    return Update(
        release,
        current_version(dockerfile, release.name),
        supported_range,
        release.version in SpecifierSet(supported_range),
    )


def rewrite(update: Update, dockerfile: str, manifest: str) -> tuple[str, str]:
    """Rewrite only this harness. Refuse a release outside the adapter contract."""
    if not update.supported:
        return dockerfile, manifest
    name, release = update.release.name, update.release
    dockerfile = re.sub(
        rf"^(ARG HARNESS_{name.upper()}_VERSION=)\S+$",
        rf"\g<1>{release.version}",
        dockerfile,
        flags=re.MULTILINE,
    )
    dockerfile = re.sub(
        rf"^(ARG HARNESS_{name.upper()}_SHA256=)\S+$",
        rf"\g<1>{release.sha256}",
        dockerfile,
        flags=re.MULTILINE,
    )
    if release.build is not None:
        dockerfile = re.sub(
            r"^(ARG HARNESS_AGY_BUILD=)\S+$",
            rf"\g<1>{release.build}",
            dockerfile,
            flags=re.MULTILINE,
        )
    if release.code_mode_host_sha256 is not None:
        dockerfile = re.sub(
            r"^(ARG CODE_MODE_HOST_SHA256=)\S+$",
            rf"\g<1>{release.code_mode_host_sha256}",
            dockerfile,
            flags=re.MULTILINE,
        )
    if name == "hermes":
        dockerfile, count = re.subn(
            r"https://files\.pythonhosted\.org/\S+/hermes_agent-"
            r"(?:\$\{HARNESS_HERMES_VERSION\}|[0-9.]+)-py3-none-any\.whl",
            release.artifact_url,
            dockerfile,
        )
        if count != 1:
            raise ValueError("expected one Hermes wheel download URL")
    manifest = re.sub(
        rf"(?<=WORKER_HARNESSES=)([^\n]*\b{name}:){re.escape(update.current_version)}\b",
        rf"\g<1>{release.version}",
        manifest,
    )
    return dockerfile, manifest


def hermes_lock(release: Release, lock: Path) -> str:
    """Resolve the new Hermes dependency closure before changing any tracked files."""
    with tempfile.TemporaryDirectory(prefix="harness-pins-") as directory:
        candidate = Path(directory) / "requirements.lock"
        # Reuse compatible dependency pins, while allowing the new release to
        # change its dependency closure. Never resolve against the host Python.
        candidate.write_text(lock.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                "-",
                "--python-version",
                "3.11",
                "--python-platform",
                "x86_64-manylinux_2_36",
                "--generate-hashes",
                "--no-emit-index-url",
                "--only-binary",
                ":all:",
                "--upgrade-package",
                "hermes-agent",
                "--no-header",
                "--output-file",
                str(candidate),
            ],
            input=f"hermes-agent=={release.version}\n",
            text=True,
            check=True,
        )
        result = candidate.read_text(encoding="utf-8")
    entries = re.split(r"(?m)^(?=[a-zA-Z0-9])", result)
    entry = next(
        (item for item in entries if item.startswith(f"hermes-agent=={release.version} ")), ""
    )
    if f"--hash=sha256:{release.sha256}" not in entry:
        raise ValueError("Hermes lock does not include the discovered wheel checksum")
    return "# Generated by tools/images/harness_pins.py for worker Python 3.11.\n" + result


def supported_range(name: str) -> str:
    return default_registry().require(name).supported_versions.text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--harness", required=True, choices=("claude_code", "codex", "agy", "hermes")
    )
    parser.add_argument("--dockerfile", type=Path, default=Path("images/worker/Dockerfile"))
    parser.add_argument("--manifest", type=Path, default=Path("images/manifest.env"))
    parser.add_argument("--lock", type=Path, default=Path("images/worker/requirements.lock"))
    parser.add_argument("--body", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dockerfile = args.dockerfile.read_text(encoding="utf-8")
    manifest = args.manifest.read_text(encoding="utf-8")
    update = inspect_update(discover(args.harness), dockerfile, supported_range(args.harness))
    changed = update.current_version != update.release.version
    status = "supported" if update.supported else "outside the adapter supported_versions range"
    body = (
        f"Updates {args.harness} from {update.current_version} "
        f"to {update.release.version}.\n\n"
        f"Adapter range: `{update.supported_range}` ({status}).\n\n"
        f"Changelog: {update.release.changelog_url}\n\n"
        "Promotion stays an explicit admin action per harness, as required by ADR 0018.\n"
    )
    if args.body is not None:
        args.body.write_text(body, encoding="utf-8")
    if changed and update.supported and not args.dry_run:
        new_dockerfile, new_manifest = rewrite(update, dockerfile, manifest)
        if args.harness == "hermes":
            new_lock = hermes_lock(update.release, args.lock)
            args.lock.write_text(new_lock, encoding="utf-8")
        args.dockerfile.write_text(new_dockerfile, encoding="utf-8")
        args.manifest.write_text(new_manifest, encoding="utf-8")
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"changed={str(changed).lower()}\n")
            output.write(f"supported={str(update.supported).lower()}\n")
            output.write(f"version={update.release.version}\n")
    print(
        json.dumps(
            {"changed": changed, "supported": update.supported, "version": update.release.version}
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
