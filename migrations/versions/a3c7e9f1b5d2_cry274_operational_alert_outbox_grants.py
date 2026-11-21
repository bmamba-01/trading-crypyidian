"""CRY-274: quyền tối thiểu cho signal-worker gửi cảnh báo vận hành qua outbox.

Consumer ``telegram_operational_alert_worker`` chạy trong process signal-worker (role DB
``crypidian_signal_worker``). Migration này cấp đúng phạm vi cần:

- ``outbox_event``: SELECT/UPDATE chỉ 14 loại sự kiện vận hành của workspace bind
  (pause/resume/kill/release/disable/enable/flatten/entry-hold/foreign-activity/cầu dao);
  UPDATE chỉ cột giao nhận (status/attempts/backoff/lease/lỗi), không sửa payload.
- ``consumer_receipt``: SELECT/INSERT chỉ receipt của chính consumer này (idempotency).
- ``telegram_binding``: thêm cột ``telegram_user_id`` (private chat id) và policy đọc
  mọi binding còn hiệu lực của workspace — cảnh báo vận hành gửi cho người đã bind,
  không chỉ người đăng ký tín hiệu.
- ``workspace``: SELECT (id, name, mode) để dựng nhãn workspace/mode trong tin.

Không cấp quyền ghi control/ledger; không mở rộng quyền admin-web. Downgrade thu hồi đủ.

Revision ID: a3c7e9f1b5d2
Revises: c9d1e3f5a7b4
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a3c7e9f1b5d2"
down_revision: str | Sequence[str] | None = "c9d1e3f5a7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER = "crypidian_signal_worker"
CONSUMER = "telegram_operational_alert_worker"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"

EVENT_TYPES = (
    "bot.paused",
    "bot.resumed",
    "bot.killed",
    "bot.kill_released",
    "bot.disabled",
    "bot.enabled",
    "workspace.flatten.confirmed",
    "workspace.flatten.failed",
    "account.foreign_activity.detected",
    "incident.foreign_activity.detected",
    "learning.incident.entry_hold.confirmed",
    "learning.incident.entry_hold.released",
    "ops.circuit_breaker.tripped",
    "ops.circuit_breaker.resumed",
)
_EVENT_LIST = ", ".join(f"'{event}'" for event in EVENT_TYPES)
_EVENT_SCOPE = f"{_WORKSPACE} AND event_type IN ({_EVENT_LIST})"

_OUTBOX_UPDATE_COLUMNS = (
    "status, attempts, available_at, lease_owner, lease_expires_at, completed_at, last_error_code"
)
_RECEIPT_SELECT_COLUMNS = "id, created_at, workspace_id, outbox_event_id, consumer_name"


def upgrade() -> None:
    # Outbox: chỉ sự kiện vận hành của workspace bind; UPDATE chỉ cột giao nhận.
    op.execute(
        f"CREATE POLICY operational_alert_outbox_select ON public.outbox_event FOR SELECT "
        f"TO {WORKER} USING ({_EVENT_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY operational_alert_outbox_update ON public.outbox_event FOR UPDATE "
        f"TO {WORKER} USING ({_EVENT_SCOPE}) WITH CHECK ({_EVENT_SCOPE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.outbox_event TO {WORKER}")
    op.execute(f"GRANT UPDATE ({_OUTBOX_UPDATE_COLUMNS}) ON TABLE public.outbox_event TO {WORKER}")

    # Receipt idempotency: chỉ consumer của cảnh báo vận hành.
    op.execute(
        f"CREATE POLICY operational_alert_receipt_select ON public.consumer_receipt FOR SELECT "
        f"TO {WORKER} USING ({_WORKSPACE} AND consumer_name = '{CONSUMER}')"
    )
    op.execute(
        f"CREATE POLICY operational_alert_receipt_insert ON public.consumer_receipt FOR INSERT "
        f"TO {WORKER} WITH CHECK ({_WORKSPACE} AND consumer_name = '{CONSUMER}')"
    )
    op.execute(
        f"GRANT SELECT ({_RECEIPT_SELECT_COLUMNS}) ON TABLE public.consumer_receipt TO {WORKER}"
    )
    op.execute(f"GRANT INSERT ON TABLE public.consumer_receipt TO {WORKER}")

    # Binding: đọc private chat id của mọi binding còn hiệu lực trong workspace
    # (policy cũ chỉ cho binding có subscription tín hiệu bật — không đủ cho cảnh báo).
    op.execute(
        f"CREATE POLICY operational_alert_binding_select ON public.telegram_binding "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE} AND verified_at IS NOT NULL "
        "AND revoked_at IS NULL)"
    )
    op.execute(f"GRANT SELECT (telegram_user_id) ON TABLE public.telegram_binding TO {WORKER}")

    # Workspace: nhãn tên + mode cho tin cảnh báo (bảng không RLS, giới hạn bằng cột).
    op.execute(f"GRANT SELECT (id, name, mode) ON TABLE public.workspace TO {WORKER}")

    # Audit giao nhận (delivered/dead_lettered): INSERT + EXECUTE bộ hàm chuỗi băm SEC-32/33
    # (next_chain_link là SECURITY DEFINER của guard role; RLS workspace vẫn bắt buộc).
    op.execute(f"GRANT INSERT ON TABLE public.audit_event TO {WORKER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.audit_event_next_chain_link(uuid) TO {WORKER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.audit_event_assign_hash_chain() TO {WORKER}")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.audit_event_compute_hash("
        "uuid, uuid, uuid, text, text, uuid, text, text, text, uuid, "
        "timestamp with time zone, bigint, text, text, timestamp with time zone, "
        f"text, text, text, text) TO {WORKER}"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.audit_event_compute_hash("
        "uuid, uuid, uuid, text, text, uuid, text, text, text, uuid, "
        "timestamp with time zone, bigint, text, text, timestamp with time zone, "
        f"text, text, text, text) FROM {WORKER}"
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.audit_event_assign_hash_chain() FROM {WORKER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.audit_event_next_chain_link(uuid) FROM {WORKER}")
    op.execute(f"REVOKE INSERT ON TABLE public.audit_event FROM {WORKER}")
    op.execute(f"REVOKE SELECT (id, name, mode) ON TABLE public.workspace FROM {WORKER}")
    op.execute(f"REVOKE SELECT (telegram_user_id) ON TABLE public.telegram_binding FROM {WORKER}")
    op.execute("DROP POLICY operational_alert_binding_select ON public.telegram_binding")
    op.execute(f"REVOKE INSERT ON TABLE public.consumer_receipt FROM {WORKER}")
    op.execute(
        f"REVOKE SELECT ({_RECEIPT_SELECT_COLUMNS}) ON TABLE public.consumer_receipt FROM {WORKER}"
    )
    op.execute("DROP POLICY operational_alert_receipt_insert ON public.consumer_receipt")
    op.execute("DROP POLICY operational_alert_receipt_select ON public.consumer_receipt")
    op.execute(
        f"REVOKE UPDATE ({_OUTBOX_UPDATE_COLUMNS}) ON TABLE public.outbox_event FROM {WORKER}"
    )
    op.execute(f"REVOKE SELECT ON TABLE public.outbox_event FROM {WORKER}")
    op.execute("DROP POLICY operational_alert_outbox_update ON public.outbox_event")
    op.execute("DROP POLICY operational_alert_outbox_select ON public.outbox_event")
