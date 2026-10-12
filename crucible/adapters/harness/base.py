"""What the four adapters share (07): the pointer prompt, report-directory parsing
against CompletionClaimV1, and exit classification from the code plus both tails (S5).

Nothing here reads a credential. The report directory an adapter parses is the
collector's copy (08), and everything in it is data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from crucible.contracts.completion_claim import load_report, parse_claim
from crucible.domain.exit_class import ExitClass, classify_exit
from crucible.domain.infrastructure import Interruption
from crucible.ports.execution import IDENTITY_MOUNT, REPORT_MOUNT
from crucible.ports.harness import ExitInfo, ParsedReport, ProviderQuotaEvent, ReportMetrics

# Argv carries only a short pointer; the identity bundle and the contract are files
# (07, S3). The same sentence for every harness.
POINTER_PROMPT = f"Read {IDENTITY_MOUNT}/IDENTITY.md and execute the task."
TRANSCRIPT_NAME = "transcript.jsonl"
TRANSCRIPT_PATH = f"{REPORT_MOUNT}/{TRANSCRIPT_NAME}"
PROGRESS_NAME = "progress.jsonl"
TAIL_LIMIT = 64 * 1024

Pattern = tuple[str, re.Pattern[str]]


def patterns(*texts: str) -> tuple[Pattern, ...]:
    return tuple((t, re.compile(re.escape(t), re.IGNORECASE)) for t in texts)


def correlated(*sources: str) -> tuple[Pattern, ...]:
    """Patterns whose text is a regular expression rather than a literal, for a signal
    that is only a signal when two things appear together on one line. `.` never crosses
    a newline here, which is what keeps the correlation to a single transcript event."""
    return tuple((s, re.compile(s, re.IGNORECASE)) for s in sources)


def first_match(tails: Sequence[str], candidates: Sequence[Pattern]) -> str | None:
    for tail in tails:
        for name, pattern in candidates:
            if pattern.search(tail):
                return name
    return None


def classify_with_patterns(
    exit: ExitInfo,
    stdout_tail: str,
    stderr_tail: str,
    *,
    auth: Sequence[Pattern],
    quota: Sequence[Pattern],
    interruption: Interruption | None = None,
) -> ExitClass:
    """The confirmed table of S5: the deterministic code first, then the auth and quota
    patterns from both tails on a non-zero exit only. A pattern never turns a clean exit
    into a failure, and a termination Crucible performed is never reclassified.

    `interruption` is the adapter's reading of its own provider error event (hades
    #353): on a non-zero exit it makes a gateway, transport or capacity failure
    `infrastructure` and a quota refusal `quota_exhausted` instead of a crash."""
    base = classify_exit(
        exit_code=exit.exit_code,
        report_present=exit.report_present,
        blocked_present=exit.blocked_present,
        lost=exit.lost,
        timed_out=exit.timed_out,
        killed=exit.killed,
    )
    if base in (ExitClass.LOST, ExitClass.TIMEOUT, ExitClass.KILLED, ExitClass.BLOCKED):
        return base
    if exit.exit_code == 0:
        return base
    if exit.oom_killed:
        return ExitClass.ENVIRONMENT
    tails = (stdout_tail[-TAIL_LIMIT:], stderr_tail[-TAIL_LIMIT:])
    if first_match(tails, auth) is not None:
        return ExitClass.AUTH_FAILURE
    if first_match(tails, quota) is not None:
        return ExitClass.QUOTA_EXHAUSTED
    return interruption.exit_class if interruption is not None else base


def with_in_flight(exit_class: ExitClass, in_flight: Sequence[str]) -> ExitClass:
    """Issue 128: a clean exit while the harness's own transcript shows a command it was
    waiting on cut off is `incomplete`, never a completion. Any other class already says the run
    did not finish cleanly, and a termination Crucible performed keeps its own class."""
    if in_flight and exit_class in (ExitClass.COMPLETED, ExitClass.COMPLETED_WITHOUT_REPORT):
        return ExitClass.INCOMPLETE
    return exit_class


def in_flight_summary(label: str, detail: str) -> str:
    """One in-flight entry as the attempt_collected event records it: short, one line."""
    text = " ".join(f"{label}: {detail}".split())
    return text[:300]


# The longest partial line a live tracker holds. A longer one is dropped whole and the
# tracker resumes at the next newline; json_lines reads a transcript under the same cap.
LIVE_LINE_LIMIT = 32 * 1024 * 1024


class LineTracker:
    """Issue 152: the shared half of a CommandTracker. It splits the live log into whole
    lines, one partial line per stream, and hands each to `line`. Kubernetes merges the
    two streams, so a tracker reads both and relies on what the line says."""

    def __init__(self) -> None:
        # Each stream's partial line as its pieces, joined once when the line ends.
        self._partial: dict[str, list[str]] = {}
        self._held: dict[str, int] = {}
        self._dropping: set[str] = set()

    def feed(self, stream: str, text: str) -> None:
        pieces = self._partial.setdefault(stream, [])
        start = 0
        while (end := text.find("\n", start)) >= 0:
            pieces.append(text[start:end])
            whole = "".join(pieces)
            pieces.clear()
            self._held[stream] = 0
            start = end + 1
            if stream in self._dropping:
                self._dropping.discard(stream)
                continue
            self.line(whole.strip())
        rest = text[start:]
        if not rest:
            return
        held = self._held.get(stream, 0) + len(rest)
        if held > LIVE_LINE_LIMIT:
            pieces.clear()
            held = 0
            self._dropping.add(stream)
        else:
            pieces.append(rest)
        self._held[stream] = held

    def line(self, text: str) -> None:
        raise NotImplementedError

    @property
    def running(self) -> tuple[tuple[str, str], ...]:
        raise NotImplementedError


def json_object(text: str) -> dict[str, Any] | None:
    """One transcript line as an object, or None: the stream is the harness's own."""
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


