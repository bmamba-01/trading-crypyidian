"""TG-4: hai topic Telegram (tín hiệu / PAPER report) cho mỗi binding (17/11/2026).

Owner chọn tách tin tín hiệu và tin PAPER report (kết quả mở/đóng lệnh, tổng hợp định kỳ, thẻ AI)
vào hai topic trong chat riêng của bot. Bot API 9.3+ hỗ trợ topic trong chat riêng và tham số
``message_thread_id`` cho ``sendMessage``/``sendPhoto``; ``createForumTopic`` trả id topic
(https://core.telegram.org/bots/api, kiểm 17/11/2026).

- ``telegram_binding.signal_thread_id`` / ``report_thread_id``: BIGINT NULL, CHECK > 0. NULL = gửi
  vào chat chính như trước (không đổi hành vi khi chưa cấu hình).
- admin-web: UPDATE đúng hai cột này (policy ``telegram_binding_admin_revoke`` theo workspace đã
  có sẵn cho FOR UPDATE); SELECT mức bảng vốn đã có.
- signal-worker: thêm SELECT theo cột cho hai cột này, giữ nguyên policy đọc binding đang hoạt động.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "60a62c705a89"
down_revision: str | None = "ad99c4f520d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "telegram_binding"
ADMIN = "crypidian_admin_web"
WORKER = "crypidian_signal_worker"
COLUMNS = ("signal_thread_id", "report_thread_id")


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column(TABLE, sa.Column(column, sa.BigInteger(), nullable=True))
        op.create_check_constraint(
            f"telegram_binding_{column.removesuffix('_id')}_positive",
            TABLE,
            f"{column} IS NULL OR {column} > 0",
        )
    op.execute(
        f"GRANT UPDATE (signal_thread_id, report_thread_id) ON TABLE public.{TABLE} TO {ADMIN}"
    )
    op.execute(
        f"GRANT SELECT (signal_thread_id, report_thread_id) ON TABLE public.{TABLE} TO {WORKER}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE SELECT (signal_thread_id, report_thread_id) ON TABLE public.{TABLE} FROM {WORKER}"
    )
    op.execute(
        f"REVOKE UPDATE (signal_thread_id, report_thread_id) ON TABLE public.{TABLE} FROM {ADMIN}"
    )
    for column in reversed(COLUMNS):
        op.drop_constraint(
            f"telegram_binding_{column.removesuffix('_id')}_positive", TABLE, type_="check"
        )
        op.drop_column(TABLE, column)
