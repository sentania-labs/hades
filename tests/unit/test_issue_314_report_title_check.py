"""hades #314: crucible-report check refuses a pull-request title the publisher will refuse.

The worker image does not ship the crucible package, so the check function must carry
its own copies of the closing-keyword and at-mention regexes that publication.py
validate_title uses. This file verifies that the check rejects titles the publisher
refuses (closing keywords, at-mentions, multi-line) and that the script's patterns
stay in sync with the ones in publication.py (parity test).
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "images" / "worker" / "crucible-report.py"


def load_checker() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_report", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load_checker()


# ---------- helper to build a minimal report ------------------------------------


def _make_report(title: str = "A title") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "summary": "A task.",
        "self_review": {
            "documentation": ["No docs."],
            "acceptance_criteria": [{"id": "AC1", "status": "met", "evidence": "covered"}],
            "omissions": [],
        },
        "acceptance_mapping": [{"id": "AC1", "status": "met", "evidence": "covered"}],
        "proposed_pull_request": {"title": title, "body": "A body."},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }


def _title_problems(problems: list[str]) -> list[str]:
    """Filter problem messages that concern the title."""
    return [p for p in problems if "title" in p.lower()]


# ---------- AC1: closing-keyword title is refused ------------------------------


class TestAclClosingKeyword:
    """AC1: crucible-report check fails a report whose title is 'Fix #148: ...'
    with the publisher's closing-keyword message."""

    @pytest.mark.parametrize(
        "title",
        [
            "Fix #148: resolve the auth issue",
            "fixes #42: update deps",
            "closed owner/repo#77",
            "resolve https://github.com/a/b/issues/1",
            "CLOSE: #10 fix it",
            "fix #1",
            "resolve #42",
        ],
    )
    def test_closing_keyword_title_is_refused(self, title: str) -> None:
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert len(title_problems) >= 1
        assert any("closing keyword" in p for p in title_problems), (
            f"Expected closing-keyword message in {title_problems}"
        )


# ---------- AC1b: at-mention title is refused ----------------------------------


class TestAclAtMention:
    """A title carrying an at-mention is refused with the publisher's message."""

    @pytest.mark.parametrize(
        "title",
        [
            "fix bug @alice",
            "@bob review this",
            "update for @team/core",
        ],
    )
    def test_at_mention_title_is_refused(self, title: str) -> None:
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert len(title_problems) >= 1
        assert any("at-mention" in p for p in title_problems), (
            f"Expected at-mention message in {title_problems}"
        )


# ---------- AC1c: multi-line title is refused ----------------------------------


class TestMultiLineTitle:
    """A multi-line title is refused with the publisher's message."""

    @pytest.mark.parametrize(
        "title",
        [
            "fix bug\nsecond line",
            "line one\r\nline two",
            "a\nb\nc",
        ],
    )
    def test_multiline_title_is_refused(self, title: str) -> None:
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert len(title_problems) >= 1
        assert any("one line" in p for p in title_problems), (
            f"Expected 'one line' message in {title_problems}"
        )


# ---------- AC1d: empty title is refused (already worked) ----------------------


class TestEmptyTitle:
    """An empty title is refused."""

    def test_empty_string(self) -> None:
        report = _make_report("")
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert len(title_problems) >= 1
        assert any("must be a one-line title" in p for p in title_problems), (
            f"Expected empty-title message in {title_problems}"
        )

    def test_whitespace_only(self) -> None:
        report = _make_report("   ")
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert len(title_problems) >= 1


# ---------- AC3: long title passes check (publisher shortens) -----------------


class TestAC3LongTitle:
    """AC3: A title over the length limit still passes check."""

    def test_long_title_passes(self) -> None:
        report = _make_report("x" * 200)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        # No title problems: length is handled by the publisher, not the check.
        assert len(title_problems) == 0


# ---------- hades #440 review: truncate before checking keywords/mentions ------


