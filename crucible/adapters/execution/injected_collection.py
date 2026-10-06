"""Classify the shell collector's data in the service, never in the worker image."""

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

from crucible.domain.gates import injected_shim_text
from crucible.domain.injected import injected_name, instruction_name_error, normalized_shim_content
from crucible.ports.execution import PathChange

LIMIT = 8 * 1024 * 1024
_META = re.compile(rb":[0-7]{6} [0-7]{6} [0-9a-f]{40,64} ([0-9a-f]{40,64}) ([A-Z])")


def nul_fields(path: Path) -> Iterator[bytes]:
    """Stream complete NUL fields, bounding individual records rather than the tree."""
    with path.open("rb") as handle:
        pending = b""
        while chunk := handle.read(65536):
            fields = (pending + chunk).split(b"\0")
            pending = fields.pop()
            yield from fields
            if len(pending) > LIMIT:
                raise ValueError("path record exceeds 8 MiB")
        if pending:
            raise ValueError("unterminated path record")


def classify_collected(output: Path) -> tuple[tuple[PathChange, ...], tuple[PathChange, ...]]:
    """Read diff and history independently; malformed/truncated records fail closed."""
    cache: dict[str, str] = {}

    def classify(blob: str) -> str:
        if blob not in cache:
            try:
                with (output / "injected-blobs" / blob).open("rb") as handle:
                    content = handle.read(LIMIT + 1)
                if len(content) > LIMIT:
                    raise ValueError("blob exceeds 8 MiB classification limit")
                digest = hashlib.sha1 if len(blob) == 40 else hashlib.sha256
                if digest(b"blob %d\0" % len(content) + content).hexdigest() != blob:
                    raise ValueError("blob content does not match object id")
                body = content.decode("utf-8")
                cache[blob] = (
                    "shim"
                    if normalized_shim_content(body)
                    == normalized_shim_content(injected_shim_text())
                    else "plain"
                )
            except (OSError, ValueError) as exc:
                cache[blob] = f"error: unreadable blob {blob}: {exc}"
        return cache[blob]

    def changes(filename: str) -> tuple[PathChange, ...]:
        kept: list[PathChange] = []
        try:
            fields = iter(nul_fields(output / filename))
            for raw_header in fields:
                header = raw_header.lstrip(b"\n")
                if not header:
                    continue
                match = _META.fullmatch(header)
                raw_path = next(fields, None)
                if match is None or not raw_path:
                    raise ValueError("malformed raw path record (missing header or path)")
                path = raw_path.decode("utf-8", "surrogateescape")
                if not injected_name(path):
                    continue
                blob, status = (part.decode("ascii") for part in match.groups())
                error = instruction_name_error(path)
                classification = (
                    f"error: {error}" if error else "deleted" if status == "D" else classify(blob)
                )
                kept.append(
                    PathChange(ascii(path) if error else path, status, blob, classification)
                )
        except (OSError, ValueError) as exc:
            kept.append(PathChange("", "", "", f"error: {filename}: {exc}"))
        return tuple(kept)

    diff = changes("diff-raw.txt")
    history = changes("commit-raw.txt")
    try:
        for raw in nul_fields(output / "base-injected.txt"):
            raw.decode("utf-8")
        if (output / "injected-error.txt").exists():
            raise ValueError("Git could not export instruction records")
    except (OSError, ValueError) as exc:
        diff += (PathChange("", "", "", f"error: base names or collection: {exc}"),)
    return diff, history
