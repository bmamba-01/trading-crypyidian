"""Gộp nhánh WP06F với migration role đọc báo cáo CRY-421."""

from collections.abc import Sequence

revision: str = "f6c8e0a2b4db"
down_revision: str | Sequence[str] | None = ("f6c8e0a2b4da", "12e554aace5d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nối hai nhánh migration không có thao tác schema bổ sung."""
    pass


def downgrade() -> None:
    """Alembic quay lại hai parent sau khi gỡ revision gộp."""
    pass
