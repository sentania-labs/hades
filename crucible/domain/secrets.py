"""Secret-pattern scanner (05, 12). Reports the path of a match, never the value."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # An installation token is about 390 characters and contains dots, underscores and
    # hyphens; the 40-character `ghs_` form most patterns assume matches nothing (S10).
    # No upper bound, and the separators are inside the class, or the match stops early.
    ("github_installation_token", re.compile(r"\bghs_[A-Za-z0-9._-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_fine_grained_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # The shapes the three harness CLIs write into their auth files (12, S1, S1b): the
    # Claude Code long-lived token; the Google OAuth access and refresh tokens AGY keeps
    # beside a JWT id token (the jwt pattern covers that one); Codex's id and access
    # tokens are JWTs and its refresh token is not: two short base64url segments and one
    # long one, dot-separated, with no JWT header (shape from a real auth.json, C5).
    ("anthropic_oauth_token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("google_oauth_access_token", re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}")),
    ("google_oauth_refresh_token", re.compile(r"\b1//[A-Za-z0-9_-]{20,}")),
    (
        "codex_refresh_token",
        re.compile(r"\b[A-Za-z0-9]{1,8}\.[A-Za-z0-9]{1,8}\.[A-Za-z0-9_-]{100,}"),
    ),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("crucible_token", re.compile(r"\bcru_[A-Za-z0-9]{26}\.[A-Za-z0-9_-]{30,}\b")),
)


@dataclass(frozen=True, slots=True)
class SecretMatch:
    path: str
    pattern: str
    excerpt: str = ""
    # FDY-0618: the 1-based line of the input the match is on (the new file's line for a
    # diff), the redacted text around it on that line, and whether the value is one the
    # repository itself declares as a fixture, which makes the match advisory.
    line: int | None = None
    context: str = ""
    advisory: bool = False


def redact_excerpt(value: str) -> str:
    """Show enough of a matched value to identify it without disclosing it: its first
    four and last three characters and its length (FDY-0618)."""
    return f"{value[:4]}...{value[-3:]} ({len(value)} chars)"


def _redacted_marker(value: str) -> str:
    return f"[{redact_excerpt(value)}]"


@dataclass(frozen=True, slots=True)
class _Hit:
    pattern: str
    start: int
    end: int
    value: str


def _hits(text: str) -> list[_Hit]:
    """Every match in `text`, in order, none overlapping another: where two patterns
    match at overlapping places the earlier one wins, and at the same place the one
    listed first."""
    found = sorted(
        (
            (match.start(), index, name, match)
            for index, (name, pattern) in enumerate(_PATTERNS)
            for match in pattern.finditer(text)
        ),
        key=lambda item: (item[0], item[1]),
    )
    hits: list[_Hit] = []
    end = -1
    for start, _index, name, match in found:
        if start < end:
            continue
        hits.append(_Hit(name, start, match.end(), match.group(0)))
        end = match.end()
    return hits


def _redacted(text: str, hits: list[_Hit]) -> tuple[str, list[tuple[int, int]]]:
    """`text` with each hit replaced by its marker, and where each marker landed."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    length = 0
    for hit in hits:
        parts.append(text[cursor : hit.start])
        length += hit.start - cursor
        marker = _redacted_marker(hit.value)
        spans.append((length, length + len(marker)))
        parts.append(marker)
        length += len(marker)
        cursor = hit.end
    parts.append(text[cursor:])
    return "".join(parts), spans


def redact_line(text: str, limit: int = 400) -> str:
    """One line with every secret-shaped run replaced by its first four and last three
    characters and its length, cut to `limit` characters (FDY-0618)."""
    shown, _ = _redacted(text.rstrip("\r\n"), _hits(text))
    return shown if len(shown) <= limit else shown[:limit] + "[...]"


# How much of the line on each side of a match its context keeps.
CONTEXT_WIDTH = 60


def _context(shown: str, span: tuple[int, int], width: int = CONTEXT_WIDTH) -> str:
    start = max(span[0] - width, 0)
    end = min(span[1] + width, len(shown))
    return (
        ("[...]" if start > 0 else "")
        + shown[start:end].strip()
        + ("[...]" if end < len(shown) else "")
    )


def match_line(
    text: str,
    *,
    path: str = "",
    line: int | None = None,
    fixture: Callable[[str], bool] | None = None,
    skip: Callable[[str, str, str], bool] | None = None,
) -> list[SecretMatch]:
    """Every match on one line, each with its rule, abbreviated value and the redacted
    text around it (FDY-0618). `skip(rule, value, line_text)` drops a match the
    repository allows; `fixture(value)` marks one whose value the repository declares
    as a fixture advisory. Neither value is kept."""
    text = text.rstrip("\r\n")
    hits = _hits(text)
    if not hits:
        return []
    shown, spans = _redacted(text, hits)
    found: list[SecretMatch] = []
    for hit, span in zip(hits, spans, strict=True):
        if skip is not None and skip(hit.pattern, hit.value, text):
            continue
        found.append(
            SecretMatch(
                path=path,
                pattern=hit.pattern,
                excerpt=redact_excerpt(hit.value),
                line=line,
                context=_context(shown, span),
                advisory=fixture is not None and fixture(hit.value),
            )
        )
    return found


def match_lines(
    text: str,
    *,
    path: str = "",
    fixture: Callable[[str], bool] | None = None,
    limit: int | None = 50,
) -> list[SecretMatch]:
    """Every match in a text, line by line, at most `limit` of them, or all matches
    when `limit` is None. A match no single line holds is still reported from the whole
    text, so the line view never reads less than `match_text` does."""
    found: list[SecretMatch] = []
    for number, line in enumerate(text.splitlines(), start=1):
        found.extend(match_line(line, path=path, line=number, fixture=fixture))
        if limit is not None and len(found) >= limit:
            return found[:limit]
    if not found:
        whole = match_text(text, path=path, fixture=fixture)
        if whole is not None:
            found.append(whole)
    return found


