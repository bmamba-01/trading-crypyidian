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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    "H03": SetupRule("H03", "Phá biên Donchian", ("donchian_breakout", "volume_above_median")),
    "H06": SetupRule(
        "H06",
        "Đảo chiều trong biên",
        ("macro_clear", "range_adx", "range_er", "extreme", "no_adverse_break"),
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
