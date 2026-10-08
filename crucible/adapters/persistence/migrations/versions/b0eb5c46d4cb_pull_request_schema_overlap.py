"""pull_request_schema_overlap

Revision ID: b0eb5c46d4cb
Revises: 0051_routing_model_references
Create Date: 2026-10-07 23:47:43.308583
"""

from __future__ import annotations

revision = "b0eb5c46d4cb"
down_revision = "0051_routing_model_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    
    op.add_column('pull_requests', sa.Column('schema_tables', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('pull_requests', sa.Column('schema_columns', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('pull_requests', sa.Column('schema_models', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    from alembic import op
    
    op.drop_column('pull_requests', 'schema_tables')
    op.drop_column('pull_requests', 'schema_columns')
    op.drop_column('pull_requests', 'schema_models')
