"""Tests for issue 149: UI polish (unsplit headers, harness display names, short image refs)."""

from __future__ import annotations

from pathlib import Path

from crucible.adapters.ui.pages.harnesses import DISPLAY_NAME, _harness_display
from crucible.adapters.ui.pages.images import _image_tag

ROOT = Path(__file__).parents[2]


class TestAC1UnsplitHeaders:
    """Table headers must never break inside a word."""

    def test_admin_css_has_overflow_wrap_normal_on_th(self) -> None:
        css = (ROOT / "crucible/adapters/ui/static/admin.css").read_text()
        # The th rule must contain overflow-wrap: normal.
        assert "overflow-wrap: normal" in css, (
            ".admin-table th must set overflow-wrap: normal so headers never break inside a word."
        )


class TestAC2HarnessDisplayNames:
    """Harness cells must show a display name with the identifier in a title attribute."""

    def test_display_name_contains_known_harnesses(self) -> None:
        assert DISPLAY_NAME["claude_code"] == "Claude Code"
        assert DISPLAY_NAME["codex"] == "Codex"
        assert DISPLAY_NAME["hermes"] == "Hermes"
        assert DISPLAY_NAME["qwen_code"] == "Qwen Code"
        assert DISPLAY_NAME["script-harness"] == "Script harness"

    def test_harness_display_returns_note_with_value_and_hint(self) -> None:
        result = _harness_display("claude_code")
        assert result["kind"] == "note"
        assert result["value"] == "Claude Code"
        assert result["hint"] == "claude_code"

    def test_harness_display_falls_back_to_name(self) -> None:
        result = _harness_display("unknown_harness")
        assert result["kind"] == "note"
        assert result["value"] == "unknown_harness"
        assert result["hint"] == "unknown_harness"


class TestAC3ImageShortReferences:
    """Image cells must show the tag with the full reference in a title attribute."""

    def test_image_tag_extracts_tag_after_colon(self) -> None:
        assert _image_tag("w:1") == "1"
        assert _image_tag("w:1.2.3") == "1.2.3"
        assert _image_tag("w:latest") == "latest"

    def test_image_tag_no_colon_returns_whole_string(self) -> None:
        assert _image_tag("sha256-abc") == "sha256-abc"

    def test_image_tag_does_not_split_on_additional_colons(self) -> None:
        # Only the first colon matters (w: ...), not subsequent ones.
        assert _image_tag("w:1.2.3:extra") == "1.2.3:extra"
