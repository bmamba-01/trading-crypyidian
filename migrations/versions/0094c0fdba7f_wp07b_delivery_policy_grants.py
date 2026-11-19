"""WP-07B: cấp quyền đọc/cập nhật cột delivery_policy cho worker và admin-web."""

from collections.abc import Sequence

from alembic import op

revision: str = "0094c0fdba7f"
down_revision: str | None = "c8d28da699c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMIN = "crypidian_admin_web"
_WORKER = "crypidian_signal_worker"


def upgrade() -> None:
    """Cấp SELECT (delivery_policy) cho signal-worker và UPDATE (delivery_policy) cho admin-web."""
    op.execute(
        f"GRANT SELECT (delivery_policy) ON TABLE public.telegram_signal_subscription TO {_WORKER}"
    )
    op.execute(
        f"GRANT UPDATE (delivery_policy) ON TABLE public.telegram_signal_subscription TO {_ADMIN}"
    )


def downgrade() -> None:
    """Thu hồi quyền cột delivery_policy."""
    op.execute(
        "REVOKE UPDATE (delivery_policy) ON TABLE public.telegram_signal_subscription "
        f"FROM {_ADMIN}"
    )
    op.execute(
        "REVOKE SELECT (delivery_policy) ON TABLE public.telegram_signal_subscription "
        f"FROM {_WORKER}"
    )
