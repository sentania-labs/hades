"""Trusted collector code embedded by scripts, never imported from the worker tree."""

from crucible.domain.injected import injected_name, instruction_name_error, normalized_shim_content


def collect_injected(
    repo: str,
    output: str,
    base: str,
    merge_base: str,
    git: list[str],
    shim: str,
) -> None:
    # Imports inside the function keep its source independently executable under -I.
    import json  # noqa: PLC0415
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    def run(*args: str) -> bytes:
        result = subprocess.run([*git, "-C", repo, *args], capture_output=True, check=False)
        if result.returncode:
            raise ValueError(f"git {args[0]} failed (exit {result.returncode})")
        return result.stdout

    cache: dict[str, str] = {}

    def classify(blob: str) -> str:
        if blob not in cache:
            try:
                if int(run("cat-file", "-s", blob)) > 8 * 1024 * 1024:
                    raise ValueError("blob exceeds 8 MiB classification limit")
                body = run("cat-file", "blob", blob).decode("utf-8")
                cache[blob] = (
                    "shim"
                    if normalized_shim_content(body) == normalized_shim_content(shim)
                    else "plain"
                )
            except (ValueError, UnicodeError) as exc:
                cache[blob] = f"error: unreadable blob {blob}: {exc}"
        return cache[blob]

    meta = re.compile(rb":[0-7]{6} [0-7]{6} [0-9a-f]{40,64} ([0-9a-f]{40,64}) ([A-Z])")
    commands = {
        "diff-raw.txt": ["diff", "--raw", "-z", "--no-renames", "--no-abbrev", merge_base, "HEAD"],
        "commit-raw.txt": [
            "log",
            "--root",
            "--full-history",
            "--diff-merges=separate",
            "--topo-order",
            "--raw",
            "-z",
            "--no-renames",
            "--no-abbrev",
            "--format=",
            f"{base}..HEAD",
        ],
    }
    for filename, args in commands.items():
        changes: list[dict[str, str]] = []
        try:
            fields = run(*args).split(b"\0")
            at = 0
            while at < len(fields):
                header = fields[at].lstrip(b"\n")
                at += 1
                if not header:
                    continue
                match = meta.fullmatch(header)
                if match is None or at >= len(fields) or not fields[at]:
                    raise ValueError("malformed raw path record")
                path = fields[at].decode("utf-8", "surrogateescape")
                at += 1
                if not injected_name(path):
                    continue
                blob, status = (p.decode("ascii") for p in match.groups())
                error = instruction_name_error(path)
                classification = (
                    f"error: {error}" if error else "deleted" if status == "D" else classify(blob)
                )
                # Escape undecodable bytes before JSON enters evidence storage.
                if error:
                    path = ascii(path)
                changes.append(
                    dict(path=path, status=status, blob=blob, classification=classification)
                )
        except (OSError, ValueError) as exc:
            changes.append(dict(path="", status="", blob="", classification=f"error: {exc}"))
        Path(output, filename).write_text(json.dumps({"version": 1, "changes": changes}))
    try:
        paths = run("ls-tree", "-r", "--name-only", "-z", merge_base).split(b"\0")
        kept = []
        for raw in paths:
            if not raw:
                continue
            path = raw.decode("utf-8")
            if injected_name(path):
                kept.append(raw)
        Path(output, "base-injected.txt").write_bytes(b"\0".join(kept) + b"\0")
    except (OSError, ValueError) as exc:
        # Record base-list failures in the same error channel as path/blob failures.
        target = Path(output, "diff-raw.txt")
        payload = json.loads(target.read_text())
        payload["changes"].append(
            dict(path="", status="", blob="", classification=f"error: base names: {exc}")
        )
        target.write_text(json.dumps(payload))