def match_text(
    text: str, *, path: str = "", fixture: Callable[[str], bool] | None = None
) -> SecretMatch | None:
    """Return the first match with its rule, safely abbreviated value, line and the
    redacted text around it; `fixture(value)` marks a declared fixture value advisory."""
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is not None:
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.start())
            line_text = text[line_start : len(text) if line_end < 0 else line_end]
            hits = _hits(line_text)
            shown, spans = _redacted(line_text, hits)
            where = match.start() - line_start
            context = next(
                (
                    _context(shown, span)
                    for hit, span in zip(hits, spans, strict=True)
                    if hit.start <= where < hit.end
                ),
                _redacted_marker(match.group(0)),
            )
            return SecretMatch(
                path=path,
                pattern=name,
                excerpt=redact_excerpt(match.group(0)),
                line=text.count("\n", 0, match.start()) + 1,
                context=context,
                advisory=fixture is not None and fixture(match.group(0)),
            )
    return None


def scan_text(text: str) -> str | None:
    """Return the name of the first matching pattern, or None."""
    match = match_text(text)
    return match.pattern if match is not None else None


# hades #398: how much of what came before each chunk a streamed scan reads again, so a
# match across a chunk boundary is still found. It is far longer than the shortest text
# any pattern matches.
SCAN_OVERLAP = 16 * 1024
# A match that touches the end of what has been read may only look whole because the
# text after it is not read yet: a trailing \b holds at the end of a string, not after
# the next letter. Such a match is held, with one character of context before it, until
# the following text decides it, up to this many characters; held longer, it is taken as
# it stands (no secret is that long, and the window must stay bounded).
SCAN_HOLD = 1024 * 1024


def _window_hit(window: str, pos: int, final: bool, hold: int) -> tuple[str | None, int | None]:
    """The first pattern a window decides, and where an undecided match starts.

    A match that ends before the window does is whole: the character after it was read.
    One that ends where the window ends is whole only when `final`; otherwise it waits
    for the next chunk and the window is carried from one character before it, so the
    next window judges it with its left context and what follows it. A waiting match
    longer than `hold` is taken as it stands."""
    keep_from: int | None = None
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(window, pos):
            if final or match.end() < len(window) or match.end() - match.start() > hold:
                return name, None
            start = max(match.start() - 1, 0)
            keep_from = start if keep_from is None else min(keep_from, start)
    return None, keep_from


def scan_chunks(
    chunks: Iterable[str], overlap: int = SCAN_OVERLAP, hold: int = SCAN_HOLD
) -> str | None:
    """Return the name of the first pattern matching the text the chunks make up, or None.

    The text is never held whole (hades #398): each window is the chunk plus the last
    `overlap` characters before it, and one character more that is read only as the
    context a word boundary looks at, so a match is found wherever the chunks split it.
    A match that touches the end of a window is not accepted until the next character
    is read (or the text ends): the window is carried from the match instead, up to
    `hold` characters, so `ghp_` and a long run of letters that ends in one more letter
    is no more a match here than it is for `scan_text`."""
    tail = ""
    pos = 0
    for chunk in chunks:
        window = tail + chunk
        hit, keep_from = _window_hit(window, pos, False, hold)
        if hit is not None:
            return hit
        cut = max(0, len(window) - (overlap + 1))
        if keep_from is not None:
            cut = min(cut, keep_from)
        tail = window[cut:]
        pos = 1 if cut > 0 else pos
    hit, _ = _window_hit(tail, pos, True, hold)
    return hit


def secret_pattern_expressions() -> tuple[str, ...]:
    """Return the canonical patterns for trusted checkout scanners.

    The preparer uses Git's PCRE matcher over a restored tree. Keep that defence in
    depth on the same patterns as the evidence scanner instead of maintaining a second
    list that can drift.
    """
    return tuple(
        f"(?i){pattern.pattern}" if pattern.flags & re.IGNORECASE else pattern.pattern
        for _name, pattern in _PATTERNS
    )


def named_secret_pattern_expressions() -> tuple[tuple[str, str], ...]:
    """Return rule names with the canonical expressions for trusted shell scanners."""
    return tuple(
        zip((name for name, _pattern in _PATTERNS), secret_pattern_expressions(), strict=True)
    )


def _walk(value: object, path: str, fixture: Callable[[str], bool] | None) -> Iterator[SecretMatch]:
    if isinstance(value, str):
        yield from match_lines(value, path=path, fixture=fixture, limit=None)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key), fixture)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]", fixture)


def find_secrets(
    document: object, root: str = "", *, fixture: Callable[[str], bool] | None = None
) -> list[SecretMatch]:
    """Walk a nested document and report every string that matches a secret pattern."""
    return list(_walk(document, root, fixture))


# What a redaction writes in place of a match. The pattern name is kept so a reader of a
# redacted log knows what was removed without learning anything about the value.
REDACTION = "[redacted:{name}]"


def redact(text: str) -> str:
    """Replace every secret-shaped run with a marker naming the pattern (12).

    Best-effort defence in depth, not the control: the control is that no secret value is
    ever placed where it could be printed. Applied to every user-controlled GitHub text
    before it is stored (23)."""
    for name, pattern in _PATTERNS:
        text = pattern.sub(REDACTION.format(name=name), text)
    return text
