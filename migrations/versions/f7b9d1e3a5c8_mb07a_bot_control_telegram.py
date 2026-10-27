"""MB-07a: grant cột disable bot cho admin-web và bảng dedup `update_id` Telegram.

- Ruling 29/34 (SEC R10): đường disable bot của admin-web cập nhật `trading_account.status`
  (trigger MB-01 `provision_account_bot` mirror sang `bot_instance`). Chỉ cấp đúng cột `status`;
  `updated_at` đã có từ SEC-01. Guard phẳng (`BotLifecycleGuard`) chạy cùng transaction.
  Fix round 1 (minor 1): trigger `trading_account_status_guard` (BEFORE UPDATE OF status,
  SECURITY INVOKER) chỉ cho `crypidian_admin_web` đổi ENABLED → DISABLED; enable lại hay trạng
  thái khác bị từ chối ở DB kể cả khi bỏ qua application. Không đếm lại vị thế trong trigger: dưới
  RLS của invoker, role không thấy bot sẽ đếm ra 0 (an toàn giả); guard phẳng vẫn ở application.
- Ruling 38 (SEC-36): `telegram_update_receipt` ghi bền vững `update_id` đã nhận theo workspace để
  bỏ qua update lặp (at-most-once). Không UPDATE; RLS ENABLE + FORCE theo `app.workspace_id`, các
  policy chỉ cho admin-web: đọc (`workspace_isolation`), ghi (`receipt_insert`) và xóa hàng quá 7
  ngày (`receipt_purge`, retention — fix round 1 minor 3; Telegram chọn `update_id` ngẫu nhiên sau
  ≥1 tuần không có update).

Revision ID: f7b9d1e3a5c8
Revises: e1f3a5c7b9d2
"""

import sqlalchemy as sa
from alembic import op

revision = "f7b9d1e3a5c8"
down_revision = "e1f3a5c7b9d2"
branch_labels = None
depends_on = None

ADMIN = "crypidian_admin_web"
TABLE = "telegram_update_receipt"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_GUARD_FUNCTION = """CREATE FUNCTION public.mb07a_guard_account_status() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF current_user = 'crypidian_admin_web' AND NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'ENABLED' AND NEW.status = 'DISABLED') THEN
        RAISE EXCEPTION 'MB07A_ACCOUNT_STATUS_TRANSITION_DENIED'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$"""


def upgrade() -> None:
    op.execute(f'GRANT UPDATE (status) ON TABLE "trading_account" TO {ADMIN}')
    op.execute(_GUARD_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION public.mb07a_guard_account_status() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER trading_account_status_guard BEFORE UPDATE OF status ON trading_account "
        "FOR EACH ROW EXECUTE FUNCTION public.mb07a_guard_account_status()"
    )
    op.create_table(
        TABLE,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "update_id > 0", name=op.f("ck_telegram_update_receipt_update_id_positive")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_telegram_update_receipt_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_update_receipt")),
        sa.UniqueConstraint("workspace_id", "update_id", name="uq_telegram_update_receipt_update"),
    )
    op.create_index(
        "ix_telegram_update_receipt_created", TABLE, ["workspace_id", "created_at"], unique=False
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON {TABLE} FOR SELECT TO {ADMIN} USING ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY receipt_insert ON {TABLE} FOR INSERT TO {ADMIN} WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY receipt_purge ON {TABLE} FOR DELETE TO {ADMIN} "
        f"USING (({_WORKSPACE}) AND created_at < now() - interval '7 days')"
    )
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON TABLE {TABLE} TO {ADMIN}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM {ADMIN}")
    op.drop_index("ix_telegram_update_receipt_created", table_name=TABLE)
    op.drop_table(TABLE)
    op.execute("DROP TRIGGER trading_account_status_guard ON trading_account")
    op.execute("DROP FUNCTION public.mb07a_guard_account_status()")
    op.execute(f'REVOKE UPDATE (status) ON TABLE "trading_account" FROM {ADMIN}')
