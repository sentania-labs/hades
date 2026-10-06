"""Secret-pattern scanner (05, 12). Reports the path of a match, never the value."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
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


def scan_text(text: str) -> str | None:
    """Return the name of the first matching pattern, or None."""
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


# hades #398: how much of what came before each chunk a streamed scan reads again, so a
# match across a chunk boundary is still found. It is far longer than the shortest text
# any pattern matches; a match touching the end of a window longer than this is taken
# as it stands, because the next window cannot see its start.
SCAN_OVERLAP = 16 * 1024


def _window_hit(window: str, pos: int, final: bool, overlap: int) -> str | None:
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(window, pos):
            # A match that ends where the window ends may only look whole because the
            # text after it is not read yet (a trailing \b); the next window reads it
            # again with what follows, unless it is too long to fit there.
            if final or match.end() < len(window) or match.end() - match.start() > overlap:
                return name
    return None


def scan_chunks(chunks: Iterable[str], overlap: int = SCAN_OVERLAP) -> str | None:
    """Return the name of the first pattern matching the text the chunks make up, or None.

    The text is never held whole (hades #398): each window is the chunk plus the last
    `overlap` characters before it, and one character more that is read only as the
    context a word boundary looks at, so a match is found wherever the chunks split it."""
    tail = ""
    pos = 0
    pending = iter(chunks)
    chunk = next(pending, None)
    while chunk is not None:
        following = next(pending, None)
        window = tail + chunk
        hit = _window_hit(window, pos, following is None, overlap)
        if hit is not None:
            return hit
        if len(window) > overlap + 1:
            tail, pos = window[-(overlap + 1) :], 1
        else:
            tail = window
        chunk = following
    return None


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


def _walk(value: object, path: str) -> Iterator[SecretMatch]:
    if isinstance(value, str):
        hit = scan_text(value)
        if hit is not None:
            yield SecretMatch(path=path, pattern=hit)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def find_secrets(document: object, root: str = "") -> list[SecretMatch]:
    """Walk a nested document and report every string that matches a secret pattern."""
    return list(_walk(document, root))


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
