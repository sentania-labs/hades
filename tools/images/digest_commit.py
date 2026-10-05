"""CI owns the digest lines of images/manifest.env (FDY-0310).

The images job rebuilds every image from the pinned inputs. A tag or a harness version
that the build does not reproduce is a build-input mismatch and fails the job. A digest
that differs while every tag and harness version reproduces is only the record of what
the build produced, so on a branch CI commits the built digests as the CI bot instead of
failing; CI then runs again on that commit. On main, on a tag, and on the bot's own
digest commit it never commits, and the job fails as before.

Two halves, so that no code from the branch ever runs with a write token (the Codex
finding on PR 420). The images job in ci.yml runs the branch's own copy of this file
with the read-only token: `apply` (through images.sh) and `extract`, whose output it
uploads as the `images-digests` artifact. The images-digest workflow, triggered by
workflow_run and therefore defined on the default branch, runs the default branch's copy
of this file with the write token: `may-commit` and `commit-artifact`, which treats the
artifact as untrusted data and re-validates it against the branch's manifest itself.

    digest_commit.py apply DECLARED BUILT        rewrite DECLARED's *_DIGEST values from
                                                 BUILT when nothing else differs; exit 1
                                                 when anything else differs
    digest_commit.py extract MANIFEST OUT        write MANIFEST's *_DIGEST lines to OUT,
                                                 the artifact the images job uploads
    digest_commit.py may-commit --event E --ref R [--default-branch B] [--head-message M]
                                                 exit 0 when this run may commit digests
    digest_commit.py commit-artifact --repository PATH --artifact-dir DIR
                                                 validate the downloaded artifact against
                                                 PATH's manifest and commit the digest-only
                                                 change as the CI bot; exit 1 on anything
                                                 else
    digest_commit.py commit [--manifest PATH]    commit the digest-only change to the
                                                 manifest as the CI bot; refuses any other
                                                 change in the working tree

Only the standard library: images.sh calls `apply` on the release runner too, and the
privileged workflow runs this file with the runner's python3 and nothing installed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DIGEST_SUFFIX = "_DIGEST"
MANIFEST = "images/manifest.env"
# The trailer that marks the bot's own commit. A run whose head carries it never
# commits again, so a build that does not reproduce its own digest fails instead of
# looping.
TRAILER = "Crucible-Images-Digest: ci"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
# The artifact the images job uploads and the privileged workflow downloads: one file of
# `KEY_DIGEST=sha256:<64 hex>` lines and nothing else.
ARTIFACT_FILE = "digests.env"
ARTIFACT_MAX_BYTES = 4096
_ARTIFACT_LINE = re.compile(r"([A-Z][A-Z0-9_]*_DIGEST)=(sha256:[0-9a-f]{64})")


@dataclass(frozen=True)
class Comparison:
    """How a built manifest differs from the declared one."""

    # Every line other than a digest value differs nowhere (tags, harness lists, keys,
    # order and the header comments all match).
    inputs_reproduced: bool
    # The keys (`WORKER`, `SCRIPT_HARNESS`) whose digest differs.
    changed_digests: tuple[str, ...]

    @property
    def digests_only(self) -> bool:
        return self.inputs_reproduced and bool(self.changed_digests)


def _digest_key(line: str) -> str | None:
    key, sep, _ = line.partition("=")
    if sep and not line.startswith("#") and key.endswith(DIGEST_SUFFIX):
        return key
    return None


def compare(declared: str, built: str) -> Comparison:
    """Compare two manifests line by line, digest values masked."""
    declared_lines = declared.splitlines()
    built_lines = built.splitlines()
    if len(declared_lines) != len(built_lines) or declared.endswith("\n") != built.endswith("\n"):
        return Comparison(inputs_reproduced=False, changed_digests=())
    changed: list[str] = []
    for old, new in zip(declared_lines, built_lines, strict=True):
        old_key, new_key = _digest_key(old), _digest_key(new)
        if old_key is None or new_key is None:
            if old != new:
                return Comparison(inputs_reproduced=False, changed_digests=())
        elif old_key != new_key:
            return Comparison(inputs_reproduced=False, changed_digests=())
        elif old != new:
            changed.append(old_key.removesuffix(DIGEST_SUFFIX))
    return Comparison(inputs_reproduced=True, changed_digests=tuple(changed))


def image_name(key: str) -> str:
    """`SCRIPT_HARNESS` -> `script-harness`, the image directory under images/."""
    return key.lower().replace("_", "-")


def commit_message(keys: tuple[str, ...]) -> str:
    names = ", ".join(image_name(key) for key in keys)
    noun = "image" if len(keys) == 1 else "images"
    return (
        f"Record the CI-built digest of the {names} {noun}\n"
        "\n"
        "The images job rebuilt every image from the pinned inputs. Every tag and\n"
        "harness version reproduced; only the *_DIGEST lines of images/manifest.env\n"
        "differed, and CI owns those lines.\n"
        "\n"
        f"{TRAILER}\n"
    )


def may_commit(
    *, event: str, ref: str, head_message: str, default_branch: str = "main"
) -> tuple[bool, str]:
    """Whether this CI run may commit corrected digests, and why not."""
    if event not in {"push", "workflow_dispatch"}:
        return False, f"event {event!r} never commits digests"
    if not ref.startswith("refs/heads/"):
        return False, f"{ref} is not a branch; tag builds never commit digests"
    if ref == f"refs/heads/{default_branch}":
        return False, f"{ref} is the default branch; it never commits digests"
    if TRAILER in head_message.splitlines():
        return False, "the head is CI's own digest commit; it never commits again"
    return True, f"{ref} may commit corrected digests"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout


def commit(repository: Path, manifest: str = MANIFEST) -> tuple[str, ...]:
    """Commit the digest-only change to the manifest; raise on anything else.

    Returns the keys whose digests changed, empty when there was nothing to commit.
    """
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=repository)
    changed_paths = {line[3:] for line in status.splitlines() if line}
    if not changed_paths:
        return ()
    if changed_paths != {manifest}:
        raise ValueError(
            f"refusing the digest commit: the working tree changes {sorted(changed_paths)}, "
            f"not only {manifest}"
        )
    declared = _git("show", f"HEAD:{manifest}", cwd=repository)
    built = (repository / manifest).read_text(encoding="utf-8")
    comparison = compare(declared, built)
    if not comparison.digests_only:
        raise ValueError(
            f"refusing the digest commit: {manifest} changes more than its digest lines"
        )
    _git(
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        commit_message(comparison.changed_digests),
        "--",
        manifest,
        cwd=repository,
    )
    return comparison.changed_digests


def digest_lines(manifest: str) -> str:
    """The manifest's *_DIGEST lines, in order: the content of the artifact."""
    lines = [line for line in manifest.splitlines() if _digest_key(line) is not None]
    return "".join(f"{line}\n" for line in lines)


