"""Widen pk_parameters.population — AI-extracted population descriptions
(e.g. cross-species comparator notes) routinely exceed 128 chars and were
getting truncation errors on insert.

Revision ID: 003
Revises: 002
Create Date: 2026-06-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "pk_parameters",
        "population",
        existing_type=sa.String(128),
        type_=sa.String(512),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "pk_parameters",
        "population",
        existing_type=sa.String(512),
        type_=sa.String(128),
        existing_nullable=True,
    )
