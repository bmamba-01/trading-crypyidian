"""Trạng thái bảo vệ vốn hiển thị trên bảng giám sát bot (CK-12) — chỉ mô tả, không chặn lệnh.

- Chỗ chạy = số lệnh PAPER đang mở (ledger) / ``max_positions`` hiệu lực; đủ chỗ → "full".
- Lỗ ròng = phần âm của tổng PnL ròng các lệnh đã đóng (hôm nay theo UTC, 24 giờ trượt); ngày
  đang lãi tính lỗ 0. So với ``max_daily_loss`` / ``max_rolling_loss``: ≥ 80% là "warning",
  ≥ 100% là "breached". Thiếu giới hạn (bot chưa publish cấu hình) → "unknown", không đoán.
Đây là ảnh hiển thị; quyết định chặn vẫn do risk engine/planner.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

PROTECTION_VERSION = "ck12-protection-v1"
WARNING_FRACTION = Decimal("0.8")
_ORDER = {"ok": 0, "unknown": 1, "warning": 2, "full": 2, "breached": 3}


def _loss(net: Decimal, limit: Decimal | None) -> dict[str, Any]:
    loss = -net if net < 0 else Decimal(0)
    if limit is None or limit <= 0:
        return {"loss": float(loss), "limit": None, "used_pct": None, "state": "unknown"}
    used = loss / limit
    state = "breached" if used >= 1 else "warning" if used >= WARNING_FRACTION else "ok"
    return {
        "loss": float(loss),
        "limit": float(limit),
        "used_pct": float(used * 100),
        "state": state,
    }


def protection_view(
    *,
    open_positions: int,
    max_positions: int | None,
    net_today: Decimal,
    net_24h: Decimal,
    max_daily_loss: Decimal | None,
    max_rolling_loss: Decimal | None,
    quote_asset: str,
) -> dict[str, Any]:
    if max_positions is None or max_positions <= 0:
        slots = {
            "used": open_positions,
            "max": None,
            "label": f"{open_positions}/—",
            "state": "unknown",
        }
    else:
        slots = {
            "used": open_positions,
            "max": max_positions,
            "label": f"{open_positions}/{max_positions}",
            "state": "full" if open_positions >= max_positions else "ok",
        }
    daily = _loss(net_today, max_daily_loss)
    rolling = _loss(net_24h, max_rolling_loss)
    overall = max((slots["state"], daily["state"], rolling["state"]), key=lambda item: _ORDER[item])
    return {
        "formula_version": PROTECTION_VERSION,
        "quote_asset": quote_asset,
        "slots": slots,
        "daily": daily,
        "rolling": rolling,
        "overall": overall,
    }
