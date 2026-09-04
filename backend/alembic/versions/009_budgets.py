"""Budgeting tool — budgets, categories, line items.

The second tool in the suite: a clinical study budget that can stand alone OR
link to a generated protocol, from which services/budget_engine.py derives real
cost-driver line items (quantities from the protocol's already-computed
visit/sampling schedule; prices always left blank for the user).

  - budgets            one budget. protocol_id is NULLABLE with ondelete=SET NULL
                       so a budget survives deletion of its source protocol.
                       org_id is a plain indexed string (the org slug), matching
                       protocols/study_reports — never an FK to organizations.
  - budget_categories  the 17-row seed template (Recruiting ... Pass-Through),
                       fully user-editable; `bucket` drives the rollup math.
  - budget_line_items  one real budget row. Every money column is NULLABLE:
                       real CRO budgets carry rows with no dollar figure at all,
                       only a status ("Out of Scope", "Actuals incurred", ...).

Plain columns / string statuses (no Postgres enum types) per this project's
Alembic enum-idempotency history (see 004/006/007/008): every status-like column
here is a native_enum=False Enum, i.e. VARCHAR(20) + a server_default.

Revision ID: 009
Revises: 008
Create Date: 2026-09-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column(
            "protocol_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protocols.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sponsor_name", sa.String(255), nullable=True),
        sa.Column("cro_name", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column(
            "status",
            sa.Enum("draft", "final", "archived", name="budget_status",
                    native_enum=False, length=20),
            nullable=False, server_default="draft",
        ),
        sa.Column("drug_inn", sa.String(255), nullable=True),
        sa.Column("study_type", sa.String(32), nullable=True),
        sa.Column("n_enrolled", sa.Integer(), nullable=True),
        sa.Column("screen_failure_rate_pct", sa.Float(), nullable=True),
        sa.Column("n_screen_failures", sa.Integer(), nullable=True),
        sa.Column("n_alternates", sa.Integer(), nullable=True),
        sa.Column("n_screened", sa.Integer(), nullable=True),
        sa.Column("n_sites", sa.Integer(), nullable=True),
        sa.Column("n_dosing_groups", sa.Integer(), nullable=True),
        sa.Column("project_duration_months", sa.Float(), nullable=True),
        sa.Column(
            "discount_type",
            sa.Enum("none", "percent", "flat", name="budget_discount_type",
                    native_enum=False, length=20),
            nullable=False, server_default="none",
        ),
        sa.Column("discount_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("derivation", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_budgets_org_id", "budgets", ["org_id"])
    op.create_index("ix_budgets_protocol_id", "budgets", ["protocol_id"])

    op.create_table(
        "budget_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "budget_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "bucket",
            sa.Enum("direct", "stipend", "variable", "pass_through",
                    name="budget_bucket", native_enum=False, length=20),
            nullable=False, server_default="direct",
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_budget_categories_budget_id", "budget_categories", ["budget_id"])

    op.create_table(
        "budget_line_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "category_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("budget_categories.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("label", sa.String(512), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("unit_cost", sa.Float(), nullable=True),
        sa.Column("unit_description", sa.String(255), nullable=True),
        sa.Column("total", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("costed", "out_of_scope", "not_applicable", "not_planned",
                    "outsourced", "actuals_only",
                    name="budget_line_status", native_enum=False, length=20),
            nullable=False, server_default="costed",
        ),
        sa.Column(
            "source",
            sa.Enum("manual", "protocol_derived", name="budget_line_source",
                    native_enum=False, length=20),
            nullable=False, server_default="manual",
        ),
        sa.Column("derivation_key", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_budget_line_items_category_id", "budget_line_items", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_budget_line_items_category_id", table_name="budget_line_items")
    op.drop_table("budget_line_items")
    op.drop_index("ix_budget_categories_budget_id", table_name="budget_categories")
    op.drop_table("budget_categories")
    op.drop_index("ix_budgets_protocol_id", table_name="budgets")
    op.drop_index("ix_budgets_org_id", table_name="budgets")
    op.drop_table("budgets")
