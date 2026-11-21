"""Gộp WP06F/CRY-421 với chuỗi bảo mật đợt 2 trên dev."""

from collections.abc import Sequence

revision: str = "e8a2c4d6f0b3"
down_revision: str | Sequence[str] | None = ("f6c8e0a2b4db", "c9d1e3f5a7b4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nối hai nhánh đã được áp dụng, không thêm thao tác schema."""
    pass


def downgrade() -> None:
    """Alembic quay lại hai parent sau khi gỡ revision gộp."""
    pass
