"""Lịch sự kiện kinh tế do owner nhập tay (CK-10, giai đoạn 1) — không dùng nguồn ngoài.

Mỗi sự kiện: tên, khu vực (mã 2–16 chữ, ví dụ ``US``), mức tác động LOW/MEDIUM/HIGH, thời điểm
(có múi giờ, lưu UTC), ghi chú. Điều kiện "Tin tức" của bảng Entry (CK-6): **không đạt** khi có sự
kiện tác động cao trong ±30 phút quanh hiện tại; lịch trống trong ±7 ngày thì "chưa xác định" vì
không phân biệt được "không có tin" với "chưa nhập". Lịch chưa nối vào rule H06 (``macro_blackout``)
— việc đó cần quyết định và backtest riêng.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

CALENDAR_VERSION = "ck10-calendar-v1"
IMPACTS = ("LOW", "MEDIUM", "HIGH")
IMPACT_LABELS_VI = {"LOW": "Thấp", "MEDIUM": "Trung bình", "HIGH": "Cao"}
NEWS_WINDOW = timedelta(minutes=30)
COVERAGE_WINDOW = timedelta(days=7)
_REGION = re.compile(r"^[A-Z]{2,16}$")


@dataclass(frozen=True, slots=True)
class EconomicEventInput:
    title: str
    region: str
    impact: str
    event_at: datetime
    note: str | None

    @classmethod
    def parse(
        cls, *, title: str, region: str, impact: str, event_at: str, note: str | None
    ) -> EconomicEventInput:
        """Chuẩn hóa và kiểm dữ liệu form; lỗi trả ``ValueError`` tiếng Việt."""
        clean_title = (title or "").strip()
        if not 1 <= len(clean_title) <= 160:
            raise ValueError("Tên sự kiện cần 1–160 ký tự.")
        clean_region = (region or "").strip().upper()
        if not _REGION.fullmatch(clean_region):
            raise ValueError("Khu vực là mã 2–16 chữ cái, ví dụ US, EU, CN.")
        clean_impact = (impact or "").strip().upper()
        if clean_impact not in IMPACTS:
            raise ValueError("Mức tác động chỉ nhận LOW, MEDIUM hoặc HIGH.")
        try:
            moment = datetime.fromisoformat((event_at or "").strip())
        except ValueError as error:
            raise ValueError("Thời điểm sự kiện không hợp lệ.") from error
        if moment.tzinfo is None:
            raise ValueError("Thời điểm sự kiện phải có múi giờ.")
        clean_note = (note or "").strip() or None
        if clean_note is not None and len(clean_note) > 500:
            raise ValueError("Ghi chú tối đa 500 ký tự.")
        return cls(clean_title, clean_region, clean_impact, moment.astimezone(UTC), clean_note)

    def as_state(self) -> dict[str, Any]:
        """Trạng thái canonical để băm vào audit (before/after)."""
        return {
            "title": self.title,
            "region": self.region,
            "impact": self.impact,
            "event_at": self.event_at.isoformat(),
            "note": self.note,
        }


def _at(event: Mapping[str, Any]) -> datetime:
    raw = event["event_at"]
    return raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))


def news_condition(events: Sequence[Mapping[str, Any]], *, now: datetime) -> dict[str, Any]:
    """Điều kiện cộng điểm "Tin tức" (CK-6) từ lịch nội bộ, cùng định dạng checklist."""
    if now.tzinfo is None:
        raise ValueError("now phải có timezone")
    covered = [event for event in events if abs(_at(event) - now) <= COVERAGE_WINDOW]
    blocking = [
        event
        for event in covered
        if event.get("impact") == "HIGH" and abs(_at(event) - now) <= NEWS_WINDOW
    ]
    if not covered:
        actual, passed = "lịch sự kiện nội bộ ±7 ngày chưa nhập", None
    elif blocking:
        first = min(blocking, key=lambda event: abs(_at(event) - now))
        minutes = int((_at(first) - now).total_seconds() // 60)
        when = f"sau {minutes} phút" if minutes >= 0 else f"{-minutes} phút trước"
        actual, passed = f"{first['title']} ({first.get('region')}) {when}", False
    else:
        actual, passed = "không có tin tác động cao trong ±30 phút", True
    return {
        "key": "news",
        "label_vi": "Không có tin tác động cao trong ±30 phút",
        "actual": actual,
        "operator": "không có",
        "threshold": "tin tác động cao (lịch nội bộ)",
        "passed": passed,
    }
