"""Dựng JSON tổng quan thị trường cho buồng lái biểu đồ (CK-1 dải thông số, CK-2 đa khung).

Đầu vào là dữ liệu đã đọc từ kho nội bộ (nến đã đóng, giá mua/bán trực tiếp, mẫu mark price);
module không gọi mạng và không đọc đồng hồ hệ thống (``now`` truyền vào). Mọi số liệu ghi rõ
nguồn; dữ liệu cũ hoặc thiếu được đánh dấu thay vì thay bằng số giả.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.application.market_chart import MARKET_CHART_INTERVAL_SECONDS
from trading_bot.domain.market.chart_quality import data_status
from trading_bot.domain.market.ict import active_killzones
from trading_bot.domain.market.overview import (
    MTF_FORMULA_VERSION,
    OVERVIEW_FORMULA_VERSION,
    TREND_LABELS_VI,
    TimeframeTrend,
    active_sessions,
    candle_clock,
    day_range,
    mtf_consensus,
    timeframe_trend,
)
from trading_bot.domain.portfolio.position_overlay import position_overlay

# Thứ tự = thứ tự hiển thị; khung đầu tiên có dữ liệu là khung chính của phép đồng thuận.
MTF_ROLES: tuple[tuple[str, str], ...] = (
    ("4h", "Xu hướng chính"),
    ("1h", "Xác nhận"),
    ("15m", "Động lượng"),
    ("5m", "Thời điểm"),
)
# CK-2 mục 3: vai trò được khai báo theo setup/strategy, không cứng trong UI.
# Chỉ dùng strategy_id làm khóa, không đụng DB schema (mục 7 vẫn chờ owner).
SETUP_MTF_ROLES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "h01": MTF_ROLES,
    "h03": (("1h", "Xu hướng chính"), ("15m", "Động lượng"), ("5m", "Thời điểm")),
    "h06": (("15m", "Xu hướng chính"), ("5m", "Thời điểm")),
}


def resolve_mtf_roles(strategy_id: str | None) -> tuple[tuple[str, str], ...]:
    """Trả vai trò khung theo setup đã khai báo; fallback an toàn khi chưa có khai báo."""
    key = (strategy_id or "").strip().lower().split("-")[0].split(":")[0]
    return SETUP_MTF_ROLES.get(key, MTF_ROLES)


# Giá trực tiếp cũ hơn ngưỡng thì không dùng làm "giá hiện tại".
QUOTE_STALE_SECONDS = 30
MARK_STALE_SECONDS = 180
OVERVIEW_BAR_LIMIT = 300


def _float(value: object) -> float | None:
    if value is None:
        return None
    return float(value) if isinstance(value, (int, float, Decimal)) else float(str(value))


def _age(now: datetime, moment: datetime) -> int:
    return max(0, int((now - moment).total_seconds()))


def _quote_payload(quote: Mapping[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if quote is None:
        return None
    bid = Decimal(str(quote["bid_price"]))
    ask = Decimal(str(quote["ask_price"]))
    mid = (bid + ask) / 2
    age = _age(now, quote["event_at"])
    return {
        "bid": float(bid),
        "ask": float(ask),
        "bid_quantity": _float(quote.get("bid_quantity")),
        "ask_quantity": _float(quote.get("ask_quantity")),
        "spread": float(ask - bid),
        "spread_bps": float((ask - bid) / mid * 10_000) if mid else None,
        "mid": float(mid),
        "event_at": quote["event_at"].astimezone(UTC).isoformat(),
        "age_seconds": age,
        "stale": age > QUOTE_STALE_SECONDS,
    }


def _mark_payload(mark: Mapping[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if mark is None:
        return None
    next_funding: datetime | None = mark.get("next_funding_time")
    rate = mark.get("funding_rate")
    age = _age(now, mark["event_at"])
    return {
        "mark_price": _float(mark["mark_price"]),
        "index_price": _float(mark.get("index_price")),
        "funding_rate": _float(rate),
        "funding_rate_pct": (float(Decimal(str(rate)) * 100) if rate is not None else None),
        "next_funding_time": next_funding.astimezone(UTC).isoformat() if next_funding else None,
        "funding_seconds_left": (
            max(0, int((next_funding - now).total_seconds())) if next_funding else None
        ),
        "event_at": mark["event_at"].astimezone(UTC).isoformat(),
        "age_seconds": age,
        "stale": age > MARK_STALE_SECONDS,
    }


def _series(
    bars: Sequence[Mapping[str, Any]],
) -> tuple[list[datetime], list[float], list[float], list[float], list[float]]:
    times = [datetime.fromtimestamp(int(bar["time"]), UTC) for bar in bars]
    return (
        times,
        [float(bar["open"]) for bar in bars],
        [float(bar["high"]) for bar in bars],
        [float(bar["low"]) for bar in bars],
        [float(bar["close"]) for bar in bars],
    )


def _price_vi(value: float) -> str:
    """Giá kiểu Việt Nam: dấu chấm phân cách nghìn, dấu phẩy thập phân (giống ``number2_vi``)."""
    digits = 2 if abs(value) >= 1 else 8
    text = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(",", "\u00a0").replace(".", ",").replace("\u00a0", ".")


def _statement(cell: TimeframeTrend) -> str:
    frame = cell.timeframe.upper()
    if cell.bar_count == 0:
        return f"Chưa có nến {frame} đã đóng trong kho dữ liệu."
    if cell.trend == "unknown":
        return f"{frame} mới có {cell.bar_count} nến đã đóng; cần thêm để xác định cấu trúc."
    if cell.trend == "range":
        return f"{frame} chưa có phá vỡ cấu trúc (BOS) nào trong vùng dữ liệu."
    if cell.invalidation_price is None:
        return f"{frame} {TREND_LABELS_VI[cell.trend]}; chưa có swing xác nhận làm mức vô hiệu."
    side = "dưới" if cell.trend == "up" else "trên"
    return (
        f"{frame} giữ {TREND_LABELS_VI[cell.trend]} khi chưa đóng cửa {side} "
        f"{_price_vi(cell.invalidation_price)}"
    )


def _cell_payload(cell: TimeframeTrend, role: str) -> dict[str, Any]:
    last_break = cell.last_break
    return {
        "timeframe": cell.timeframe,
        "role": role,
        "trend": cell.trend,
        "label": cell.label_vi,
        "bar_count": cell.bar_count,
        "last_close": cell.last_close,
        "change_pct": cell.last_change_pct,
        "invalidation": cell.invalidation_price,
        "range_high": cell.range_high,
        "range_low": cell.range_low,
        "position_pct": cell.position_pct,
        "zone": cell.zone,
        "last_break": (
            {
                "kind": last_break.kind,
                "direction": last_break.direction,
                "level": last_break.level,
                "at": last_break.at.isoformat(),
            }
            if last_break is not None
            else None
        ),
        "as_of": cell.as_of.isoformat() if cell.as_of else None,
        "statement": _statement(cell),
    }


def build_market_overview(
    *,
    symbol: str,
    timeframe: str,
    now: datetime,
    bars_by_timeframe: Mapping[str, Sequence[Mapping[str, Any]]],
    quote: Mapping[str, Any] | None,
    mark: Mapping[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] = (),
    open_gaps: Sequence[Mapping[str, Any]] = (),
    source_label: str = "—",
    mtf_roles: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    """JSON cho dải thông số (CK-1) và ma trận đa khung (CK-2) của một mã."""
    roles = mtf_roles or MTF_ROLES
    if not roles or len({frame for frame, _ in roles}) != len(roles):
        raise ValueError("Vai trò khung phải khác nhau và không rỗng.")
    if now.tzinfo is None:
        raise ValueError("now phải có timezone")
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise ValueError("Khung thời gian không được hỗ trợ")
    notices: list[str] = []
    quote_view = _quote_payload(quote, now)
    mark_view = _mark_payload(mark, now)
    if quote_view is None:
        notices.append(
            "Chưa có giá bid/ask trực tiếp: bật stream book_ticker trong ingestion-worker."
        )
    elif quote_view["stale"]:
        notices.append(
            f"Giá bid/ask đã cũ {quote_view['age_seconds']} giây; stream có thể đã ngắt."
        )
    if mark_view is None:
        notices.append("Chưa có mark price/funding: bật stream mark_price trong ingestion-worker.")

    selected = list(bars_by_timeframe.get(timeframe, ()))
    last_close = float(selected[-1]["close"]) if selected else None
    if quote_view is not None and not quote_view["stale"]:
        price, source = quote_view["mid"], "quote_mid"
    elif mark_view is not None and not mark_view["stale"]:
        price, source = mark_view["mark_price"], "mark"
    else:
        price, source = last_close, "last_close"
    clock = candle_clock(now, MARKET_CHART_INTERVAL_SECONDS[timeframe])
    candle = {
        "timeframe": timeframe,
        "open_at": clock.open_at.isoformat(),
        "close_at": clock.close_at.isoformat(),
        "seconds_left": clock.seconds_left,
        "price": price,
        "price_source": source,
        "last_close": last_close,
        "change_pct": (
            (price - last_close) / last_close * 100
            if price is not None and last_close not in (None, 0)
            else None
        ),
    }

    # Biên ngày ưu tiên nến 1h (đủ một ngày với ít nến), nếu không thì khung đang xem.
    day_frame = "1h" if bars_by_timeframe.get("1h") else timeframe
    day_bars = list(bars_by_timeframe.get(day_frame, ()))
    day: dict[str, Any] | None = None
    if day_bars:
        times, _opens, highs, lows, _closes = _series(day_bars)
        result = day_range(
            times,
            highs,
            lows,
            timedelta(seconds=MARKET_CHART_INTERVAL_SECONDS[day_frame]),
            now,
            live_price=price if source != "last_close" else None,
        )
        day = {
            "timeframe": day_frame,
            "day_start": result.day_start.isoformat(),
            "high": result.high,
            "low": result.low,
            "previous_high": result.previous_high,
            "previous_low": result.previous_low,
            "previous_complete": result.previous_complete,
        }

    cells: list[TimeframeTrend] = []
    payload_cells: list[dict[str, Any]] = []
    for frame, role in roles:
        rows = list(bars_by_timeframe.get(frame, ()))[-OVERVIEW_BAR_LIMIT:]
        times, opens, highs, lows, closes = _series(rows)
        cell = timeframe_trend(frame, times, highs, lows, closes, opens=opens)
        cells.append(cell)
        payload_cells.append(_cell_payload(cell, role))
    primary = next((cell.timeframe for cell in cells if cell.bar_count), roles[0][0])
    consensus = mtf_consensus(cells, primary=primary)

    # CK-7: UPnL vị thế PAPER theo mark price còn mới, nếu không thì theo giá hiện tại kèm nguồn.
    if mark_view is not None and not mark_view["stale"] and mark is not None:
        upnl_price: Decimal | None = Decimal(str(mark["mark_price"]))
        upnl_source: str | None = "mark"
    elif price is not None:
        upnl_price, upnl_source = Decimal(str(price)), source
    else:
        upnl_price, upnl_source = None, None
    position_views = [
        position_overlay(item, mark_price=upnl_price, mark_source=upnl_source)
        for item in positions
        if item.get("entry_fills")
    ]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "generated_at": now.astimezone(UTC).isoformat(),
        "positions": position_views,
        # CK-9: thanh trạng thái dữ liệu của khung đang xem.
        "data_status": data_status(
            selected,
            interval_seconds=MARKET_CHART_INTERVAL_SECONDS[timeframe],
            now=now,
            source_label=source_label,
            open_gaps=open_gaps,
        ),
        "formula_versions": {"overview": OVERVIEW_FORMULA_VERSION, "mtf": MTF_FORMULA_VERSION},
        "quote": quote_view,
        "mark": mark_view,
        "candle": candle,
        "day": day,
        "sessions": [
            {
                "key": item.key,
                "label": item.label_vi,
                "close_at": item.close_at.isoformat(),
            }
            for item in active_sessions(now)
        ],
        "killzones": [
            {
                "key": item.key,
                "label": item.label_vi,
                "kind": item.kind,
                "close_at": item.close_at.isoformat(),
            }
            for item in active_killzones(now)
        ],
        "mtf": {
            "cells": payload_cells,
            "roles_source": "setup" if mtf_roles is not None else "default",
            "consensus": {
                "primary": consensus.primary,
                "direction": consensus.direction,
                "aligned": consensus.aligned,
                "known": consensus.known,
                "label": consensus.label_vi,
            },
        },
        "notices": notices,
    }
