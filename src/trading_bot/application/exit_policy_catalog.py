"""Bảng chỉ đọc chính sách thoát lệnh (SL/trailing/TP/giữ tối đa) theo từng strategy release.

CRY-428: owner cần biết mỗi release đặt stop-loss (SL) và take-profit (TP) ra sao. Giá trị lấy
thẳng từ mã đã ghim hash của release, không có bản sao thủ công:

- Rule có ``recipe`` (``ExitRecipe``): H01, H03 v1/v2/v3.
- Rule có ``recipe_manifest`` dạng cặp (tên, giá trị): H06 v1/v2, scalping H18–H20.
- Thang chốt lời theo bội số R lấy từ ``ladder_for_release`` (SIG-3/SIG-4).
- Strategy Card: SL/TP do vùng giá L6 của thẻ quyết định ở từng tín hiệu rồi quy đổi sang ATR,
  nên không có bội số cố định.

Ví dụ quy đổi ra phần trăm giá dùng ATR(14) của khung vào lệnh tại một mốc giá:
``% = bội số × ATR / giá × 100``. 1R = khoảng cách vào lệnh→stop, nên TP theo R chỉ quy đổi
được khi stop là bội số ATR cố định. Bảng hiện giá trị gốc của release; đổi ATR multiple
hay thang TP của **release** là đổi release (hash mới). Từ 17/11/2026 owner cho ghi đè theo bot
(``ExitOverride``, overlay ``exit``) với trường release vốn có, bắt buộc backtest trước publish.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import entry_timeframe_for_release
from trading_bot.domain.strategy.exit_override import ExitOverride
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.registry import StrategyRegistry
from trading_bot.domain.strategy.scalping_rules import ladder_for_release

_HUNDRED = Decimal("100")
_TIMEFRAME_SPANS: Mapping[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}
_STRUCTURAL_STOPS_VI: Mapping[str, str] = {
    "wick_extreme_plus_0.1_atr": "Sau đỉnh/đáy râu quét + 0,1 ATR (theo cấu trúc)",
    "opposite_confirmed_swing": "Tại swing đối diện đã xác nhận (theo cấu trúc)",
}


@dataclass(frozen=True, slots=True)
class LadderStepView:
    r_multiple: Decimal
    fraction: Decimal


@dataclass(frozen=True, slots=True)
class ExitPolicyView:
    """Chính sách thoát của một release, đã chuẩn hóa để hiển thị."""

    release_id: str
    strategy_id: str
    name_vi: str
    timeframe: str | None
    stop_atr_multiple: Decimal | None
    stop_label_vi: str
    trailing_atr_multiple: Decimal | None
    take_profit_atr_multiple: Decimal | None
    take_profit_label_vi: str
    ladder: tuple[LadderStepView, ...] = ()
    breakeven_after_first: bool = False
    other_exits_vi: tuple[str, ...] = ()
    max_hold_bars: int | None = None
    max_hold_label_vi: str = "—"
    source: str = "recipe"
    notes_vi: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PercentExample:
    """Ví dụ quy đổi % theo ATR hiện tại; None = không quy đổi được (stop theo cấu trúc…)."""

    stop_pct: Decimal | None
    trailing_pct: Decimal | None
    take_profit_pct: Decimal | None
    ladder_pcts: tuple[Decimal, ...] = ()


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str, Decimal)):
        return Decimal(str(value))
    return None


def _hold_label(bars: int | None, timeframe: str | None) -> str:
    if bars is None:
        return "—"
    if timeframe is None or timeframe not in _TIMEFRAME_SPANS:
        return f"{bars} nến"
    total = _TIMEFRAME_SPANS[timeframe] * bars
    hours = total.total_seconds() / 3600
    approx = f"{hours:g} giờ" if hours < 48 else f"{hours / 24:g} ngày"
    return f"{bars} nến {timeframe} (≈ {approx})"


def _from_recipe(recipe: ExitRecipe) -> dict[str, Any]:
    others: list[str] = []
    if recipe.opposite_signal_exit:
        # Monitor PAPER hiện luôn truyền opposing_signal=False (giới hạn đã ghi ở RT-1).
        others.append("Thoát khi có tín hiệu ngược chiều (runtime PAPER chưa áp)")
    if recipe.exit_channel_period:
        others.append(f"Thoát khi giá chạm biên ngược kênh {recipe.exit_channel_period} nến")
    if recipe.exit_feature_name:
        others.append(
            f"Thoát khi {recipe.exit_feature_name} {recipe.exit_feature_operator} "
            f"{recipe.exit_feature_threshold}"
        )
    return {
        "stop": _decimal(recipe.stop_atr_multiple),
        "stop_text": None,
        "trailing": _decimal(recipe.trailing_atr_multiple),
        "take_profit": _decimal(recipe.take_profit_atr_multiple),
        "max_hold_bars": recipe.max_hold_bars,
        "others": tuple(others),
    }


def _from_manifest(manifest: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    values = dict(manifest)
    others: list[str] = []
    feature = values.get("exit_feature_name")
    long_exit = values.get("long_exit")
    short_exit = values.get("short_exit")
    if feature and long_exit and short_exit:
        others.append(
            f"Thoát khi {feature} {long_exit[0]} {long_exit[1]} (LONG) / "
            f"{short_exit[0]} {short_exit[1]} (SHORT)"
        )
    stop_text = values.get("stop")
    return {
        "stop": _decimal(values.get("stop_atr_multiple")),
        "stop_text": _STRUCTURAL_STOPS_VI.get(str(stop_text), str(stop_text))
        if stop_text is not None
        else None,
        "trailing": _decimal(values.get("trailing_atr_multiple")),
        "take_profit": _decimal(values.get("take_profit_atr_multiple")),
        "max_hold_bars": values.get("max_hold_bars"),
        "others": tuple(others),
    }


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


def build_exit_policy_catalog(registry: StrategyRegistry | None = None) -> list[ExitPolicyView]:
    """Liệt kê chính sách thoát của mọi release trong catalog sản phẩm, theo release ID."""
    catalog = registry or strategy_registry()
    views: list[ExitPolicyView] = []
    for release in catalog.list_releases():
        rule = catalog.describe_rule(release.release_id)
        timeframe = entry_timeframe_for_release(release.release_id)
        recipe = getattr(rule, "recipe", None)
        manifest = getattr(rule, "recipe_manifest", None)
        if isinstance(recipe, ExitRecipe):
            parsed: dict[str, Any] | None = _from_recipe(recipe)
            source = "recipe"
        elif isinstance(manifest, (tuple, list)):
            parsed = _from_manifest(manifest)
            source = "manifest"
        else:
            parsed = None
            source = "strategy_card"
        if parsed is None:
            views.append(
                ExitPolicyView(
                    release_id=release.release_id,
                    strategy_id=release.strategy_id,
                    name_vi=release.name_vi,
                    timeframe=timeframe,
                    stop_atr_multiple=None,
                    stop_label_vi="Theo vùng giá L6 của thẻ, quy đổi sang ATR ở từng tín hiệu",
                    trailing_atr_multiple=None,
                    take_profit_atr_multiple=None,
                    take_profit_label_vi="Theo vùng giá L6 của thẻ (một mục tiêu)",
                    max_hold_bars=12,
                    max_hold_label_vi=_hold_label(12, timeframe),
                    source=source,
                    notes_vi=("Bội số thay đổi theo tín hiệu nên không có ví dụ % cố định.",),
                )
            )
            continue
        ladder = ladder_for_release(release.release_id)
        stop = parsed["stop"]
        stop_label = f"{_fmt(stop)} × ATR" if stop is not None else (parsed["stop_text"] or "—")
        steps = (
            tuple(LadderStepView(step.r_multiple, step.fraction) for step in ladder.steps)
            if ladder is not None
            else ()
        )
        take_profit = parsed["take_profit"]
        if steps:
            tp_label = " → ".join(
                f"{_fmt(step.r_multiple)}R chốt {_fmt(step.fraction * _HUNDRED)}%" for step in steps
            )
            if ladder is not None and ladder.breakeven_after_first:
                tp_label += "; sau bậc 1 dời stop về hòa vốn"
        elif take_profit is not None:
            tp_label = f"Tối đa {_fmt(take_profit)} × ATR"
        else:
            tp_label = "Không đặt TP cố định (thoát bằng trailing/kênh/thời gian)"
        hold = parsed["max_hold_bars"]
        hold_bars = int(hold) if isinstance(hold, int) else None
        views.append(
            ExitPolicyView(
                release_id=release.release_id,
                strategy_id=release.strategy_id,
                name_vi=release.name_vi,
                timeframe=timeframe,
                stop_atr_multiple=stop,
                stop_label_vi=stop_label,
                trailing_atr_multiple=parsed["trailing"],
                take_profit_atr_multiple=take_profit,
                take_profit_label_vi=tp_label,
                ladder=steps,
                breakeven_after_first=bool(ladder and ladder.breakeven_after_first),
                other_exits_vi=parsed["others"],
                max_hold_bars=hold_bars,
                max_hold_label_vi=_hold_label(hold_bars, timeframe),
                source=source,
            )
        )
    return views


def percent_example(policy: ExitPolicyView, *, atr: Decimal, price: Decimal) -> PercentExample:
    """Quy đổi bội số ATR/R ra % giá vào lệnh; ATR và giá phải dương."""
    if atr <= 0 or price <= 0 or not atr.is_finite() or not price.is_finite():
        raise ValueError("ATR và giá phải dương, hữu hạn.")
    atr_pct = atr / price * _HUNDRED

    def pct(multiple: Decimal | None) -> Decimal | None:
        return None if multiple is None else multiple * atr_pct

    stop_pct = pct(policy.stop_atr_multiple)
    ladder_pcts = (
        tuple(step.r_multiple * stop_pct for step in policy.ladder) if stop_pct is not None else ()
    )
    return PercentExample(
        stop_pct=stop_pct,
        trailing_pct=pct(policy.trailing_atr_multiple),
        take_profit_pct=pct(policy.take_profit_atr_multiple),
        ladder_pcts=ladder_pcts,
    )


def policy_for_release(
    release_id: str, registry: StrategyRegistry | None = None
) -> ExitPolicyView | None:
    """Chính sách thoát gốc của một release; None nếu release không có trong catalog."""
    return next(
        (view for view in build_exit_policy_catalog(registry) if view.release_id == release_id),
        None,
    )


def validate_exit_override(
    release_id: str, override: ExitOverride, registry: StrategyRegistry | None = None
) -> list[tuple[str, str]]:
    """Quyết định owner 17/11/2026: chỉ ghi đè trường release vốn có.

    Trả danh sách (trường, lý do) vi phạm; rỗng là hợp lệ. SL theo cấu trúc (H18/H20) và Strategy
    Card không có bội số SL cố định nên không chỉnh SL; release không có thang TP thì không thêm.
    """
    policy = policy_for_release(release_id, registry)
    if policy is None:
        return [("exit", "Release không có trong catalog chiến lược.")]
    issues: list[tuple[str, str]] = []
    checks = (
        ("stop_atr_multiple", policy.stop_atr_multiple, "SL của release không theo bội số ATR."),
        (
            "trailing_atr_multiple",
            policy.trailing_atr_multiple,
            "Release không dùng trailing stop.",
        ),
        (
            "take_profit_atr_multiple",
            policy.take_profit_atr_multiple,
            "Release không có TP theo bội số ATR.",
        ),
        ("max_hold_bars", policy.max_hold_bars, "Release không có giới hạn thời gian giữ."),
    )
    for name, base, message in checks:
        if getattr(override, name) is not None and base is None:
            issues.append((f"exit.{name}", message))
    if override.ladder is not None and not policy.ladder:
        issues.append(("exit.ladder", "Release không có thang TP theo R."))
    return issues
