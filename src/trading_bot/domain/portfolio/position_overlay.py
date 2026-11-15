"""Lớp vị thế PAPER trên chart (CK-7): giá vào, SL/TP, UPnL theo mark price — chỉ hiển thị.

Số liệu lấy nguyên văn từ ledger (fill ENTRY, kế hoạch đã qua risk gate, sự kiện dời stop);
module chỉ tính phần chưa thực hiện:

- giá vào = trung bình theo khối lượng các fill ENTRY;
- UPnL = (mark − giá vào) × khối lượng (LONG), đảo dấu với SHORT; **chưa trừ phí thoát và
  funding**, kèm thêm bản đã trừ phí vào lệnh đã ghi;
- R = UPnL / rủi ro ban đầu (|giá vào − SL ban đầu| × khối lượng); không có SL thì không có R.
Giống Binance, UPnL dùng mark price; thiếu mark thì tầng gọi có thể truyền giá đóng nến kèm
``mark_source`` để giao diện ghi rõ nguồn.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

POSITION_OVERLAY_VERSION = "ck7-position-v1"


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def position_overlay(
    position: Mapping[str, Any],
    *,
    mark_price: Decimal | None,
    mark_source: str | None,
) -> dict[str, Any]:
    """Dữ liệu vẽ một vị thế đang mở; ``entry_fills`` cần ít nhất một fill."""
    fills = list(position.get("entry_fills") or [])
    if not fills:
        raise ValueError("Vị thế đang mở cần ít nhất một fill ENTRY")
    quantity = sum((Decimal(fill["quantity"]) for fill in fills), Decimal(0))
    if quantity <= 0:
        raise ValueError("Khối lượng fill phải dương")
    entry = (
        sum((Decimal(f["price"]) * Decimal(f["quantity"]) for f in fills), Decimal(0)) / quantity
    )
    fees = sum((Decimal(fill.get("fee") or 0) for fill in fills), Decimal(0))
    side = str(position["side"]).upper()
    sign = Decimal(1) if side == "LONG" else Decimal(-1)
    initial_stop = position.get("initial_stop")
    current_stop = position.get("current_stop") or initial_stop
    upnl = None if mark_price is None else (Decimal(mark_price) - entry) * quantity * sign
    risk = None if initial_stop is None else abs(entry - Decimal(initial_stop)) * quantity
    first = min(fills, key=lambda fill: fill["occurred_at"])
    return {
        "trade_id": str(position["trade_id"]),
        "symbol": position["symbol"],
        "side": side,
        "quote_asset": position.get("quote_asset"),
        "entry_price": float(entry),
        "quantity": float(quantity),
        "stop": _float(None if current_stop is None else Decimal(current_stop)),
        "initial_stop": _float(None if initial_stop is None else Decimal(initial_stop)),
        "target": _float(None if position.get("target") is None else Decimal(position["target"])),
        "filled_at": first["occurred_at"].isoformat(),
        "fill_price": float(Decimal(first["price"])),
        "entry_fees": float(fees),
        "mark_price": _float(None if mark_price is None else Decimal(mark_price)),
        "mark_source": mark_source,
        "upnl": _float(upnl),
        "upnl_after_entry_fees": _float(None if upnl is None else upnl - fees),
        "upnl_pct": (
            None
            if mark_price is None
            else float((Decimal(mark_price) - entry) / entry * 100 * sign)
        ),
        "r_multiple": (None if upnl is None or risk is None or risk == 0 else float(upnl / risk)),
        "formula_version": POSITION_OVERLAY_VERSION,
    }
