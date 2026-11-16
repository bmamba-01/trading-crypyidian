"""Bù ba bảng live market của ``b4d6f8a0c2e5`` cho DB đã nâng cấp theo nhánh ``main`` (CRY-88).

``b4d6f8a0c2e5`` (``market_mark_price``, ``market_liquidation``, ``market_trade_flow``) chỉ có
trên ``dev`` và được chèn giữa chuỗi: ``d2e4f6a8b0c1.down_revision`` đổi từ ``c7e9a1b3d5f8`` sang
nó. ``main`` vẫn trỏ ``d2e4`` về ``c7e9``. Sau khi hợp nhất (revision gộp ``c1fec3af85ea``),
Alembic coi ``b4d6`` là tổ tiên của head cũ ``645ed2998ab1`` của ``main``; DB đã ở đó sẽ được coi
là có ba bảng dù chưa từng tạo. Migration này kiểm trực tiếp catalog:

- đủ ba bảng (DB đi theo ``dev``, DB mới, hoặc đã vá tay như stack ``crypidian-review``) → không
  làm gì;
- thiếu cả ba → chạy đúng ``upgrade()`` của ``b4d6`` (bảng, index, RLS, trigger, quyền);
- thiếu một phần → dừng để đối soát tay, không tự đoán.

``downgrade`` không gỡ bảng: không biết DB đã có bảng từ ``b4d6`` hay từ bước bù này.
"""

from collections.abc import Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "7707d82de246"
down_revision: str | None = "c1fec3af85ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE_MARKET_MIGRATION = "b4d6f8a0c2e5_live_market_event_tables.py"


def _live_market_module() -> ModuleType:
    path = Path(__file__).with_name(_LIVE_MARKET_MIGRATION)
    spec = spec_from_file_location("crypidian_migration_b4d6f8a0c2e5", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Không nạp được migration {_LIVE_MARKET_MIGRATION}.")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    live_market = _live_market_module()
    inspector = sa.inspect(op.get_bind())
    tables = tuple(live_market._TABLES)
    present = [table for table in tables if inspector.has_table(table)]
    if len(present) == len(tables):
        return
    if present:
        raise RuntimeError(
            "Chỉ có một phần bảng live market của b4d6f8a0c2e5; cần đối soát thủ công: "
            + ", ".join(present)
        )
    live_market.upgrade()


def downgrade() -> None:
    """Không gỡ bảng (xem docstring module)."""
