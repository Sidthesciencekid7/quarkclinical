"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Enums ---
    op.execute("DO $$ BEGIN CREATE TYPE user_role AS ENUM ('generator', 'reviewer', 'approver'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE pk_parameter AS ENUM ('Cmax','AUC0t','AUC0inf','Tmax','t_half','Vd','CL','bioavailability','protein_binding'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE literature_source AS ENUM ('pubmed', 'dailymed', 'ct_gov'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE study_type AS ENUM ('BE', 'biosimilar_pk', 'food_effect'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE protocol_status AS ENUM ('draft', 'review', 'approved'); EXCEPTION WHEN duplicate_object THEN null; END $$")

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.Enum("generator", "reviewer", "approver", name="user_role", create_type=False), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # --- drugs ---
    op.create_table(
        "drugs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("inn", sa.String(255), nullable=False),
        sa.Column("brand_names", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("rxnorm_id", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_drugs_inn", "drugs", ["inn"], unique=True)

    # --- pk_parameters ---
    op.create_table(
        "pk_parameters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("drug_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parameter", sa.Enum("Cmax","AUC0t","AUC0inf","Tmax","t_half","Vd","CL","bioavailability","protein_binding", name="pk_parameter", create_type=False), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("unit", sa.String(64), nullable=False),
        sa.Column("population", sa.String(128), nullable=True),
        sa.Column("source_pmid", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pk_parameters_drug_id", "pk_parameters", ["drug_id"])

    # --- literature_cache ---
    op.create_table(
        "literature_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pmid", sa.String(64), nullable=False),
        sa.Column("drug_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("abstract", sa.Text, nullable=True),
        sa.Column("source", sa.Enum("pubmed", "dailymed", "ct_gov", name="literature_source", create_type=False), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ttl_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_literature_cache_pmid", "literature_cache", ["pmid"])
    op.create_index("ix_literature_cache_drug_id", "literature_cache", ["drug_id"])

    # --- protocols ---
    op.create_table(
        "protocols",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("drug_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("drugs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("study_type", sa.Enum("BE", "biosimilar_pk", "food_effect", name="study_type", create_type=False), nullable=False),
        sa.Column("population_config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("sections", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.Enum("draft", "review", "approved", name="protocol_status", create_type=False), nullable=False, server_default="draft"),
        sa.Column("guideline_version", sa.String(64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_protocols_org_id", "protocols", ["org_id"])
    op.create_index("ix_protocols_drug_id", "protocols", ["drug_id"])

    # --- audit_logs (append-only) ---
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("diff", postgresql.JSONB, nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])

    # Revoke UPDATE and DELETE on audit_logs at the DB level
    op.execute("REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC")


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("protocols")
    op.drop_table("literature_cache")
    op.drop_table("pk_parameters")
    op.drop_table("drugs")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS protocol_status")
    op.execute("DROP TYPE IF EXISTS study_type")
    op.execute("DROP TYPE IF EXISTS literature_source")
    op.execute("DROP TYPE IF EXISTS pk_parameter")
    op.execute("DROP TYPE IF EXISTS user_role")
