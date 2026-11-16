"""CK-10: lịch sự kiện kinh tế do owner nhập và điều kiện "Tin tức" của bảng Entry (CK-6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from trading_bot.domain.market.economic_calendar import (
    CALENDAR_VERSION,
    EconomicEventInput,
    news_condition,
)

NOW = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)


def _event(minutes: int, impact: str = "HIGH", title: str = "CPI Mỹ") -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "title": title,
        "region": "US",
        "impact": impact,
        "event_at": (NOW + timedelta(minutes=minutes)).isoformat(),
    }


def test_input_validation_normalises_and_rejects_bad_values() -> None:
    assert CALENDAR_VERSION == "ck10-calendar-v1"
    item = EconomicEventInput.parse(
        title="  FOMC  ", region="us", impact="high", event_at="2026-11-18T19:00:00+00:00", note=""
    )
    assert (item.title, item.region, item.impact, item.note) == ("FOMC", "US", "HIGH", None)
    assert item.event_at == datetime(2026, 11, 18, 19, tzinfo=UTC)
    when = "2026-11-18T19:00:00+00:00"
    for bad, message in (
        ({"title": "", "region": "US", "impact": "HIGH", "event_at": when}, "Tên"),
        ({"title": "x", "region": "U$", "impact": "HIGH", "event_at": when}, "Khu vực"),
        ({"title": "x", "region": "US", "impact": "EXTREME", "event_at": when}, "tác động"),
        ({"title": "x", "region": "US", "impact": "HIGH", "event_at": when[:19]}, "múi giờ"),
        ({"title": "x" * 161, "region": "US", "impact": "HIGH", "event_at": when}, "Tên"),
    ):
        with pytest.raises(ValueError, match=message):
            EconomicEventInput.parse(**bad, note=None)  # type: ignore[arg-type]


def test_news_condition_blocks_high_impact_within_30_minutes() -> None:
    near = news_condition([_event(20), _event(-200, "LOW")], now=NOW)
    assert near["key"] == "news"
    assert near["passed"] is False
    assert "CPI Mỹ" in near["actual"]
    clear = news_condition([_event(45), _event(10, "MEDIUM")], now=NOW)
    assert clear["passed"] is True  # tin tác động cao cách 45 phút; tin trung bình không chặn
    past = news_condition([_event(-29)], now=NOW)
    assert past["passed"] is False  # vừa ra tin trong 30 phút trước vẫn tính
    empty = news_condition([], now=NOW)
    assert empty["passed"] is None
    assert "chưa nhập" in empty["actual"]


class _Unit:
    def __init__(self, store: dict[str, Any], **kwargs: Any) -> None:
        self.store = store
        self.kwargs = kwargs
        self.session = store

    async def __aenter__(self) -> _Unit:
        self.store["units"].append(self.kwargs)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def record_audit(self, **kwargs: Any) -> None:
        self.store["audits"].append(kwargs)


class _Row:
    def __init__(self, data: Any) -> None:
        self.id = uuid4()
        self.title, self.region, self.impact = data.title, data.region, data.impact
        self.event_at, self.note, self.revision = data.event_at, data.note, 1


class _Repository:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def create(self, **kwargs: Any) -> _Row:
        row = _Row(kwargs["data"])
        self.store["rows"][row.id] = row
        return row

    async def get_for_update(self, **kwargs: Any) -> _Row | None:
        return self.store["rows"].get(kwargs["event_id"])

    async def update(self, row: _Row, data: Any) -> None:
        row.title, row.impact, row.revision = data.title, data.impact, row.revision + 1

    async def soft_delete(self, row: _Row) -> None:
        self.store["rows"].pop(row.id)

    async def list_between(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _service(store: dict[str, Any]) -> Any:
    from trading_bot.application.economic_calendar import EconomicCalendarService

    return EconomicCalendarService(
        object(),  # type: ignore[arg-type]
        unit_of_work=lambda sessions, **kwargs: _Unit(store, **kwargs),
        repository=lambda session: _Repository(store),
    )


def _principal(*roles: Any) -> Any:
    from trading_bot.auth.policy import Principal

    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset(roles),
        mfa_verified_at=NOW,
    )


@pytest.mark.asyncio
async def test_service_audits_create_update_delete_and_checks_permission() -> None:
    from trading_bot.application.economic_calendar import EconomicCalendarError
    from trading_bot.auth.policy import Role

    store: dict[str, Any] = {"units": [], "audits": [], "rows": {}}
    owner = _principal(Role.OWNER)
    data = EconomicEventInput.parse(
        title="CPI Mỹ", region="US", impact="HIGH", event_at="2026-11-18T13:30:00+00:00", note=None
    )
    created = await _service(store).create(owner, data, reason="Lịch tháng 11")
    event_id = next(iter(store["rows"]))
    assert created["title"] == "CPI Mỹ"
    changed = EconomicEventInput.parse(
        title="CPI Mỹ (lõi)",
        region="US",
        impact="HIGH",
        event_at="2026-11-18T13:30:00+00:00",
        note=None,
    )
    await _service(store).update(owner, event_id, changed)
    await _service(store).delete(owner, event_id, reason="Nhập trùng")
    actions = [audit["action"] for audit in store["audits"]]
    assert actions == ["economic_event.create", "economic_event.update", "economic_event.delete"]
    assert store["audits"][1]["before_state"]["title"] == "CPI Mỹ"
    assert store["audits"][1]["after_state"]["title"] == "CPI Mỹ (lõi)"
    assert all(unit["actor_membership_id"] == owner.membership_id for unit in store["units"])
    with pytest.raises(EconomicCalendarError) as missing:
        await _service(store).delete(owner, event_id)
    assert missing.value.status_code == 404
    operator = _principal(Role.OPERATOR)
    with pytest.raises(EconomicCalendarError) as denied:
        await _service(store).create(operator, data)
    assert denied.value.status_code == 403
    assert await _service(store).around(operator, now=NOW) == []  # operator vẫn được xem


def test_migration_creates_rls_table_without_delete_grant() -> None:
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "migrations/versions/b8d0f2a4c6e8_ck10_economic_event.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "a7c9e1b3d5f6"' in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE economic_event TO crypidian_admin_web" in source
    assert "DELETE" not in source.split("def downgrade")[0]  # không cấp quyền xóa vật lý
