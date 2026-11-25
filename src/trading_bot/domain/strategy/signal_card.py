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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_bot.domain.strategy.signal_calibration import SignalCalibrationReport

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


def _price_vi(value: Decimal) -> str:
    """Giá kiểu Việt Nam (nhóm nghìn bằng dấu chấm, thập phân bằng dấu phẩy), bỏ số 0 thừa."""
    text = f"{value.normalize():f}"
    whole, _, fraction = text.partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    grouped = f"{int(digits):,}".replace(",", ".") if digits else "0"
    return f"{sign}{grouped}" + (f",{fraction}" if fraction else "")


def _invalidation(recipe: Mapping[str, Any], stop: Decimal, expires_at: str | None) -> list[str]:
    lines = [f"Giá chạm SL {_price_vi(stop)} (dừng lỗ theo recipe/kế hoạch)."]
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


def _fraction_pct_vi(fraction: Decimal) -> str:
    """Hiển thị tỷ lệ chốt lời dạng phần trăm gọn (ví dụ 0.5 → '50%', 0.333 → '33,3%')."""
    pct = (fraction * Decimal("100")).quantize(Decimal("0.1")).normalize()
    text = format(pct, "f").replace(".", ",")
    return f"{text}%"


def _structured_invalidation(
    candidate: Mapping[str, Any],
    recipe: Mapping[str, Any],
    stop: Decimal,
    expires_at: str | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Gộp điều kiện mất hiệu lực có cấu trúc từ candidate v2 với recipe/kế hoạch."""
    rules: list[dict[str, Any]] = []
    lines: list[str] = []
    seen_lines: set[str] = set()

    def _add(line: str, rule: dict[str, Any] | None = None) -> None:
        clean = line.strip()
        if not clean:
            return
        if clean not in seen_lines:
            seen_lines.add(clean)
            lines.append(clean)
        if rule is not None:
            rules.append(rule)

    raw_inv = candidate.get("invalidation")
    if isinstance(raw_inv, Sequence) and not isinstance(raw_inv, str):
        for item in raw_inv:
            if isinstance(item, Mapping) and item.get("detail_vi"):
                _add(str(item["detail_vi"]), dict(item))
            elif isinstance(item, str) and item.strip():
                _add(item, {"kind": "CUSTOM", "detail_vi": item.strip()})

    has_stop = any(r.get("kind") == "STOP_LOSS" for r in rules)
    has_expiry = any(r.get("kind") == "EXPIRY" for r in rules)
    has_max_hold = any(r.get("kind") == "MAX_HOLD_BARS" for r in rules)

    if not has_stop:
        stop_line = f"Giá chạm SL {_price_vi(stop)} (dừng lỗ theo recipe/kế hoạch)."
        _add(stop_line, {"kind": "STOP_LOSS", "detail_vi": stop_line, "price": str(stop)})
    if expires_at and not has_expiry:
        exp_line = "Tín hiệu hết hạn nếu chưa vào lệnh trước mốc hết hạn ghi ở đầu thẻ."
        _add(exp_line, {"kind": "EXPIRY", "detail_vi": exp_line, "expires_at": expires_at})
    if recipe.get("trailing_atr_multiple"):
        line = f"Dời stop theo trailing {recipe['trailing_atr_multiple']}×ATR khi có lãi."
        _add(line, {"kind": "TRAILING_STOP", "detail_vi": line})
    if recipe.get("opposite_signal_exit"):
        line = "Thoát khi có tín hiệu ngược chiều của cùng chiến lược."
        _add(line, {"kind": "OPPOSITE_SIGNAL", "detail_vi": line})
    if recipe.get("exit_channel_period"):
        line = f"Thoát khi giá đóng phá kênh {recipe['exit_channel_period']} nến ngược hướng."
        _add(line, {"kind": "CHANNEL_BREAK", "detail_vi": line})
    if recipe.get("exit_feature_name"):
        line = (
            f"Thoát khi {recipe['exit_feature_name']} {recipe.get('exit_feature_operator', '')} "
            f"{recipe.get('exit_feature_threshold')}."
        )
        _add(line, {"kind": "FEATURE_THRESHOLD", "detail_vi": line})
    if recipe.get("max_hold_bars") and not has_max_hold:
        line = f"Thoát theo thời gian sau tối đa {recipe['max_hold_bars']} nến."
        _add(
            line,
            {
                "kind": "MAX_HOLD_BARS",
                "detail_vi": line,
                "bars": int(recipe["max_hold_bars"]),
            },
        )
    return lines, rules


def _confidence_from_tier(tier: str) -> dict[str, Any]:
    """Ánh xạ hạng tin cậy có cấu trúc (không hiện % khi chưa có báo cáo hiệu chuẩn SIG-6)."""
    clean = tier.strip()
    lower = clean.lower()
    if lower == "uncalibrated":
        return {"tier": "uncalibrated", "label": "Chưa hiệu chuẩn"}
    labels = {
        "high": "Hạng Cao",
        "medium": "Hạng Trung bình",
        "low": "Hạng Thấp",
        "a": "Hạng A",
        "b": "Hạng B",
        "c": "Hạng C",
    }
    return {"tier": clean, "label": labels.get(lower, f"Hạng {clean}")}


def build_signal_card(
    candidate: Mapping[str, Any],
    *,
    checklist: Sequence[Mapping[str, Any]] = (),
    plan: Mapping[str, Any] | None = None,
    fee_rate: Decimal | float | None = None,
    account_risk_pct: Decimal | float | None = None,
    confidence: Mapping[str, Any] | None = None,
    calibration_report: SignalCalibrationReport | None = None,
    calibrated_probability: float | None = None,
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
        stop = (
            _decimal(candidate["stop_price"], "stop_price")
            if candidate.get("stop_price") is not None
            else reference - sign * atr * stop_multiple
        )

    zone_points: list[Decimal] = [reference, entry]
    if candidate.get("entry_zone_low") is not None and candidate.get("entry_zone_high") is not None:
        zone_points.append(_decimal(candidate["entry_zone_low"], "entry_zone_low"))
        zone_points.append(_decimal(candidate["entry_zone_high"], "entry_zone_high"))
    entry_low, entry_high = min(zone_points), max(zone_points)
    risk = abs(entry - stop)
    if risk <= 0:
        raise SignalCardError("Khoảng rủi ro bằng 0.")

    raw_fee = (
        fee_rate
        if fee_rate is not None
        else (
            plan.get("fee_rate")
            if plan is not None and plan.get("fee_rate") is not None
            else candidate.get("fee_rate")
        )
    )
    one_way_fee = _decimal(raw_fee, "fee_rate") if raw_fee is not None else None
    raw_risk_pct = (
        account_risk_pct
        if account_risk_pct is not None
        else (
            plan.get("account_risk_pct")
            if plan is not None and plan.get("account_risk_pct") is not None
            else candidate.get("account_risk_pct")
        )
    )
    resolved_risk_pct = (
        float(_decimal(raw_risk_pct, "account_risk_pct")) if raw_risk_pct is not None else None
    )

    def _compute_net_r(price: Decimal) -> float | None:
        if one_way_fee is not None and one_way_fee >= 0:
            net_reward = (price - entry) * sign - (entry + price) * one_way_fee
            net_risk = risk + (entry + stop) * one_way_fee
            if net_risk > 0:
                return float((net_reward / net_risk).quantize(Decimal("0.01")))
        return None

    targets: list[dict[str, Any]] = []
    has_take_profit_ladder = False
    raw_plan_targets = plan.get("targets") if isinstance(plan, Mapping) else None
    raw_cand_targets = candidate.get("targets")

    if (
        isinstance(raw_plan_targets, Sequence)
        and not isinstance(raw_plan_targets, str)
        and len(raw_plan_targets) > 0
    ):
        has_take_profit_ladder = True
        for idx, raw_t in enumerate(raw_plan_targets, start=1):
            if not isinstance(raw_t, Mapping):
                continue
            price = _decimal(raw_t.get("price"), f"plan.targets[{idx}].price")
            target_idx = int(raw_t.get("index", idx))
            fraction = (
                _decimal(raw_t["fraction"], f"plan.targets[{idx}].fraction")
                if raw_t.get("fraction") is not None
                else None
            )
            gross_r = (
                _decimal(raw_t["r_multiple"], f"plan.targets[{idx}].r_multiple").quantize(
                    Decimal("0.01")
                )
                if raw_t.get("r_multiple") is not None
                else ((price - entry) * sign / risk).quantize(Decimal("0.01"))
            )
            raw_net_r = raw_t.get("net_reward_to_risk", raw_t.get("net_r_multiple"))
            net_r = (
                float(_decimal(raw_net_r, f"plan.targets[{idx}].net_r").quantize(Decimal("0.01")))
                if raw_net_r is not None
                else _compute_net_r(price)
            )
            label = (
                str(raw_t["label"])
                if raw_t.get("label")
                else (
                    f"TP{target_idx} ({_fraction_pct_vi(fraction)})"
                    if fraction is not None
                    else f"TP{target_idx}"
                )
            )
            targets.append(
                {
                    "label": label,
                    "price": float(price),
                    "r_multiple": float(gross_r),
                    "net_r_multiple": net_r,
                    "close_fraction": float(fraction) if fraction is not None else None,
                    "stop_to_entry_after": bool(raw_t.get("stop_to_entry_after", False)),
                    "source": "plan_ladder",
                }
            )
        targets.sort(key=lambda item: Decimal(str(item["price"])) * sign)
    elif (
        isinstance(raw_cand_targets, Sequence)
        and not isinstance(raw_cand_targets, str)
        and len(raw_cand_targets) > 1
    ):
        has_take_profit_ladder = True
        for idx, raw_t in enumerate(raw_cand_targets, start=1):
            if not isinstance(raw_t, Mapping):
                continue
            price = _decimal(raw_t.get("price"), f"candidate.targets[{idx}].price")
            target_idx = int(raw_t.get("index", idx))
            fraction = (
                _decimal(raw_t["fraction"], f"candidate.targets[{idx}].fraction")
                if raw_t.get("fraction") is not None
                else None
            )
            gross_r = (
                _decimal(raw_t["r_multiple"], f"candidate.targets[{idx}].r_multiple").quantize(
                    Decimal("0.01")
                )
                if raw_t.get("r_multiple") is not None
                else ((price - entry) * sign / risk).quantize(Decimal("0.01"))
            )
            label = (
                str(raw_t["label"])
                if raw_t.get("label")
                else (
                    f"TP{target_idx} ({_fraction_pct_vi(fraction)})"
                    if fraction is not None
                    else f"TP{target_idx}"
                )
            )
            targets.append(
                {
                    "label": label,
                    "price": float(price),
                    "r_multiple": float(gross_r),
                    "net_r_multiple": _compute_net_r(price),
                    "close_fraction": float(fraction) if fraction is not None else None,
                    "stop_to_entry_after": bool(raw_t.get("stop_to_entry_after", False)),
                    "source": "candidate_ladder",
                }
            )
        targets.sort(key=lambda item: Decimal(str(item["price"])) * sign)
    else:
        recipe_target: Decimal | None = None
        if plan is not None and plan.get("target_price") is not None:
            recipe_target = _decimal(plan["target_price"], "target_price")
        elif (
            isinstance(raw_cand_targets, Sequence)
            and not isinstance(raw_cand_targets, str)
            and len(raw_cand_targets) == 1
            and isinstance(raw_cand_targets[0], Mapping)
            and raw_cand_targets[0].get("price") is not None
        ):
            recipe_target = _decimal(raw_cand_targets[0]["price"], "candidate.targets[0].price")
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
        plan_net_rr = (
            float(
                _decimal(plan["net_reward_to_risk"], "net_reward_to_risk").quantize(Decimal("0.01"))
            )
            if plan is not None and plan.get("net_reward_to_risk") is not None
            else None
        )
        for label, price, source in ladder:
            gross_r = ((price - entry) * sign / risk).quantize(Decimal("0.01"))
            net_r = (
                plan_net_rr
                if (source == "recipe" and plan_net_rr is not None)
                else _compute_net_r(price)
            )
            targets.append(
                {
                    "label": label,
                    "price": float(price),
                    "r_multiple": float(gross_r),
                    "net_r_multiple": net_r,
                    "source": source,
                }
            )

    deduplicated: list[dict[str, Any]] = []
    for target in targets:
        if deduplicated and abs(deduplicated[-1]["price"] - target["price"]) < 1e-12:
            if target["source"] in {"recipe", "plan_ladder", "candidate_ladder"}:
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
    raw_conf = confidence if confidence is not None else candidate.get("confidence")
    raw_prob = (
        calibrated_probability
        if calibrated_probability is not None
        else candidate.get("calibrated_probability")
    )
    if calibration_report is not None or raw_prob is not None:
        from trading_bot.domain.strategy.signal_calibration import format_calibrated_confidence

        prob_val = float(raw_prob) if raw_prob is not None else None
        resolved_conf = format_calibrated_confidence(prob_val, calibration_report)
    elif isinstance(raw_conf, Mapping) and raw_conf.get("label"):
        resolved_conf = dict(raw_conf)
    elif isinstance(candidate.get("confidence_tier"), str) and candidate["confidence_tier"].strip():
        resolved_conf = _confidence_from_tier(candidate["confidence_tier"])
    else:
        resolved_conf = {"tier": "uncalibrated", "label": "Chưa hiệu chuẩn"}
    snapshot_id = candidate.get("feature_snapshot_id") or (
        plan.get("feature_snapshot_id") if plan is not None else None
    )
    invalidation_lines, invalidation_rules = _structured_invalidation(
        candidate, recipe, stop, candidate.get("expires_at")
    )
    return {
        "card_version": CARD_VERSION,
        "symbol": candidate.get("symbol"),
        "direction": direction,
        "strategy_id": candidate.get("strategy_id"),
        "strategy_release_id": candidate.get("strategy_release_id"),
        "setup_id": candidate.get("setup_id"),
        "reason_code": candidate.get("reason_code"),
        "feature_snapshot_id": snapshot_id,
        "signal_at": candidate.get("signal_at"),
        "expires_at": candidate.get("expires_at"),
        "reference_price": float(reference),
        "entry_zone": {"low": float(entry_low), "high": float(entry_high)},
        "stop": float(stop),
        "risk_per_unit": float(risk),
        "account_risk_pct": resolved_risk_pct,
        "targets": deduplicated,
        "has_take_profit_ladder": has_take_profit_ladder,
        "plan_source": "approved_plan" if plan is not None else "recipe",
        "checklist": items,
        "checklist_passed": sum(1 for item in items if item.get("passed") is True),
        "checklist_total": len(items),
        "invalidation": invalidation_lines,
        "invalidation_rules": invalidation_rules,
        "confidence": resolved_conf,
    }
