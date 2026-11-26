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

from trading_bot.domain.market.bar_source_policy import REAL_BAR_SOURCES
from trading_bot.domain.market.features import atr, ema, macd, rsi, vwap
from trading_bot.domain.market.ict import (
    ICT_FORMULA_VERSION,
    KILLZONE_TABLE_VERSION,
    PriceZone,
    balanced_price_ranges,
    equal_levels,
    fair_value_gaps,
    intermediate_swings,
    killzone_windows,
    order_blocks,
)
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
from trading_bot.domain.market.tpo import (
    TPO_BLOCK,
    TPO_FORMULA_VERSION,
    TpoProfile,
    naked_levels,
    tpo_profile,
)
from trading_bot.domain.market.volume_profile import (
    VOLUME_PROFILE_FORMULA_VERSION,
    VolumeProfile,
    cumulative_delta,
    volume_profile,
)
from trading_bot.domain.market.vsa import (
    VSA_FORMULA_VERSION,
    VSA_LABELS_VI,
    VSA_STRENGTH,
    ZIGZAG_FORMULA_VERSION,
    vsa_labels,
    zigzag,
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
# CK-4: số ngày UTC đã đóng gần nhất được xét POC "trần" (nPOC).
NAKED_POC_DAYS = 5
# CK-5: nhịp ZigZag tối thiểu = 2 × ATR14 tại nến xác nhận swing ngược chiều.
ZIGZAG_MIN_MOVE_ATR = 2.0
# CK-3: dải killzone chỉ vẽ ở khung ≤ 15 phút (khung lớn hơn chỉ còn 1–3 nến mỗi cửa sổ) và
# trong 3 ngày gần nhất; số vùng vẽ có trần để chart mượt trên mobile.
KILLZONE_MAX_INTERVAL_SECONDS = 900
KILLZONE_LOOKBACK = timedelta(days=3)
ICT_ZONE_LIMITS = {"fvg": 12, "order_block": 8, "bpr": 4}
ICT_LIVE_STATES = frozenset({"active", "partial", "inverted"})
ZONE_LABELS_VI = {
    ("fvg", "bullish"): "FVG tăng",
    ("fvg", "bearish"): "FVG giảm",
    ("order_block", "bullish"): "OB tăng",
    ("order_block", "bearish"): "OB giảm",
    ("bpr", "bullish"): "BPR tăng",
    ("bpr", "bearish"): "BPR giảm",
}

NOTICE_NO_TAKER = (
    "Chưa có dữ liệu khối lượng mua chủ động (taker buy) cho các nến này nên lớp áp lực "
    "mua/bán và CVD bị ẩn; volume profile chỉ hiện tổng khối lượng. Cần gói SIG-0 để thu."
)
NOTICE_SESSIONS_HIDDEN = "Khung thời gian từ 4 giờ trở lên không hiển thị phiên Á/Âu/Mỹ."
NOTICE_STRUCTURE_DELAY = (
    "Đỉnh/đáy (swing) chỉ được xác nhận sau {right} nến bên phải đóng; nhãn, BOS/CHoCH và "
    "trendline được vẽ từ thời điểm đã biết, không vẽ lùi."
)


REAL_MARKET_SOURCES = REAL_BAR_SOURCES


def market_source_label(source: str) -> str:
    """Nhãn tiếng Việt cho nguồn nến hiển thị trên chart."""
    if source == "BINANCE_USDM_REST_BACKFILL":
        return "Binance USDⓈ-M · REST công khai"
    if source == "BINANCE_USDM":
        return "Binance USDⓈ-M"
    return f"Dữ liệu mẫu ({source})" if source else "—"


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


def _zone_payload(zone: PriceZone, times: Sequence[int], last_close: int) -> dict[str, Any]:
    """Vùng ngừng kéo dài khi mất hiệu lực, hoặc khi FVG bị lấp đầy mà chưa đảo vai."""
    state = zone.state
    ended = zone.invalidated_index
    if ended is None and state == "filled":
        ended = zone.filled_index
    label = ZONE_LABELS_VI[(zone.kind, zone.direction)]
    if state == "inverted":
        # FVG tăng bị đóng cửa xuyên xuống trở thành kháng cự (IFVG giảm) và ngược lại.
        label = "IFVG giảm" if zone.direction == "bullish" else "IFVG tăng"
    return {
        "kind": zone.kind,
        "direction": zone.direction,
        "label": label,
        "top": zone.top,
        "bottom": zone.bottom,
        "start_time": times[zone.created_index],
        "confirmed_time": times[zone.confirmed_index],
        "end_time": times[ended] if ended is not None else last_close,
        "state": state,
        "touched_time": (
            times[zone.first_touch_index] if zone.first_touch_index is not None else None
        ),
    }


def _ict_payload(
    *,
    times: Sequence[int],
    open_times: Sequence[datetime],
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    atr_values: Sequence[float | None],
    structure_swings: Sequence[Any],
    structure_breaks: Sequence[Any],
    interval: timedelta,
) -> dict[str, Any]:
    """Lớp ICT/SMC (CK-3) đã giới hạn số vùng; ưu tiên vùng còn hiệu lực, rồi vùng mới nhất."""
    last_close_at = open_times[-1] + interval
    last_close = int(last_close_at.timestamp())
    killzones: list[dict[str, Any]] = []
    if interval.total_seconds() <= KILLZONE_MAX_INTERVAL_SECONDS:
        window_start = max(open_times[0], last_close_at - KILLZONE_LOOKBACK)
        killzones = [
            {
                "key": item.key,
                "label": item.label_vi,
                "kind": item.kind,
                "start": int(item.open_at.timestamp()),
                "end": int(item.close_at.timestamp()),
            }
            for item in killzone_windows(window_start, last_close_at)
        ]
    gaps = fair_value_gaps(opens, highs, lows, closes, atr_values=atr_values)
    groups: dict[str, tuple[PriceZone, ...]] = {
        "fvg": gaps,
        "order_block": order_blocks(opens, highs, lows, closes, structure_breaks),
        "bpr": balanced_price_ranges(gaps, closes),
    }
    zones: list[dict[str, Any]] = []
    for kind, items in groups.items():
        ranked = sorted(
            items,
            key=lambda zone: (zone.state in ICT_LIVE_STATES, zone.confirmed_index),
            reverse=True,
        )[: ICT_ZONE_LIMITS[kind]]
        zones.extend(_zone_payload(zone, times, last_close) for zone in ranked)
    zones.sort(key=lambda item: item["confirmed_time"])
    levels = [
        {
            "side": level.side,
            "label": "Đỉnh bằng nhau (EQH)" if level.side == "high" else "Đáy bằng nhau (EQL)",
            "price": level.price,
            "start_time": times[level.swing_indices[0]],
            "confirmed_time": times[level.confirmed_index],
            "end_time": times[level.swept_index] if level.swept_index is not None else last_close,
            "swept": level.swept_index is not None,
        }
        for level in equal_levels(structure_swings, highs, lows, atr_values)
    ][-6:]
    swings = [
        {
            "kind": item.kind,
            "label": "ITH" if item.kind == "high" else "ITL",
            "time": times[item.index],
            "confirmed_time": times[item.confirmed_index],
            "price": item.price,
        }
        for item in intermediate_swings(structure_swings)
    ][-8:]
    return {
        "killzones": killzones,
        "zones": zones,
        "equal_levels": levels,
        "intermediate_swings": swings,
        "counts": {"active": sum(1 for item in zones if item["state"] in ICT_LIVE_STATES)},
    }


def _tpo_payload(profile: TpoProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "poc": profile.poc_price,
        "vah": profile.value_area_high,
        "val": profile.value_area_low,
        "total": profile.total_tpo,
        "blocks": profile.block_count,
        "rows": [
            {"low": row.price_low, "high": row.price_high, "count": row.count}
            for row in profile.rows
        ],
    }


def _profile_levels(
    *,
    times: Sequence[int],
    open_times: Sequence[datetime],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    interval: timedelta,
) -> list[dict[str, Any]]:
    """CK-4: POC "trần" (khối lượng và TPO) của các ngày UTC đã đóng đủ nến, cùng VAH/VAL
    khối lượng của ngày đã đóng gần nhất. Mức trần bị bỏ ngay khi có nến chạm giá."""
    last_close = int((open_times[-1] + interval).timestamp())
    days: dict[Any, list[int]] = {}
    for index, moment in enumerate(open_times):
        days.setdefault(moment.date(), []).append(index)
    complete: list[tuple[Any, list[int], int]] = []
    for day, members in days.items():
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        covered = (
            open_times[members[0]] == day_start and open_times[members[-1]] + interval == day_end
        )
        after = members[-1] + 1 if members[-1] + 1 < len(open_times) else None
        if covered and after is not None:
            complete.append((day, members, after))
    output: list[dict[str, Any]] = []
    for position, (day, members, armed) in enumerate(complete[-NAKED_POC_DAYS:]):
        is_latest = position == len(complete[-NAKED_POC_DAYS:]) - 1
        label_day = day.strftime("%d/%m")
        profile = volume_profile(
            [highs[i] for i in members],
            [lows[i] for i in members],
            [volumes[i] for i in members],
            bins=PROFILE_BINS,
        )
        candidates: list[tuple[str, str, float]] = []
        if profile is not None:
            candidates.append(("npoc_volume", f"nPOC {label_day}", profile.poc_price))
            if is_latest:
                candidates.append(("vah", "VAH ngày trước", profile.value_area_high))
                candidates.append(("val", "VAL ngày trước", profile.value_area_low))
        if interval <= TPO_BLOCK:
            tpo = tpo_profile(
                [open_times[i] for i in members],
                [highs[i] for i in members],
                [lows[i] for i in members],
                interval,
                rows=PROFILE_BINS,
            )
            if tpo is not None:
                candidates.append(("npoc_tpo", f"nPOC TPO {label_day}", tpo.poc_price))
        touched = naked_levels([(price, armed) for _, _, price in candidates], highs, lows)
        for (kind, label, price), hit in zip(candidates, touched, strict=True):
            if kind.startswith("npoc") and hit is not None:
                continue  # đã bị chạm → không còn là POC trần
            output.append(
                {
                    "kind": kind,
                    "label": label,
                    "price": price,
                    "start_time": times[armed],
                    # VAH/VAL là đường tham chiếu: luôn kéo tới nến cuối, chỉ ghi đã chạm hay chưa.
                    "end_time": last_close,
                    "touched": hit is not None,
                }
            )
    return output


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
            "ict": ICT_FORMULA_VERSION,
            "killzone_table": KILLZONE_TABLE_VERSION,
            "tpo": TPO_FORMULA_VERSION,
            "vsa": VSA_FORMULA_VERSION,
            "zigzag": ZIGZAG_FORMULA_VERSION,
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
        "profile_levels": [],
        "vsa": [],
        "zigzag": {"points": [], "pending": None},
        "ict": {
            "killzones": [],
            "zones": [],
            "equal_levels": [],
            "intermediate_swings": [],
            "counts": {"active": 0},
        },
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

    source = str(bars[-1].get("source") or "")
    payload["source"] = source or None
    payload["source_label"] = market_source_label(source)
    if source and source not in REAL_MARKET_SOURCES:
        payload["notices"].append(
            f"Nến từ nguồn {source} là dữ liệu mẫu/kiểm thử, không phải giá thị trường thật."
        )
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
    payload["vsa"] = [
        {
            "time": times[index],
            "key": label,
            "label": VSA_LABELS_VI[label],
            "group": "strength" if label in VSA_STRENGTH else "weakness",
        }
        for index, label in enumerate(vsa_labels(highs, lows, closes, volumes))
        if label is not None
    ]
    zig = zigzag(structure.swings, atr_values, min_move_atr=ZIGZAG_MIN_MOVE_ATR)
    payload["zigzag"] = {
        "points": [
            {
                "kind": point.kind,
                "time": times[point.index],
                "price": point.price,
                "final_time": times[point.final_index],
            }
            for point in zig.points
        ],
        "pending": (
            {"kind": zig.pending.kind, "time": times[zig.pending.index], "price": zig.pending.price}
            if zig.pending is not None
            else None
        ),
        "last": {"time": times[-1], "price": closes[-1]},
    }
    payload["ict"] = _ict_payload(
        times=times,
        open_times=open_times,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        atr_values=atr_values,
        structure_swings=structure.swings,
        structure_breaks=structure.breaks,
        interval=interval,
    )

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
                item["tpo"] = (
                    _tpo_payload(
                        tpo_profile(
                            [open_times[i] for i in members],
                            [highs[i] for i in members],
                            [lows[i] for i in members],
                            interval,
                            rows=PROFILE_BINS,
                        )
                    )
                    if interval <= TPO_BLOCK
                    else None
                )
                profiles.append(item)
        payload["profile_levels"] = _profile_levels(
            times=times,
            open_times=open_times,
            highs=highs,
            lows=lows,
            volumes=volumes,
            interval=interval,
        )
    payload["volume_profiles"] = profiles
    if not has_taker:
        payload["notices"].append(NOTICE_NO_TAKER)
    return payload
