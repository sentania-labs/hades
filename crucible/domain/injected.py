"""Pure instruction-path and shim matching for collected evidence (#400)."""

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
# NFC and casefold do not fold lookalikes. Cover both cases of the Greek and
# Cyrillic letters resembling the Latin letters in instruction names (#400).
_LOOKALIKES = str.maketrans(
    {
        "\u0410": "a",
        "\u0430": "a",
        "\u0421": "c",
        "\u0441": "c",
        "\u0415": "e",
        "\u0435": "e",
        "\u0406": "i",
        "\u0456": "i",
        "\u0408": "j",
        "\u0458": "j",
        "\u041e": "o",
        "\u043e": "o",
        "\u0420": "p",
        "\u0440": "p",
        "\u0405": "s",
        "\u0455": "s",
        "\u0425": "x",
        "\u0445": "x",
        "\u0423": "y",
        "\u0443": "y",
        "\u0500": "d",
        "\u0501": "d",
        "\u050c": "g",
        "\u050d": "g",
        "\u04c0": "l",
        "\u04cf": "l",
        "\u041c": "m",
        "\u043c": "m",
        "\u0422": "t",
        "\u0442": "t",
        "\u051c": "w",
        "\u051d": "w",
        "\u0391": "a",
        "\u03b1": "a",
        "\u0395": "e",
        "\u03b5": "e",
        "\u0399": "i",
        "\u03b9": "i",
        "\u039a": "k",
        "\u03ba": "k",
        "\u039d": "n",
        "\u03bd": "n",
        "\u039f": "o",
        "\u03bf": "o",
        "\u03a1": "p",
        "\u03c1": "p",
        "\u03a4": "t",
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
    return unicodedata.normalize("NFC", stripped).translate(_LOOKALIKES).casefold()


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
