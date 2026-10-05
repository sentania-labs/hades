"""Merge the three 0044 heads that landed on main in parallel.

Revision ID: 0045_merge_0044_heads
Revises: 0044_attempt_stall_shape, 0044_editor_leftovers_policy, 0044_merge_423_424

PRs 426, 438 and 441 each numbered their revision 0044 on a parallel branch, so main
had three heads and every startup failed with MultipleHeads. This revision only joins
them; it changes no table, column or row.
"""

from __future__ import annotations

revision = "0045_merge_0044_heads"
down_revision = (
    "0044_attempt_stall_shape",
    "0044_editor_leftovers_policy",
    "0044_merge_423_424",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
