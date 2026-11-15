"""TG-2: thêm trạng thái ``SUPPRESSED`` cho delivery tín hiệu Telegram (chống spam).

Delivery bị chặn vì trùng trong cooldown hoặc vượt trần tin/giờ được ghi ``SUPPRESSED`` cùng mã
lý do ở ``last_error_code`` để truy vết, thay vì xóa hoặc gửi. Worker đã có quyền UPDATE cột
``status``/``last_error_code`` nên không cần grant mới. Thêm index đọc lịch sử theo subscription.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3e5a7b9d1f4"
down_revision: str | None = "b2d4f6a8c0e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "telegram_signal_delivery"
INDEX = "ix_telegram_signal_delivery_subscription_sent"
_BASE = "'PENDING', 'PROCESSING', 'SENT', 'EXPIRED', 'DEAD', 'FILTERED'"


def _replace_status_check(statuses: str) -> None:
    """Thay CHECK trạng thái, giữ nguyên tên thật (naming convention cắt tên + hash)."""
    op.execute(
        f"""
        DO $$
        DECLARE target text;
        BEGIN
            SELECT conname INTO STRICT target FROM pg_constraint
            WHERE conrelid = 'public.{TABLE}'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%status%'
              AND pg_get_constraintdef(oid) LIKE '%FILTERED%';
            EXECUTE format('ALTER TABLE public.{TABLE} DROP CONSTRAINT %I', target);
            EXECUTE format(
                'ALTER TABLE public.{TABLE} ADD CONSTRAINT %I CHECK (status IN ({statuses}))',
                target
            );
        END $$;
        """
    )


def upgrade() -> None:
    _replace_status_check(f"{_BASE}, 'SUPPRESSED'".replace("'", "''"))
    op.execute(f"CREATE INDEX {INDEX} ON public.{TABLE} (workspace_id, subscription_id, sent_at)")


def downgrade() -> None:
    op.execute(f"DROP INDEX public.{INDEX}")
    # Không ép SUPPRESSED về trạng thái cũ mà mất nghĩa: đổi thành FILTERED, giữ mã lý do.
    op.execute(f"UPDATE public.{TABLE} SET status = 'FILTERED' WHERE status = 'SUPPRESSED'")
    _replace_status_check(_BASE.replace("'", "''"))
