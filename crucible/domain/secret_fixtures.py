"""What the repository itself declares about secret-shaped text (FDY-0618).

`make scan` runs gitleaks, which honours two declarations a repository carries: the
exact fingerprints listed in `.gitleaksignore` and the allowlists in `.gitleaks.toml`.
The `no_secrets` scanner reads the same two files, from the merge base and never from
the worker's tree, so a worker cannot allow its own match, and agrees with gitleaks on
what they allow:

- a fingerprint `path:rule:line` (gitleaks' form for a scan of a directory, which is
  what `make scan-tree` runs) skips that rule's match on that line of that path in the
  diff. A `commit:path:rule:line` fingerprint names a commit already in history and so
  never matches a new diff line;
- an allowlist's `paths` skip every match in a matching path; its `regexes` (against
  the secret, the whole match or the line, as `regexTarget` says) and `stopwords`
  (contained in the secret) skip that match. `targetRules` and `condition` are read as
  gitleaks reads them; a `commits` criterion never holds for a diff line.

A value that the merge base holds at a place one of those declarations allows is a
fixture value the repository declares. A match of that same value anywhere else, a
transcript line that printed it, say, is reported as advisory rather than blocking.
Only digests of those values are kept.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def gitleaks_rule_id(pattern: str) -> str:
    """The id `.gitleaks.toml` gives a scanner pattern (tests/unit/test_secrets.py
    holds the two equal)."""
    return f"crucible-{pattern.replace('_', '-')}"


def value_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def _compile(expression: object) -> re.Pattern[str] | None:
    """A gitleaks (Go RE2) expression as a Python one; None when Python reads it
    differently enough to refuse it, which allows nothing (fail closed)."""
    if not isinstance(expression, str):
        return None
    try:
        return re.compile(expression)
    except re.error:
        return None


@dataclass(frozen=True, slots=True)
class Allowlist:
    paths: tuple[re.Pattern[str], ...] = ()
    regexes: tuple[re.Pattern[str], ...] = ()
    regex_target: str = "secret"
    stopwords: tuple[str, ...] = ()
    target_rules: frozenset[str] = frozenset()
    has_commits: bool = False
    condition_and: bool = False

    def applies_to(self, rule_id: str) -> bool:
        return not self.target_rules or rule_id in self.target_rules

    def path_allowed(self, path: str) -> bool:
        """The whole path is allowed: only `paths` are given, or under OR any is."""
        # A rule-scoped allowlist can only skip matches for those rules. The file must
        # still be scanned for every other rule.
        if self.target_rules:
            return False
        if not self.paths or not any(p.search(path) for p in self.paths):
            return False
        if not self.condition_and:
            return True
        return not (self.regexes or self.stopwords or self.has_commits)

    def allows(self, rule_id: str, path: str, value: str, line: str) -> bool:
        if not self.applies_to(rule_id):
            return False
        target = {"match": value, "line": line}.get(self.regex_target, value)
        checks: list[bool] = []
        if self.paths:
            checks.append(any(p.search(path) for p in self.paths))
        if self.regexes:
            checks.append(any(r.search(target) for r in self.regexes))
        if self.stopwords:
            lowered = value.lower()
            checks.append(any(word.lower() in lowered for word in self.stopwords))
        if self.has_commits:
            checks.append(False)
        if not checks:
            return False
        return all(checks) if self.condition_and else any(checks)


def _allowlist(raw: Mapping[str, Any]) -> Allowlist:
    def patterns(key: str) -> tuple[re.Pattern[str], ...]:
        items = raw.get(key)
        compiled = [_compile(item) for item in items] if isinstance(items, list) else []
        return tuple(p for p in compiled if p is not None)

    words = raw.get("stopwords")
    rules = raw.get("targetRules")
    return Allowlist(
        paths=patterns("paths"),
        regexes=patterns("regexes"),
        regex_target=str(raw.get("regexTarget") or "secret"),
        stopwords=tuple(w for w in words if isinstance(w, str) and w)
        if isinstance(words, list)
        else (),
        target_rules=frozenset(r for r in rules if isinstance(r, str))
        if isinstance(rules, list)
        else frozenset(),
        has_commits=bool(raw.get("commits")),
        condition_and=str(raw.get("condition") or "OR").upper() == "AND",
    )


def parse_gitleaks_config(text: str) -> tuple[Allowlist, ...]:
    """The global allowlists a `.gitleaks.toml` declares: the `[allowlist]` table and
    each `[[allowlists]]` entry. A file that does not parse declares nothing."""
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    found: list[Allowlist] = []
    single = config.get("allowlist")
    if isinstance(single, dict):
        found.append(_allowlist(single))
    many = config.get("allowlists")
    if isinstance(many, list):
        found.extend(_allowlist(item) for item in many if isinstance(item, dict))
    return tuple(found)


def parse_gitleaksignore(text: str) -> frozenset[str]:
    """The `path:rule:line` fingerprints a `.gitleaksignore` lists. Comments, blank
    lines and `commit:path:rule:line` fingerprints (which name history, not a new diff
    line) are left out."""
    found: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, sep, rest = line.partition(":")
        if sep and _COMMIT.fullmatch(head) and rest.count(":") >= 2:
            continue
        parts = line.rsplit(":", 2)
        if len(parts) == 3 and parts[0] and parts[1] and parts[2].isdigit():
            found.add(line)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class SecretDeclarations:
    """The repository's own declarations, read from the merge base."""

    fingerprints: frozenset[str] = frozenset()
    allowlists: tuple[Allowlist, ...] = ()
    # Digests of the values the merge base holds at declared places, never the values.
    fixture_digests: frozenset[str] = field(default_factory=frozenset)

    def path_allowed(self, path: str) -> bool:
        return any(a.path_allowed(path) for a in self.allowlists)

    def allowed(
        self, path: str, pattern: str, line_number: int | None, value: str, line: str
    ) -> bool:
        """gitleaks would not report this match: its fingerprint is listed, or an
        allowlist allows it."""
        rule_id = gitleaks_rule_id(pattern)
        if line_number is not None and f"{path}:{rule_id}:{line_number}" in self.fingerprints:
            return True
        return any(a.allows(rule_id, path, value, line) for a in self.allowlists)

    def is_fixture(self, value: str) -> bool:
        return bool(self.fixture_digests) and value_digest(value) in self.fixture_digests


@dataclass(frozen=True, slots=True)
class BaseMatch:
    """A secret-shaped value the merge base holds: where, which rule, and the value,
    held only long enough to decide whether it is declared."""

    path: str
    pattern: str
    line: int
    value: str
    line_text: str = ""


def declarations(
    gitleaksignore: str, gitleaks_toml: str, base_matches: Iterable[BaseMatch] = ()
) -> SecretDeclarations:
    """Read both files and keep the digest of each base value at a declared place."""
    partial = SecretDeclarations(
        fingerprints=parse_gitleaksignore(gitleaksignore),
        allowlists=parse_gitleaks_config(gitleaks_toml),
    )
    digests = frozenset(
        value_digest(m.value)
        for m in base_matches
        if m.value
        and (
            partial.path_allowed(m.path)
            or partial.allowed(m.path, m.pattern, m.line, m.value, m.line_text or m.value)
        )
    )
    return SecretDeclarations(
        fingerprints=partial.fingerprints,
        allowlists=partial.allowlists,
        fixture_digests=digests,
    )