class TestTruncationBeforeTitleChecks:
    """The publisher shortens a long title before checking it for closing keywords
    and mentions (crucible/domain/publication.py validate_title). The checker must
    decide on the same shortened candidate, or it refuses a title the publisher
    would have accepted once truncated (PR #440 review)."""

    def test_keyword_beyond_truncation_boundary_passes(self) -> None:
        """A closing keyword that only appears after the publisher's truncation
        point is never seen by the publisher, so the checker must not flag it."""
        title = "A" * 66 + " fix #1"
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert title_problems == [], f"Unexpected title problems: {title_problems}"

    def test_mention_beyond_truncation_boundary_passes(self) -> None:
        title = "A" * 66 + " @alice"
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert title_problems == [], f"Unexpected title problems: {title_problems}"

    def test_keyword_within_truncation_boundary_still_fails(self) -> None:
        """A closing keyword that survives the publisher's truncation is still
        refused: truncating must not become a way to smuggle one through."""
        title = "fix #1 " + "A" * 70
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert any("closing keyword" in p for p in title_problems), (
            f"Expected closing-keyword message in {title_problems}"
        )

    def test_mention_within_truncation_boundary_still_fails(self) -> None:
        title = "@alice " + "A" * 70
        report = _make_report(title)
        problems = checker.check(report, criteria=None)
        title_problems = _title_problems(problems)
        assert any("at-mention" in p for p in title_problems), (
            f"Expected at-mention message in {title_problems}"
        )

    def test_shorten_title_agrees_with_publication(self) -> None:
        """The checker's copy of shorten_title must cut a long title exactly where
        publication.py's shorten_title does, or the two disagree about what the
        publisher actually sees."""
        from crucible.domain.publication import shorten_title as pub_shorten_title  # noqa: PLC0415

        titles = [
            "A" * 66 + " fix #1",
            "fix #1 " + "A" * 70,
            "x" * 200,
            "no spaces at all" + "y" * 60,
            "short with a trailing word boundary near the cutoff point exactly here",
        ]
        for title in titles:
            clean = " ".join(title.split())
            assert checker.shorten_title(clean) == pub_shorten_title(clean), (
                f"shorten_title drifted on {title!r}"
            )


# ---------- AC2: parity test - script regexes match publication.py regexes -----


class TestAC2Parity:
    """AC2: A parity test fails if the script's title regexes drift from
    crucible/domain/publication.py."""

    def test_closing_keyword_patterns_agree(self) -> None:
        """The checker's closing-keyword regex equals publication.py's."""
        from crucible.domain.publication import _CLOSING_RE  # noqa: PLC0415

        script_pattern = checker.CLOSING_RE.pattern
        crucible_pattern = _CLOSING_RE.pattern
        assert script_pattern == crucible_pattern, (
            f"closing-keyword pattern drifted: script={script_pattern!r} "
            f"crucible={crucible_pattern!r}"
        )

    def test_mention_pattern_agrees(self) -> None:
        """The checker's mention regex equals publication.py's."""
        from crucible.domain.publication import _MENTION_RE  # noqa: PLC0415

        script_pattern = checker.MENTION_RE.pattern
        crucible_pattern = _MENTION_RE.pattern
        assert script_pattern == crucible_pattern, (
            f"at-mention pattern drifted: script={script_pattern!r} crucible={crucible_pattern!r}"
        )

    def test_both_regexes_match_the_same_corpus(self) -> None:
        """Both regexes agree on a shared test corpus: no false-positives or
        false-negatives between the two copies."""
        from crucible.domain.publication import _CLOSING_RE, _MENTION_RE  # noqa: PLC0415

        # Closing-keyword corpus: titles that should or should not match.
        closing_positive = [
            "Fix #148: bug",
            "FIXES #42",
            "closes https://github.com/a/b/issues/1",
        ]
        closing_negative = [
            "fix the typo in README",  # no issue ref
            "fixup! amend commit",  # no issue ref
            "fix my local build",  # no #ref
            "close pull/3",  # PR ref, not issue ref
            "Resolved issue #99",  # "issue" between keyword and ref
        ]

        for title in closing_positive:
            assert _CLOSING_RE.search(title) is not None, (
                f"publisher regex missed closing keyword in {title!r}"
            )
            assert checker.CLOSING_RE.search(title) is not None, (
                f"script regex missed closing keyword in {title!r}"
            )
            # Both should agree.
            pub_hits = _CLOSING_RE.search(title)
            scr_hits = checker.CLOSING_RE.search(title)
            assert (pub_hits is not None) == (scr_hits is not None), (
                f"closing keywords disagree on {title!r}"
            )

        for title in closing_negative:
            assert _CLOSING_RE.search(title) is None, f"publisher regex false-positive on {title!r}"
            assert checker.CLOSING_RE.search(title) is None, (
                f"script regex false-positive on {title!r}"
            )

        # Mention corpus.
        mention_positive = [
            "fix bug @alice",
            "@bob review this",
            "update @user/name",
        ]
        mention_negative = [
            "fix the @ sign in the file",  # @ preceded by space, but followed by non-alnum
            "hello @ world",  # @ followed by space
        ]

        for title in mention_positive:
            assert _MENTION_RE.search(title) is not None, (
                f"publisher regex missed mention in {title!r}"
            )
            assert checker.MENTION_RE.search(title) is not None, (
                f"script regex missed mention in {title!r}"
            )
            pub_hits = _MENTION_RE.search(title)
            scr_hits = checker.MENTION_RE.search(title)
            assert (pub_hits is not None) == (scr_hits is not None), (
                f"mentions disagree on {title!r}"
            )

        for title in mention_negative:
            assert _MENTION_RE.search(title) is None, (
                f"publisher regex false-positive on mention in {title!r}"
            )
            assert checker.MENTION_RE.search(title) is None, (
                f"script regex false-positive on mention in {title!r}"
            )
