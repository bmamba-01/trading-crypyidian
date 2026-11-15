"""Checklist điều kiện có cấu trúc cho H01/H03/H06 (SIG-3 / MI-1), chỉ để diễn giải.

Checklist đọc **cùng** ``DecisionSnapshot`` và ngưỡng của rule nhưng nằm ở module riêng: hash
release (``release_content_hash``) băm mã nguồn module rules, nên sửa rules để sinh checklist sẽ
đổi danh tính mọi release đang ghim. Test parity bảo đảm: rule ra candidate ⇔ mọi điều kiện thị
trường trong checklist đạt. Checklist không quyết định vào lệnh; rule vẫn là nguồn sự thật.

``passed`` là ``None`` khi thiếu dữ liệu hoặc điều kiện chưa áp dụng (ví dụ chưa biết hướng).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from trading_bot.domain.strategy.contracts import DecisionSnapshot, StrategySelection

CHECKLIST_VERSION = "sig3-checklist-v1"


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """Một điều kiện: giá trị thực, phép so, ngưỡng và kết quả."""

    key: str
    label_vi: str
    actual: str
    operator: str
    threshold: str
    passed: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label_vi": self.label_vi,
            "actual": self.actual,
            "operator": self.operator,
            "threshold": self.threshold,
            "passed": self.passed,
        }


def _fmt(value: float | Decimal | None, digits: int = 4) -> str:
    if value is None:
        return "thiếu dữ liệu"
    number = float(value)
    return f"{number:.{digits}f}".rstrip("0").rstrip(".") if abs(number) < 1e6 else f"{number:.0f}"


def _feature(snapshot: DecisionSnapshot, name: str, timeframe: str) -> Any:
    feature = snapshot.get_feature(name, timeframe)
    if feature is None or feature.value is None:
        return None
    return feature


def _h01(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    fast = _feature(snapshot, "ema_20", "1h")
    slow = _feature(snapshot, "ema_80", "1h")
    direction: str | None = None
    if (
        fast is not None
        and slow is not None
        and fast.previous_value is not None
        and slow.previous_value is not None
    ):
        if fast.previous_value <= slow.previous_value and fast.value > slow.value:
            direction = "long"
        elif fast.previous_value >= slow.previous_value and fast.value < slow.value:
            direction = "short"
        cross = ChecklistItem(
            "ema_cross",
            "EMA20 vừa cắt EMA80 (1h)",
            f"EMA20 {_fmt(fast.value)} / EMA80 {_fmt(slow.value)}; nến trước "
            f"{_fmt(fast.previous_value)} / {_fmt(slow.previous_value)}",
            "cắt",
            "EMA20 đổi phía so với EMA80",
            direction is not None,
        )
    else:
        cross = ChecklistItem(
            "ema_cross", "EMA20 vừa cắt EMA80 (1h)", "thiếu dữ liệu", "cắt", "—", None
        )
    items = [cross]
    h4 = snapshot.get_series("4h")
    ema200 = _feature(snapshot, "ema_200", "4h")
    if h4 is not None and h4.bars and ema200 is not None:
        close = h4.bars[-1].close
        side_ok = (
            None
            if direction is None
            else (
                Decimal(str(ema200.value)) < close
                if direction == "long"
                else Decimal(str(ema200.value)) > close
            )
        )
        items.append(
            ChecklistItem(
                "htf_trend",
                "Giá đóng 4h cùng phía EMA200 với hướng giao cắt",
                f"đóng 4h {_fmt(close, 2)} / EMA200 {_fmt(ema200.value, 2)}",
                ">" if direction == "long" else "<" if direction == "short" else "cùng phía",
                "EMA200 4h",
                side_ok,
            )
        )
    else:
        items.append(
            ChecklistItem("htf_trend", "Giá 4h cùng phía EMA200", "thiếu dữ liệu", "—", "—", None)
        )
    adx = _feature(snapshot, "adx_14", "1h")
    items.append(
        ChecklistItem(
            "adx",
            "ADX14 1h đủ mạnh",
            _fmt(adx.value if adx else None, 2),
            ">=",
            "20",
            None if adx is None else adx.value >= 20,
        )
    )
    return tuple(items)


def _h03(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    parameters = dict(settings.parameters)
    lookback = parameters.get("donchian_lookback", 55)
    volume_lookback = parameters.get("volume_median_lookback", 20)
    series = snapshot.get_series("1h")
    if series is None or len(series.bars) < lookback + 1:
        return (
            ChecklistItem(
                "donchian_breakout",
                f"Giá đóng phá kênh Donchian {lookback} nến (1h)",
                "thiếu dữ liệu",
                "phá",
                "—",
                None,
            ),
        )
    prior = series.bars[-(lookback + 1) : -1]
    current = series.bars[-1]
    high = max(bar.high for bar in prior)
    low = min(bar.low for bar in prior)
    broke_up, broke_down = current.close > high, current.close < low
    items = [
        ChecklistItem(
            "donchian_breakout",
            f"Giá đóng phá kênh Donchian {lookback} nến (1h)",
            f"đóng {_fmt(current.close, 2)}; kênh {_fmt(low, 2)} – {_fmt(high, 2)}",
            ">" if broke_up else "<" if broke_down else "ngoài kênh",
            _fmt(high, 2) if not broke_down else _fmt(low, 2),
            broke_up or broke_down,
        )
    ]
    volumes = series.quote_volumes
    if volumes is None or len(volumes) < volume_lookback + 1:
        items.append(
            ChecklistItem(
                "volume_above_median",
                f"Quote volume > median {volume_lookback} nến trước",
                "thiếu dữ liệu",
                ">",
                "—",
                None,
            )
        )
    else:
        reference = median(volumes[-(volume_lookback + 1) : -1])
        items.append(
            ChecklistItem(
                "volume_above_median",
                f"Quote volume > median {volume_lookback} nến trước",
                _fmt(volumes[-1], 0),
                ">",
                _fmt(reference, 0),
                volumes[-1] > reference,
            )
        )
    return tuple(items)


def _h06(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    def value(name: str) -> float | None:
        feature = _feature(snapshot, name, "15m")
        return None if feature is None else float(feature.value)

    adx, er, z, rsi = (value(n) for n in ("adx_14", "efficiency_ratio_40", "zscore_20", "rsi_14"))
    macro_ok = (
        None
        if snapshot.macro_blackout is None or snapshot.macro_is_fresh is None
        else (not snapshot.macro_blackout and snapshot.macro_is_fresh)
    )
    long_extreme = z is not None and rsi is not None and z < -2 and rsi < 30
    short_extreme = z is not None and rsi is not None and z > 2 and rsi > 70
    items = [
        ChecklistItem(
            "macro_clear",
            "Không trong khoảng cấm quanh sự kiện vĩ mô, lịch còn mới",
            "thiếu dữ liệu"
            if macro_ok is None
            else ("đang cấm" if snapshot.macro_blackout else "rõ"),
            "=",
            "không cấm",
            macro_ok,
        ),
        ChecklistItem(
            "range_adx",
            "ADX14 15m thấp (đi ngang)",
            _fmt(adx, 2),
            "<",
            "18",
            None if adx is None else adx < 18,
        ),
        ChecklistItem(
            "range_er",
            "Efficiency ratio 40 thấp",
            _fmt(er, 3),
            "<",
            "0.25",
            None if er is None else er < 0.25,
        ),
        ChecklistItem(
            "extreme",
            "Z20 và RSI14 cùng vượt ngưỡng",
            f"Z20 {_fmt(z, 2)} / RSI14 {_fmt(rsi, 1)}",
            "vượt",
            "Z < −2 & RSI < 30, hoặc Z > 2 & RSI > 70",
            None if z is None or rsi is None else (long_extreme or short_extreme),
        ),
    ]
    series = snapshot.get_series("15m")
    if series is not None and len(series.bars) >= 121 and (long_extreme or short_extreme):
        prior = series.bars[-121:-1]
        close = series.bars[-1].close
        floor, ceiling = min(bar.low for bar in prior), max(bar.high for bar in prior)
        safe = close >= floor if long_extreme else close <= ceiling
        items.append(
            ChecklistItem(
                "no_adverse_break",
                "Chưa phá biên 120 nến theo hướng bất lợi",
                f"đóng {_fmt(close, 2)}; biên {_fmt(floor, 2)} – {_fmt(ceiling, 2)}",
                ">=" if long_extreme else "<=",
                _fmt(floor if long_extreme else ceiling, 2),
                safe,
            )
        )
    return tuple(items)


_BUILDERS: dict[str, Callable[[DecisionSnapshot, StrategySelection], tuple[ChecklistItem, ...]]] = {
    "H01": _h01,
    "H03": _h03,
    "H06": _h06,
}


def strategy_checklist(
    release_id: str, snapshot: DecisionSnapshot, settings: StrategySelection
) -> tuple[ChecklistItem, ...]:
    """Checklist cho release ``h01-*``/``h03-*``/``h06-*``; release khác trả rỗng."""
    builder = _BUILDERS.get(release_id.split("-", 1)[0].upper())
    if builder is None:
        return ()
    return builder(snapshot, settings)
