"""Unit test cho dashboard trạng thái liên kết, lịch sử delivery & test message (CRY-347)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.adapters.database.models.entities import AuditEvent
from trading_bot.admin.telegram_delivery_service import (
    list_telegram_delivery_dashboard,
    send_telegram_test_message,
)
from trading_bot.admin.telegram_signal_router import router as telegram_signal_router
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity


class _StubSession:
    """Session giả lập tối giản cho list_telegram_delivery_dashboard & test message."""

    def __init__(
        self,
        *,
        bots: Sequence[tuple[object, str]] | None = None,
        bindings: Sequence[object] | None = None,
        subscriptions: Sequence[object] | None = None,
        deliveries: Sequence[object] | None = None,
        scalar_binding: object | None = None,
    ) -> None:
        self._bots = bots or []
        self._bindings = bindings or []
        self._subscriptions = subscriptions or []
        self._deliveries = deliveries or []
        self._scalar_binding = scalar_binding
        self.added: list[object] = []

    async def execute(self, _stmt: object) -> object:
        rows = [SimpleNamespace(id=bid, name=name) for bid, name in self._bots]
        return SimpleNamespace(all=lambda: rows)

    async def scalars(self, stmt: object) -> object:
        sql_text = str(stmt)
        if "FROM telegram_binding" in sql_text:
            data = self._bindings
        elif "FROM telegram_signal_subscription" in sql_text:
            data = self._subscriptions
        else:
            data = self._deliveries
        return SimpleNamespace(all=lambda: data)

    async def scalar(self, _stmt: object) -> object:
        return self._scalar_binding

    def add(self, obj: object) -> None:
        self.added.append(obj)


@pytest.mark.asyncio
async def test_list_telegram_delivery_dashboard_counts_and_privacy() -> None:
    ws_id = uuid4()
    bot_id = uuid4()
    bind_id = uuid4()
    now = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
    binding = SimpleNamespace(
        id=bind_id,
        revoked_at=None,
        verified_at=now - timedelta(days=1),
        telegram_user_id="999888777",
    )
    sub = SimpleNamespace(
        id=uuid4(),
        bot_id=bot_id,
        telegram_binding_id=bind_id,
        enabled=True,
        telegram_chat_id=999888777,
    )
    deliveries = [
        SimpleNamespace(
            id=uuid4(),
            bot_id=bot_id,
            decision_id=uuid4(),
            status="SENT",
            attempts=1,
            last_error_code=None,
            created_at=now - timedelta(minutes=5),
            available_at=now - timedelta(minutes=5),
            sent_at=now - timedelta(minutes=4),
        ),
        SimpleNamespace(
            id=uuid4(),
            bot_id=bot_id,
            decision_id=uuid4(),
            status="DEAD",
            attempts=1,
            last_error_code="TELEGRAM_HTTP_403",
            created_at=now - timedelta(minutes=10),
            available_at=now - timedelta(minutes=10),
            sent_at=None,
        ),
        SimpleNamespace(
            id=uuid4(),
            bot_id=bot_id,
            decision_id=uuid4(),
            status="FILTERED",
            attempts=0,
            last_error_code=None,
            created_at=now - timedelta(minutes=12),
            available_at=now - timedelta(minutes=12),
            sent_at=None,
        ),
        SimpleNamespace(
            id=uuid4(),
            bot_id=bot_id,
            decision_id=uuid4(),
            status="EXPIRED",
            attempts=0,
            last_error_code=None,
            created_at=now - timedelta(minutes=15),
            available_at=now - timedelta(minutes=15),
            sent_at=None,
        ),
        SimpleNamespace(
            id=uuid4(),
            bot_id=bot_id,
            decision_id=uuid4(),
            status="SUPPRESSED",
            attempts=0,
            last_error_code=None,
            created_at=now - timedelta(minutes=20),
            available_at=now - timedelta(minutes=20),
            sent_at=None,
        ),
    ]
    session = _StubSession(
        bots=[(bot_id, "Bot-H01")],
        bindings=[binding],
        subscriptions=[sub],
        deliveries=deliveries,
    )
    payload = await list_telegram_delivery_dashboard(
        session,  # type: ignore[arg-type]
        workspace_id=ws_id,
        allowed_bot_ids=frozenset({bot_id}),
    )
    assert payload["summary"]["sent_count"] == 1
    assert payload["summary"]["dead_count"] == 1
    assert payload["summary"]["filtered_count"] == 1
    assert payload["summary"]["expired_count"] == 1
    assert payload["summary"]["suppressed_count"] == 1
    assert payload["summary"]["last_error_code"] == "TELEGRAM_HTTP_403"
    assert "999888777" not in json.dumps(payload, ensure_ascii=False)

    # Nếu không có quyền đọc bot nào -> trả payload rỗng an toàn
    empty = await list_telegram_delivery_dashboard(
        session,  # type: ignore[arg-type]
        workspace_id=ws_id,
        allowed_bot_ids=frozenset(),
    )
    assert empty["summary"]["total"] == 0
    assert empty["deliveries"] == []


@pytest.mark.asyncio
async def test_send_telegram_test_message_sends_and_audits_without_signals() -> None:
    ws_id = uuid4()
    mem_id = uuid4()
    bind_id = uuid4()
    now = datetime(2026, 11, 24, 13, 15, tzinfo=UTC)
    binding = SimpleNamespace(
        id=bind_id,
        telegram_user_id="123456789",
        signal_thread_id=77,
    )
    sent_calls: list[tuple[int, str, int | None]] = []

    class _StubSender:
        async def send_message(
            self, chat_id: int, text: str, *, message_thread_id: int | None = None
        ) -> dict[str, object]:
            sent_calls.append((chat_id, text, message_thread_id))
            return {"ok": True}

    session = _StubSession(scalar_binding=binding)
    res = await send_telegram_test_message(
        session,  # type: ignore[arg-type]
        workspace_id=ws_id,
        membership_id=mem_id,
        sender=_StubSender(),  # type: ignore[arg-type]
        request_id="req-test-1",
        now=now,
    )
    assert res["sent"] is True
    assert len(sent_calls) == 1
    assert sent_calls[0][0] == 123456789
    assert sent_calls[0][2] == 77
    assert "không tạo tín hiệu hay lệnh" in sent_calls[0][1]
    assert len(session.added) == 1
    audit = session.added[0]
    assert isinstance(audit, AuditEvent)
    assert audit.action == "telegram.test_message.sent"
    assert audit.target_id == bind_id


def test_telegram_delivery_dashboard_http_endpoints() -> None:
    app = FastAPI()
    app.include_router(admin_web_router)
    app.include_router(telegram_signal_router)
    identity = AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=uuid4(),
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@crypidian.test",
    )
    app.dependency_overrides[current_identity] = lambda: identity
    app.dependency_overrides[require_csrf] = lambda: identity

    client = TestClient(app)
    r1 = client.get("/admin/api/telegram/deliveries")
    assert r1.status_code == 200
    assert r1.headers.get("cache-control") == "no-store"
    assert "deliveries" in r1.json()

    r2 = client.get(f"/api/v1/telegram-signals/{uuid4()}/deliveries")
    assert r2.status_code == 200
    assert r2.headers.get("cache-control") == "no-store"

    r3 = client.post("/admin/api/telegram/test-message", json={})
    assert r3.status_code == 503
