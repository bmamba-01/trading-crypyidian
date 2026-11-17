"""Unit test cho trang/API/SSE theo dõi bot trực tiếp (LIVE-MON, CRY-429)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.admin import live_monitor_router
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
BOT_ON = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
BOT_OFF = UUID("88228dd5-7d8e-518f-9a34-059e851b57d3")
CURSOR = "2026-11-17T12:00:00+00:00"


class FakeOperations:
    def for_principal(self, principal: Principal) -> FakeOperations:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return [
            {"id": str(BOT_ON), "name": "Bot bật", "status": "ENABLED", "entry_allowed": True},
            {"id": str(BOT_OFF), "name": "Bot tắt", "status": "DISABLED", "entry_allowed": False},
        ]


class FakeLiveService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def read(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        event = {
            "id": f"order:{len(self.calls)}",
            "kind": "ORDER_ACCEPTED",
            "severity": "notice",
            "bot_id": str(BOT_ON),
        }
        return {
            "bot_ids": [str(bot_id) for bot_id in kwargs["bot_ids"]],
            "events": [event],
            "cursor": CURSOR,
            "state": {"observed_at": CURSOR, "positions": []} if kwargs["include_state"] else None,
        }


class FakeAuth:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        if self.fail:
            raise AuthenticationFailure(401, "Phiên hết hạn.")
        return _identity()

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        return "csrf"


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            workspace_id=WORKSPACE,
            membership_id=MEMBERSHIP,
            user_id=UUID(int=9),
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@crypidian.test",
    )


@pytest.fixture
def fast_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_monitor_router, "STREAM_TICK_SECONDS", 0.01)
    monkeypatch.setattr(live_monitor_router, "STREAM_MAX_SECONDS", 0.2)


def _client(service: FakeLiveService | None = None) -> tuple[TestClient, FakeLiveService]:
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = _identity
    app.state.bot_operations_service = FakeOperations()
    app.state.auth_service = FakeAuth()
    live = service or FakeLiveService()
    app.state.live_monitor_service = live
    return TestClient(app), live


def _sse_messages(body: str) -> list[dict[str, str]]:
    messages = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _sep, value = line.partition(":")
            fields[key] = value.strip()
        messages.append(fields)
    return messages


def test_live_page_defaults_to_enabled_bots_and_has_no_control_forms() -> None:
    client, _live = _client()
    response = client.get("/admin/live")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    assert f'data-bot-ids="{BOT_ON}"' in html
    assert 'data-stream-url="/admin/api/live/stream"' in html
    assert "/static/live-monitor.js" in html
    # Chỉ đọc: không có form POST điều khiển trên trang Live.
    assert 'method="post"' not in html.lower()
    assert 'href="/admin/live"' in html  # link trong thanh điều hướng


def test_live_page_respects_selection_and_ignores_foreign_bot() -> None:
    client, _live = _client()
    foreign = UUID(int=5)
    response = client.get(f"/admin/live?bot={BOT_OFF}&bot={foreign}")
    assert f'data-bot-ids="{BOT_OFF}"' in response.text
    assert str(foreign) not in response.text


def test_snapshot_is_no_store_and_parses_cursor() -> None:
    client, live = _client()
    response = client.get(f"/admin/api/live/snapshot?bot={BOT_ON}&since={CURSOR}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"]["positions"] == []
    call = live.calls[-1]
    assert call["bot_ids"] == [BOT_ON]
    assert call["since"].encode() == CURSOR
    assert call["include_state"] is True
    assert set(call["runtime"]) == {str(BOT_ON), str(BOT_OFF)}


def test_snapshot_rejects_too_many_bots() -> None:
    client, _live = _client()
    query = "&".join(f"bot={UUID(int=index)}" for index in range(51))
    assert client.get(f"/admin/api/live/snapshot?{query}").status_code == 422


def test_unauthenticated_requests_are_rejected() -> None:
    app = create_app(AppSettings(app_env="test"))
    app.state.auth_service = FakeAuth(fail=True)
    client = TestClient(app)
    assert client.get("/admin/api/live/snapshot").status_code == 401
    assert client.get("/admin/api/live/stream").status_code == 401


@pytest.mark.usefixtures("fast_stream")
def test_stream_sends_events_with_cursor_id_state_and_rotates() -> None:
    client, live = _client()
    response = client.get(f"/admin/api/live/stream?bot={BOT_ON}", headers={"Last-Event-ID": CURSOR})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    messages = _sse_messages(response.text)
    assert messages[0].get("retry") == "3000"
    events = [message for message in messages if message.get("event") == "events"]
    assert events
    assert events[0]["id"] == CURSOR
    first = json.loads(events[0]["data"])
    assert first["initial"] is True
    assert first["events"][0]["kind"] == "ORDER_ACCEPTED"
    assert any(message.get("event") == "state" for message in messages)
    assert messages[-1].get("event") == "rotate"
    # Last-Event-ID được ưu tiên làm con trỏ; trạng thái gửi cách một nhịp.
    assert live.calls[0]["since"].encode() == CURSOR
    assert [call["include_state"] for call in live.calls[:3]] == [True, False, True]
    # Slot luồng được trả lại sau khi đóng.
    assert client.app.state.live_stream_slots[str(MEMBERSHIP)] == {}  # type: ignore[attr-defined]


@pytest.mark.usefixtures("fast_stream")
def test_stream_closes_with_auth_event_when_session_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_monitor_router, "STREAM_AUTH_RECHECK_SECONDS", 0.0)
    client, _live = _client()
    client.app.state.auth_service = FakeAuth(fail=True)  # type: ignore[attr-defined]
    response = client.get(f"/admin/api/live/stream?bot={BOT_ON}")
    messages = _sse_messages(response.text)
    assert messages[-1].get("event") == "auth"


def test_stream_limit_per_membership_returns_429() -> None:
    client, _live = _client()
    now = __import__("time").monotonic()
    client.app.state.live_stream_slots = {  # type: ignore[attr-defined]
        str(MEMBERSHIP): {f"t{index}": now for index in range(4)}
    }
    response = client.get(f"/admin/api/live/stream?bot={BOT_ON}")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


def test_stream_limit_ignores_expired_slots() -> None:
    client, _live = _client()
    client.app.state.live_stream_slots = {  # type: ignore[attr-defined]
        str(MEMBERSHIP): {f"t{index}": -10_000.0 for index in range(4)}
    }
    token = live_monitor_router._acquire_stream_slot(
        type("R", (), {"app": client.app})(),  # type: ignore[arg-type]
        _identity(),
    )
    assert list(client.app.state.live_stream_slots[str(MEMBERSHIP)]) == [token]  # type: ignore[attr-defined]
