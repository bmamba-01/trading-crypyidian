"""Kiểm chất lượng nến đang xem trên chart và trạng thái dữ liệu (CK-9), chỉ đọc.

Khác ``inspect_bar_series`` (kiểm nến thô cho pipeline), module này làm việc trên nến đã dựng
cho chart (dict ``time/open/high/low/close/volume/taker_buy_volume``) để trả lời nhanh nút
"Kiểm dữ liệu": thiếu nến, trùng mốc, OHLC sai, khối lượng âm, taker buy vượt khối lượng, nến
chưa đóng, chuỗi bị trễ. Không so với REST của sàn (admin-web không gọi mạng sàn).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

CHART_QUALITY_VERSION = "ck9-quality-v1"
# Chuỗi coi là trễ khi nến cuối đã đóng quá 2 khung mà chưa có nến mới.
STALE_INTERVALS = 2


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def inspect_chart_bars(
    bars: Sequence[Mapping[str, Any]],
    *,
    interval_seconds: int,
    now: datetime,
    revised_bars: int = 0,
) -> dict[str, Any]:
    """Báo cáo chất lượng các nến (sắp tăng dần theo ``time``); không sửa dữ liệu."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds phải dương")
    if now.tzinfo is None:
        raise ValueError("now phải có timezone")
    times = [int(bar["time"]) for bar in bars]
    duplicates: list[str] = []
    missing: list[dict[str, Any]] = []
    previous: int | None = None
    for time in times:
        if previous is not None:
            if time == previous:
                duplicates.append(_iso(time))
            elif time - previous > interval_seconds:
                count = (time - previous) // interval_seconds - 1
                missing.append(
                    {
                        "from": _iso(previous + interval_seconds),
                        "to": _iso(time - interval_seconds),
                        "bars": count,
                    }
                )
        previous = time
    invalid_ohlc: list[str] = []
    negative_volume: list[str] = []
    taker_exceeds: list[str] = []
    for bar in bars:
        open_, high, low, close = (float(bar[key]) for key in ("open", "high", "low", "close"))
        if min(open_, high, low, close) <= 0 or low > min(open_, close) or high < max(open_, close):
            invalid_ohlc.append(_iso(int(bar["time"])))
        volume = float(bar["volume"])
        if volume < 0:
            negative_volume.append(_iso(int(bar["time"])))
        taker = bar.get("taker_buy_volume")
        if taker is not None and volume >= 0 and float(taker) > volume:
            taker_exceeds.append(_iso(int(bar["time"])))
    now_epoch = int(now.timestamp())
    unclosed = sum(1 for time in times if time + interval_seconds > now_epoch)
    stale = (
        bool(times)
        and now_epoch - (times[-1] + interval_seconds) > STALE_INTERVALS * interval_seconds
    )
    missing_bars = sum(item["bars"] for item in missing)
    problems: list[str] = []
    if missing_bars:
        problems.append(f"thiếu {missing_bars} nến ({len(missing)} khoảng)")
    if duplicates:
        problems.append(f"{len(duplicates)} mốc trùng")
    if invalid_ohlc:
        problems.append(f"{len(invalid_ohlc)} nến OHLC sai")
    if negative_volume:
        problems.append(f"{len(negative_volume)} nến khối lượng âm")
    if taker_exceeds:
        problems.append(f"{len(taker_exceeds)} nến taker buy vượt khối lượng")
    if unclosed:
        problems.append(f"{unclosed} nến chưa đóng")
    if stale:
        problems.append("chuỗi bị trễ (chưa có nến mới)")
    ok = not problems
    summary = (
        f"Đạt: {len(times)} nến liên tục, OHLC hợp lệ."
        if ok
        else f"Phát hiện vấn đề trong {len(times)} nến: " + "; ".join(problems) + "."
    )
    return {
        "formula_version": CHART_QUALITY_VERSION,
        "checked": len(times),
        "ok": ok,
        "summary": summary,
        "missing": missing,
        "missing_bars": missing_bars,
        "duplicates": duplicates,
        "invalid_ohlc": invalid_ohlc,
        "negative_volume": negative_volume,
        "taker_exceeds_volume": taker_exceeds,
        "unclosed": unclosed,
        "stale": stale,
        "revised_bars": revised_bars,
    }


def data_status(
    bars: Sequence[Mapping[str, Any]],
    *,
    interval_seconds: int,
    now: datetime,
    source_label: str,
    open_gaps: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Thanh trạng thái: nguồn, số nến, nến đóng gần nhất, độ trễ ước tính, khoảng trống mở."""
    if not bars:
        return {
            "source_label": source_label,
            "bar_count": 0,
            "last_close_at": None,
            "delay_seconds": None,
            "delayed": None,
            "open_gaps": len(open_gaps),
            "gaps": list(open_gaps)[:5],
        }
    last_close = int(bars[-1]["time"]) + interval_seconds
    delay = max(0, int(now.timestamp()) - last_close)
    return {
        "source_label": source_label,
        "bar_count": len(bars),
        "last_close_at": _iso(last_close),
        # Trễ ước tính = thời gian từ lúc nến cuối đóng; > 1 khung nghĩa là thiếu ít nhất một nến.
        "delay_seconds": delay,
        "delayed": delay > interval_seconds,
        "open_gaps": len(open_gaps),
        "gaps": list(open_gaps)[:5],
    }
