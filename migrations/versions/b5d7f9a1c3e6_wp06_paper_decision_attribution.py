"""WP06: nối typed BotDecision vào intent PAPER mà không backfill lineage JSON."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d7f9a1c3e6"
down_revision: str | None = "b7d9f1a3c5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("order_intent", sa.Column("bot_decision_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_order_intent_bot_decision",
        "order_intent",
        "bot_decision",
        ["workspace_id", "bot_id", "bot_decision_id"],
        ["workspace_id", "bot_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_order_intent_bot_decision",
        "order_intent",
        ["workspace_id", "bot_id", "bot_decision_id"],
        unique=True,
        postgresql_where=sa.text("bot_decision_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM public.order_intent WHERE bot_decision_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'WP06_PAPER_DECISION_ATTRIBUTION_DOWNGRADE_BLOCKED';
            END IF;
        END $$;
        """
    )
    op.drop_index("uq_order_intent_bot_decision", table_name="order_intent")
    op.drop_constraint("fk_order_intent_bot_decision", "order_intent", type_="foreignkey")
    op.drop_column("order_intent", "bot_decision_id")
