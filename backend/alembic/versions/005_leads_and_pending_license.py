"""Leads table + LicenseStatus.pending.

No Stripe/payment automation — enterprise deals are sales-assisted. Adds a
`leads` table for the landing page's "Contact sales" form.

`licenses.status` (models.py::License, native_enum=False) is a plain
VARCHAR(20) with no CHECK constraint in this database — confirmed by direct
inspection before writing this migration — so adding the new "pending" value
(self-serve org creation now lands there instead of "active"; the operator
manually activates via POST /api/orgs/{id}/activate-license once a deal is
confirmed out-of-band) needs no schema change at all, just the new value
being written by the app going forward.

Revision ID: 005
Revises: 004
Create Date: 2026-07-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("contact_name", sa.String(255), nullable=False),
        sa.Column("contact_email", sa.String(255), nullable=False),
        sa.Column("team_size_estimate", sa.String(64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Enum("new", "contacted", "won", "lost", name="lead_status", native_enum=False, length=20),
            nullable=False, server_default="new",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_leads_contact_email", "leads", ["contact_email"])


def downgrade() -> None:
    op.drop_index("ix_leads_contact_email", table_name="leads")
    op.drop_table("leads")
