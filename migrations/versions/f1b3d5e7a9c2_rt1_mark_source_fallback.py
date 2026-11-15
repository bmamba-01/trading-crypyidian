"""RT-1 lát 2: mark nhận nến REST backfill khi không có nến live cùng thời điểm.

Revision ID: f1b3d5e7a9c2
Revises: e3a5c7e9f1b4

Collector SIG-0 chỉ ghi nến đóng nguồn ``BINANCE_USDM_REST_BACKFILL``; hàm
``workspace_account_risk_state`` chỉ nhận ``mb.source = p_mark_source`` (``BINANCE_USDM``) nên
vốn/exposure của vị thế PAPER đang mở luôn ``unknown``. Bản này giữ nguyên chữ ký, owner và grant
(``CREATE OR REPLACE``) và chỉ đổi LATERAL chọn mark:

- nguồn hợp lệ là ``p_mark_source`` cộng nguồn dự phòng của venue theo
  ``MARK_SOURCE_FALLBACKS`` trong ``domain/risk/workspace_budget.py`` (hiện chỉ
  ``BINANCE_USDM`` → ``BINANCE_USDM_REST_BACKFILL``);
- cùng ``close_time`` thì nến live đứng trước (``mb.source = p_mark_source`` DESC), sau đó mới tới
  ``available_at``/``revision`` như cũ.

Freshness, hình dạng nến và timeframe giữ nguyên. Downgrade dựng lại nguyên văn hàm của
``b3d5f7a9c1e4`` bằng ``CREATE OR REPLACE`` (owner/grant không đổi).
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "f1b3d5e7a9c2"
down_revision: str | None = "e3a5c7e9f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bảng dự phòng được mã hóa trong SQL; phải khớp `MARK_SOURCE_FALLBACKS` (unit test đối chiếu).
FALLBACK_SOURCES = (("BINANCE_USDM", "BINANCE_USDM_REST_BACKFILL"),)

_OLD_FILTER = "AND mb.source = p_mark_source AND mb.timeframe = ANY (p_mark_timeframes)"
_NEW_FILTER = (
    "AND (mb.source = p_mark_source OR "
    + " OR ".join(
        f"(p_mark_source = '{primary}' AND mb.source = '{fallback}')"
        for primary, fallback in FALLBACK_SOURCES
    )
    + ") AND mb.timeframe = ANY (p_mark_timeframes)"
)
_OLD_ORDER = "ORDER BY mb.close_time DESC, mb.available_at DESC, mb.revision DESC"
_NEW_ORDER = (
    "ORDER BY mb.close_time DESC, (mb.source = p_mark_source) DESC, "
    "mb.available_at DESC, mb.revision DESC"
)


def _previous_revision() -> ModuleType:
    """Nạp nguyên văn SQL của `b3d5f7a9c1e4` (bản hiện hành trước migration này)."""
    path = next(Path(__file__).parent.glob("b3d5f7a9c1e4_*.py"))
    spec = importlib.util.spec_from_file_location(f"_rt1_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("RT1_PREVIOUS_MIGRATION_NOT_FOUND")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replaceable(function_sql: str) -> str:
    if "CREATE FUNCTION public.workspace_account_risk_state(" not in function_sql:
        raise RuntimeError("RT1_STATE_FUNCTION_SHAPE_CHANGED")
    return function_sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def account_state_function_sql() -> str:
    """SQL hàm mới; tách hàm để unit test kiểm tra mà không cần PostgreSQL."""
    previous = _previous_revision().ACCOUNT_STATE_FUNCTION
    if previous.count(_OLD_FILTER) != 1 or previous.count(_OLD_ORDER) != 1:
        raise RuntimeError("RT1_STATE_FUNCTION_SHAPE_CHANGED")
    return _replaceable(previous.replace(_OLD_FILTER, _NEW_FILTER).replace(_OLD_ORDER, _NEW_ORDER))


def upgrade() -> None:
    op.execute(account_state_function_sql())


def downgrade() -> None:
    op.execute(_replaceable(_previous_revision().ACCOUNT_STATE_FUNCTION))
