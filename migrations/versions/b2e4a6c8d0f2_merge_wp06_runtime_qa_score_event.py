"""Gộp các nhánh migration WP06 C4, QA và score-event."""

from collections.abc import Sequence

revision: str = "b2e4a6c8d0f2"
down_revision: tuple[str, str, str] = (
    "5a6c8e0f2b4d",
    "8ac4e2f6b0d1",
    "f6c8a0b2d4e7",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Gộp ba nhánh schema độc lập mà không thay đổi dữ liệu."""


def downgrade() -> None:
    """Phân nhánh lại về các revision trước khi gộp."""
