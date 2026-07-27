"""Protocol async-generation lifecycle columns.

Real AI generation runs for minutes — too long to hold one HTTP request open
behind Railway's edge proxy, which drops the connection (the browser then
reports "Failed to fetch"). create_protocol now returns a "generating" row
immediately and a background task fills it in; the client polls until it flips
to "complete" or "failed".

Adds two columns to `protocols`:
  - generation_status  text, NOT NULL, server_default 'complete'
  - generation_error   text, NULL

server_default 'complete' backfills every EXISTING protocol as done (so already
generated protocols open normally and never look stuck), and makes every
non-generation insert path (clone, section edits) correct without extra code.
Plain text columns, no enum type — deliberately avoiding the Alembic
enum-idempotency problems this project has hit before (see 004/006).

Revision ID: 007
Revises: 006
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "protocols",
        sa.Column(
            "generation_status",
            sa.String(16),
            nullable=False,
            server_default="complete",
        ),
    )
    op.add_column(
        "protocols",
        sa.Column("generation_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("protocols", "generation_error")
    op.drop_column("protocols", "generation_status")
