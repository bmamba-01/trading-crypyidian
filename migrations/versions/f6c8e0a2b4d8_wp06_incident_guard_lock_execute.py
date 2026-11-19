"""Giữ quyền gọi khóa phạm vi incident cho trigger sau khi chuyển owner."""

from collections.abc import Sequence

from alembic import op

revision: str = "f6c8e0a2b4d8"
down_revision: str | None = "0094c0fdba7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTIONS = (
    "public.wp06_lock_learning_incident_scope(uuid,uuid)",
    "public.wp06_learning_incident_eligible_target(uuid,uuid)",
)
GUARD = "crypidian_learning_incident_guard"


def upgrade() -> None:
    """Cấp lại EXECUTE sau khi migration gộp đổi owner hàm sang role khác."""
    for function in FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {GUARD}")


def downgrade() -> None:
    """Thu hồi quyền riêng mà migration này đã thêm."""
    for function in FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM {GUARD}")
