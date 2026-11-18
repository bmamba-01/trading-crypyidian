"""WP-07B delivery-policy: thêm cột JSONB delivery_policy cho telegram_signal_subscription."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1a2b3c4d5f6"
down_revision: str | None = "d8e2f4a6b8c0"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Thêm cột delivery_policy (quiet hours, timezone, daily cap, notification types)."""
    op.add_column(
        "telegram_signal_subscription",
        sa.Column(
            "delivery_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "telegram_signal_delivery_policy_object",
        "telegram_signal_subscription",
        "jsonb_typeof(delivery_policy) = 'object'",
    )


def downgrade() -> None:
    """Gỡ ràng buộc và cột delivery_policy."""
    op.drop_constraint(
        "telegram_signal_delivery_policy_object",
        "telegram_signal_subscription",
        type_="check",
    )
    op.drop_column("telegram_signal_subscription", "delivery_policy")
