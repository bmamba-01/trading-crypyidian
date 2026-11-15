"""Thẻ tín hiệu (SIG-3, lát 1): dựng từ candidate đã lưu, chỉ để hiển thị — không đặt lệnh.

Quy ước ``sig3-card-v1``:
- **Vùng vào** = từ giá tham chiếu (giá đóng nến tín hiệu) tới giá vào đã gồm trượt giá theo kế
  hoạch đã duyệt nếu có; không có kế hoạch thì vùng thu về đúng giá tham chiếu.
- **SL** theo kế hoạch đã duyệt nếu có, nếu không theo recipe: tham chiếu ∓ ATR × hệ số stop.
- **Mục tiêu**: mục tiêu của recipe (nếu recipe có chốt lời) và các mốc 1R/2R/3R **tham chiếu**
  để người đọc hình dung khoảng cách; mốc R không phải lệnh chốt — PAPER chưa chốt từng phần.
- **R** là bội số thô (chưa trừ phí); R:R ròng sau phí do planner kiểm (≥ 1,0) khi lập kế hoạch.
- **Độ tin cậy**: chưa có mô hình hiệu chuẩn (SIG-6) nên chỉ ghi "Chưa hiệu chuẩn", không hiện %.
- Thứ tự giá được kiểm theo hướng: LONG cần SL < vùng vào < các mục tiêu tăng dần; SHORT ngược lại.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

CARD_VERSION = "sig3-card-v1"
R_LADDER = (Decimal("1"), Decimal("2"), Decimal("3"))


class SignalCardError(ValueError):
    """Dữ liệu không đủ hoặc thứ tự giá sai hướng; thẻ không được hiển thị như hợp lệ."""


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise SignalCardError(f"{name} không phải số.") from error
    if not result.is_finite():
        raise SignalCardError(f"{name} không hữu hạn.")
    return result


def validate_price_ladder(
    direction: str,
    *,
    entry_low: Decimal,
    entry_high: Decimal,
    stop: Decimal,
    targets: Sequence[Decimal],
) -> None:
    """Chặn thẻ có SL/TP sai phía (ví dụ lệnh bán mà TP3 nằm trên vùng vào)."""
    if entry_low > entry_high:
        raise SignalCardError("Vùng vào có cận dưới lớn hơn cận trên.")
    if direction == "long":
        if not stop < entry_low:
            raise SignalCardError("LONG: SL phải thấp hơn vùng vào.")
        previous = entry_high
        for target in targets:
            if not target > previous:
                raise SignalCardError("LONG: mục tiêu phải cao hơn vùng vào và tăng dần.")
            previous = target
    elif direction == "short":
        if not stop > entry_high:
            raise SignalCardError("SHORT: SL phải cao hơn vùng vào.")
        previous = entry_low
        for target in targets:
            if not target < previous:
                raise SignalCardError("SHORT: mục tiêu phải thấp hơn vùng vào và giảm dần.")
            previous = target
    else:
        raise SignalCardError("Hướng lệnh không hợp lệ.")


def _invalidation(recipe: Mapping[str, Any], stop: Decimal, expires_at: str | None) -> list[str]:
    lines = [f"Giá chạm SL {stop.normalize():f} (dừng lỗ theo recipe/kế hoạch)."]
    if expires_at:
        lines.append("Tín hiệu hết hạn nếu chưa vào lệnh trước mốc hết hạn ghi ở đầu thẻ.")
    if recipe.get("trailing_atr_multiple"):
        lines.append(f"Dời stop theo trailing {recipe['trailing_atr_multiple']}×ATR khi có lãi.")
    if recipe.get("opposite_signal_exit"):
        lines.append("Thoát khi có tín hiệu ngược chiều của cùng chiến lược.")
    if recipe.get("exit_channel_period"):
        lines.append(
            f"Thoát khi giá đóng phá kênh {recipe['exit_channel_period']} nến ngược hướng."
        )
    if recipe.get("exit_feature_name"):
        lines.append(
            f"Thoát khi {recipe['exit_feature_name']} {recipe.get('exit_feature_operator', '')} "
            f"{recipe.get('exit_feature_threshold')}."
        )
    if recipe.get("max_hold_bars"):
        lines.append(f"Thoát theo thời gian sau tối đa {recipe['max_hold_bars']} nến.")
    return lines


def build_signal_card(
    candidate: Mapping[str, Any],
    *,
    checklist: Sequence[Mapping[str, Any]] = (),
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Dựng thẻ tín hiệu từ ``bot_decision.payload.candidate`` (và kế hoạch đã duyệt nếu có)."""
    direction = str(candidate.get("direction", ""))
    reference = _decimal(candidate.get("reference_price"), "reference_price")
    atr = _decimal(candidate.get("atr_value"), "atr_value")
    recipe = candidate.get("recipe") or {}
    stop_multiple = _decimal(recipe.get("stop_atr_multiple"), "stop_atr_multiple")
    sign = Decimal("1") if direction == "long" else Decimal("-1")
    if plan is not None:
        entry = _decimal(plan.get("entry_price"), "entry_price")
        stop = _decimal(plan.get("stop_price"), "stop_price")
    else:
        entry = reference
        stop = reference - sign * atr * stop_multiple
    entry_low, entry_high = sorted((reference, entry))
    risk = abs(entry - stop)
    if risk <= 0:
        raise SignalCardError("Khoảng rủi ro bằng 0.")
    targets: list[dict[str, Any]] = []
    recipe_target: Decimal | None = None
    if plan is not None and plan.get("target_price") is not None:
        recipe_target = _decimal(plan["target_price"], "target_price")
    elif recipe.get("take_profit_atr_multiple") is not None:
        recipe_target = reference + sign * atr * _decimal(
            recipe["take_profit_atr_multiple"], "take_profit_atr_multiple"
        )
    ladder = [
        (f"Mốc {int(r)}R (tham chiếu)", entry + sign * risk * r, "r_ladder") for r in R_LADDER
    ]
    if recipe_target is not None:
        ladder.append(("Mục tiêu recipe", recipe_target, "recipe"))
    ladder.sort(key=lambda item: item[1] * sign)
    for label, price, source in ladder:
        targets.append(
            {
                "label": label,
                "price": float(price),
                "r_multiple": float(((price - entry) * sign / risk).quantize(Decimal("0.01"))),
                "source": source,
            }
        )
    deduplicated: list[dict[str, Any]] = []
    for target in targets:
        if deduplicated and abs(deduplicated[-1]["price"] - target["price"]) < 1e-12:
            if target["source"] == "recipe":
                deduplicated[-1] = target
            continue
        deduplicated.append(target)
    validate_price_ladder(
        direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        targets=[Decimal(str(t["price"])) for t in deduplicated],
    )
    items = [dict(item) for item in checklist]
    return {
        "card_version": CARD_VERSION,
        "symbol": candidate.get("symbol"),
        "direction": direction,
        "strategy_id": candidate.get("strategy_id"),
        "strategy_release_id": candidate.get("strategy_release_id"),
        "reason_code": candidate.get("reason_code"),
        "signal_at": candidate.get("signal_at"),
        "expires_at": candidate.get("expires_at"),
        "reference_price": float(reference),
        "entry_zone": {"low": float(entry_low), "high": float(entry_high)},
        "stop": float(stop),
        "risk_per_unit": float(risk),
        "targets": deduplicated,
        "plan_source": "approved_plan" if plan is not None else "recipe",
        "checklist": items,
        "checklist_passed": sum(1 for item in items if item.get("passed") is True),
        "checklist_total": len(items),
        "invalidation": _invalidation(recipe, stop, candidate.get("expires_at")),
        "confidence": {"tier": "uncalibrated", "label": "Chưa hiệu chuẩn"},
    }
