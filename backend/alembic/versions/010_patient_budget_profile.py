"""Patient/oncology budget rate-card profile.

Adds a `subject_profile` axis to Budget, orthogonal to the existing
`study_type`: `study_type` (BE / food_effect / biosimilar_pk) keeps selecting
the *scientific* shape of a study; `subject_profile` (healthy / patient)
selects which RATE CARD prices it — the existing frozen 17-category
healthy-volunteer card (services/budget_rate_card.HEALTHY_RATE_CARD, alias
RATE_CARD) or the new 22-category patient/oncology card
(services/budget_rate_card.PATIENT_RATE_CARD).

Every new Budget column is NULLABLE (so every existing row is untouched) and
is backfilled to the healthy defaults where a value is meaningful
(subject_profile='healthy' on every pre-existing row), so a healthy-volunteer
budget created before or after this migration produces byte-for-byte the same
numbers.

Also:
  (a) adds the two new BudgetBucket values used only by the patient card
      (investigator_grants, software) and the new BudgetLineSource value
      (researched) used to badge a cited/researched unit cost — both are
      plain VARCHAR columns with NO CHECK constraint (this project's
      native_enum=False convention; SQLAlchemy >= 1.4 defaults
      create_constraint=False, and this project runs 2.0.36 — verified,
      which is why the pre-existing 'rate_card' BudgetLineSource value
      already works today despite migration 009 listing only
      'manual'/'protocol_derived'). No DDL is needed for the new enum
      *values* themselves.
  (b) widens budget_categories.bucket and budget_line_items.source from
      VARCHAR(20) to VARCHAR(32) as defensive headroom: "investigator_grants"
      is 19 characters, one short of the old 20-character limit.

Revision ID: 010
Revises: 009
Create Date: 2026-09-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every new nullable Budget column added by this migration, as
# (name, sqlalchemy type). Declared once and reused by both upgrade (to build
# the column) and downgrade (to drop it), so the two can never drift apart.
_NEW_NULLABLE_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("therapeutic_area", sa.String(64)),
    ("indication", sa.String(255)),
    ("n_countries", sa.Integer()),
    ("n_sites_selected", sa.Integer()),
    ("dropout_rate_pct", sa.Float()),
    ("n_completed", sa.Integer()),
    ("startup_months", sa.Float()),
    ("recruitment_months", sa.Float()),
    ("treatment_months", sa.Float()),
    ("followup_months", sa.Float()),
    ("closeout_months", sa.Float()),
    ("n_expected_saes", sa.Integer()),
    ("n_expected_susars", sa.Integer()),
    ("n_monitoring_visits_onsite", sa.Integer()),
    ("n_monitoring_visits_remote", sa.Integer()),
    ("n_pharmacy_visits", sa.Integer()),
    ("n_visits_per_patient", sa.Integer()),
    ("n_protocol_amendments", sa.Integer()),
    ("geography_profile", sa.String(32)),
    ("geography_factor", sa.Float()),
    ("archival_retention_years", sa.Integer()),
    ("bicr_required", sa.Boolean()),
    ("comparator_arm_fraction", sa.Float()),
]


def upgrade() -> None:
    # --- (a) the subject_profile selector itself -------------------------
    op.add_column(
        "budgets",
        sa.Column(
            "subject_profile",
            sa.Enum("healthy", "patient", name="budget_subject_profile",
                    native_enum=False, length=16),
            nullable=True,
        ),
    )

    # --- (b) every other new, nullable Budget column ----------------------
    for name, coltype in _NEW_NULLABLE_COLUMNS:
        op.add_column("budgets", sa.Column(name, coltype, nullable=True))

    # --- (c) the one NOT NULL new column, with a safe server_default ------
    op.add_column(
        "budgets",
        sa.Column(
            "comparator_sourced_by_sponsor", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )

    # --- (d) widen the two enum-backed VARCHARs for headroom ---------------
    # Plain VARCHAR columns (native_enum=False), so this is a column-width
    # change only — no CHECK constraint exists to touch, and no data need be
    # rewritten (every existing value is well within 32 characters).
    op.alter_column(
        "budget_categories", "bucket",
        existing_type=sa.String(20), type_=sa.String(32),
        existing_nullable=False, existing_server_default="direct",
    )
    op.alter_column(
        "budget_line_items", "source",
        existing_type=sa.String(20), type_=sa.String(32),
        existing_nullable=False, existing_server_default="manual",
    )

    # --- (e) backfill: every pre-existing budget is a healthy-volunteer one
    # Explicit, not left NULL, so `_is_user_authoritative`-style resolution
    # treats it as a real first-run value and the drug-knowledge-base
    # patient-inference path can never silently reclassify a historic row.
    op.execute("UPDATE budgets SET subject_profile = 'healthy' WHERE subject_profile IS NULL")


def downgrade() -> None:
    op.alter_column(
        "budget_line_items", "source",
        existing_type=sa.String(32), type_=sa.String(20),
        existing_nullable=False, existing_server_default="manual",
    )
    op.alter_column(
        "budget_categories", "bucket",
        existing_type=sa.String(32), type_=sa.String(20),
        existing_nullable=False, existing_server_default="direct",
    )

    op.drop_column("budgets", "comparator_sourced_by_sponsor")

    for name, _coltype in reversed(_NEW_NULLABLE_COLUMNS):
        op.drop_column("budgets", name)

    op.drop_column("budgets", "subject_profile")
