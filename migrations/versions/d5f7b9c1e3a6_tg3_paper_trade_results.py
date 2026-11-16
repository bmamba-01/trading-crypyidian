"""TG-3: gửi kết quả giao dịch PAPER và tin tổng hợp định kỳ qua Telegram signal-worker.

Thêm hai delivery ledger idempotent:

- ``telegram_trade_notification``: một hàng cho mỗi (subscription, trade, OPENED|CLOSED); RLS
  theo bot (``app.bot_id``) như ``telegram_signal_delivery``.
- ``telegram_paper_report_delivery``: một hàng cho mỗi (chat, cửa sổ tổng hợp); RLS theo
  workspace vì một tin gộp nhiều bot của cùng chat.

Quyền tối thiểu cho ``crypidian_signal_worker`` để dựng nội dung: chỉ ``SELECT`` theo cột trên
``trade``, ``fill``, ``position_event``, ``risk_reservation`` (cột ``reserved_risk`` cho 1R) và
``bot_instance`` (tên bot), kèm policy RLS chỉ cho bot đang đặt trong ``app.bot_id`` và chỉ hàng
PAPER. Không cấp INSERT/UPDATE/DELETE/TRUNCATE trên bất kỳ bảng ledger nào, không cấp quyền mức
bảng (``has_table_privilege(..., 'SELECT')`` vẫn sai), không chạm order/venue/reservation write.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f7b9c1e3a6"
down_revision: str | None = "c3e5a7b9d1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
WORKER = "crypidian_signal_worker"
NOTIFICATIONS = "telegram_trade_notification"
REPORTS = "telegram_paper_report_delivery"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_BOT = "NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid"
_BOT_SCOPE = f"{_WORKSPACE} AND bot_id = {_BOT}"
_STATUSES = "'PENDING', 'PROCESSING', 'SENT', 'DEAD', 'FILTERED'"
_DELIVERY_UPDATE_COLUMNS = (
    "status, attempts, available_at, lease_owner, lease_expires_at, sent_at, last_error_code"
)
# Cột ledger signal-worker được đọc; mọi cột khác (lineage, chi phí chi tiết, plan…) vẫn bị cấm.
LEDGER_READ_COLUMNS: dict[str, str] = {
    "trade": (
        "workspace_id, bot_id, account_id, id, intent_id, mode, status, symbol, side, "
        "quote_asset, net_pnl, opened_at, closed_at, created_at, risk_reservation_id"
    ),
    "fill": "workspace_id, bot_id, account_id, intent_id, mode, fill_role, quantity, price, "
    "occurred_at",
    "position_event": "workspace_id, bot_id, account_id, intent_id, mode, event_type, payload, "
    "created_at",
    "risk_reservation": "workspace_id, bot_id, id, reserved_risk",
}
_POLICY = "telegram_trade_result_worker_select"


def _delivery_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("status", sa.String(16), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        NOTIFICATIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("trade_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        *_delivery_columns(),
        sa.CheckConstraint(
            "kind IN ('OPENED', 'CLOSED')", name="telegram_trade_notification_kind_valid"
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUSES})", name="telegram_trade_notification_status_valid"
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="telegram_trade_notification_attempts_nonnegative"
        ),
        sa.CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)",
            name="telegram_trade_notification_sent_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "subscription_id"],
            [
                "telegram_signal_subscription.workspace_id",
                "telegram_signal_subscription.bot_id",
                "telegram_signal_subscription.id",
            ],
            ondelete="CASCADE",
            name="fk_telegram_trade_notification_subscription",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_trade_notification")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_telegram_trade_notification_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "subscription_id",
            "trade_id",
            "kind",
            name="uq_telegram_trade_notification_event",
        ),
    )
    op.create_index(
        "ix_telegram_trade_notification_ready",
        NOTIFICATIONS,
        ["workspace_id", "bot_id", "status", "available_at"],
    )
    op.create_table(
        REPORTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        *_delivery_columns(),
        sa.CheckConstraint("telegram_chat_id > 0", name="telegram_paper_report_chat_id_positive"),
        sa.CheckConstraint("window_end > window_start", name="telegram_paper_report_window_valid"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="telegram_paper_report_status_valid"),
        sa.CheckConstraint("attempts >= 0", name="telegram_paper_report_attempts_nonnegative"),
        sa.CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)",
            name="telegram_paper_report_sent_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
            name="fk_telegram_paper_report_delivery_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_paper_report_delivery")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_telegram_paper_report_delivery_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "telegram_chat_id",
            "window_start",
            name="uq_telegram_paper_report_delivery_window",
        ),
    )
    op.create_index(
        "ix_telegram_paper_report_delivery_ready",
        REPORTS,
        ["workspace_id", "status", "available_at"],
    )

    for table in (NOTIFICATIONS, REPORTS):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
        # Default privileges có thể cấp cho group role cũ; chỉ giữ grant tường minh bên dưới.
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, crypidian_app")
        op.execute(
            f"CREATE POLICY {table}_admin_select ON public.{table} "
            f"FOR SELECT TO {ADMIN} USING ({_WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {ADMIN}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO {WORKER}")
        op.execute(f"GRANT UPDATE ({_DELIVERY_UPDATE_COLUMNS}) ON TABLE public.{table} TO {WORKER}")

    for action, clause in (
        ("select", f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"),
        ("insert", f"FOR INSERT TO {WORKER} WITH CHECK ({_BOT_SCOPE})"),
        ("update", f"FOR UPDATE TO {WORKER} USING ({_BOT_SCOPE}) WITH CHECK ({_BOT_SCOPE})"),
    ):
        op.execute(
            f"CREATE POLICY {NOTIFICATIONS}_worker_{action} ON public.{NOTIFICATIONS} {clause}"
        )
    for action, clause in (
        ("select", f"FOR SELECT TO {WORKER} USING ({_WORKSPACE})"),
        ("insert", f"FOR INSERT TO {WORKER} WITH CHECK ({_WORKSPACE})"),
        ("update", f"FOR UPDATE TO {WORKER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"),
    ):
        op.execute(f"CREATE POLICY {REPORTS}_worker_{action} ON public.{REPORTS} {clause}")

    # Đọc ledger PAPER theo đúng bot đang xử lý; không có policy ghi nào cho signal-worker.
    for table, columns in LEDGER_READ_COLUMNS.items():
        paper = "" if table == "risk_reservation" else " AND mode = 'PAPER'"
        op.execute(
            f"CREATE POLICY {_POLICY} ON public.{table} "
            f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE}{paper})"
        )
        op.execute(f"GRANT SELECT ({columns}) ON TABLE public.{table} TO {WORKER}")
    op.execute(
        f"CREATE POLICY {_POLICY} ON public.bot_instance FOR SELECT TO {WORKER} "
        f"USING ({_WORKSPACE} AND id = {_BOT})"
    )
    op.execute(f"GRANT SELECT (workspace_id, id, name) ON TABLE public.bot_instance TO {WORKER}")

    # Rào bảo vệ: signal-worker không có quyền mức bảng hay quyền ghi trên ledger/order/venue.
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM unnest(ARRAY[
                    'order_intent', 'fill', 'trade', 'trade_cost', 'position_event',
                    'risk_decision', 'risk_reservation', 'venue_order_intent',
                    'venue_order_transition', 'exchange_order', 'bot_instance'
                ]) AS t(table_name)
                CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'])
                    AS p(privilege)
                WHERE pg_catalog.has_table_privilege(
                    'crypidian_signal_worker', t.table_name, p.privilege)
            ) OR EXISTS (
                SELECT 1 FROM unnest(ARRAY[
                    'order_intent', 'fill', 'trade', 'trade_cost', 'position_event',
                    'risk_decision', 'risk_reservation', 'venue_order_intent',
                    'venue_order_transition', 'exchange_order', 'bot_instance'
                ]) AS t(table_name)
                CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS p(privilege)
                WHERE pg_catalog.has_any_column_privilege(
                    'crypidian_signal_worker', t.table_name, p.privilege)
            ) OR EXISTS (
                SELECT 1 FROM unnest(ARRAY[
                    'order_intent', 'trade_cost', 'risk_decision', 'venue_order_intent',
                    'venue_order_transition', 'exchange_order'
                ]) AS t(table_name)
                WHERE pg_catalog.has_any_column_privilege(
                    'crypidian_signal_worker', t.table_name, 'SELECT')
            ) THEN
                RAISE EXCEPTION 'TG3_SIGNAL_WORKER_LEDGER_PRIVILEGE_TOO_WIDE';
            END IF;
        END $$"""
    )


def downgrade() -> None:
    op.execute(f"REVOKE SELECT (workspace_id, id, name) ON TABLE public.bot_instance FROM {WORKER}")
    op.execute(f"DROP POLICY {_POLICY} ON public.bot_instance")
    for table, columns in LEDGER_READ_COLUMNS.items():
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {WORKER}")
        op.execute(f"DROP POLICY {_POLICY} ON public.{table}")
    for table in (NOTIFICATIONS, REPORTS):
        for action in ("select", "insert", "update"):
            op.execute(f"DROP POLICY {table}_worker_{action} ON public.{table}")
        op.execute(f"DROP POLICY {table}_admin_select ON public.{table}")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {ADMIN}, {WORKER}")
    op.drop_index("ix_telegram_paper_report_delivery_ready", table_name=REPORTS)
    op.drop_table(REPORTS)
    op.drop_index("ix_telegram_trade_notification_ready", table_name=NOTIFICATIONS)
    op.drop_table(NOTIFICATIONS)
