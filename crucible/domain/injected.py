"""Pure instruction-path and shim matching, also embedded in the collector (#400)."""

import posixpath
import unicodedata

INJECTED_PREFIXES: tuple[str, ...] = (
    ".codex/",
    ".claude/",
    ".hermes/",
    ".gemini/",
    ".crucible/",
    "crucible/identity/",
    ".crucible-shims/",
)
INJECTED_NAMES: frozenset[str] = frozenset(
    {".crucible", "crucible-identity.md", "crucible-shim", ".crucible-identity"}
)
# NFC and casefold do not fold lookalikes. Cover the Greek and Cyrillic letters
# resembling the Latin letters in instruction names, including Cyrillic U+0410 (#400).
_LOOKALIKES = str.maketrans(
    {
        "\u0430": "a",
        "\u0441": "c",
        "\u0435": "e",
        "\u0456": "i",
        "\u0458": "j",
        "\u043e": "o",
        "\u0440": "p",
        "\u0455": "s",
        "\u0445": "x",
        "\u0443": "y",
        "\u0501": "d",
        "\u050d": "g",
        "\u04cf": "l",
        "\u043c": "m",
        "\u0442": "t",
        "\u051d": "w",
        "\u03b1": "a",
        "\u03b5": "e",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bd": "v",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
    }
)


def normalized_instruction_path(path: str) -> str:
    """Normalize spelling only; ownership still compares the original Git path."""
    stripped = "".join(
        c
        for c in posixpath.normpath(path)
        if unicodedata.category(c) != "Cf"
        and c not in "\u034f\u180b\u180c\u180d"
        and not 0xFE00 <= ord(c) <= 0xFE0F
        and not 0xE0100 <= ord(c) <= 0xE01EF
    )
    return unicodedata.normalize("NFC", stripped.casefold()).translate(_LOOKALIKES)


def instruction_name_error(path: str) -> str:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in path):
        return "undecodable name (invalid UTF-8)"
    return ""


def injected_prefix(path: str) -> bool:
    parts = normalized_instruction_path(path).split("/")
    # Include the directory entry itself, including a symlink, without traversing
    # its target outside the collected tree. Also cover harness directories at depth.
    return any(
        parts[at : at + len(prefix.rstrip("/").split("/"))] == prefix.rstrip("/").split("/")
        for prefix in INJECTED_PREFIXES
        for at in range(len(parts))
    )


def injected_name(path: str) -> bool:
    if instruction_name_error(path):
        return True
    name = posixpath.basename(normalized_instruction_path(path))
    return (
        name in INJECTED_NAMES
        or (name.startswith(("agents", "claude", "gemini")) and name.endswith(".md"))
        or injected_prefix(path)
    )


def normalized_shim_content(content: str) -> str:
    """Ignore line endings, trailing whitespace and final newlines, not words."""
    return "\n".join(line.rstrip() for line in content.splitlines()).rstrip("\n")