def parse_artifact(text: str) -> dict[str, str]:
    """Read the artifact as untrusted input: `KEY_DIGEST=sha256:<hex>` lines only."""
    digests: dict[str, str] = {}
    for line in text.splitlines():
        match = _ARTIFACT_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"the artifact has a line that is not a digest: {line[:80]!r}")
        key, value = match.groups()
        if key in digests:
            raise ValueError(f"the artifact names {key} twice")
        digests[key] = value
    if not digests:
        raise ValueError("the artifact names no digest")
    return digests


def apply_artifact(declared: str, artifact: str) -> tuple[str, Comparison]:
    """The branch's manifest with the artifact's digests; raise unless only digests move.

    Independent of whatever the images job decided: the artifact must name exactly the
    manifest's digest keys, and the result must compare as a digest-only change.
    """
    digests = parse_artifact(artifact)
    declared_keys = [key for line in declared.splitlines() if (key := _digest_key(line))]
    if sorted(declared_keys) != sorted(digests):
        raise ValueError(
            f"the artifact names {sorted(digests)}, the manifest declares {sorted(declared_keys)}"
        )
    lines = []
    for line in declared.splitlines(keepends=True):
        key = _digest_key(line)
        if key is not None:
            ending = "\n" if line.endswith("\n") else ""
            lines.append(f"{key}={digests[key]}{ending}")
        else:
            lines.append(line)
    updated = "".join(lines)
    comparison = compare(declared, updated)
    if not comparison.inputs_reproduced:
        raise ValueError("the artifact would change more than the manifest's digest lines")
    return updated, comparison


