"""Danh sách lựa chọn cho các trường cấu hình trên Admin Web.

Mục tiêu: người dùng chọn từ danh sách thay vì gõ tay. Mọi giá trị vẫn được máy chủ kiểm lại theo
lược đồ gốc (overlay bot, bộ lọc tín hiệu, form backtest); danh sách ở đây chỉ là gợi ý an toàn.
Giá trị đã lưu nhưng không còn trong danh mục vẫn được giữ và đánh dấu, không bị bỏ âm thầm.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import get_args

from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.config.workspace_settings import Timeframe

COIN_CATALOG_PATH = (
    Path(__file__).parents[1] / "static" / "data" / "binance-usdm-perpetual-usdt.json"
)
TIMEFRAMES: tuple[str, ...] = tuple(get_args(Timeframe))
DIRECTIONS: tuple[tuple[str, str], ...] = (
    ("LONG", "Mua lên (LONG)"),
    ("SHORT", "Bán xuống (SHORT)"),
)
# Mã setup của ứng viên không đến từ rule H0x (`{strategy_id}_{LONG|SHORT}_ENTRY`).
EXTRA_SETUP_CODES: tuple[str, ...] = ("QUANTSMART_L6_ZONE_CONFIRMED",)

# Mốc gợi ý. Overlay bot chỉ được siết chặt hơn cấu hình gốc; máy chủ từ chối giá trị vượt gốc.
RISK_FRACTION_PRESETS: tuple[str, ...] = ("0.0025", "0.005", "0.0075", "0.01", "0.015", "0.02")
OPEN_RISK_FRACTION_PRESETS: tuple[str, ...] = ("0.01", "0.02", "0.03", "0.05", "0.1", "0.2")
CONCENTRATION_FRACTION_PRESETS: tuple[str, ...] = ("0.1", "0.2", "0.25", "0.33", "0.5", "1")
AMOUNT_PRESETS: tuple[str, ...] = (
    "100",
    "250",
    "500",
    "1000",
    "2500",
    "5000",
    "10000",
    "25000",
    "50000",
)
LEVERAGE_PRESETS: tuple[str, ...] = ("1", "2", "3", "5", "10", "20")
MAX_POSITION_PRESETS: tuple[str, ...] = tuple(str(value) for value in range(1, 11))
BOT_MAX_NOTIONAL_PRESETS: tuple[str, ...] = (
    "1000",
    "2500",
    "5000",
    "10000",
    "25000",
    "50000",
    "100000",
)

# Form backtest: vốn theo tài sản định giá, các tỷ lệ là số thập phân (0,01 = 1%).
BACKTEST_EQUITY_PRESETS: tuple[str, ...] = ("1000", "5000", "10000", "25000", "50000", "100000")
BACKTEST_RISK_PRESETS: tuple[str, ...] = (
    "0.0025",
    "0.005",
    "0.01",
    "0.015",
    "0.02",
    "0.025",
    "0.03",
    "0.05",
)
BACKTEST_FEE_PRESETS: tuple[str, ...] = ("0", "0.0002", "0.0004", "0.0005", "0.001", "0.002")
BACKTEST_SLIPPAGE_PRESETS: tuple[str, ...] = ("0", "0.0001", "0.0002", "0.0005", "0.001", "0.002")


@dataclass(frozen=True, slots=True)
class Choice:
    """Một option của `<select>`; `data` thành thuộc tính `data-<khóa>` cho Tom Select."""

    value: str
    label: str
    selected: bool = False
    data: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RiskField:
    """Một trường rủi ro của overlay bot hiển thị dưới dạng ô chọn."""

    name: str
    label: str
    help_vi: str
    choices: tuple[Choice, ...]


def percent_label(value: str) -> str:
    """Hiển thị tỷ lệ thập phân dưới dạng phần trăm kiểu Việt Nam, ví dụ 0.005 → 0,5%."""
    percent = (Decimal(value) * 100).normalize()
    text = format(percent, "f")
    return f"{text.replace('.', ',')}%"


def number_label(value: str) -> str:
    """Số nguyên có dấu chấm ngăn nghìn, ví dụ 10000 → 10.000."""
    integer = int(Decimal(value))
    return f"{integer:,}".replace(",", ".")


@lru_cache(maxsize=1)
def coin_catalog() -> tuple[tuple[str, str, str], ...]:
    """(mã, tên, biểu tượng) từ danh mục perpetual USDT đã lưu cùng ứng dụng."""
    payload = json.loads(COIN_CATALOG_PATH.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    for entry in payload.get("symbols", ()):
        symbol = str(entry.get("symbol") or "")
        if symbol:
            rows.append((symbol, str(entry.get("name") or ""), str(entry.get("icon") or "")))
    return tuple(rows)


def _merge(
    options: Iterable[tuple[str, str, dict[str, str] | None]], selected: Sequence[str]
) -> tuple[Choice, ...]:
    chosen = set(selected)
    seen: set[str] = set()
    result: list[Choice] = []
    for value, label, data in options:
        seen.add(value)
        result.append(Choice(value=value, label=label, selected=value in chosen, data=data))
    # Giữ giá trị đã lưu nhưng không còn trong danh mục để lưu lại không làm mất dữ liệu.
    for value in selected:
        if value not in seen:
            seen.add(value)
            result.append(
                Choice(value=value, label=f"{value} (ngoài danh mục)", selected=True, data=None)
            )
    return tuple(result)


def coin_choices(
    selected: Sequence[str] = (), *, only: Sequence[str] | None = None
) -> tuple[Choice, ...]:
    """Mã coin; `only` giới hạn trong tập cho phép (ví dụ universe của cấu hình gốc)."""
    allowed = set(only) if only is not None else None
    rows = (
        (symbol, f"{symbol} · {name}" if name else symbol, _coin_data(name, icon))
        for symbol, name, icon in coin_catalog()
        if allowed is None or symbol in allowed
    )
    extra = (
        (symbol, symbol, None)
        for symbol in (only or ())
        if symbol not in {row[0] for row in coin_catalog()}
    )
    return _merge((*rows, *extra), selected)


def _coin_data(name: str, icon: str) -> dict[str, str]:
    data: dict[str, str] = {}
    if name:
        data["name"] = name
    if icon:
        data["icon"] = icon
    return data


def timeframe_choices(
    selected: Sequence[str] = (), *, only: Sequence[str] | None = None
) -> tuple[Choice, ...]:
    values = tuple(only) if only is not None else TIMEFRAMES
    return _merge(((value, value, None) for value in values), selected)


def direction_choices(selected: Sequence[str] = ()) -> tuple[Choice, ...]:
    return _merge(((value, label, None) for value, label in DIRECTIONS), selected)


def strategy_id_choices(selected: Sequence[str] = ()) -> tuple[Choice, ...]:
    names: dict[str, str] = {}
    for release in strategy_registry().list_releases():
        names.setdefault(release.strategy_id, release.name_vi)
    return _merge(
        ((strategy_id, f"{strategy_id} · {name}", None) for strategy_id, name in names.items()),
        selected,
    )


def setup_code_choices(selected: Sequence[str] = ()) -> tuple[Choice, ...]:
    codes: list[str] = []
    for release in strategy_registry().list_releases():
        for direction in ("LONG", "SHORT"):
            code = f"{release.strategy_id}_{direction}_ENTRY"
            if code not in codes:
                codes.append(code)
    codes.extend(code for code in EXTRA_SETUP_CODES if code not in codes)
    return _merge(((code, code, None) for code in codes), selected)


def release_choices(selected: str = "", *, inherit_label: str) -> tuple[Choice, ...]:
    """Release chiến lược; option rỗng nghĩa là kế thừa cấu hình gốc."""
    options = [("", inherit_label, None)] + [
        (release.release_id, f"{release.name_vi} ({release.release_id})", None)
        for release in strategy_registry().list_releases()
    ]
    return _merge(options, (selected,))


def preset_choices(
    presets: Sequence[str],
    selected: str,
    *,
    formatter: str = "number",
    empty_label: str | None = None,
) -> tuple[Choice, ...]:
    """Ô chọn một giá trị từ mốc có sẵn; `empty_label` thêm option rỗng (kế thừa)."""
    label_of: Callable[[str], str] = {
        "percent": percent_label,
        "plain": lambda value: value,
    }.get(formatter, number_label)
    options: list[tuple[str, str, dict[str, str] | None]] = []
    if empty_label is not None:
        options.append(("", empty_label, None))
    options.extend((value, label_of(value), None) for value in presets)
    normalized = _normalize_selected(selected, presets)
    return _merge(options, (normalized,) if normalized or empty_label is not None else ())


def _normalize_selected(selected: str, presets: Sequence[str]) -> str:
    """Khớp `0.010` với mốc `0.01` để giá trị đã lưu được chọn đúng."""
    if not selected:
        return ""
    try:
        target = Decimal(selected)
    except ArithmeticError:
        return selected
    for value in presets:
        if Decimal(value) == target:
            return value
    return selected


RISK_FIELD_SPECS: tuple[tuple[str, str, str, Sequence[str], str], ...] = (
    (
        "per_trade_risk_fraction",
        "Rủi ro mỗi lệnh",
        "Phần vốn tối đa chấp nhận mất cho một lệnh.",
        RISK_FRACTION_PRESETS,
        "percent",
    ),
    (
        "bot_max_open_risk_fraction",
        "Tổng rủi ro mở của bot",
        "Phần vốn tối đa đang chịu rủi ro cùng lúc trên mọi vị thế của bot.",
        OPEN_RISK_FRACTION_PRESETS,
        "percent",
    ),
    (
        "max_concentration_fraction",
        "Tỷ trọng tối đa một mã",
        "Phần rủi ro tối đa dồn vào một cặp giao dịch.",
        CONCENTRATION_FRACTION_PRESETS,
        "percent",
    ),
    (
        "max_positions",
        "Số vị thế mở tối đa",
        "Số vị thế được mở cùng lúc.",
        MAX_POSITION_PRESETS,
        "plain",
    ),
    (
        "max_notional",
        "Giá trị danh nghĩa tối đa",
        "Tổng giá trị danh nghĩa (notional) của bot, theo tài sản định giá.",
        AMOUNT_PRESETS,
        "number",
    ),
    (
        "max_symbol_notional",
        "Danh nghĩa tối đa mỗi mã",
        "Giá trị danh nghĩa tối đa trên một cặp giao dịch.",
        AMOUNT_PRESETS,
        "number",
    ),
    (
        "max_leverage",
        "Đòn bẩy tối đa",
        "Đòn bẩy cao nhất bot được dùng.",
        LEVERAGE_PRESETS,
        "plain",
    ),
    (
        "max_aggregate_open_risk",
        "Tổng rủi ro mở (số tiền)",
        "Số tiền tối đa đang chịu rủi ro cùng lúc.",
        AMOUNT_PRESETS,
        "number",
    ),
    (
        "max_daily_loss",
        "Lỗ tối đa trong ngày",
        "Lỗ ròng đã chốt tối đa từ 00:00 UTC.",
        AMOUNT_PRESETS,
        "number",
    ),
    (
        "max_rolling_loss",
        "Lỗ tối đa 24 giờ trượt",
        "Lỗ ròng đã chốt tối đa trong 24 giờ gần nhất.",
        AMOUNT_PRESETS,
        "number",
    ),
)


def risk_fields(current: dict[str, object] | None = None) -> tuple[RiskField, ...]:
    """Các trường rủi ro của overlay; option rỗng là kế thừa cấu hình gốc."""
    current = current or {}
    fields: list[RiskField] = []
    for name, label, help_vi, presets, formatter in RISK_FIELD_SPECS:
        raw = current.get(name)
        selected = "" if raw is None else str(raw)
        fields.append(
            RiskField(
                name=name,
                label=label,
                help_vi=help_vi,
                choices=preset_choices(
                    presets, selected, formatter=formatter, empty_label="Kế thừa cấu hình gốc"
                ),
            )
        )
    return tuple(fields)
