"""WP-07B: allowlist tín hiệu và receipt cho delivery bị lọc."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8a2c4e6d0b1"
down_revision: str | None = "d4f6a8c0e2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FILTERS = ("symbols", "timeframes", "directions", "strategy_ids", "setup_codes")
_ADMIN = "crypidian_admin_web"
_WORKER = "crypidian_signal_worker"
_OLD_STATUS_CHECK = "ck_telegram_signal_delivery_telegram_signal_delivery_status_valid"
_NEW_STATUS_CHECK = "ck_telegram_signal_delivery_telegram_signal_delivery_status_valid"


def upgrade() -> None:
    for name in _FILTERS:
        op.add_column(
            "telegram_signal_subscription",
            sa.Column(
                name,
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
        )
        op.create_check_constraint(
            op.f(f"ck_telegram_signal_subscription_telegram_signal_{name}_filter_valid"),
            "telegram_signal_subscription",
            f"jsonb_typeof({name}) = 'array' AND jsonb_array_length({name}) <= 100",
        )

    op.execute(
        f"GRANT UPDATE ({', '.join((*_FILTERS, 'updated_at'))}) "
        "ON TABLE public.telegram_signal_subscription TO "
        f"{_ADMIN}"
    )
    op.execute(
        "GRANT SELECT (symbols, timeframes, directions, strategy_ids, setup_codes) "
        "ON TABLE public.telegram_signal_subscription TO "
        f"{_WORKER}"
    )

    op.drop_constraint(op.f(_OLD_STATUS_CHECK), "telegram_signal_delivery", type_="check")
    op.create_check_constraint(
        op.f(_NEW_STATUS_CHECK),
        "telegram_signal_delivery",
        "status IN ('PENDING', 'PROCESSING', 'SENT', 'EXPIRED', 'DEAD', 'FILTERED')",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM public.telegram_signal_subscription
                WHERE symbols <> '[]'::jsonb OR timeframes <> '[]'::jsonb
                    OR directions <> '[]'::jsonb OR strategy_ids <> '[]'::jsonb
                    OR setup_codes <> '[]'::jsonb
            ) OR EXISTS (
                SELECT 1 FROM public.telegram_signal_delivery WHERE status = 'FILTERED'
            ) THEN
                RAISE EXCEPTION 'WP07_SIGNAL_FILTERS_DOWNGRADE_BLOCKED';
            END IF;
        END $$"""
    )
    op.execute(
        "REVOKE SELECT (symbols, timeframes, directions, strategy_ids, setup_codes) "
        "ON TABLE public.telegram_signal_subscription FROM "
        f"{_WORKER}"
    )
    op.execute(
        f"REVOKE UPDATE ({', '.join((*_FILTERS, 'updated_at'))}) "
        "ON TABLE public.telegram_signal_subscription FROM "
        f"{_ADMIN}"
    )
    op.drop_constraint(op.f(_NEW_STATUS_CHECK), "telegram_signal_delivery", type_="check")
    op.create_check_constraint(
        op.f(_OLD_STATUS_CHECK),
        "telegram_signal_delivery",
        "status IN ('PENDING', 'PROCESSING', 'SENT', 'EXPIRED', 'DEAD')",
    )
    for name in reversed(_FILTERS):
        op.drop_constraint(
            op.f(f"ck_telegram_signal_subscription_telegram_signal_{name}_filter_valid"),
            "telegram_signal_subscription",
            type_="check",
        )
        op.drop_column("telegram_signal_subscription", name)
