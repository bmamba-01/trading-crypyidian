"""Tổng hợp kết quả tín hiệu (CRY-430) — dùng chung cho trang Admin và Run Report.

Thuần (không DB): nhận các hàng đã chấm ở một horizon, trả số đếm/tỷ lệ/phân phối R dạng chuỗi
Decimal (tất định, hợp với validator số của AI Analyst).

- ``hit_rate_percent`` = số tín hiệu **đúng** (nhãn +1) / số tín hiệu đã chấm (``SCORED``) × 100.
- ``wrong_rate_percent`` tương tự với nhãn −1; phần còn lại là hòa (|R| ≤ 0,1R).
- Phân phối R theo các dải cố định của ``R_BUCKETS`` (đơn vị R của ``outcome_r``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

_Q = Decimal("0.0001")
_PCT = Decimal("0.1")

# (khóa, nhãn hiển thị, cận dưới, cận trên) — dải nửa mở [lo, hi); None = vô cực.
R_BUCKETS: tuple[tuple[str, str, Decimal | None, Decimal | None], ...] = (
    ("le_m1", "≤ −1R", None, Decimal("-0.9999")),
    ("m1_m05", "−1R … −0,5R", Decimal("-0.9999"), Decimal("-0.5")),
    ("m05_m01", "−0,5R … −0,1R", Decimal("-0.5"), Decimal("-0.1")),
    ("flat", "±0,1R (hòa)", Decimal("-0.1"), Decimal("0.1001")),
    ("p01_p05", "0,1R … 0,5R", Decimal("0.1001"), Decimal("0.5")),
    ("p05_p1", "0,5R … 1R", Decimal("0.5"), Decimal("1")),
    ("ge_p1", "≥ 1R", Decimal("1"), None),
)


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """Một hàng ``signal_outcome`` tối thiểu để tổng hợp."""

    status: str
    label: int | None = None
    outcome_r: Decimal | None = None
    mfe_r: Decimal | None = None
    mae_r: Decimal | None = None
    first_touch: str | None = None
    same_bar_conflict: bool | None = None
    seconds_to_first_touch: int | None = None


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_Q))


def _pct(part: int, whole: int) -> str | None:
    if whole == 0:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(_PCT))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def bucket_key(value: Decimal) -> str:
    for key, _label, low, high in R_BUCKETS:
        if (low is None or value >= low) and (high is None or value < high):
            return key
    return R_BUCKETS[-1][0]


def summarize(rows: Iterable[OutcomeRow]) -> dict[str, Any]:
    """Thống kê một nhóm hàng cùng horizon."""
    items = list(rows)
    scored = [row for row in items if row.status == "SCORED"]
    labels = Counter(row.label for row in scored)
    r_values = [row.outcome_r for row in scored if row.outcome_r is not None]
    mfe = [row.mfe_r for row in scored if row.mfe_r is not None]
    mae = [row.mae_r for row in scored if row.mae_r is not None]
    touches = Counter(row.first_touch for row in scored)
    seconds = [
        Decimal(row.seconds_to_first_touch)
        for row in scored
        if row.seconds_to_first_touch is not None and row.first_touch == "TP1"
    ]
    buckets = Counter(bucket_key(value) for value in r_values)
    return {
        "signals": len(items),
        "scored": len(scored),
        "data_gap": sum(1 for row in items if row.status == "DATA_GAP"),
        "invalid": sum(1 for row in items if row.status == "INVALID_SIGNAL"),
        "correct": labels.get(1, 0),
        "wrong": labels.get(-1, 0),
        "neutral": labels.get(0, 0),
        "hit_rate_percent": _pct(labels.get(1, 0), len(scored)),
        "wrong_rate_percent": _pct(labels.get(-1, 0), len(scored)),
        "tp1_first": touches.get("TP1", 0),
        "sl_first": touches.get("SL", 0),
        "no_touch": touches.get("NONE", 0),
        "same_bar_conflicts": sum(1 for row in scored if row.same_bar_conflict),
        "outcome_r_mean": _text(_mean(r_values)),
        "outcome_r_median": _text(Decimal(median(r_values)) if r_values else None),
        "mfe_r_mean": _text(_mean(mfe)),
        "mae_r_mean": _text(_mean(mae)),
        "median_seconds_to_tp1": (int(median(seconds)) if seconds else None),
        "r_distribution": {key: buckets.get(key, 0) for key, *_ in R_BUCKETS},
    }


def group_summaries(
    rows: Iterable[tuple[Hashable, OutcomeRow]],
    *,
    key_text: Callable[[Any], str] = str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Tổng hợp theo nhóm; sắp nhóm nhiều tín hiệu trước, rồi theo tên nhóm."""
    groups: dict[Hashable, list[OutcomeRow]] = defaultdict(list)
    for key, row in rows:
        groups[key].append(row)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), key_text(item[0])))
    if limit is not None:
        ordered = ordered[:limit]
    return [{"key": key_text(key), **summarize(items)} for key, items in ordered]


def repeated_wrong(
    rows: Iterable[tuple[Hashable, OutcomeRow]],
    *,
    key_text: Callable[[Any], str] = str,
    min_wrong: int = 3,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Nhóm có tín hiệu **sai lặp lại** (≥ ``min_wrong`` nhãn −1), sai nhiều nhất trước."""
    groups: dict[Hashable, list[OutcomeRow]] = defaultdict(list)
    for key, row in rows:
        groups[key].append(row)
    result = []
    for key, items in groups.items():
        summary = summarize(items)
        if summary["wrong"] >= min_wrong:
            result.append({"key": key_text(key), **summary})
    result.sort(key=lambda item: (-item["wrong"], item["key"]))
    return result[:limit]


def bucket_labels() -> Mapping[str, str]:
    return {key: label for key, label, *_ in R_BUCKETS}


__all__ = [
    "R_BUCKETS",
    "OutcomeRow",
    "bucket_key",
    "bucket_labels",
    "group_summaries",
    "repeated_wrong",
    "summarize",
]
