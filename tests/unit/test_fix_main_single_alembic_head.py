"""The migration graph has exactly one head (FDY-0385).

Parallel branches that each number a revision the same way merge cleanly in git and
leave alembic with several heads, which only surfaces as MultipleHeads when a stack
starts. This test reads the graph without a database, so the next collision fails the
unit tier instead. The fix is a merge revision whose down_revision is every head."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from crucible.adapters.persistence import migrate

REPO = Path(__file__).resolve().parents[2]
MERGE = "0045_merge_0044_heads"
MERGED = {"0044_attempt_stall_shape", "0044_editor_leftovers_policy", "0044_merge_423_424"}


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))


def test_the_migration_graph_has_a_single_head() -> None:
    heads = _script().get_heads()
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads {sorted(heads)}; add a merge revision whose "
        "down_revision is the tuple of them"
    )


def test_the_0045_merge_joins_the_three_0044_heads() -> None:
    script = _script()
    merge = script.get_revision(MERGE)
    assert merge is not None
    assert set(merge.down_revision or ()) == MERGED
    assert script.get_current_head() == MERGE


def test_the_cli_config_sees_the_same_single_head() -> None:
    """`uv run alembic heads` reads alembic.ini at the repository root."""
    cfg = Config(str(REPO / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    assert Path(script.dir).resolve() == migrate.MIGRATIONS_DIR.resolve()
    assert script.get_heads() == [MERGE]
