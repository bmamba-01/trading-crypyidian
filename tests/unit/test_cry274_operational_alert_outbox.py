"""Kiểm thử CRY-274: consumer outbox cảnh báo vận hành Telegram, chịu lỗi và hiển thị Admin Web."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    AuditEvent,
    ConsumerReceipt,
    OutboxEvent,
    TelegramBinding,
    Workspace,
)
from trading_bot.application.operational_alert_outbox import (
    OPERATIONAL_ALERT_CONSUMER,
    OperationalAlertOutboxConsumer,
    format_operational_alert_vi,
    list_operational_alerts,
    sanitize_alert_text,
)
from trading_bot.auth.bot_policy import BotPermission


def _run_sync(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


class _StubSender:
    def __init__(self, *, fail_times: int = 0, error: Exception | None = None) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self._fail_times = fail_times
        self._error = error or ConnectionError("Telegram API outage")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._error
        self.sent.append((chat_id, text, message_thread_id))
        return {"ok": True}


class _FakeUnitSession:
    """Session tối giản giả lập PostgreSQL in-memory cho UnitOfWork & OutboxRepository."""

    def __init__(self, store: dict[str, list[Any]]) -> None:
        self._store = store
        self.info: dict[str, Any] = {}
        self._in_tx = False

    async def begin(self) -> None:
        self._in_tx = True

    async def commit(self) -> None:
        self._in_tx = False

    async def rollback(self) -> None:
        self._in_tx = False

    async def close(self) -> None:
        self._in_tx = False

    async def execute(self, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        if "FROM workspace" in sql:
            ws = self._store["workspace"]
            return _ExecuteResult(row=SimpleNamespace(name=ws.name, mode=ws.mode))
        if "FROM telegram_binding" in sql:
            active = [
                SimpleNamespace(
                    telegram_user_id=b.telegram_user_id, report_thread_id=b.report_thread_id
                )
                for b in self._store["bindings"]
                if b.verified_at is not None and b.revoked_at is None
            ]
            return _ExecuteResult(rows=active)
        return _ExecuteResult()

    def add(self, obj: Any) -> None:
        if isinstance(obj, ConsumerReceipt):
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()
            self._store["receipts"].append(obj)
        elif isinstance(obj, AuditEvent):
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()
            self._store["audits"].append(obj)

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement)
        if "FROM workspace" in sql:
            return self._store["workspace"]
        if "FROM consumer_receipt" in sql:
            # Tìm receipt khớp outbox_event_id trong store
            compiled = statement.compile()
            params = list(compiled.params.values())
            for receipt in self._store["receipts"]:
                if receipt.outbox_event_id in params and receipt.consumer_name in params:
                    return receipt.id
            return None
        if "FROM outbox_event" in sql:
            compiled = statement.compile()
            params = list(compiled.params.values())
            for event in self._store["events"]:
                if event.id in params:
                    return event
            return None
        return None

    async def scalars(self, statement: Any) -> Any:
        sql = str(statement)
        if "FROM outbox_event" in sql:
            events = self._store["events"]
            if "FOR UPDATE" in sql:
                # claim_ready: lọc PENDING/PROCESSING, available_at <= now, attempts < max
                compiled = statement.compile()
                params = compiled.params
                now = next(
                    (v for v in params.values() if isinstance(v, datetime)),
                    datetime.now(UTC),
                )
                max_attempts = next(
                    (v for k, v in params.items() if "attempts" in k and isinstance(v, int)),
                    5,
                )
                ready = [
                    ev
                    for ev in events
                    if ev.status in ("PENDING", "PROCESSING")
                    and ev.available_at <= now
                    and ev.attempts < max_attempts
                    and (ev.lease_expires_at is None or ev.lease_expires_at <= now)
                ]
                return _ScalarResult(ready)
            return _ScalarResult(list(events))
        if "FROM telegram_binding" in sql:
            active = [
                b
                for b in self._store["bindings"]
                if b.verified_at is not None and b.revoked_at is None
            ]
            return _ScalarResult(active)
        if "FROM consumer_receipt" in sql:
            return _ScalarResult([r.outbox_event_id for r in self._store["receipts"]])
        return _ScalarResult([])


class _ExecuteResult:
    def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row = row
        self._rows = rows if rows is not None else []

    def one_or_none(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return list(self._rows)


class _ScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)


def _make_store(
    workspace_id: UUID,
) -> tuple[dict[str, list[Any]], async_sessionmaker[AsyncSession]]:
    now = datetime.now(UTC)
    ws = Workspace(id=workspace_id, name="Crypidian Paper", slug="crypidian-paper", mode="PAPER")
    binding = TelegramBinding(
        id=uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid4(),
        telegram_user_id="99887766",
        report_thread_id=42,
        verified_at=now,
        revoked_at=None,
    )
    store: dict[str, Any] = {
        "workspace": ws,
        "bindings": [binding],
        "events": [],
        "receipts": [],
        "audits": [],
    }
    factory: Any = lambda: _FakeUnitSession(store)  # noqa: E731
    return store, factory


def _make_event(
    workspace_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        event_type=event_type,
        aggregate_type="bot_instance",
        aggregate_id=uuid4(),
        payload=payload,
        status="PENDING",
        attempts=0,
        available_at=now - timedelta(seconds=1),
        created_at=now - timedelta(seconds=1),
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        completed_at=None,
    )


def test_format_operational_alert_vi_includes_required_fields_and_redacts_secrets() -> None:
    """Tin tiếng Việt nêu rõ mode/workspace/lý do/link và tuyệt đối không lộ secret/token."""
    now = datetime.now(UTC)
    ws_id = uuid4()
    bot_id = str(uuid4())
    event = _make_event(
        ws_id,
        "bot.paused",
        {
            "bot_id": bot_id,
            "reason": (
                "Tạm dừng khẩn cấp; api_key=SUPER_SECRET_123 "
                "Bearer eyJhbGciOiJIUzI1NiJ9.abcdef "
                "123456789:AAH_FakeTelegramBotTokenValue12345"
            ),
        },
        now=now,
    )

    msg = format_operational_alert_vi(
        event,
        mode="PAPER",
        workspace_label="Crypidian Demo",
        admin_base_url="https://admin.crypidian.local",
    )

    assert "[CẢNH BÁO VẬN HÀNH · PAPER]" in msg.text_vi
    assert "Tạm dừng bot" in msg.text_vi
    assert "Workspace: Crypidian Demo" in msg.text_vi
    assert f"https://admin.crypidian.local/admin/bots/{bot_id}" in msg.text_vi
    assert "SUPER_SECRET_123" not in msg.text_vi
    assert "eyJhbGciOiJIUzI1NiJ9" not in msg.text_vi
    assert "AAH_FakeTelegramBotTokenValue12345" not in msg.text_vi
    assert "[ĐÃ ẨN BÍ MẬT]" in msg.text_vi
    assert sanitize_alert_text("password=my_pass_999") == "[ĐÃ ẨN BÍ MẬT]"


@pytest.mark.asyncio
async def test_consumer_delivers_idempotently_and_audits() -> None:
    """Gửi thành công tạo ConsumerReceipt + AuditEvent; chạy lặp lại không gửi trùng."""
    ws_id = uuid4()
    now = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    store, sessions = _make_store(ws_id)
    event = _make_event(
        ws_id,
        "bot.killed",
        {"bot_id": str(uuid4()), "reason": "Vượt ngưỡng drawdown"},
        now=now,
    )
    store["events"].append(event)
    sender = _StubSender()
    consumer = OperationalAlertOutboxConsumer(
        sessions,
        sender=sender,
        workspace_id=ws_id,
        admin_base_url="https://admin.local",
        now_provider=lambda: now,
    )

    first = await consumer.process_once(workspace_id=ws_id)
    assert first.claimed == 1
    assert first.delivered == 1
    assert first.skipped_duplicate == 0
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == 99887766
    assert sender.sent[0][2] == 42
    assert event.status == "DELIVERED"
    assert len(store["receipts"]) == 1
    assert any(a.action == "operational_alert.delivered" for a in store["audits"])

    # Giả lập event bị đưa lại PENDING (replay/update lặp): có receipt nên không gửi trùng
    event.status = "PENDING"
    event.available_at = now - timedelta(seconds=1)
    event.attempts = 0
    second = await consumer.process_once(workspace_id=ws_id)
    assert second.claimed == 1
    assert second.delivered == 0
    assert second.skipped_duplicate == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_telegram_outage_retries_then_dead_letters_and_remains_visible_on_web() -> None:
    """Telegram outage áp backoff, chuyển DEAD sau max_attempts và vẫn hiển thị trên Admin Web."""
    ws_id = uuid4()
    current_time = [datetime(2026, 11, 19, 10, 0, tzinfo=UTC)]
    store, sessions = _make_store(ws_id)
    event = _make_event(
        ws_id,
        "ops.circuit_breaker.tripped",
        {"reason": "Lỗ ngày chạm trần 3%"},
        now=current_time[0],
    )
    store["events"].append(event)
    sender = _StubSender(fail_times=5, error=TimeoutError("Telegram timeout"))
    consumer = OperationalAlertOutboxConsumer(
        sessions,
        sender=sender,
        workspace_id=ws_id,
        max_attempts=2,
        base_delay_seconds=10,
        max_delay_seconds=60,
        now_provider=lambda: current_time[0],
    )

    # Lần 1 thất bại -> quay về PENDING với backoff 10s
    r1 = await consumer.process_once(workspace_id=ws_id)
    assert r1.retried == 1
    assert r1.dead_lettered == 0
    assert event.status == "PENDING"
    assert event.last_error_code == "TimeoutError"
    assert event.available_at == current_time[0] + timedelta(seconds=10)

    # Trước khi hết backoff -> không claim sớm
    r_early = await consumer.process_once(workspace_id=ws_id)
    assert r_early.claimed == 0

    # Hết backoff, lần 2 thất bại -> chuyển DEAD + ghi audit dead_lettered
    current_time[0] = current_time[0] + timedelta(seconds=15)
    r2 = await consumer.process_once(workspace_id=ws_id)
    assert r2.dead_lettered == 1
    assert event.status == "DEAD"
    assert any(a.action == "operational_alert.dead_lettered" for a in store["audits"])

    # Admin Web vẫn đọc được cảnh báo DEAD từ outbox dù Telegram outage
    web_alerts = await list_operational_alerts(
        _FakeUnitSession(store),  # type: ignore[arg-type]
        workspace_id=ws_id,
        include_delivered=False,
    )
    assert len(web_alerts) == 1
    assert web_alerts[0].event_id == event.id
    assert web_alerts[0].status == "DEAD"
    assert web_alerts[0].last_error_code == "TimeoutError"
    assert "Lỗ ngày chạm trần 3%" in web_alerts[0].reason
    assert web_alerts[0].admin_link == "/admin/live"
    _ = (OPERATIONAL_ALERT_CONSUMER, select)


def test_control_pause_emits_outbox_alert_once_and_noop_emits_nothing() -> None:
    """CRY-274: pause/resume phát sự kiện outbox cùng transaction; update lặp không phát thêm."""
    from tests.unit.test_bot_runtime_control import BOT_A, WORKSPACE, Store, _operator, _service

    store = Store()
    operator = _operator(
        store, BotPermission.READ, BotPermission.RUNTIME_PAUSE, BotPermission.RUNTIME_RESUME
    )
    control = _service(store).for_principal(operator)

    paused = _run_sync(control.pause_bot(BOT_A, "Bảo trì khẩn cấp"))
    assert paused.changed is True
    assert len(store.outbox) == 1
    envelope = store.outbox[0]
    assert envelope.event_type == "bot.paused"
    assert envelope.workspace_id == WORKSPACE
    assert envelope.payload["bot_id"] == str(BOT_A)
    assert envelope.payload["reason"] == "Bảo trì khẩn cấp"
    assert envelope.payload["reason_code"] == "OPERATOR_PAUSE"
    assert envelope.idempotency_key == f"ops-alert:bot-control:{BOT_A}:{paused.revision}"

    # Update lặp (noop): không phát thêm sự kiện, không gửi trùng.
    again = _run_sync(control.pause_bot(BOT_A, "Bảo trì khẩn cấp"))
    assert again.changed is False
    assert len(store.outbox) == 1

    resumed = _run_sync(
        control.resume_bot(BOT_A, "Đã bảo trì xong", expected_revision=again.revision)
    )
    assert resumed.changed is True
    assert len(store.outbox) == 2
    assert store.outbox[1].event_type == "bot.resumed"


def test_workspace_kill_and_release_emit_outbox_alerts() -> None:
    """CRY-274: kill switch workspace phát bot.killed / bot.kill_released khi đổi trạng thái."""
    from tests.unit.test_bot_runtime_control import WORKSPACE, Store, _service, _wide

    from trading_bot.auth.policy import Role

    store = Store()
    owner = _wide(store, Role.OWNER)
    control = _service(store).for_principal(owner)

    killed = _run_sync(control.pause_workspace("Sự cố nguồn nến"))
    assert killed.changed is True
    assert len(store.outbox) == 1
    assert store.outbox[0].event_type == "bot.killed"
    assert store.outbox[0].aggregate_id == WORKSPACE
    assert store.outbox[0].payload["reason"] == "Sự cố nguồn nến"

    noop = _run_sync(control.pause_workspace("Sự cố nguồn nến"))
    assert noop.changed is False
    assert len(store.outbox) == 1

    released = _run_sync(control.release_workspace("Đã khắc phục", expected_revision=noop.revision))
    assert released.changed is True
    assert len(store.outbox) == 2
    assert store.outbox[1].event_type == "bot.kill_released"
