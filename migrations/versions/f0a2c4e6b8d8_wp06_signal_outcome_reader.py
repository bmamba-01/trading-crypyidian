"""WP06.14: đọc kết quả tín hiệu theo quyền report hiện hành của từng bot."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d8"
down_revision: str | None = "f0a2c4e6b8d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER = "crypidian_admin_report_reader"
OUTCOME_POLICY = "wp06_admin_report_reader_signal_outcome_select"
DELIVERY_POLICY = "wp06_admin_report_reader_telegram_signal_delivery_select"
OUTCOME_COLUMNS = (
    "id, workspace_id, bot_id, decision_id, method_version, horizon_bars, status, symbol, "
    "timeframe, direction, strategy_release_id, signal_at, first_touch, same_bar_conflict, "
    "seconds_to_first_touch, mfe_r, mae_r, outcome_r, label"
)
DELIVERY_COLUMNS = "workspace_id, bot_id, decision_id, status"


def upgrade() -> None:
    """Cấp đúng dữ liệu UI cần và giữ lọc hàng bởi membership/grant hiện hành."""
    op.execute(f"GRANT SELECT ({OUTCOME_COLUMNS}) ON TABLE public.signal_outcome TO {READER}")
    op.execute(
        f"GRANT SELECT ({DELIVERY_COLUMNS}) ON TABLE public.telegram_signal_delivery TO {READER}"
    )
    op.execute("ALTER TABLE public.signal_outcome ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.telegram_signal_delivery ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {OUTCOME_POLICY} ON public.signal_outcome "
        f"FOR SELECT TO {READER} USING ("
        "public.wp06_admin_report_reader_can_access(workspace_id, bot_id))"
    )
    op.execute(
        f"CREATE POLICY {DELIVERY_POLICY} ON public.telegram_signal_delivery "
        f"FOR SELECT TO {READER} USING ("
        "public.wp06_admin_report_reader_can_access(workspace_id, bot_id))"
    )


def downgrade() -> None:
    """Thu hồi policy và allowlist riêng do revision này tạo."""
    op.execute(f"DROP POLICY {DELIVERY_POLICY} ON public.telegram_signal_delivery")
    op.execute(f"DROP POLICY {OUTCOME_POLICY} ON public.signal_outcome")
    op.execute(
        f"REVOKE SELECT ({DELIVERY_COLUMNS}) ON TABLE public.telegram_signal_delivery FROM {READER}"
    )
    op.execute(f"REVOKE SELECT ({OUTCOME_COLUMNS}) ON TABLE public.signal_outcome FROM {READER}")
