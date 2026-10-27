"""MB-07a: grant cột disable bot cho admin-web và bảng dedup `update_id` Telegram.

- Ruling 29/34 (SEC R10): đường disable bot của admin-web cập nhật `trading_account.status`
  (trigger MB-01 `provision_account_bot` mirror sang `bot_instance`). Chỉ cấp đúng cột `status`;
  `updated_at` đã có từ SEC-01. Guard phẳng (`BotLifecycleGuard`) chạy cùng transaction.
- Ruling 38 (SEC-36): `telegram_update_receipt` ghi bền vững `update_id` đã nhận theo workspace để
  bỏ qua update lặp (at-most-once). Bảng append-only: admin-web chỉ `SELECT, INSERT`, không
  UPDATE/DELETE; RLS ENABLE + FORCE theo `app.workspace_id`, policy chỉ cho admin-web. Dọn dữ
  liệu cũ (retention) để việc sau.

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


def upgrade() -> None:
    op.execute(f'GRANT UPDATE (status) ON TABLE "trading_account" TO {ADMIN}')
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
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON {TABLE} TO {ADMIN} "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {TABLE} TO {ADMIN}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM {ADMIN}")
    op.drop_table(TABLE)
    op.execute(f'REVOKE UPDATE (status) ON TABLE "trading_account" FROM {ADMIN}')
