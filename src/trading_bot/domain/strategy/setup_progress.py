"""Tiến độ setup k/n cho bảng Entry (CK-6), chỉ để diễn giải — không quyết định vào lệnh.

Đọc checklist **đã lưu** trong quyết định bot (SIG-3) và phân loại điều kiện ở tầng hiển thị,
nên payload/hash của quyết định cũ và mới không đổi. Quy ước `ck6-setup-v1`:

- Điều kiện **bắt buộc** = các điều kiện thị trường của rule (parity SIG-3: rule ra candidate ⇔
  mọi điều kiện này đạt). Thứ tự trong ``SetupRule.required`` là thứ tự ưu tiên của "Còn thiếu".
- Điều kiện **cộng điểm** = điều kiện ngữ cảnh tính tại thời điểm xem (killzone CK-3, cách mốc
  funding, lịch tin tức CK-10). Rule hiện tại không dùng chúng, nên ``bonus_k`` mặc định 0: chỉ
  hiện "x/n" để tham khảo cho tới khi owner chọn k và đưa vào rule (phải qua backtest).
- Trạng thái: ``ready`` (quyết định mới nhất là CANDIDATE còn hạn, đủ k cộng điểm),
  ``expired`` (CANDIDATE đã quá ``expires_at``), ``not_ready``, ``no_data``.
- "Thời điểm đạt" của điều kiện = quyết định cũ nhất trong chuỗi liên tục các quyết định gần
  nhất mà điều kiện đều đạt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_bot.domain.market.economic_calendar import news_condition
from trading_bot.domain.market.ict import active_killzones

SETUP_FORMULA_VERSION = "ck6-setup-v1"
FUNDING_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class SetupRule:
    strategy_id: str
    label_vi: str
    required: tuple[str, ...]
    bonus_k: int = 0


SETUP_RULES: dict[str, SetupRule] = {
    "H01": SetupRule("H01", "Giao cắt EMA theo xu hướng", ("ema_cross", "htf_trend", "adx")),
    "H02": SetupRule("H02", "Động lượng đa chân trời (TSMOM 4h)", ("rv_regime", "tsmom_horizon")),
    "H03": SetupRule("H03", "Phá biên Donchian", ("donchian_breakout", "volume_above_median")),
    "H04": SetupRule(
        "H04",
        "Nén biến động Bollinger–Keltner",
        ("prior_squeeze", "bw_expansion", "volume_surge", "band_breakout"),
    ),
    "H05": SetupRule(
        "H05",
        "Nhịp hồi xu hướng EMA20",
        ("htf_trend", "ema_alignment", "adx", "rsi_band", "pullback_reclaim"),
    ),
    "H06": SetupRule(
        "H06",
        "Đảo chiều trong biên",
        ("macro_clear", "range_adx", "range_er", "extreme", "no_adverse_break"),
    ),
    "H07": SetupRule(
        "H07",
        "Hồi quy Session VWAP",
        ("session_warmup", "range_regime", "vwap_deviation", "cost_hurdle"),
    ),
    "H21": SetupRule(
        "H21",
        "Động lượng MACD lọc theo ADX",
        ("htf_trend", "adx", "di_alignment", "macd_hist_cross"),
    ),
    "H22": SetupRule(
        "H22",
        "Phá vỡ dòng lệnh Taker-Flow",
        ("taker_flow_valid", "adx", "volume_surge", "taker_imbalance_breakout"),
    ),
    "H18": SetupRule(
        "H18",
        "Quét & lấy lại mức phiên/PDH/PDL (M5)",
        ("sweep_reclaim", "taker_flow_valid", "delta_reversed", "atr_positive"),
    ),
    "H19": SetupRule(
        "H19",
        "Hồi về VWAP phiên trong biên (M5)",
        ("range_regime", "session_bars", "session_volume", "vwap_extreme", "atr_positive"),
    ),
    "H20": SetupRule(
        "H20",
        "BOS & retest M5 thuận M15",
        ("enough_bars", "recent_bos", "htf_alignment", "break_delta_imbalance", "retest_level"),
    ),
}
STATUS_LABELS_VI = {
    "ready": "Đủ điều kiện",
    "expired": "Mất hiệu lực",
    "not_ready": "Chưa đủ",
    "no_data": "Chưa có quyết định",
}


def _condition(
    key: str, label: str, actual: str, operator: str, threshold: str, passed: bool | None
) -> dict[str, Any]:
    return {
        "key": key,
        "label_vi": label,
        "actual": actual,
        "operator": operator,
        "threshold": threshold,
        "passed": passed,
    }


def context_conditions(
    now: datetime,
    *,
    next_funding_time: datetime | None,
    calendar_events: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Điều kiện cộng điểm tính tại ``now``: killzone, cách mốc funding, lịch tin tức (CK-10).

    ``calendar_events`` = None nghĩa là không đọc được lịch nội bộ → "chưa xác định".
    """
    if now.tzinfo is None:
        raise ValueError("now phải có timezone")
    windows = active_killzones(now)
    killzone = _condition(
        "killzone",
        "Đang trong killzone ICT (giờ New York)",
        ", ".join(item.label_vi for item in windows) or "ngoài killzone",
        "trong",
        "killzone / Silver Bullet / macro",
        bool(windows),
    )
    if next_funding_time is None:
        funding = _condition(
            "funding_clear",
            "Cách mốc funding hơn 10 phút",
            "chưa có mark price",
            "≥",
            "10 phút",
            None,
        )
    else:
        gap = abs(next_funding_time - now)
        minutes = int(gap.total_seconds() // 60)
        funding = _condition(
            "funding_clear",
            "Cách mốc funding hơn 10 phút",
            f"{minutes // 60} giờ {minutes % 60} phút" if minutes >= 60 else f"{minutes} phút",
            "≥",
            "10 phút",
            gap > FUNDING_WINDOW,
        )
    news = (
        news_condition(calendar_events, now=now)
        if calendar_events is not None
        else _condition(
            "news",
            "Không có tin tác động cao trong ±30 phút",
            "không đọc được lịch sự kiện nội bộ",
            "không có",
            "tin tác động cao (lịch nội bộ)",
            None,
        )
    )
    return (killzone, funding, news)


_DURATION_HM_RE = re.compile(r"^\s*(\d+)\s*giờ\s*(\d+)\s*phút\s*$")
_DURATION_M_RE = re.compile(r"^\s*(\d+)\s*phút\s*$")
_NUMERIC_TOKEN_RE = re.compile(r"^\s*([+-]?\d[\d.,]*)\s*%?\s*$")


def _parse_number(raw: Any) -> Decimal | None:
    """Phân tích số từ giá trị thô hoặc chuỗi kiểu Việt Nam/quốc tế."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float | Decimal):
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            return None
        return value if value.is_finite() else None
    text = str(raw).strip()
    match = _NUMERIC_TOKEN_RE.match(text)
    if not match:
        return None
    token = match.group(1)
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        token = token.replace(",", ".")
    elif token.count(".") > 1:
        token = token.replace(".", "")
    elif "." in token:
        whole, frac = token.split(".", 1)
        if len(frac) == 3 and whole.lstrip("+-").isdigit() and int(whole.lstrip("+-")) >= 10:
            token = whole + frac
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _parse_minutes(raw: Any) -> int | None:
    """Phân tích thời lượng dạng '5 phút' hoặc '1 giờ 10 phút' ra tổng số phút."""
    if not isinstance(raw, str):
        return None
    hm = _DURATION_HM_RE.match(raw)
    if hm:
        return int(hm.group(1)) * 60 + int(hm.group(2))
    m = _DURATION_M_RE.match(raw)
    if m:
        return int(m.group(1))
    return None


def _fmt_vi(value: Decimal, *, decimals: int = 2, signed: bool = False) -> str:
    """Định dạng số kiểu Việt Nam (dấu chấm nghìn, dấu phẩy thập phân)."""
    q = Decimal(1).scaleb(-decimals)
    rounded = value.quantize(q)
    whole_str, _, frac_str = f"{abs(rounded):,.{decimals}f}".partition(".")
    whole_vi = whole_str.replace(",", ".")
    frac_clean = frac_str.rstrip("0")
    sign = "-" if rounded < 0 else ("+" if signed and rounded > 0 else "")
    return f"{sign}{whole_vi}" + (f",{frac_clean}" if frac_clean else "")


def counterfactual_gap(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Tính khoảng cách counterfactual tới ngưỡng cho một điều kiện chưa đạt (MI-2).

    Không dùng ML; chỉ tính chênh lệch số học hoặc thời gian từ ``actual``, ``operator``,
    ``threshold`` đã có. Trả ``None`` nếu điều kiện đã đạt hoặc không suy ra được ngưỡng số.
    """
    if item.get("passed") is True:
        return None
    operator = str(item.get("operator") or "").strip()
    key = str(item.get("key") or "")
    label = str(item.get("label_vi") or key)
    actual_raw = item.get("actual")
    threshold_raw = item.get("threshold")

    actual_min = _parse_minutes(actual_raw)
    threshold_min = _parse_minutes(threshold_raw)
    if actual_min is not None and threshold_min is not None:
        if operator in ("<", "<=") and actual_min >= threshold_min:
            gap_min = actual_min - threshold_min + (1 if operator == "<" else 0)
            gap_text = f"cần {operator} {threshold_raw}, còn giảm {gap_min} phút"
            delta_min_val = -gap_min
        else:
            gap_min = max(0, threshold_min - actual_min + (1 if operator == ">" else 0))
            gap_text = f"cần {operator} {threshold_raw}, còn {gap_min} phút"
            delta_min_val = gap_min
        return {
            "key": key,
            "label_vi": label,
            "operator": operator,
            "actual": actual_raw,
            "threshold": threshold_raw,
            "target_value": float(threshold_min),
            "delta_abs": float(delta_min_val),
            "delta_pct": None,
            "gap_text_vi": gap_text,
            "summary_vi": f"Còn thiếu: {label} — {gap_text}",
        }

    actual_num = _parse_number(actual_raw)
    threshold_num = _parse_number(threshold_raw)
    if (
        actual_num is None
        or threshold_num is None
        or operator not in (">", ">=", "≥", "<", "<=", "≤")
    ):
        return None

    delta_abs = threshold_num - actual_num
    delta_pct = (delta_abs / abs(actual_num) * Decimal("100")) if actual_num != 0 else None
    pct_part = (
        f", còn {_fmt_vi(delta_pct, decimals=2, signed=True)}%" if delta_pct is not None else ""
    )
    abs_part = f" ({_fmt_vi(delta_abs, decimals=4, signed=True)})" if pct_part == "" else ""
    gap_text = f"cần {operator} {threshold_raw}{pct_part}{abs_part}"
    return {
        "key": key,
        "label_vi": label,
        "operator": operator,
        "actual": actual_raw,
        "threshold": threshold_raw,
        "target_value": float(threshold_num),
        "delta_abs": float(delta_abs),
        "delta_pct": float(round(delta_pct, 4)) if delta_pct is not None else None,
        "gap_text_vi": gap_text,
        "summary_vi": f"Còn thiếu: {label} — hiện {actual_raw}, {gap_text}",
    }


def missing_counterfactual(items: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Tổng hợp counterfactual cho điều kiện đầu tiên chưa đạt và điều kiện gần đạt nhất (MI-2)."""
    failing = [item for item in items if item.get("passed") is not True]
    if not failing:
        return None
    gaps = [gap for item in failing if (gap := counterfactual_gap(item)) is not None]
    if not gaps:
        return None
    first = gaps[0]

    def _closeness(gap: dict[str, Any]) -> tuple[int, float]:
        if gap["delta_pct"] is not None:
            return (0, abs(float(gap["delta_pct"])))
        if gap["delta_abs"] is not None:
            return (1, abs(float(gap["delta_abs"])))
        return (2, float("inf"))

    closest = min(gaps, key=_closeness)
    return {
        **first,
        "closest": closest,
    }


def format_setup_missing_vi(progress: Mapping[str, Any]) -> str | None:
    """Chuỗi hiển thị dùng chung cho Telegram và trang chi tiết bot (MI-2)."""
    cf = progress.get("counterfactual")
    if isinstance(cf, Mapping) and cf.get("summary_vi"):
        text = str(cf["summary_vi"])
        closest = cf.get("closest")
        if (
            isinstance(closest, Mapping)
            and closest.get("key") != cf.get("key")
            and closest.get("gap_text_vi")
        ):
            text += f" (gần đạt nhất: {closest.get('label_vi')} — {closest.get('gap_text_vi')})"
        return text
    missing = progress.get("missing")
    return str(missing) if missing else None


def _missing_text(item: Mapping[str, Any]) -> str:
    if item.get("passed") is None:
        return f"Còn thiếu: {item['label_vi']} — chưa xác định ({item.get('actual')})"
    return (
        f"Còn thiếu: {item['label_vi']} — hiện {item.get('actual')}, "
        f"cần {item.get('operator')} {item.get('threshold')}"
    )


def setup_progress(
    *,
    strategy_release_id: str,
    decisions: Sequence[Mapping[str, Any]],
    bonus: Sequence[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Tiến độ một setup; ``decisions`` sắp mới nhất trước, mỗi phần tử có ``checklist`` (list)."""
    strategy_id = strategy_release_id.split("-", 1)[0].upper()
    rule = SETUP_RULES.get(strategy_id)
    result: dict[str, Any] = {
        "strategy_id": strategy_id,
        "strategy_release_id": strategy_release_id,
        "label": rule.label_vi if rule else strategy_id,
        "formula_version": SETUP_FORMULA_VERSION,
        "status": "no_data",
        "status_label": STATUS_LABELS_VI["no_data"],
        "required": {"passed": 0, "total": 0},
        "bonus": {
            "passed": sum(1 for item in bonus if item.get("passed") is True),
            "total": len(bonus),
            "k": rule.bonus_k if rule else 0,
        },
        "direction": None,
        "missing": None,
        "counterfactual": None,
        "evaluated_at": None,
        "conditions": [],
    }
    if not decisions:
        return result
    latest = decisions[0]
    result["evaluated_at"] = latest.get("evaluated_at")
    checklist = list(latest.get("checklist") or [])
    by_key = {str(item.get("key")): item for item in checklist}
    order = list(rule.required) if rule else []
    order += [key for key in by_key if key not in order]
    required_items = [by_key[key] for key in order if key in by_key]

    def achieved_at(key: str) -> str | None:
        found: str | None = None
        for decision in decisions:
            items = {str(item.get("key")): item for item in decision.get("checklist") or []}
            if items.get(key, {}).get("passed") is not True:
                break
            found = decision.get("evaluated_at")
        return found

    items: list[dict[str, Any]] = [
        dict(item) | {"kind": "required", "achieved_at": achieved_at(str(item["key"]))}
        for item in required_items
    ]
    items += [dict(item) | {"kind": "bonus", "achieved_at": None} for item in bonus]
    result["conditions"] = items
    passed = sum(1 for item in required_items if item.get("passed") is True)
    result["required"] = {"passed": passed, "total": len(required_items)}

    candidate = latest.get("candidate")
    if latest.get("status") == "CANDIDATE" and candidate:
        result["direction"] = candidate.get("direction")
        try:
            expired = datetime.fromisoformat(str(candidate.get("expires_at"))) <= now
        except ValueError:
            expired = True
        if expired:
            result["status"] = "expired"
        elif result["bonus"]["passed"] >= result["bonus"]["k"]:
            result["status"] = "ready"
        else:
            result["status"] = "not_ready"
            result["missing"] = (
                f"Còn thiếu: cộng điểm {result['bonus']['passed']}/{result['bonus']['total']}, "
                f"cần ≥ {result['bonus']['k']}"
            )
    else:
        result["status"] = "not_ready"
        failing = next((item for item in required_items if item.get("passed") is not True), None)
        if failing is not None:
            result["missing"] = _missing_text(failing)
            result["counterfactual"] = missing_counterfactual(required_items)
        elif not required_items:
            result["missing"] = (
                f"Quyết định gần nhất chưa có checklist: {latest.get('reason_code') or '—'}"
            )
        else:
            result["missing"] = (
                f"Điều kiện thị trường đã đạt; rule chưa ra tín hiệu: {latest.get('detail_vi')}"
            )
    result["status_label"] = STATUS_LABELS_VI[result["status"]]
    return result


def monitor_verdict(progress: Mapping[str, Any]) -> str:
    """Câu kết luận ngắn cho bảng giám sát bot (CK-12) từ kết quả ``setup_progress``."""
    required = progress["required"]
    bonus = progress["bonus"]
    side = {"long": "MUA", "short": "BÁN"}.get(str(progress.get("direction")))
    side_text = f" {side}" if side else ""
    status = progress["status"]
    counts = f"{required['passed']}/{required['total']}"
    if status == "no_data":
        return "Chưa có quyết định nào"
    if status == "expired":
        return f"Tín hiệu{side_text} đã hết hạn"
    if status == "ready":
        bonus_text = f"cộng điểm {bonus['passed']}/{bonus['total']}"
        return f"Đủ điều kiện{side_text} ({counts} bắt buộc, {bonus_text})"
    waiting = f"Chờ{side_text}" if side else "Chờ tín hiệu"
    need = f" (cần ≥ {bonus['k']}/{bonus['total']} cộng điểm)" if bonus.get("k") else ""
    return f"{waiting}: {counts} điều kiện bắt buộc{need}"
