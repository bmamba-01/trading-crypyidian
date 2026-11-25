"""Lớp vị thế PAPER trên chart (CK-7): giá vào, SL/TP (gồm thang TP1–3), UPnL theo mark price.

Số liệu lấy nguyên văn từ ledger (fill ENTRY/EXIT, kế hoạch đã qua risk gate, sự kiện dời stop);
module chỉ tính phần chưa thực hiện:

- giá vào = trung bình theo khối lượng các fill ENTRY;
- khối lượng mở = tổng ENTRY trừ tổng EXIT (chốt một phần TP1/TP2);
- UPnL = (mark − giá vào) × khối lượng mở (LONG), đảo dấu với SHORT; **chưa trừ phí thoát và
  funding**, kèm thêm bản đã trừ phí vào lệnh đã ghi;
- R = UPnL / rủi ro ban đầu trên phần khối lượng đang mở (|giá vào − SL ban đầu| × khối lượng mở);
  không có SL thì không có R;
- thang chốt lời ``targets`` (TP1–3): lấy từ kế hoạch đã duyệt, gắn trạng thái ``hit`` (đã chạm)
  theo fill EXIT khớp ``exit_reason`` (``TAKE_PROFIT_{index}``), hậu tố ``:TP{index}`` trên
  ``fill_key``, hoặc giá fill EXIT đạt mức mục tiêu.
Giống Binance, UPnL dùng mark price; thiếu mark thì tầng gọi có thể truyền giá đóng nến kèm
``mark_source`` để giao diện ghi rõ nguồn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

POSITION_OVERLAY_VERSION = "ck7-position-v1"


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _iso_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _match_target_hit(
    *,
    index: int,
    price: Decimal,
    side: str,
    exit_fills: Sequence[Mapping[str, Any]],
) -> tuple[bool, str | None]:
    """Xác định mục tiêu TP{index} đã chạm dựa trên fill EXIT thực tế của vị thế."""
    expected_reason = f"TAKE_PROFIT_{index}"
    expected_suffix = f":TP{index}"
    for fill in exit_fills:
        reason = str(fill.get("exit_reason") or "").upper()
        fill_key = str(fill.get("fill_key") or "")
        if reason == expected_reason or fill_key.endswith(expected_suffix):
            return True, _iso_time(fill.get("occurred_at"))
    # Dự phòng cho fill EXIT không gắn nhãn TP: kiểm tra giá chốt đã đạt mục tiêu theo chiều lệnh.
    for fill in exit_fills:
        if fill.get("exit_reason") or ":TP" in str(fill.get("fill_key") or ""):
            continue
        raw_price = fill.get("price")
        if raw_price is None:
            continue
        fill_price = Decimal(str(raw_price))
        reached = fill_price >= price if side == "LONG" else fill_price <= price
        if reached:
            return True, _iso_time(fill.get("occurred_at"))
    return False, None


def _build_target_views(
    raw_targets: Sequence[Mapping[str, Any]],
    *,
    side: str,
    exit_fills: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Chuẩn hóa danh sách mục tiêu TP1–3 kèm tỷ lệ chốt, dời hòa vốn và trạng thái đã chạm."""
    output: list[dict[str, Any]] = []
    for default_idx, item in enumerate(raw_targets, start=1):
        if not isinstance(item, Mapping) or item.get("price") is None:
            continue
        index = int(item.get("index") or default_idx)
        price = Decimal(str(item["price"]))
        raw_fraction = item.get("close_fraction", item.get("fraction"))
        close_fraction = None if raw_fraction is None else float(Decimal(str(raw_fraction)))
        raw_r = item.get("r_multiple")
        r_multiple = None if raw_r is None else float(Decimal(str(raw_r)))
        if item.get("hit") is True:
            hit = True
            hit_at = _iso_time(item.get("hit_at"))
        else:
            hit, hit_at = _match_target_hit(
                index=index, price=price, side=side, exit_fills=exit_fills
            )
        output.append(
            {
                "index": index,
                "label": str(item.get("label") or f"TP{index}"),
                "price": float(price),
                "close_fraction": close_fraction,
                "r_multiple": r_multiple,
                "stop_to_entry_after": bool(item.get("stop_to_entry_after", False)),
                "hit": hit,
                "hit_at": hit_at,
            }
        )
    output.sort(key=lambda target: target["index"])
    return output


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
    entry_quantity = sum((Decimal(str(fill["quantity"])) for fill in fills), Decimal(0))
    if entry_quantity <= 0:
        raise ValueError("Khối lượng fill phải dương")
    exit_fills = list(position.get("exit_fills") or [])
    closed_quantity = sum((Decimal(str(fill["quantity"])) for fill in exit_fills), Decimal(0))
    quantity = entry_quantity - closed_quantity
    if quantity <= 0:
        raise ValueError("Khối lượng mở còn lại phải dương")
    entry = (
        sum(
            (Decimal(str(f["price"])) * Decimal(str(f["quantity"])) for f in fills),
            Decimal(0),
        )
        / entry_quantity
    )
    fees = sum((Decimal(str(fill.get("fee") or 0)) for fill in fills), Decimal(0))
    side = str(position["side"]).upper()
    sign = Decimal(1) if side == "LONG" else Decimal(-1)
    initial_stop = position.get("initial_stop")
    current_stop = position.get("current_stop") or initial_stop
    upnl = None if mark_price is None else (Decimal(mark_price) - entry) * quantity * sign
    risk = None if initial_stop is None else abs(entry - Decimal(initial_stop)) * quantity
    first = min(fills, key=lambda fill: fill["occurred_at"])
    raw_targets = position.get("targets")
    targets = (
        _build_target_views(raw_targets, side=side, exit_fills=exit_fills)
        if isinstance(raw_targets, Sequence) and not isinstance(raw_targets, str)
        else []
    )
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
        "targets": targets,
        "filled_at": first["occurred_at"].isoformat(),
        "fill_price": float(Decimal(str(first["price"]))),
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