_RESET_KEYS = frozenset({"reset_at", "resetAt", "resets_at", "resetsAt", "reset_time"})


def _reset_values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _RESET_KEYS:
                yield item
            yield from _reset_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _reset_values(item)


def _reset_from_document(document: Mapping[str, Any]) -> datetime | None:
    for raw in _reset_values(document):
        if isinstance(raw, (int, float)):
            seconds = float(raw) / (1000 if raw > 10_000_000_000 else 1)
            try:
                return datetime.fromtimestamp(seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


# Hades #378: a reset the harness states as a duration from now rather than a
# timestamp. AGY's own words for an exhausted account are "Individual quota reached
# ... Resets in 3h52m"; the duration is hours, minutes and seconds, each optional, in
# that order, with or without spaces ("3h52m", "3h 52m 10s", "45m"). Human prose
# elsewhere in a document never qualifies: the match needs the word before the
# duration, and the caller only asks about the one event its predicate accepted.
_RESET_IN = re.compile(
    r"\bresets?\s+in\s+"
    r"(?=\d)"
    r"(?:(?P<days>\d+)\s*d)?\s*"
    r"(?:(?P<hours>\d+)\s*h)?\s*"
    r"(?:(?P<minutes>\d+)\s*m)?\s*"
    r"(?:(?P<seconds>\d+)\s*s)?"
    r"(?![\w:])",
    re.IGNORECASE,
)


def reset_after(text: str) -> timedelta | None:
    """The "Resets in XhYmZs" duration in `text`, or None when there is none."""
    for match in _RESET_IN.finditer(text):
        parts = {name: int(value) for name, value in match.groupdict().items() if value}
        if parts:
            return timedelta(**parts)
    return None


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _reset_after_from_document(document: Mapping[str, Any]) -> timedelta | None:
    for text in _strings(document):
        after = reset_after(text)
        if after is not None:
            return after
    return None


def quota_reset_at(*tails: str, quota: Sequence[Pattern]) -> datetime | None:
    """Parse a machine timestamp only from a line that also proves quota exhaustion."""

    for tail in tails:
        for line in reversed(tail[-TAIL_LIMIT:].splitlines()):
            if first_match((line,), quota) is None:
                continue
            try:
                document = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(document, dict):
                return _reset_from_document(document)
    return None


def provider_quota_event(
    *tails: str,
    predicate: Callable[[Mapping[str, Any]], bool],
    now: datetime | None = None,
    model_only: Callable[[Mapping[str, Any]], bool] | None = None,
) -> ProviderQuotaEvent | None:
    """Return the refusal and reset from the same structured harness event.

    The reset is the event's own timestamp when it carries one; otherwise a duration
    the event states ("Resets in 3h52m", hades #378) counted from `now`, the moment
    the refusal was observed (the supervisor's clock; the wall clock when no caller
    says). An event that says neither leaves `reset_at` None and the pool's default
    cooldown applies.

    `model_only`, when a harness gives one, is asked about the accepted event: True
    makes the result a model-only refusal (hades #373), which excludes the model and
    leaves the pool unmarked. Without it every refusal is the account's."""
    for tail in tails:
        for line in reversed(tail[-TAIL_LIMIT:].splitlines()):
            try:
                document = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(document, dict) and predicate(document):
                reset_at = _reset_from_document(document)
                if reset_at is None:
                    after = _reset_after_from_document(document)
                    if after is not None:
                        reset_at = (now or datetime.now(UTC)) + after
                return ProviderQuotaEvent(
                    reset_at=reset_at,
                    model_only=model_only is not None and model_only(document),
                )
    return None


def provider_quota_exhausted(*tails: str, signals: Sequence[Pattern]) -> bool:
    """Shared pool state requires a structured signal emitted by the harness itself."""
    return text_matches(signals, *tails)


def text_matches(signals: Sequence[Pattern], *tails: str) -> bool:
    """Whether any of `signals` appears in the tails, structured or not."""
    return first_match(tuple(tail[-TAIL_LIMIT:] for tail in tails), signals) is not None


def document_strings(document: Mapping[str, Any]) -> Iterator[str]:
    """Every string value in a harness event, nested ones included: the words an
    adapter reads when a refusal's meaning is in its text (hades #373)."""
    return _strings(document)


def read_text(path: Path, limit: int = 8 * 1024 * 1024) -> str | None:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def json_lines(path: Path, limit: int = 32 * 1024 * 1024) -> Iterator[dict[str, Any]]:
    """Every parseable JSON object in a JSONL file. A line that is not one is skipped:
    the transcript is the harness's own stream and nothing here trusts its shape."""
    text = read_text(path, limit)
    if text is None:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def parse_report_dir(
    report_dir: Path,
    exit: ExitInfo,
    *,
    metrics: ReportMetrics,
    transcript_lines: int,
    in_flight: Sequence[str] = (),
) -> ParsedReport:
    """`report.yaml` against CompletionClaimV1, `blocked.md`, and `progress.jsonl` (07).

    A missing report with exit 0 is `completed_without_report`; that is the caller's
    classification, and this only says whether the file was there and whether it parsed."""
    report_file = report_dir / "report.yaml"
    raw = read_text(report_file) if report_file.is_file() else None
    claim: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = []
    if raw is not None:
        claim, errors = load_report(raw)
        if claim is not None:
            _, errors = parse_claim(claim)
    blocked = report_dir / "blocked.md"
    blocked_md = read_text(blocked) if blocked.is_file() else None
    progress = tuple(json_lines(report_dir / PROGRESS_NAME, 4 * 1024 * 1024))[:1000]
    transcript = report_dir / TRANSCRIPT_NAME
    return ParsedReport(
        claim=claim,
        raw=raw,
        errors=errors,
        blocked_md=blocked_md,
        report_present=raw is not None,
        progress=progress,
        metrics=metrics,
        transcript_lines=transcript_lines,
        transcript_name=TRANSCRIPT_NAME if transcript.is_file() else None,
        in_flight=tuple(in_flight),
    )


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def integer(value: Any) -> int | None:
    """A JSON number as a whole count, or None (a boolean is not a count)."""
    return _int(value)


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def usage_totals(usage: Any) -> tuple[int | None, int | None]:
    """(tokens_in, tokens_out) from the usage object shapes the three CLIs emit."""
    if not isinstance(usage, dict):
        return None, None
    tokens_in = _int(usage.get("input_tokens"))
    if tokens_in is None:
        tokens_in = _int(usage.get("prompt_tokens"))
    if tokens_in is None:
        tokens_in = _int(usage.get("promptTokenCount"))
    tokens_out = _int(usage.get("output_tokens"))
    if tokens_out is None:
        tokens_out = _int(usage.get("completion_tokens"))
    if tokens_out is None:
        tokens_out = _int(usage.get("candidatesTokenCount"))
    return tokens_in, tokens_out


def cache_read(usage: Any) -> int | None:
    """hades #604: the prompt-cache reads in a usage object, in the name each CLI uses:
    Claude Code `cache_read_input_tokens`, Codex `cached_input_tokens`, Qwen Code
    `cache_read_input_tokens` (or `cached` in its stats)."""
    if not isinstance(usage, dict):
        return None
    for key in ("cache_read_input_tokens", "cached_input_tokens", "cacheReadInputTokens"):
        found = _int(usage.get(key))
        if found is not None:
            return found
    return None


def add(total: int | None, value: int | None) -> int | None:
    """A running sum that stays None until something was reported."""
    if value is None:
        return total
    return (total or 0) + value


def cost_usd(value: Any) -> float | None:
    return _float(value)
