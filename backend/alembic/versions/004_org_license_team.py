"""Organization + License + team-join structure.

Additive only — does not touch the existing free-text org_id columns on
users/protocols, so the existing org-isolation checks (get_protocol_scoped
etc., which compare those strings directly) are untouched. Each distinct
existing org_id string gets one Organization row (mapped via `slug`) with an
immediately-active License, so today's seeded orgs/users keep working exactly
as before with no disruption. No payment fields — see the enterprise-
licensing plan; License.status/seat_count are settable directly for now.

Revision ID: 004
Revises: 003
Create Date: 2026-07-11
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("join_token", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)
    op.create_index("ix_organizations_join_token", "organizations", ["join_token"], unique=True)

    op.create_table(
        "licenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "status", sa.Enum("active", "suspended", name="license_status", native_enum=False, length=20),
            nullable=False, server_default="active",
        ),
        sa.Column("seat_count", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_licenses_organization_id", "licenses", ["organization_id"])

    op.add_column(
        "users",
        sa.Column("is_org_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # --- Backfill: one Organization + active License per existing org_id ---
    conn = op.get_bind()
    slugs = [
        row[0]
        for row in conn.execute(sa.text(
            "SELECT DISTINCT org_id FROM users "
            "UNION SELECT DISTINCT org_id FROM protocols"
        )).fetchall()
        if row[0]
    ]

    now = datetime.now(timezone.utc)
    for slug in slugs:
        org_id = uuid.uuid4()
        conn.execute(
            sa.text(
                "INSERT INTO organizations (id, slug, name, created_at) "
                "VALUES (:id, :slug, :slug, :now)"
            ),
            {"id": org_id, "slug": slug, "now": now},
        )

        headcount = conn.execute(
            sa.text("SELECT count(*) FROM users WHERE org_id = :slug"), {"slug": slug}
        ).scalar_one()
        seat_count = max(headcount + 10, 20)

        conn.execute(
            sa.text(
                "INSERT INTO licenses "
                "(id, organization_id, status, seat_count, period_start, period_end, created_at, updated_at) "
                "VALUES (:id, :org_id, 'active', :seats, :start, :end, :now, :now)"
            ),
            {
                "id": uuid.uuid4(), "org_id": org_id, "seats": seat_count,
                "start": now, "end": now + timedelta(days=365), "now": now,
            },
        )

        # Deterministic-but-arbitrary owner pick (User has no created_at column
        # to order by "earliest") — every existing seeded org gets exactly one
        # owner so the new team panel has someone who can manage the join link.
        owner_id = conn.execute(
            sa.text("SELECT id FROM users WHERE org_id = :slug ORDER BY id LIMIT 1"), {"slug": slug}
        ).scalar_one_or_none()
        if owner_id:
            conn.execute(
                sa.text("UPDATE users SET is_org_owner = true WHERE id = :uid"), {"uid": owner_id}
            )


def downgrade() -> None:
    op.drop_column("users", "is_org_owner")
    op.drop_index("ix_licenses_organization_id", table_name="licenses")
    op.drop_table("licenses")
    op.drop_index("ix_organizations_join_token", table_name="organizations")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
