"""Dòng thời gian CK-10 (tín hiệu, khớp lệnh PAPER, lịch vĩ mô) và trạng thái H06 (CRY-228).

Module thuần (không I/O):
- Gộp sự kiện tín hiệu (``CANDIDATE`` đã xác minh hash), khớp lệnh PAPER (``ENTRY``/``EXIT``)
  và lịch vĩ mô (nguồn chính thức AI-4 hoặc lịch nội bộ owner nhập) theo thứ tự thời gian UTC.
- Minh bạch nguồn lịch cho H06: ``macro_blackout`` chỉ kích hoạt từ lịch chính thức AI-4
  (FOMC, BEA, OMB PFEI), không dùng lịch nội bộ tự nhập chưa thẩm định.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from trading_bot.domain.research.macro_calendar import (
    MACRO_POLICY_VERSION,
    MacroCalendarFetch,
    macro_digest,
)

TIMELINE_VERSION = "ck10-timeline-v1"
AI4_OFFICIAL_SOURCE_LABEL = "AI-4 official calendar"
AI4_OFFICIAL_SOURCE_VI = "Lịch vĩ mô chính thức AI-4 (FOMC, BEA, OMB PFEI)"
INTERNAL_CALENDAR_SOURCE_VI = "Lịch nội bộ"

_EXIT_REASON_VI = {
    "STOP_LOSS": "Cắt lỗ",
    "TAKE_PROFIT": "Chốt lời",
    "TIME_EXIT": "Hết thời gian",
    "MANUAL": "Thủ công",
}


def _parse_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _fmt_price(value: object) -> str | None:
    if value is None or value == "":
        return None
    try:
        num = float(str(value))
    except ValueError:
        return None
    if abs(num) >= 1:
        return f"{num:,.2f}".replace(",", ".")
    return f"{num:.6f}".rstrip("0").rstrip(".")


def _normalize_side(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if raw in ("LONG", "BUY"):
        return "LONG"
    if raw in ("SHORT", "SELL"):
        return "SHORT"
    return None


def _side_vi(side: str | None) -> str:
    if side == "LONG":
        return "MUA"
    if side == "SHORT":
        return "BÁN"
    return ""


def _exit_reason_vi(reason: object) -> str | None:
    if not isinstance(reason, str) or not reason.strip():
        return None
    clean = reason.strip()
    return _EXIT_REASON_VI.get(clean, clean)


def build_chart_timeline(
    *,
    symbol: str,
    signals: Sequence[Mapping[str, Any]] = (),
    trades: Sequence[Mapping[str, Any]] = (),
    open_positions: Sequence[Mapping[str, Any]] = (),
    official_macro_events: Sequence[Mapping[str, Any]] = (),
    internal_calendar_events: Sequence[Mapping[str, Any]] = (),
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Dựng dòng thời gian chuẩn UTC (mới nhất trước) cho cột phân tích và dock CK-10."""
    target_symbol = symbol.strip().upper()
    items: list[dict[str, Any]] = []

    # 1. Tín hiệu (chỉ lấy CANDIDATE khớp mã hoặc không gắn mã khác)
    for decision in signals:
        status = str(decision.get("status") or "").strip().upper()
        if status and status != "CANDIDATE":
            continue
        sig_symbol = str(decision.get("symbol") or target_symbol).strip().upper()
        if sig_symbol != target_symbol:
            continue
        dt = _parse_utc(decision.get("evaluated_at") or decision.get("signal_at"))
        if dt is None:
            continue
        side = _normalize_side(decision.get("direction") or decision.get("side"))
        release = str(decision.get("strategy_release_id") or decision.get("strategy_id") or "—")
        price_str = _fmt_price(decision.get("reference_price") or decision.get("entry_price"))
        side_txt = f" {_side_vi(side)}" if side else ""
        price_txt = f" · giá tham chiếu {price_str}" if price_str else ""
        reason = str(decision.get("reason_code") or "").strip()
        reason_txt = f" ({reason})" if reason else ""
        items.append(
            {
                "occurred_at": dt.isoformat(),
                "timestamp": int(dt.timestamp()),
                "kind": "signal",
                "kind_label_vi": "Tín hiệu",
                "source_badge": release,
                "symbol": sig_symbol,
                "side": side,
                "price": price_str,
                "exit_reason": None,
                "summary_vi": f"Tín hiệu{side_txt} {sig_symbol} · {release}{price_txt}{reason_txt}",
            }
        )

    # 2. Khớp lệnh PAPER từ lịch sử lệnh đã đóng (ưu tiên fills chi tiết; fallback opened/closed)
    for trade in trades:
        trade_symbol = str(trade.get("symbol") or target_symbol).strip().upper()
        if trade_symbol != target_symbol:
            continue
        side = _normalize_side(trade.get("side"))
        side_txt = _side_vi(side)
        raw_fills = trade.get("fills")
        has_fills = (
            isinstance(raw_fills, Sequence)
            and not isinstance(raw_fills, (str, bytes))
            and bool(raw_fills)
        )
        if has_fills:
            assert isinstance(raw_fills, Sequence)
            for fill in raw_fills:
                if not isinstance(fill, Mapping):
                    continue
                dt = _parse_utc(fill.get("occurred_at"))
                if dt is None:
                    continue
                role = str(fill.get("role") or "").strip().upper()
                role_vi = "VÀO" if role == "ENTRY" else "THOÁT"
                price_str = _fmt_price(fill.get("price"))
                price_txt = f" · giá {price_str}" if price_str else ""
                exit_raw = fill.get("exit_reason") or (
                    trade.get("exit_reason") if role == "EXIT" else None
                )
                exit_vi = _exit_reason_vi(exit_raw) if role == "EXIT" else None
                exit_txt = f" · {exit_vi}" if exit_vi else ""
                items.append(
                    {
                        "occurred_at": dt.isoformat(),
                        "timestamp": int(dt.timestamp()),
                        "kind": "paper_fill",
                        "kind_label_vi": "Khớp PAPER",
                        "source_badge": "PAPER ledger",
                        "symbol": trade_symbol,
                        "side": side,
                        "price": price_str,
                        "exit_reason": str(exit_raw) if role == "EXIT" and exit_raw else None,
                        "summary_vi": (
                            f"Khớp PAPER {role_vi} {side_txt} {trade_symbol}{price_txt}{exit_txt}"
                        ),
                    }
                )
        else:
            opened_dt = _parse_utc(trade.get("opened_at"))
            if opened_dt is not None:
                items.append(
                    {
                        "occurred_at": opened_dt.isoformat(),
                        "timestamp": int(opened_dt.timestamp()),
                        "kind": "paper_fill",
                        "kind_label_vi": "Khớp PAPER",
                        "source_badge": "PAPER ledger",
                        "symbol": trade_symbol,
                        "side": side,
                        "price": None,
                        "exit_reason": None,
                        "summary_vi": f"Khớp PAPER VÀO {side_txt} {trade_symbol}",
                    }
                )
            closed_dt = _parse_utc(trade.get("closed_at"))
            if closed_dt is not None:
                exit_raw = trade.get("exit_reason")
                exit_vi = _exit_reason_vi(exit_raw)
                exit_txt = f" · {exit_vi}" if exit_vi else ""
                pnl_raw = trade.get("net_pnl")
                quote = str(trade.get("quote_asset") or "").strip()
                pnl_txt = f" · PnL {pnl_raw} {quote}".rstrip() if pnl_raw is not None else ""
                items.append(
                    {
                        "occurred_at": closed_dt.isoformat(),
                        "timestamp": int(closed_dt.timestamp()),
                        "kind": "paper_fill",
                        "kind_label_vi": "Khớp PAPER",
                        "source_badge": "PAPER ledger",
                        "symbol": trade_symbol,
                        "side": side,
                        "price": None,
                        "exit_reason": str(exit_raw) if exit_raw else None,
                        "summary_vi": (
                            f"Khớp PAPER THOÁT {side_txt} {trade_symbol}{exit_txt}{pnl_txt}"
                        ),
                    }
                )

    # 3. Vị thế PAPER đang mở (ENTRY fill từ overview)
    for pos in open_positions:
        pos_symbol = str(pos.get("symbol") or target_symbol).strip().upper()
        if pos_symbol != target_symbol:
            continue
        dt = _parse_utc(pos.get("filled_at"))
        if dt is None:
            continue
        side = _normalize_side(pos.get("side"))
        price_str = _fmt_price(pos.get("entry_price"))
        price_txt = f" · giá {price_str}" if price_str else ""
        items.append(
            {
                "occurred_at": dt.isoformat(),
                "timestamp": int(dt.timestamp()),
                "kind": "paper_fill",
                "kind_label_vi": "Khớp PAPER",
                "source_badge": "PAPER đang mở",
                "symbol": pos_symbol,
                "side": side,
                "price": price_str,
                "exit_reason": None,
                "summary_vi": f"Khớp PAPER VÀO {_side_vi(side)} {pos_symbol}{price_txt} (đang mở)",
            }
        )

    # 4. Lịch vĩ mô chính thức AI-4
    for event in official_macro_events:
        dt = _parse_utc(event.get("scheduled_at_utc") or event.get("scheduled_at"))
        if dt is None:
            continue
        label = str(event.get("label_vi") or event.get("title") or "Sự kiện vĩ mô")
        source_id = str(event.get("source_id") or "")
        src_txt = f" ({source_id})" if source_id else ""
        items.append(
            {
                "occurred_at": dt.isoformat(),
                "timestamp": int(dt.timestamp()),
                "kind": "macro_calendar",
                "kind_label_vi": "Lịch vĩ mô",
                "source_badge": AI4_OFFICIAL_SOURCE_LABEL,
                "symbol": "US",
                "side": None,
                "price": None,
                "exit_reason": None,
                "summary_vi": f"Lịch vĩ mô AI-4: {label}{src_txt}",
            }
        )

    # 5. Lịch nội bộ do owner nhập (không kích hoạt H06 macro_blackout)
    for event in internal_calendar_events:
        dt = _parse_utc(event.get("event_at"))
        if dt is None:
            continue
        title = str(event.get("title") or "Sự kiện nội bộ")
        region = str(event.get("region") or "US")
        impact = str(event.get("impact") or "").upper()
        impact_txt = f" · {impact}" if impact else ""
        items.append(
            {
                "occurred_at": dt.isoformat(),
                "timestamp": int(dt.timestamp()),
                "kind": "macro_calendar",
                "kind_label_vi": "Lịch vĩ mô",
                "source_badge": INTERNAL_CALENDAR_SOURCE_VI,
                "symbol": region,
                "side": None,
                "price": None,
                "exit_reason": None,
                "summary_vi": f"Lịch nội bộ ({region}{impact_txt}): {title}",
            }
        )

    items.sort(
        key=lambda item: (item["timestamp"], item["occurred_at"], item["kind"], item["summary_vi"]),
        reverse=True,
    )
    return items[: max(1, limit)]


