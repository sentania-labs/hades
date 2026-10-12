from __future__ import annotations

import pytest

from crucible.domain.secrets import find_secrets, scan_text


# Every fixture is assembled at runtime from fragments so no committed line is itself
# secret-shaped: the repository must scan clean, and Crucible's own no_secrets gate
# runs the same kind of scanner over its PRs.
def _repeat(char: str, count: int) -> str:
    return char * count


def _join(*parts: str) -> str:
    return "".join(parts)


SECRET_FIXTURES: list[tuple[str, str]] = [
    (_join("token ghp_", _repeat("a", 36), " here"), "github_token"),
    # The old 40-character `ghs_` shape and the real 390-character one with dots both
    # belong to the installation-token pattern now (S10).
    (_join("ghs_", _repeat("B", 36)), "github_installation_token"),
    (
        _join("ghs_", _repeat("aB9._-", 64)),
        "github_installation_token",
    ),
    (_join("github_pat_", _repeat("x", 30)), "github_fine_grained_token"),
    (_join("-----BEGIN ", "RSA PRIVATE KEY", "-----"), "private_key_header"),
    (_join("-----BEGIN ", "PRIVATE KEY", "-----"), "private_key_header"),
    (_join("Authorization: ", "Bearer ", "abcdefghijklmnopqrstuvwxyz0123"), "bearer_token"),
    (_join("AK", "IA", "ABCDEFGH", "IJKLMNOP"), "aws_access_key"),
    (_join("sk-", _repeat("q", 24)), "openai_style_key"),
    (_join("xox", "b-", "1234567890", "-abcdef"), "slack_token"),
    (
        ".".join(
            [_join("eyJ", "hbGciOiJIUzI1NiJ9"), _join("eyJ", "zdWIiOiIxMjM0In0"), "abcdefghijkl"]
        ),
        "jwt",
    ),
    (_join("cru_", "01ARZ3NDEKTSV4RRFFQ69G5FAV", ".", _repeat("s", 40)), "crucible_token"),
    # The shapes the three harness CLIs keep in their auth files (12, S1, S1b): the
    # Claude Code long-lived token, and Google's OAuth access and refresh tokens.
    (_join("sk-ant-", "oat01-", _repeat("k", 40)), "anthropic_oauth_token"),
    (_join("ya29.", "a0AfH6SM", _repeat("g", 40)), "google_oauth_access_token"),
    (_join("1//", "0gABCDEF", _repeat("r", 40)), "google_oauth_refresh_token"),
    (_join("ab", ".", "c", ".", _repeat("K9_-", 40)), "codex_refresh_token"),
]


@pytest.mark.parametrize(
    ("text", "pattern"),
    SECRET_FIXTURES,
    ids=[f"{p}-{i}" for i, (_, p) in enumerate(SECRET_FIXTURES)],
)
def test_patterns_match(text: str, pattern: str) -> None:
    assert scan_text(text) == pattern


@pytest.mark.parametrize(
    "text",
    [
        "make lint && make test",
        "the bearer of bad news",
        "https://github.com/example-org/example-service/issues/17",
        "sk-short",
        "ghp_tooshort",
    ],
)
def test_ordinary_text_is_clean(text: str) -> None:
    assert scan_text(text) is None


def test_find_secrets_reports_path_not_value() -> None:
    doc = {"a": {"b": ["fine", _join("ghp_", _repeat("c", 36))]}, "c": "ok"}
    matches = find_secrets(doc)
    assert [(m.path, m.pattern) for m in matches] == [("a.b[1]", "github_token")]
    assert matches[0].excerpt == "ghp_...ccc (40 chars)"
    assert _repeat("c", 4) not in repr(matches)


def test_gitleaks_config_carries_every_scanner_pattern() -> None:
    """12: `make scan` runs gitleaks, whose default rules miss the harness token shapes.
    `.gitleaks.toml` extends them with the scanner's own patterns, and this holds the
    two equal so a pattern added to one cannot be missing from the other."""
    import re  # noqa: PLC0415
    import tomllib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from crucible.domain.secrets import _PATTERNS  # noqa: PLC0415

    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / ".gitleaks.toml").read_text(encoding="utf-8")
    )
    assert config["extend"]["useDefault"] is True
    rules = {rule["id"]: rule["regex"] for rule in config["rules"]}
    expected = {}
    for name, pattern in _PATTERNS:
        regex = pattern.pattern
        if pattern.flags & re.IGNORECASE:
            regex = "(?i)" + regex
        expected[f"crucible-{name.replace('_', '-')}"] = regex
    assert rules == expected


def test_codex_refresh_token_shape_matches_and_a_jwt_or_a_version_does_not() -> None:
    from crucible.domain.secrets import scan_text  # noqa: PLC0415

    token = _join("ab", ".", "c", ".", _repeat("Q7", 60), "_-", _repeat("z", 40))
    assert scan_text(token) == "codex_refresh_token"
    assert scan_text("version 0.153.4 of the CLI") is None
    assert scan_text("a.b.c") is None
