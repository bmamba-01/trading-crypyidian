"""Dựng dữ liệu chart thị trường kiểu TradingView (SIG-7) từ nến đã đóng.

Mọi lớp phủ được tính bằng chính các hàm feature của domain (SIG-1) để chart hiển thị đúng
thứ chiến lược nhìn thấy; trình duyệt chỉ vẽ, không tự tính chỉ báo. Không lớp nào được
"bịa" dữ liệu: thiếu taker buy thì lớp áp lực mua/bán bị ẩn kèm ghi chú.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.domain.market.features import atr, ema, macd, rsi, vwap
from trading_bot.domain.market.sessions import (
    LIQUIDITY_FORMULA_VERSION,
    SESSION_FORMULA_VERSION,
    liquidity_levels,
    session_ranges,
)
from trading_bot.domain.market.structure import (
    STRUCTURE_FORMULA_VERSION,
    TRENDLINE_FORMULA_VERSION,
    auto_trendlines,
    market_structure,
    structure_state,
)
from trading_bot.domain.market.volume_profile import (
    VOLUME_PROFILE_FORMULA_VERSION,
    VolumeProfile,
    cumulative_delta,
    volume_profile,
)

CHART_VERSION = "sig7-market-chart-v1"
MARKET_CHART_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
# Phiên và mức thanh khoản theo phiên chỉ có nghĩa khi nến nhỏ hơn phiên.
SESSION_MAX_INTERVAL_SECONDS = 3600
MARKET_CHART_DEFAULT_LIMIT = 600
MARKET_CHART_MAX_LIMIT = 1000
SWING_LEFT_BARS = 3
SWING_RIGHT_BARS = 3
PROFILE_BINS = 24

NOTICE_NO_TAKER = (
    "Chưa có dữ liệu khối lượng mua chủ động (taker buy) cho các nến này nên lớp áp lực "
    "mua/bán và CVD bị ẩn; volume profile chỉ hiện tổng khối lượng. Cần gói SIG-0 để thu."
)
NOTICE_SESSIONS_HIDDEN = "Khung thời gian từ 4 giờ trở lên không hiển thị phiên Á/Âu/Mỹ."
NOTICE_STRUCTURE_DELAY = (
    "Đỉnh/đáy (swing) chỉ được xác nhận sau {right} nến bên phải đóng; nhãn, BOS/CHoCH và "
    "trendline được vẽ từ thời điểm đã biết, không vẽ lùi."
)


def _series(times: Sequence[int], values: Sequence[float | None]) -> list[dict[str, float]]:
    return [
        {"time": time, "value": round(value, 10)}
        for time, value in zip(times, values, strict=True)
        if value is not None
    ]


def _session_vwap(times: Sequence[int], bars: Sequence[Mapping[str, Any]]) -> list[float | None]:
    """VWAP reset theo ngày UTC, giá điển hình (H+L+C)/3 như feature ``session_vwap``."""
    output: list[float | None] = []
    group: list[int] = []
    current_day: object = None

    def flush() -> None:
        if not group:
            return
        typical = [
            (float(bars[i]["high"]) + float(bars[i]["low"]) + float(bars[i]["close"])) / 3
            for i in group
        ]
        output.extend(vwap(typical, [float(bars[i]["volume"]) for i in group]))
        group.clear()

    for index, time in enumerate(times):
        day = datetime.fromtimestamp(time, UTC).date()
        if day != current_day:
            flush()
            current_day = day
        group.append(index)
    flush()
    return output


def _profile_payload(
    profile: VolumeProfile | None, *, label: str, start: int, end: int, complete: bool
) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "label": label,
        "start": start,
        "end": end,
        "complete": complete,
        "poc": profile.poc_price,
        "vah": profile.value_area_high,
        "val": profile.value_area_low,
        "has_split": profile.imbalance_reference is not None,
        "bins": [
            {
                "low": item.price_low,
                "high": item.price_high,
                "volume": item.volume,
                "buy": item.buy,
                "sell": item.sell,
                "category": item.category,
            }
            for item in profile.bins
        ],
    }


def chart_fingerprint(symbol: str, timeframe: str, bars: Sequence[Mapping[str, Any]]) -> str:
    """Dấu vân tay rẻ cho polling: đổi khi có nến mới hoặc nến cuối bị sửa."""
    last = bars[-1] if bars else {}
    raw = f"{CHART_VERSION}|{symbol}|{timeframe}|{len(bars)}|" + "|".join(
        str(last.get(key)) for key in ("time", "open", "high", "low", "close", "volume")
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_market_chart_payload(
    *,
    symbol: str,
    timeframe: str,
    bars: Sequence[Mapping[str, Any]],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Dữ liệu JSON cho trang chart; ``bars`` đã sắp tăng dần theo ``time`` (epoch giây UTC)."""
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise ValueError("Khung thời gian không được hỗ trợ")
    interval_seconds = MARKET_CHART_INTERVAL_SECONDS[timeframe]
    interval = timedelta(seconds=interval_seconds)
    generated = (generated_at or datetime.now(UTC)).isoformat()
    payload: dict[str, Any] = {
        "chart_version": CHART_VERSION,
        "symbol": symbol,
        "timeframe": timeframe,
        "generated_at": generated,
        "fingerprint": chart_fingerprint(symbol, timeframe, bars),
        "formula_versions": {
            "structure": STRUCTURE_FORMULA_VERSION,
            "trendline": TRENDLINE_FORMULA_VERSION,
            "session": SESSION_FORMULA_VERSION,
            "liquidity": LIQUIDITY_FORMULA_VERSION,
            "volume_profile": VOLUME_PROFILE_FORMULA_VERSION,
        },
        "bars": [],
        "volume": [],
        "lines": {},
        "panes": {},
        "structure": {"swings": [], "breaks": [], "trend": None, "state": "unknown"},
        "trendlines": [],
        "sessions": [],
        "levels": [],
        "volume_profiles": [],
        "notices": [],
    }
    if not bars:
        payload["notices"].append(
            "Chưa có nến đã đóng cho mã và khung này trong kho dữ liệu nội bộ."
        )
        return payload

    times = [int(bar["time"]) for bar in bars]
    opens = [float(bar["open"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    closes = [float(bar["close"]) for bar in bars]
    volumes = [float(bar["volume"]) for bar in bars]
    takers: list[float | None] = [
        None if bar.get("taker_buy_volume") is None else float(bar["taker_buy_volume"])
        for bar in bars
    ]
    has_taker = all(value is not None for value in takers)
    open_times = [datetime.fromtimestamp(time, UTC) for time in times]

    payload["bars"] = [
        {"time": t, "open": o, "high": h, "low": lo, "close": c}
        for t, o, h, lo, c in zip(times, opens, highs, lows, closes, strict=True)
    ]
    deltas = (
        [
            2.0 * taker - volume
            for taker, volume in zip(takers, volumes, strict=True)
            if taker is not None
        ]
        if has_taker
        else None
    )
    payload["volume"] = [
        {
            "time": t,
            "value": v,
            "tone": (
                ("up" if deltas[i] > 0 else "down" if deltas[i] < 0 else "flat")
                if deltas is not None
                else ("up" if c > o else "down" if c < o else "flat")
            ),
            "delta": deltas[i] if deltas is not None else None,
        }
        for i, (t, v, o, c) in enumerate(zip(times, volumes, opens, closes, strict=True))
    ]
    payload["lines"] = {
        "ema_20": _series(times, ema(closes, 20)),
        "ema_50": _series(times, ema(closes, 50)),
        "session_vwap": _series(times, _session_vwap(times, bars)),
    }
    macd_line, signal_line, histogram = macd(closes)
    payload["panes"] = {
        "rsi_14": _series(times, rsi(closes, 14)),
        "macd": {
            "macd": _series(times, macd_line),
            "signal": _series(times, signal_line),
            "histogram": _series(times, histogram),
        },
        "cvd": (
            _series(
                times,
                cumulative_delta(volumes, takers, [moment.date() for moment in open_times]),
            )
            if has_taker
            else None
        ),
    }

    structure = market_structure(
        highs, lows, closes, left_bars=SWING_LEFT_BARS, right_bars=SWING_RIGHT_BARS
    )
    atr_values = atr(highs, lows, closes, 14)
    lines = auto_trendlines(
        highs,
        lows,
        closes,
        atr_values,
        left_bars=SWING_LEFT_BARS,
        right_bars=SWING_RIGHT_BARS,
    )
    payload["structure"] = {
        "swings": [
            {
                "time": times[swing.index],
                "confirmed_time": times[swing.confirmed_index],
                "price": swing.price,
                "kind": swing.kind,
                "label": swing.label,
            }
            for swing in structure.swings
        ],
        "breaks": [
            {
                "time": times[item.break_index],
                "from_time": times[item.swing_index],
                "level": item.level,
                "kind": item.kind,
                "direction": item.direction,
            }
            for item in structure.breaks
        ],
        "trend": structure.trend[-1],
        "state": structure_state(lines),
    }
    last_index = len(times) - 1
    payload["trendlines"] = [
        {
            "side": line.side,
            "p1": {"time": times[line.p1_index], "price": line.p1_price},
            "p2": {"time": times[line.p2_index], "price": line.p2_price},
            "confirmed_time": times[line.confirmed_index],
            "end": {
                "time": times[line.end_index if line.end_index is not None else last_index],
                "price": line.price_at(
                    line.end_index if line.end_index is not None else last_index
                ),
            },
            "active": line.end_index is None,
            "invalidated": line.invalidated_index is not None,
        }
        for line in lines.lines[-8:]
    ]
    payload["notices"].append(NOTICE_STRUCTURE_DELAY.format(right=SWING_RIGHT_BARS))

    if interval_seconds <= SESSION_MAX_INTERVAL_SECONDS:
        last_close = int((open_times[-1] + interval).timestamp())
        payload["sessions"] = [
            {
                "key": item.key,
                "label": item.label_vi,
                "start": int(item.open_at.timestamp()),
                "end": min(int(item.close_at.timestamp()), last_close),
                "high": item.high,
                "low": item.low,
                "complete": item.complete,
            }
            for item in session_ranges(open_times, highs, lows, interval)
        ]
        payload["levels"] = [
            {
                "key": level.key,
                "label": level.label_vi,
                "side": level.side,
                "price": level.price,
                "armed_time": int(level.armed_at.timestamp()),
                "end_time": min(
                    int(level.expires_at.timestamp()),
                    times[level.event_index] if level.event_index is not None else last_close,
                ),
                "state": level.state,
                "event_time": times[level.event_index] if level.event_index is not None else None,
            }
            for level in liquidity_levels(open_times, highs, lows, closes, interval)
            if level.armed_at.timestamp() <= last_close
        ][-12:]
    else:
        payload["notices"].append(NOTICE_SESSIONS_HIDDEN)

    # Volume profile: ngày UTC gần nhất đã đóng và ngày hiện tại đang hình thành.
    day_keys = [moment.date() for moment in open_times]
    last_day = day_keys[-1]
    last_bar_close = open_times[-1] + interval
    day_is_complete = last_bar_close >= datetime.combine(
        last_day, datetime.min.time(), tzinfo=UTC
    ) + timedelta(days=1)
    profiles: list[dict[str, Any]] = []
    if interval_seconds <= SESSION_MAX_INTERVAL_SECONDS:
        previous_days = sorted({day for day in day_keys if day < last_day})
        selections = []
        if previous_days:
            selections.append((previous_days[-1], True, "Ngày UTC trước"))
        selections.append(
            (last_day, day_is_complete, "Hôm nay (UTC)" if not day_is_complete else "Ngày UTC")
        )
        for day, complete, label in selections:
            members = [i for i, key in enumerate(day_keys) if key == day]
            profile = volume_profile(
                [highs[i] for i in members],
                [lows[i] for i in members],
                [volumes[i] for i in members],
                [takers[i] for i in members] if has_taker else None,
                bins=PROFILE_BINS,
            )
            item = _profile_payload(
                profile,
                label=label if complete else f"{label} — đang hình thành",
                start=times[members[0]],
                end=times[members[-1]],
                complete=complete,
            )
            if item is not None:
                profiles.append(item)
    payload["volume_profiles"] = profiles
    if not has_taker:
        payload["notices"].append(NOTICE_NO_TAKER)
    return payload
