"""protocol_versions table for 21 CFR Part 11 version history

Revision ID: 002
Revises: 001
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "protocol_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "protocol_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protocols.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("sections", postgresql.JSONB, nullable=False),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=True),
        # AI_REGEN | MANUAL_EDIT | INITIAL | SIGNED
        sa.Column("change_type", sa.String(64), nullable=False),
        sa.Column("change_detail", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_protocol_versions_protocol_id",
        "protocol_versions",
        ["protocol_id"],
    )
    # Each (protocol_id, version) pair must be unique — enforces append-only semantics
    op.create_unique_constraint(
        "uq_protocol_versions_protocol_version",
        "protocol_versions",
        ["protocol_id", "version"],
    )


def downgrade() -> None:
    op.drop_table("protocol_versions")
