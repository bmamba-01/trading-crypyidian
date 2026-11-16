"""Lịch công bố vĩ mô chính thức của Mỹ (AI-4 lát 1, 16/11/2026) và trạng thái macro cho H06.

Nguồn (kiểm 16/11/2026):

- BEA: ``https://apps.bea.gov/API/signup/release_dates.json`` — JSON tên release → danh sách mốc
  công bố có múi giờ UTC; trang lịch https://www.bea.gov/news/schedule.
- Fed: ``https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`` — HTML theo năm; tuyên
  bố FOMC công bố 2:00 p.m. giờ miền Đông (ET) ngày cuối cuộc họp (tiêu đề các thông cáo 2026).
- OMB (Nhà Trắng): "Schedule of Release Dates for Principal Federal Economic Indicators" — PDF
  theo năm, liệt kê ngày công bố CPI, Employment Situation, PPI (BLS) và Retail Sales (Census);
  danh sách PDF trên https://www.whitehouse.gov/omb/information-resources/guidance/
  us-principal-federal-economic-indicators/ (kiểm 16/11/2026). Thay cho việc thu trực tiếp từ
  BLS — BLS chặn bot không có thông tin liên hệ của chủ sở hữu (https://www.bls.gov/bls/pss.htm).
  PDF chỉ có ngày; giờ 8:30 a.m. ET lấy theo lịch BLS ("All times on calendar are Eastern
  Time", các bản tin trên là 8:30 AM). Hạn chế: OMB không cập nhật PDF khi cơ quan đổi lịch đột
  xuất (vd đóng cửa chính phủ 2025) — lịch có thể lệch trong tình huống đó.

Module thuần: không I/O. Mỗi lần tải là một ``MacroCalendarFetch`` bất biến (danh sách sự kiện +
mốc tải); trạng thái tại ``decision_time`` chỉ dùng lần tải gần nhất **trước** mốc đó (không nhìn
trước). Cửa sổ cấm và ngưỡng độ mới là lựa chọn vận hành khởi điểm, chưa được nghiên cứu tối ưu.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BEA_SOURCE_ID = "BEA_RELEASE_SCHEDULE"
FOMC_SOURCE_ID = "FED_FOMC_CALENDAR"
PFEI_SOURCE_ID = "OMB_PFEI_SCHEDULE"
MACRO_SOURCE_IDS = (BEA_SOURCE_ID, FOMC_SOURCE_ID, PFEI_SOURCE_ID)
# v2: thêm nguồn OMB PFEI (CPI, việc làm, PPI, bán lẻ) vào nhóm nguồn bắt buộc.
MACRO_POLICY_VERSION = "macro-blackout-v2"
BLACKOUT_BEFORE = timedelta(minutes=30)
BLACKOUT_AFTER = timedelta(minutes=60)
MAX_FETCH_AGE = timedelta(hours=36)
MIN_COVERAGE_AHEAD = timedelta(days=7)
# Release BEA gây biến động thị trường đáng kể; các release còn lại lưu nhưng không cấm vào lệnh.
BEA_HIGH_IMPACT = frozenset({"Gross Domestic Product", "Personal Income and Outlays"})
_ET = ZoneInfo("America/New_York")
_FOMC_RELEASE_TIME = time(14, 0)
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
_MONTH_ABBR = {name[:3]: index for name, index in _MONTHS.items()}


@dataclass(frozen=True, slots=True)
class MacroEvent:
    """Một mốc công bố; ``high_impact`` quyết định có cấm vào lệnh quanh mốc hay không."""

    source_id: str
    key: str
    title: str
    scheduled_at: datetime
    high_impact: bool

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "scheduled_at": self.scheduled_at.astimezone(UTC).isoformat(),
            "high_impact": self.high_impact,
        }

    @classmethod
    def from_payload(cls, source_id: str, item: Mapping[str, Any]) -> MacroEvent:
        return cls(
            source_id=source_id,
            key=str(item["key"]),
            title=str(item["title"]),
            scheduled_at=datetime.fromisoformat(str(item["scheduled_at"])).astimezone(UTC),
            high_impact=bool(item["high_impact"]),
        )


def events_hash(events: Sequence[MacroEvent]) -> str:
    """SHA-256 của danh sách sự kiện đã chuẩn hóa (sắp theo mốc, khóa)."""
    canonical = json.dumps(
        [e.payload() for e in sorted(events, key=lambda e: (e.scheduled_at, e.key))],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_bea_release_dates(payload: Mapping[str, Any]) -> list[MacroEvent]:
    """JSON BEA ``{release: {"release_dates": [ISO8601 UTC, ...]}}`` → sự kiện (bỏ mốc trùng)."""
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("Lịch BEA rỗng hoặc sai định dạng.")
    events: dict[str, MacroEvent] = {}
    for title, body in payload.items():
        dates = body.get("release_dates") if isinstance(body, Mapping) else None
        if not isinstance(dates, list):
            continue
        for value in dates:
            instant = datetime.fromisoformat(str(value))
            if instant.tzinfo is None:
                raise ValueError("Mốc BEA thiếu múi giờ.")
            instant = instant.astimezone(UTC)
            key = f"BEA:{title}:{instant.isoformat()}"
            events[key] = MacroEvent(
                BEA_SOURCE_ID, key, str(title), instant, str(title) in BEA_HIGH_IMPACT
            )
    if not events:
        raise ValueError("Lịch BEA không có mốc công bố nào.")
    return sorted(events.values(), key=lambda e: (e.scheduled_at, e.key))


_FOMC_YEAR = re.compile(r"(\d{4}) FOMC Meetings")
_FOMC_ROW = re.compile(
    r'fomc-meeting__month[^"]*"><strong>([^<]+)</strong>.*?fomc-meeting__date[^"]*">([^<]+)<',
    re.S,
)


def _month_index(text: str) -> int:
    token = text.strip().split("/")[-1].strip().lower()
    if token in _MONTHS:
        return _MONTHS[token]
    if token[:3] in _MONTH_ABBR:
        return _MONTH_ABBR[token[:3]]
    raise ValueError(f"Tháng FOMC không nhận ra: {text!r}.")


def parse_fomc_calendar(html: str) -> list[MacroEvent]:
    """Trang lịch FOMC → quyết định lãi suất 14:00 ET ngày cuối mỗi cuộc họp định kỳ.

    Bỏ cuộc họp bất thường/biểu quyết bằng văn bản (không có lịch trước). Cuộc họp vắt tháng
    (vd "Apr/May" "30-1") lấy tháng sau cùng.
    """
    if not isinstance(html, str) or "FOMC Meetings" not in html:
        raise ValueError("Trang lịch FOMC sai định dạng.")
    sections = list(_FOMC_YEAR.finditer(html))
    events: dict[str, MacroEvent] = {}
    for index, heading in enumerate(sections):
        year = int(heading.group(1))
        end = sections[index + 1].start() if index + 1 < len(sections) else len(html)
        for month_text, date_text in _FOMC_ROW.findall(html[heading.end() : end]):
            lowered = date_text.lower()
            if any(word in lowered for word in ("unscheduled", "notation", "cancel")):
                continue
            days = [int(value) for value in re.findall(r"\d+", date_text)]
            if not days:
                continue
            day = date(year, _month_index(month_text), days[-1])
            instant = datetime.combine(day, _FOMC_RELEASE_TIME, tzinfo=_ET).astimezone(UTC)
            key = f"FOMC:{day.isoformat()}"
            events[key] = MacroEvent(FOMC_SOURCE_ID, key, "FOMC rate decision", instant, True)
    if not events:
        raise ValueError("Không đọc được cuộc họp FOMC nào.")
    return sorted(events.values(), key=lambda e: (e.scheduled_at, e.key))


# Dòng tiêu đề trong PDF OMB → tên ngắn. Cả bốn chỉ số công bố 8:30 a.m. ET (không có chú thích
# giờ khác trong PDF) và được coi là tác động mạnh.
PFEI_INDICATORS = {
    "The Employment Situation": "Employment Situation",
    "Consumer Price Index": "Consumer Price Index",
    "Producer Price Indexes": "Producer Price Index",
    "Advance Monthly Sales for Retail and Food Services": "Retail Sales",
}
_PFEI_RELEASE_TIME = time(8, 30)
_PFEI_YEAR = re.compile(r"PRINCIPAL FEDERAL ECONOMIC INDICATORS\s+FOR\s+(\d{4})")
_PFEI_ROW = re.compile(r"^(?:(?:\d{1,2}|--)\s+){11}(?:\d{1,2}|--)$")


def _pfei_document_events(text: str) -> list[MacroEvent]:
    years = {int(value) for value in _PFEI_YEAR.findall(text)}
    if len(years) != 1:
        raise ValueError("PDF OMB PFEI không xác định được đúng một năm.")
    year = years.pop()
    lines = [line.strip() for line in text.splitlines()]
    events: list[MacroEvent] = []
    for heading, title in PFEI_INDICATORS.items():
        try:
            start = lines.index(heading)
        except ValueError as error:
            raise ValueError(f"PDF OMB PFEI {year} thiếu chỉ số {heading!r}.") from error
        # Hàng 12 tháng nằm ngay sau dòng tiêu đề và dòng chú thích "(Data are for ...)".
        row = next((line for line in lines[start + 1 : start + 4] if _PFEI_ROW.match(line)), None)
        if row is None:
            raise ValueError(f"PDF OMB PFEI {year}: không đọc được hàng ngày của {heading!r}.")
        for month, token in enumerate(row.split(), start=1):
            if token == "--":
                continue
            day = date(year, month, int(token))
            if day.weekday() >= 5:
                raise ValueError(f"PDF OMB PFEI: {title} rơi vào cuối tuần ({day}).")
            instant = datetime.combine(day, _PFEI_RELEASE_TIME, tzinfo=_ET).astimezone(UTC)
            key = f"PFEI:{title}:{day.isoformat()}"
            events.append(MacroEvent(PFEI_SOURCE_ID, key, title, instant, True))
    return events


def parse_pfei_schedule(documents: Sequence[tuple[str, str]]) -> list[MacroEvent]:
    """Văn bản các PDF OMB PFEI ``[(url, text), ...]`` → sự kiện 8:30 ET của bốn chỉ số.

    Một PDF sai định dạng làm hỏng cả lần tải (fail-closed): không lưu lịch thiếu một phần.
    """
    if not documents:
        raise ValueError("Không có PDF OMB PFEI nào.")
    events: dict[str, MacroEvent] = {}
    for _url, text in documents:
        if not isinstance(text, str):
            raise ValueError("Nội dung PDF OMB PFEI phải là văn bản.")
        for event in _pfei_document_events(text):
            events[event.key] = event
    return sorted(events.values(), key=lambda e: (e.scheduled_at, e.key))


# ---------------------------------------------------------------------------
# Trạng thái macro tại decision_time
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MacroCalendarFetch:
    """Một lần tải đã lưu: định danh (bằng chứng), nguồn, mốc tải, sự kiện."""

    fetch_id: str
    source_id: str
    fetched_at: datetime
    events: tuple[MacroEvent, ...]

    @property
    def coverage_end(self) -> datetime | None:
        return max((e.scheduled_at for e in self.events), default=None)


@dataclass(frozen=True, slots=True)
class MacroState:
    """Đúng các trường ``DecisionSnapshot`` cần; ``None`` = chưa biết (H06 abstain)."""

    blackout: bool | None
    evidence_ids: tuple[str, ...]
    evidence_available_at: datetime | None
    is_fresh: bool | None
    blocking_event: str | None = None


UNKNOWN_MACRO = MacroState(None, (), None, None)


def macro_state(
    fetches: Iterable[MacroCalendarFetch],
    decision_time: datetime,
    *,
    required_sources: Sequence[str] = MACRO_SOURCE_IDS,
) -> MacroState:
    """Trạng thái macro point-in-time từ lần tải gần nhất (``fetched_at ≤ decision_time``).

    - Thiếu lần tải của một nguồn bắt buộc → chưa biết.
    - Lần tải quá ``MAX_FETCH_AGE`` hoặc lịch không phủ tới ``decision_time + MIN_COVERAGE_AHEAD``
      → không còn mới (H06 dừng ở STALE).
    - Cấm khi có sự kiện tác động mạnh trong ``[mốc − 30 phút, mốc + 60 phút)``.
    """
    if decision_time.tzinfo is None:
        raise ValueError("decision_time phải có múi giờ.")
    instant = decision_time.astimezone(UTC)
    latest: dict[str, MacroCalendarFetch] = {}
    for fetch in fetches:
        if fetch.fetched_at > instant or fetch.source_id not in required_sources:
            continue
        current = latest.get(fetch.source_id)
        if current is None or fetch.fetched_at > current.fetched_at:
            latest[fetch.source_id] = fetch
    if set(latest) != set(required_sources):
        return UNKNOWN_MACRO
    used = [latest[source] for source in required_sources]
    fresh = all(
        instant - fetch.fetched_at <= MAX_FETCH_AGE
        and fetch.coverage_end is not None
        and fetch.coverage_end >= instant + MIN_COVERAGE_AHEAD
        for fetch in used
    )
    blocking = next(
        (
            event
            for fetch in used
            for event in fetch.events
            if event.high_impact
            and event.scheduled_at - BLACKOUT_BEFORE
            <= instant
            < event.scheduled_at + BLACKOUT_AFTER
        ),
        None,
    )
    return MacroState(
        blackout=blocking is not None,
        evidence_ids=tuple(sorted(fetch.fetch_id for fetch in used)),
        evidence_available_at=max(fetch.fetched_at for fetch in used),
        is_fresh=fresh,
        blocking_event=blocking.key if blocking else None,
    )


def upcoming_events(
    fetches: Iterable[MacroCalendarFetch], *, after: datetime, within: timedelta
) -> list[MacroEvent]:
    """Sự kiện tác động mạnh sắp tới (cho digest AI/Telegram), từ lần tải mới nhất mỗi nguồn."""
    latest: dict[str, MacroCalendarFetch] = {}
    for fetch in fetches:
        if fetch.fetched_at <= after and (
            fetch.source_id not in latest or fetch.fetched_at > latest[fetch.source_id].fetched_at
        ):
            latest[fetch.source_id] = fetch
    return sorted(
        (
            event
            for fetch in latest.values()
            for event in fetch.events
            if event.high_impact and after <= event.scheduled_at < after + within
        ),
        key=lambda e: (e.scheduled_at, e.key),
    )


__all__ = [
    "BEA_SOURCE_ID",
    "BLACKOUT_AFTER",
    "BLACKOUT_BEFORE",
    "FOMC_SOURCE_ID",
    "MACRO_POLICY_VERSION",
    "MACRO_SOURCE_IDS",
    "MAX_FETCH_AGE",
    "MIN_COVERAGE_AHEAD",
    "PFEI_INDICATORS",
    "PFEI_SOURCE_ID",
    "UNKNOWN_MACRO",
    "MacroCalendarFetch",
    "MacroEvent",
    "MacroState",
    "events_hash",
    "macro_state",
    "parse_bea_release_dates",
    "parse_fomc_calendar",
    "parse_pfei_schedule",
    "upcoming_events",
]
