"""Study reports (CSR) — post-study results + ICH E3 narrative.

Adds two tables for the Clinical Study Report feature (the post-study bookend to
protocols):

  - study_reports          one CSR per completed protocol. `results` holds the
                           DETERMINISTIC computed numbers (per-subject NCA, BE
                           GMR/90% CI/CV/verdict, safety summary); `sections`
                           holds the AI-drafted narrative around them. Raw
                           subject-level data is NOT stored — only a SHA-256
                           checksum + a non-identifying summary. Reuses the async
                           generation lifecycle (generation_status/error) from 007.
  - study_report_versions  immutable snapshots for the 21 CFR Part 11 trail.

Plain columns / string statuses (no Postgres enum types) per this project's
Alembic enum-idempotency history (see 004/006/007).

Revision ID: 008
Revises: 007
Create Date: 2026-07-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "study_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "protocol_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("input_kind", sa.String(16), nullable=False, server_default="params"),
        sa.Column("dataset_checksum", sa.String(64), nullable=True),
        sa.Column("dataset_summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sections", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generation_status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("generation_error", sa.Text(), nullable=True),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_study_reports_protocol_id", "study_reports", ["protocol_id"])
    op.create_index("ix_study_reports_org_id", "study_reports", ["org_id"])

    op.create_table(
        "study_report_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "study_report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("study_reports.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sections", postgresql.JSONB(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=True),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_type", sa.String(64), nullable=False),
        sa.Column("change_detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("study_report_id", "version", name="uq_study_report_versions_sr_version"),
    )
    op.create_index("ix_study_report_versions_sr_id", "study_report_versions", ["study_report_id"])


def downgrade() -> None:
    op.drop_index("ix_study_report_versions_sr_id", table_name="study_report_versions")
    op.drop_table("study_report_versions")
    op.drop_index("ix_study_reports_org_id", table_name="study_reports")
    op.drop_index("ix_study_reports_protocol_id", table_name="study_reports")
    op.drop_table("study_reports")