def macro_h06_status_view(
    fetches: Iterable[MacroCalendarFetch],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Trạng thái `macro_blackout` H06 minh bạch nguồn (chỉ từ lịch chính thức AI-4)."""
    digest = macro_digest(fetches, now)
    state = digest["state"]
    blackout = state["blackout"]
    is_fresh = state["is_fresh"]
    if blackout is None or is_fresh is None:
        status = "unknown"
        status_label_vi = "Chưa đủ nguồn lịch AI-4 (H06 tạm từ chối vào lệnh)"
    elif is_fresh is not True:
        status = "stale"
        status_label_vi = "Lịch AI-4 đã cũ (H06 tạm dừng ở H06_MACRO_STALE)"
    elif blackout is True:
        status = "blackout"
        status_label_vi = "Đang trong khoảng cấm H06 (macro_blackout)"
    else:
        status = "clear"
        status_label_vi = "Thông thoáng (không trong khoảng cấm H06)"
    return {
        "source": AI4_OFFICIAL_SOURCE_LABEL,
        "source_label_vi": AI4_OFFICIAL_SOURCE_VI,
        "policy_version": MACRO_POLICY_VERSION,
        "status": status,
        "status_label_vi": status_label_vi,
        "blackout": blackout,
        "is_fresh": is_fresh,
        "blocking_event": state["blocking_event"],
        "missing_sources": list(digest["missing_sources"]),
        "sources": list(digest["sources"]),
        "upcoming": list(digest["upcoming"]),
        "manual_calendar_isolated": True,
        "as_of": digest["as_of"],
    }


__all__ = [
    "AI4_OFFICIAL_SOURCE_LABEL",
    "AI4_OFFICIAL_SOURCE_VI",
    "INTERNAL_CALENDAR_SOURCE_VI",
    "TIMELINE_VERSION",
    "build_chart_timeline",
    "macro_h06_status_view",
]
