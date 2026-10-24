"""Differentiate entry and exit fills in the PAPER ledger.

Revision ID: 6c2f91a4e7b3
Revises: 5b8c3f4a1d92
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c2f91a4e7b3"
down_revision: str | None = "5b8c3f4a1d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fill",
        sa.Column("fill_role", sa.String(length=8), server_default="ENTRY", nullable=False),
    )
    op.create_check_constraint("fill_role_valid", "fill", "fill_role IN ('ENTRY', 'EXIT')")


def downgrade() -> None:
    op.drop_constraint("fill_role_valid", "fill", type_="check")
    op.drop_column("fill", "fill_role")
