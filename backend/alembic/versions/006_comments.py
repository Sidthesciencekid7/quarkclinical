"""Comments table — persistent, org-shared section review notes.

Replaces the editor's per-browser localStorage notes with a real table so a
reviewer's comments are visible to the rest of the organization and attributed
to a user. No new enum types: `resolved` is a plain boolean and the author is
stored as plain strings, deliberately avoiding the Alembic enum-idempotency
problems this project has hit before (see 004/005).

Org isolation is enforced in the router (get_protocol_scoped on the parent
protocol), so no org_id column is needed here; the FK cascade means deleting a
protocol removes its notes.

Revision ID: 006
Revises: 005
Create Date: 2026-07-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "protocol_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("section_name", sa.String(128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_id", sa.String(255), nullable=True),
        sa.Column("author_name", sa.String(255), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_comments_protocol_id", "comments", ["protocol_id"])


def downgrade() -> None:
    op.drop_index("ix_comments_protocol_id", table_name="comments")
    op.drop_table("comments")
