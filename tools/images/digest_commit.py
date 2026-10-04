"""CI owns the digest lines of images/manifest.env (FDY-0310).

The images job rebuilds every image from the pinned inputs. A tag or a harness version
that the build does not reproduce is a build-input mismatch and fails the job. A digest
that differs while every tag and harness version reproduces is only the record of what
the build produced, so on a branch the job writes the built digests back and commits
them as the CI bot instead of failing; CI then runs again on that commit. On main, on a
tag, and on the bot's own digest commit it never commits, and the job fails as before.

    digest_commit.py apply DECLARED BUILT        rewrite DECLARED's *_DIGEST values from
                                                 BUILT when nothing else differs; exit 1
                                                 when anything else differs
    digest_commit.py may-commit --event E --ref R [--default-branch B] [--head-message M]
                                                 exit 0 when this run may commit digests
    digest_commit.py commit [--manifest PATH]    commit the digest-only change to the
                                                 manifest as the CI bot; refuses any other
                                                 change in the working tree

Only the standard library: images.sh calls `apply` on the release runner too, where
nothing but python3 is guaranteed.
"""

from __future__ import annotations

import argparse
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
    try:
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