def read_artifact(directory: Path) -> str:
    """The downloaded artifact's one file, refusing anything else in the directory."""
    entries = sorted(entry.name for entry in directory.iterdir())
    if entries != [ARTIFACT_FILE]:
        raise ValueError(f"the artifact holds {entries}, not only {ARTIFACT_FILE}")
    path = directory / ARTIFACT_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"the artifact's {ARTIFACT_FILE} is not a regular file")
    if path.stat().st_size > ARTIFACT_MAX_BYTES:
        raise ValueError(
            f"the artifact's {ARTIFACT_FILE} is larger than {ARTIFACT_MAX_BYTES} bytes"
        )
    return path.read_text(encoding="utf-8")


def commit_artifact(
    repository: Path, artifact_dir: Path, manifest: str = MANIFEST
) -> tuple[str, ...]:
    """Validate the artifact against the branch checkout's manifest and commit it.

    Runs in the privileged workflow from the default branch's copy of this file. The
    checkout is the branch's, so it is data: the manifest is read from the HEAD blob,
    must be a regular file there, and the working tree must be clean before the write.
    Returns the keys whose digests changed, empty when the artifact matches the branch.
    """
    if _git("status", "--porcelain", "--untracked-files=all", cwd=repository):
        raise ValueError("refusing the digest commit: the checkout is not clean")
    entry = _git("ls-tree", "HEAD", "--", manifest, cwd=repository).split()
    if not entry or entry[0] != "100644" or entry[1] != "blob":
        raise ValueError(f"refusing the digest commit: {manifest} is not a regular file on HEAD")
    declared = _git("show", f"HEAD:{manifest}", cwd=repository)
    updated, comparison = apply_artifact(declared, read_artifact(artifact_dir))
    if not comparison.changed_digests:
        return ()
    path = repository / manifest
    # Replace rather than write through whatever the path is now.
    path.unlink()
    path.write_text(updated, encoding="utf-8")
    return commit(repository, manifest)


def _apply(declared_path: Path, built_path: Path) -> int:
    declared = declared_path.read_text(encoding="utf-8")
    built = built_path.read_text(encoding="utf-8")
    comparison = compare(declared, built)
    if not comparison.inputs_reproduced:
        print(
            "digest_commit.py: a tag, harness version or entry differs, not only a digest",
            file=sys.stderr,
        )
        return 1
    if comparison.changed_digests:
        # Equal outside the digest values, so the built file is the corrected one.
        declared_path.write_text(built, encoding="utf-8")
        names = ", ".join(image_name(key) for key in comparison.changed_digests)
        print(f"digest_commit.py: wrote the built digest of {names} to {declared_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("declared", type=Path)
    apply_parser.add_argument("built", type=Path)
    may_parser = commands.add_parser("may-commit")
    may_parser.add_argument("--event", required=True)
    may_parser.add_argument("--ref", required=True)
    may_parser.add_argument("--default-branch", default="main")
    may_parser.add_argument("--head-message", default="")
    extract_parser = commands.add_parser("extract")
    extract_parser.add_argument("manifest", type=Path)
    extract_parser.add_argument("out", type=Path)
    artifact_parser = commands.add_parser("commit-artifact")
    artifact_parser.add_argument("--repository", type=Path, required=True)
    artifact_parser.add_argument("--artifact-dir", type=Path, required=True)
    artifact_parser.add_argument("--manifest", default=MANIFEST)
    commit_parser = commands.add_parser("commit")
    commit_parser.add_argument("--repository", type=Path, default=Path.cwd())
    commit_parser.add_argument("--manifest", default=MANIFEST)
    args = parser.parse_args(argv)

    if args.command == "apply":
        return _apply(args.declared, args.built)
    if args.command == "may-commit":
        allowed, reason = may_commit(
            event=args.event,
            ref=args.ref,
            head_message=args.head_message,
            default_branch=args.default_branch,
        )
        print(f"digest_commit.py: {reason}")
        return 0 if allowed else 1
    if args.command == "extract":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            digest_lines(args.manifest.read_text(encoding="utf-8")), encoding="utf-8"
        )
        return 0
    try:
        if args.command == "commit-artifact":
            keys = commit_artifact(args.repository, args.artifact_dir, args.manifest)
        else:
            keys = commit(args.repository, args.manifest)
    except ValueError as error:
        print(f"digest_commit.py: {error}", file=sys.stderr)
        return 1
    if keys:
        print(f"digest_commit.py: committed the digest of {', '.join(map(image_name, keys))}")
    else:
        print("digest_commit.py: nothing to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
