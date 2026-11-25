"""CRY-345: retention 90 ngày cho ảnh chụp phái sinh, kèm backfill và index hết hạn."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "c8e1a2b4d9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "derivatives_snapshot"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        f"UPDATE {_TABLE} SET expires_at = collected_at + interval '90 days' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column(_TABLE, "expires_at", nullable=False)
    op.create_index(
        "ix_derivatives_snapshot_expires",
        _TABLE,
        ["workspace_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_derivatives_snapshot_expires", table_name=_TABLE)
    op.drop_column(_TABLE, "expires_at")
