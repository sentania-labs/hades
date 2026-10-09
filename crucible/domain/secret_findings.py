"""How a secret-scan match is told to people (FDY-0618, hades #488, #556, #559).

A `no_secrets` finding is a dict on the scanner_result evidence row: `where` (the input:
`diff:<path>`, `artifact:<name>`, `report...`, `commit[i].message`), `pattern` (the
rule), `excerpt` (the value's first four and last three characters and its length),
and, when known, `line`, `context` (the redacted text around the match on that line),
`command` (for a transcript, the redacted command whose output or input held it),
`window` (the artifact holding the redacted transcript lines around it) and `advisory`
(the value is a fixture the repository declares). Nothing here ever sees a value.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from crucible.domain.secrets import redact_line

# What the attempt keeps when the transcript matched: the redacted lines around each
# match, as one small artifact.
TRANSCRIPT_WINDOW_ARTIFACT = "secret-scan/transcript-windows.txt"
TRANSCRIPT_WINDOW_TYPE = "secret_scan_window"
WINDOW_BEFORE = 3
WINDOW_AFTER = 3
WINDOW_LINE_LIMIT = 400
# How far back from a match a transcript is read for the command that produced it.
COMMAND_LOOKBACK = 200
COMMAND_LIMIT = 300
# JSON command attribution is optional; do not repeatedly parse megabyte tool output.
COMMAND_INPUT_LIMIT = 64 * 1024
# At most this many windows go into the artifact.
WINDOW_LIMIT = 10


def is_transcript(name: str) -> bool:
    """An artifact that is a harness transcript (`report/transcript.jsonl`)."""
    return posixpath.basename(name).startswith("transcript")


def _commands(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("command", "cmd") and isinstance(item, str) and item.strip():
                yield item
            elif (
                key in ("command", "cmd")
                and isinstance(item, list)
                and item
                and all(isinstance(part, str) for part in item)
            ):
                yield " ".join(item)
            else:
                yield from _commands(item)
    elif isinstance(value, list):
        for item in value:
            yield from _commands(item)


def transcript_command(lines: Sequence[str], line: int) -> str | None:
    """The command a transcript recorded last at or before `line` (1-based), redacted
    and cut short: the tool call whose input or output holds the match. Each harness
    writes JSON lines, and each names a shell command under a `command` (or `cmd`) key
    somewhere in its tool call; the nearest one is taken."""
    first = max(line - COMMAND_LOOKBACK, 1)
    for number in range(min(line, len(lines)), first - 1, -1):
        text = lines[number - 1]
        if len(text) > COMMAND_INPUT_LIMIT or ("command" not in text and "cmd" not in text):
            continue
        try:
            parsed = json.loads(text)
            found = list(_commands(parsed))
        except (ValueError, RecursionError):
            continue
        if found:
            return redact_line(found[-1].replace("\n", " "), COMMAND_LIMIT)
    return None


def transcript_window(lines: Sequence[str], line: int) -> str:
    """The transcript lines around `line`, each numbered, redacted, and cut short."""
    start = max(line - WINDOW_BEFORE, 1)
    end = min(line + WINDOW_AFTER, len(lines))
    return "\n".join(
        f"{'>' if number == line else ' '} {number}: "
        f"{redact_line(lines[number - 1], WINDOW_LINE_LIMIT)}"
        for number in range(start, end + 1)
    )


def window_document(name: str, findings: Sequence[Mapping[str, Any]], lines: Sequence[str]) -> str:
    """The artifact a transcript match keeps: for each match, what matched and the
    redacted lines around it, so a person can see which command printed it."""
    parts = [
        f"Redacted lines of {name} around each secret-shaped match the no_secrets scan "
        "found. Every secret-shaped string is shown as its first four and last three "
        "characters and its length.",
    ]
    for finding in list(findings)[:WINDOW_LIMIT]:
        line = finding.get("line")
        if not isinstance(line, int):
            continue
        heading = f"line {line}, rule {finding.get('pattern')}"
        if finding.get("command"):
            heading += f", command: {finding['command']}"
        if finding.get("advisory"):
            heading += " (advisory: a fixture value the repository declares)"
        parts.append(f"{heading}\n{transcript_window(lines, line)}")
    return "\n\n".join(parts) + "\n"


def describe(finding: Mapping[str, Any]) -> str:
    """One match as the gate's detail names it: input, line, rule, the redacted value
    and the redacted text around it."""
    where = str(finding.get("where") or "?")
    text = where
    if finding.get("line") is not None:
        text += f" line {finding['line']}"
    text += f", rule {finding.get('pattern') or '?'}"
    if finding.get("excerpt"):
        text += f", value {finding['excerpt']}"
    if finding.get("context"):
        text += f", in: {finding['context']}"
    if finding.get("command"):
        text += f", printed by the command: {finding['command']}"
    if finding.get("window"):
        text += f", redacted window kept as artifact {finding['window']}"
    return text


def _source(finding: Mapping[str, Any]) -> str:
    """What produced a match, in the worker's terms."""
    where = str(finding.get("where") or "")
    line = finding.get("line")
    at = f" line {line}" if line is not None else ""
    if where.startswith("diff:"):
        return f"the file `{where[5:]}` you changed,{at}"
    if where == "diff":
        return "the diff"
    if where.startswith("diff-path:"):
        return f"the name of the path `{where[10:]}`"
    if where.startswith("commit["):
        return f"the commit message {where.removesuffix('.message')}{at}"
    if where.startswith("report"):
        return f"your report ({where}){at}"
    if where.startswith("artifact:"):
        name = where[9:]
        if is_transcript(name):
            command = finding.get("command")
            text = f"the transcript ({name}){at}"
            if command:
                text += f", from the command `{command}`"
            return text
        return f"the file {name} you left in the report directory,{at}"
    return f"{where}{at}"


NO_SECRETS_ADVICE = (
    "How to avoid it:\n"
    "- Remove the value. Where a token belongs, write an angle-bracket placeholder such "
    "as `<github-token>`, in code, tests, comments, commit messages and the report.\n"
    "- Build a fake value at run time (join short parts in the test) instead of writing "
    "one out.\n"
    "- To find where a value occurs, search with `grep -l` or `grep -c`, which print only "
    "file names or counts; never print the matching lines (`grep -n`, `cat`, `sed -n`, a "
    "test failure that echoes them), because the transcript is scanned too.\n"
    "- A test fixture the repository already declares in `.gitleaksignore` or the "
    "`.gitleaks.toml` allowlist is allowed; do not add your own entries to allow a match."
)


def no_secrets_correction(findings: Sequence[Mapping[str, Any]], limit: int = 10) -> str:
    """The correction Hades composes for a no_secrets failure: which command or file
    produced each blocking match, the rule, and how to avoid it."""
    blocking = [f for f in findings if not f.get("advisory")]
    if not blocking:
        return ""
    lines = ["The no_secrets gate stopped the last attempt. What produced each match:"]
    for finding in blocking[:limit]:
        line = (
            f"- {_source(finding)} (rule {finding.get('pattern') or '?'}, "
            f"value {finding.get('excerpt') or 'redacted'})"
        )
        if finding.get("context"):
            line += f": {finding['context']}"
        if finding.get("window"):
            line += f". The redacted transcript lines are kept as {finding['window']}."
        lines.append(line)
    if len(blocking) > limit:
        lines.append(f"- and {len(blocking) - limit} more match(es) of the same kind")
    return "\n".join(lines) + "\n\n" + NO_SECRETS_ADVICE
