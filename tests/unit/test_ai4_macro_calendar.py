"""AI-4 lát 1: parser lịch BEA/FOMC và trạng thái macro point-in-time cho H06."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.research.macro_calendar import (
    BEA_SOURCE_ID,
    FOMC_SOURCE_ID,
    MacroCalendarFetch,
    MacroEvent,
    events_hash,
    macro_state,
    parse_bea_release_dates,
    parse_fomc_calendar,
    upcoming_events,
)

FOMC_HTML = """
<h4><a>2026 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-md-2"><strong>October</strong></div>
<div class="fomc-meeting__date col-md-10">27-28</div>
<div class="fomc-meeting__month col-md-2"><strong>December</strong></div>
<div class="fomc-meeting__date col-md-10">8-9*</div>
<div class="fomc-meeting__month col-md-2"><strong>March</strong></div>
<div class="fomc-meeting__date col-md-10">16 (unscheduled)</div>
<h4><a>2027 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-md-2"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-md-10">30-1</div>
"""


def test_fomc_decision_at_2pm_eastern_on_last_meeting_day() -> None:
    events = parse_fomc_calendar(FOMC_HTML)
    assert [e.key for e in events] == ["FOMC:2026-10-28", "FOMC:2026-12-09", "FOMC:2027-05-01"]
    # 14:00 EDT = 18:00 UTC (tháng 10), 14:00 EST = 19:00 UTC (tháng 12): DST được tính đúng.
    assert events[0].scheduled_at == datetime(2026, 10, 28, 18, tzinfo=UTC)
    assert events[1].scheduled_at == datetime(2026, 12, 9, 19, tzinfo=UTC)
    assert all(e.high_impact for e in events)
    with pytest.raises(ValueError, match="FOMC"):
        parse_fomc_calendar("<html></html>")


def test_bea_release_dates_dedup_and_high_impact_flag() -> None:
    payload = {
        "Gross Domestic Product": {
            "release_dates": ["2026-11-25T13:30:00+00:00", "2026-11-25T13:30:00+00:00"]
        },
        "Travel and Tourism Satellite Account": {"release_dates": ["2026-12-01T15:00:00+00:00"]},
        "ignored": "not-a-mapping",
    }
    events = parse_bea_release_dates(payload)
    assert [(e.title, e.high_impact) for e in events] == [
        ("Gross Domestic Product", True),
        ("Travel and Tourism Satellite Account", False),
    ]
    with pytest.raises(ValueError, match="múi giờ"):
        parse_bea_release_dates({"GDP": {"release_dates": ["2026-11-25T13:30:00"]}})
    with pytest.raises(ValueError, match="BEA"):
        parse_bea_release_dates({})


def _fetch(fetch_id: str, source: str, at: datetime, *events: MacroEvent) -> MacroCalendarFetch:
    return MacroCalendarFetch(fetch_id, source, at, events)


GDP = MacroEvent(BEA_SOURCE_ID, "gdp", "GDP", datetime(2026, 11, 25, 13, 30, tzinfo=UTC), True)
TRAVEL = MacroEvent(BEA_SOURCE_ID, "t", "Travel", datetime(2026, 11, 26, 15, tzinfo=UTC), False)
FAR_BEA = MacroEvent(BEA_SOURCE_ID, "far", "GDP", datetime(2027, 3, 1, 13, 30, tzinfo=UTC), True)
FOMC = MacroEvent(FOMC_SOURCE_ID, "f", "FOMC", datetime(2026, 12, 9, 19, tzinfo=UTC), True)
FAR_FOMC = MacroEvent(FOMC_SOURCE_ID, "f2", "FOMC", datetime(2027, 3, 17, 18, tzinfo=UTC), True)
FETCHED = datetime(2026, 11, 25, 6, tzinfo=UTC)


def _fetches(at: datetime = FETCHED) -> list[MacroCalendarFetch]:
    return [
        _fetch("bea-1", BEA_SOURCE_ID, at, GDP, TRAVEL, FAR_BEA),
        _fetch("fomc-1", FOMC_SOURCE_ID, at, FOMC, FAR_FOMC),
    ]


def test_blackout_window_is_half_open_around_high_impact_events() -> None:
    fetches = _fetches()
    before = macro_state(fetches, GDP.scheduled_at - timedelta(minutes=31))
    assert before.blackout is False
    assert before.is_fresh is True
    assert before.evidence_ids == ("bea-1", "fomc-1")
    assert before.evidence_available_at == FETCHED
    start = macro_state(fetches, GDP.scheduled_at - timedelta(minutes=30))
    assert start.blackout is True
    assert start.blocking_event == "gdp"
    assert macro_state(fetches, GDP.scheduled_at + timedelta(minutes=59)).blackout is True
    assert macro_state(fetches, GDP.scheduled_at + timedelta(minutes=60)).blackout is False
    # Release tác động thấp không cấm.
    assert macro_state(fetches, TRAVEL.scheduled_at).blackout is False


def test_unknown_without_every_source_and_no_lookahead() -> None:
    only_bea = [_fetches()[0]]
    assert macro_state(only_bea, GDP.scheduled_at).blackout is None
    # Lần tải sau decision_time không được dùng (không nhìn trước).
    state = macro_state(_fetches(), FETCHED - timedelta(seconds=1))
    assert state.blackout is None
    assert state.evidence_ids == ()


def test_stale_when_fetch_too_old_or_coverage_too_short() -> None:
    old = macro_state(_fetches(), FETCHED + timedelta(hours=37))
    assert old.is_fresh is False
    assert old.blackout is not None
    short = [
        _fetch("bea-2", BEA_SOURCE_ID, FETCHED, GDP),
        _fetch("fomc-2", FOMC_SOURCE_ID, FETCHED, FOMC, FAR_FOMC),
    ]
    assert macro_state(short, FETCHED + timedelta(hours=1)).is_fresh is False
    # Lần tải mới nhất được ưu tiên.
    newer = [*_fetches(), _fetch("bea-3", BEA_SOURCE_ID, FETCHED + timedelta(hours=2), FAR_BEA)]
    state = macro_state(newer, FETCHED + timedelta(hours=3))
    assert state.evidence_ids == ("bea-3", "fomc-1")


def test_upcoming_events_and_hash_are_stable() -> None:
    events = upcoming_events(_fetches(), after=FETCHED, within=timedelta(days=30))
    assert [e.key for e in events] == ["gdp", "f"]
    assert events_hash([GDP, TRAVEL]) == events_hash([TRAVEL, GDP])
    assert events_hash([GDP]) != events_hash([TRAVEL])
    assert MacroEvent.from_payload(BEA_SOURCE_ID, GDP.payload()) == GDP


# --- Collector -----------------------------------------------------------------------------


class _Evidence:
    policy_fingerprint = "c" * 64


class _Gate:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    async def authorize(self, purpose):
        from trading_bot.domain.research.source_policy import SourceUsePurpose

        assert purpose is SourceUsePurpose.PAPER_DEMO
        self.calls += 1
        if not self.allowed:
            raise PermissionError("Nguồn chưa có policy.")
        return _Evidence()


class _Store:
    def __init__(self, allowed: dict[str, bool], last: dict[str, datetime]) -> None:
        self.gates = {source: _Gate(ok) for source, ok in allowed.items()}
        self.last = last
        self.rows: list[dict] = []

    def gate(self, source_id: str) -> _Gate:
        return self.gates[source_id]

    async def last_fetched_at(self, source_id: str) -> datetime | None:
        return self.last.get(source_id)

    async def insert(self, record: dict) -> None:
        self.rows.append(record)


def _collector(store: _Store, fetcher) -> object:
    from contextlib import asynccontextmanager
    from uuid import UUID

    from trading_bot.application.macro_calendar_collector import MacroCalendarCollector

    @asynccontextmanager
    async def scope():
        yield store

    return MacroCalendarCollector(workspace_id=UUID(int=1), fetcher=fetcher, store_scope=scope)


class _Fetcher:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def fetch(self, source_id: str):
        self.calls.append(source_id)
        if source_id == self.fail:
            raise RuntimeError("HTTP 503")
        if source_id == BEA_SOURCE_ID:
            return "https://bea", {
                "Gross Domestic Product": {"release_dates": [GDP.payload()["scheduled_at"]]}
            }
        return "https://fed", FOMC_HTML


def test_collector_stores_only_with_rights_and_isolates_failures() -> None:
    import asyncio

    now = datetime(2026, 11, 16, 5, tzinfo=UTC)
    store = _Store({BEA_SOURCE_ID: True, FOMC_SOURCE_ID: False}, {})
    fetcher = _Fetcher()
    results = asyncio.run(_collector(store, fetcher).collect_once(now))
    assert [(r.source_id, r.status) for r in results] == [
        (BEA_SOURCE_ID, "stored"),
        (FOMC_SOURCE_ID, "rights_denied"),
    ]
    # Thiếu quyền thì không tải nguồn đó.
    assert fetcher.calls == [BEA_SOURCE_ID]
    row = store.rows[0]
    assert row["event_count"] == 1
    assert row["policy_fingerprint"] == "c" * 64
    assert row["coverage_end"] == GDP.scheduled_at
    assert len(row["events_sha256"]) == 64
    # Quyền được kiểm trước và sau khi tải.
    assert store.gates[BEA_SOURCE_ID].calls == 2

    recent = _Store(
        {BEA_SOURCE_ID: True, FOMC_SOURCE_ID: True},
        {BEA_SOURCE_ID: now - timedelta(hours=1)},
    )
    failing = _Fetcher(fail=FOMC_SOURCE_ID)
    results = asyncio.run(_collector(recent, failing).collect_once(now))
    assert [(r.source_id, r.status) for r in results] == [
        (BEA_SOURCE_ID, "skipped_recent"),
        (FOMC_SOURCE_ID, "error"),
    ]
    assert recent.rows == []


def test_only_h06_releases_load_macro_state() -> None:
    from trading_bot.application.macro_calendar_state import release_uses_macro

    assert release_uses_macro("h06-v1")
    assert release_uses_macro("h06-v2")
    assert not release_uses_macro("h03-v3-1h")
    assert not release_uses_macro("h01-v1")
