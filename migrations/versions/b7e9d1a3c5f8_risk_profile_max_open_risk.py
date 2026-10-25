"""Thêm max_open_risk_fraction vào risk_profile_version (MB04.3).

Revision ID: b7e9d1a3c5f8
Revises: f4d6b8c0e2a5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e9d1a3c5f8"
down_revision: str | None = "f4d6b8c0e2a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_profile_version",
        sa.Column(
            "max_open_risk_fraction",
            sa.Numeric(12, 10, asdecimal=True),
            nullable=True,
            server_default=sa.text("1.0"),
        ),
    )
    op.execute(
        """
        UPDATE risk_profile_version
        SET max_open_risk_fraction = 1.0
        WHERE max_open_risk_fraction IS NULL;
        """
    )
    op.alter_column("risk_profile_version", "max_open_risk_fraction", nullable=False)
    op.create_check_constraint(
        "open_risk_fraction_range",
        "risk_profile_version",
        "max_open_risk_fraction > 0 AND max_open_risk_fraction <= 1",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM risk_profile_version
                WHERE max_open_risk_fraction <> 1.0
            ) THEN
                RAISE EXCEPTION 'MB04_DOWNGRADE_REQUIRES_EXPORT_OF_CUSTOM_RISK_PROFILES';
            END IF;
        END $$"""
    )
    op.drop_constraint("open_risk_fraction_range", "risk_profile_version", type_="check")
    op.drop_column("risk_profile_version", "max_open_risk_fraction")
