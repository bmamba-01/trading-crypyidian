"""Gộp hai nhánh migration khi hợp nhất `main` vào `dev` (16/11/2026, CRY-88).

- Nhánh `main`: … → `86afb7c1ba2d` proposal_backtest → `62d0ea00fae5` macro_calendar_fetch →
  `00912d59476e` derivatives_snapshot → `645ed2998ab1` listing_snapshot.
- Nhánh `dev`: … → `a7c9e1b3d5f6` market_live_quote → `b8d0f2a4c6e8` economic_event.

Hai nhánh tạo bảng khác tên và không sửa cùng đối tượng, nên revision gộp không có thao tác.
"""

from collections.abc import Sequence

revision: str = "c1fec3af85ea"
down_revision: str | tuple[str, ...] | None = ("645ed2998ab1", "b8d0f2a4c6e8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
