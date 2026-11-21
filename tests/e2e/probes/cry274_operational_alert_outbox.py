# ruff: noqa: F821
"""Probe CRY-274: outbox cảnh báo vận hành chịu Telegram outage trên PostgreSQL thật (Compose).

Chuỗi pha (mỗi pha chạy trong container của service giữ role DB tương ứng):
- ``seed`` (migrator): workspace PAPER, owner/operator, bot ENABLED, binding Telegram đã verify,
  2 sự kiện cầu dao outbox (một sẵn sàng, một khóa tương lai).
- ``admin_pause`` (admin-web): pause thật qua ``BotRuntimeControlService`` → đổi trạng thái +
  phát ``bot.paused`` cùng transaction; pause lặp (noop) không phát thêm.
- ``worker_outage`` (signal-worker): sender lỗi toàn bộ → retry backoff rồi DEAD sau max_attempts;
  trạng thái control không đổi vì gửi nằm ngoài transaction điều khiển.
- ``unblock`` (migrator): mở khóa sự kiện cầu dao thứ hai.
- ``worker_deliver`` (signal-worker): sender tốt → DELIVERED + ConsumerReceipt + audit.
- ``replay`` (migrator): đưa sự kiện đã gửi về PENDING (giả lập update lặp/replay).
- ``worker_idempotent`` (signal-worker): có receipt → không gửi trùng, chỉ acknowledge.
- ``admin_verify`` (admin-web): cảnh báo DEAD vẫn đọc được qua ``list_operational_alerts``;
  control vẫn PAUSED; sự kiện replay DELIVERED.
- ``inspect`` (migrator): receipt/audit đúng consumer, không lộ secret trong audit.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

PHASE = os.environ["CRY274_PHASE"]
STATE = json.loads(os.environ.get("CRY274_STATE", "{}"))
CONSUMER = "telegram_operational_alert_worker"


def _password() -> str:
    with open(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"], encoding="utf-8") as handle:
        return handle.read().strip()


def _db_kwargs() -> dict[str, str]:
    return {
        "host": os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        "dbname": os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        "user": os.environ["TRADING_BOT_DATABASE_USER"],
        "password": _password(),
    }


def _pg_connect():
    import psycopg

    return psycopg.connect(**_db_kwargs())


def _sessions():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    kwargs = _db_kwargs()
    url = (
        f"postgresql+psycopg://{kwargs['user']}:{kwargs['password']}"
        f"@{kwargs['host']}:5432/{kwargs['dbname']}"
    )
    engine = create_async_engine(url)
    return async_sessionmaker(engine, expire_on_commit=False)


def _insert(connection, table: str, **values) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    columns = list(values)
    payload = [Jsonb(v) if isinstance(v, dict) else v for v in values.values()]
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        ),
        payload,
    )


def _seed() -> dict:
    now = datetime.now(UTC)
    workspace = uuid4()
    owner_user, operator_user = uuid4(), uuid4()
    owner, operator = uuid4(), uuid4()
    account = uuid4()
    breaker_ready, breaker_later = uuid4(), uuid4()
    with _pg_connect() as connection, connection.transaction():
        _insert(
            connection,
            "workspace",
            id=workspace,
            name="CRY-274 E2E",
            slug=f"cry274-{workspace.hex[:8]}",
            mode="PAPER",
            status="ACTIVE",
        )
        for user, email in ((owner_user, "owner"), (operator_user, "operator")):
            _insert(
                connection,
                "user_account",
                id=user,
                email_normalized=f"cry274-{email}-{user.hex[:6]}@example.test",
                password_hash="unused",
                status="ACTIVE",
            )
        for member, user, role in (
            (owner, owner_user, "owner"),
            (operator, operator_user, "operator"),
        ):
            _insert(
                connection,
                "user_membership",
                id=member,
                workspace_id=workspace,
                user_id=user,
                status="ACTIVE",
            )
            _insert(
                connection,
                "role_binding",
                id=uuid4(),
                workspace_id=workspace,
                membership_id=member,
                role=role,
            )
        _insert(
            connection,
            "trading_account",
            id=account,
            workspace_id=workspace,
            name="cry274-bot",
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status="ENABLED",
            max_notional=Decimal("10000"),
        )
        bot = connection.execute(
            "SELECT id FROM bot_instance WHERE trading_account_id=%s", (account,)
        ).fetchone()[0]
        for permission in ("bot:read", "bot:runtime:pause", "bot:runtime:resume"):
            _insert(
                connection,
                "bot_grant",
                id=uuid4(),
                workspace_id=workspace,
                bot_id=bot,
                membership_id=operator,
                permission=permission,
                granted_by=owner,
            )
        for member, chat, thread in ((owner, "777001", None), (operator, "777002", 42)):
            _insert(
                connection,
                "telegram_binding",
                id=uuid4(),
                workspace_id=workspace,
                membership_id=member,
                telegram_user_id=chat,
                verified_at=now,
                report_thread_id=thread,
            )
        for event_id, available, key in (
            (breaker_ready, now - timedelta(seconds=5), "cry274-breaker-ready"),
            (breaker_later, now + timedelta(hours=1), "cry274-breaker-later"),
        ):
            _insert(
                connection,
                "outbox_event",
                id=event_id,
                workspace_id=workspace,
                event_type="ops.circuit_breaker.tripped",
                aggregate_type="circuit_breaker",
                aggregate_id=bot,
                occurred_at=now,
                correlation_id=uuid4(),
                idempotency_key=key,
                schema_version=1,
                payload={
                    "workspace_id": str(workspace),
                    "bot_id": str(bot),
                    "reason": "Lỗ ngày chạm trần E2E",
                    "reasons": ["DAILY_LOSS_CAP"],
                },
                status="PENDING",
                attempts=0,
                available_at=available,
            )
    return {
        "workspace": str(workspace),
        "owner": str(owner),
        "owner_user": str(owner_user),
        "operator": str(operator),
        "bot": str(bot),
        "breaker_ready": str(breaker_ready),
        "breaker_later": str(breaker_later),
    }


def _principal(workspace: str, membership: str, user: str):
    from trading_bot.auth.policy import Principal, Role

    return Principal(
        user_id=UUID(user),
        membership_id=UUID(membership),
        workspace_id=UUID(workspace),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=1),
    )


def _admin_pause() -> dict:
    from sqlalchemy import select

    from trading_bot.adapters.database.models import OutboxEvent
    from trading_bot.application.bot_runtime_control import BotRuntimeControlService
    from trading_bot.application.outbox import UnitOfWork

    sessions = _sessions()
    workspace = UUID(STATE["workspace"])
    bot = UUID(STATE["bot"])
    principal = _principal(STATE["workspace"], STATE["owner"], STATE["owner_user"])
    control = BotRuntimeControlService(sessions).for_principal(principal)

    async def run() -> dict:
        paused = await control.pause_bot(bot, "CRY-274 E2E: tạm dừng khi Telegram outage")
        assert paused.changed is True, "pause lần đầu phải đổi trạng thái"
        again = await control.pause_bot(bot, "CRY-274 E2E: tạm dừng khi Telegram outage")
        assert again.changed is False, "pause lặp phải noop"
        async with UnitOfWork(sessions, workspace_id=workspace) as unit:
            events = (
                await unit.session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.workspace_id == workspace,
                        OutboxEvent.event_type == "bot.paused",
                    )
                )
            ).all()
            assert len(events) == 1, "noop không được phát thêm sự kiện"
            event = events[0]
            assert event.payload["reason_code"] == "OPERATOR_PAUSE"
            assert event.payload["bot_id"] == str(bot)
            return {"pause_event": str(event.id), "revision": paused.revision}

    return asyncio.run(run())


class _FailingSender:
    async def send_message(self, chat_id, text, **kwargs):
        raise ConnectionError("CRY-274 E2E Telegram outage")


class _RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []

    async def send_message(
        self, chat_id, text, *, message_thread_id=None, reply_to_message_id=None
    ):
        self.sent.append((chat_id, text, message_thread_id))
        return len(self.sent)


def _worker_outage() -> dict:
    from trading_bot.application.operational_alert_outbox import OperationalAlertOutboxConsumer

    sessions = _sessions()
    workspace = UUID(STATE["workspace"])
    clock = [datetime.now(UTC)]
    consumer = OperationalAlertOutboxConsumer(
        sessions,
        sender=_FailingSender(),  # type: ignore[arg-type]
        workspace_id=workspace,
        max_attempts=2,
        base_delay_seconds=10,
        max_delay_seconds=60,
        now_provider=lambda: clock[0],
    )

    async def run() -> dict:
        first = await consumer.process_once(workspace_id=workspace)
        assert first.claimed == 2, f"phải claim bot.paused + cầu dao: {first}"
        assert first.retried == 2
        assert first.dead_lettered == 0
        early = await consumer.process_once(workspace_id=workspace)
        assert early.claimed == 0, "backoff chưa hết không được claim sớm"
        clock[0] = clock[0] + timedelta(seconds=15)
        second = await consumer.process_once(workspace_id=workspace)
        assert second.dead_lettered == 2, f"max_attempts=2 phải DEAD: {second}"
        return {"dead_lettered": second.dead_lettered}

    return asyncio.run(run())


def _unblock() -> dict:
    with _pg_connect() as connection, connection.transaction():
        connection.execute(
            "UPDATE outbox_event SET available_at=now() WHERE id=%s",
            (UUID(STATE["breaker_later"]),),
        )
    return {}


def _worker_deliver() -> dict:
    from trading_bot.application.operational_alert_outbox import OperationalAlertOutboxConsumer

    sessions = _sessions()
    workspace = UUID(STATE["workspace"])
    sender = _RecordingSender()
    consumer = OperationalAlertOutboxConsumer(
        sessions,
        sender=sender,  # type: ignore[arg-type]
        workspace_id=workspace,
        max_attempts=2,
    )

    async def run() -> dict:
        summary = await consumer.process_once(workspace_id=workspace)
        assert summary.delivered == 1, f"sự kiện mở khóa phải gửi được: {summary}"
        # Một sự kiện gửi tới mọi binding còn hiệu lực: owner (chat chính) + operator (topic 42).
        assert len(sender.sent) == 2, f"phải gửi đủ 2 đích: {sender.sent}"
        by_chat = {chat: (text, thread) for chat, text, thread in sender.sent}
        assert set(by_chat) == {777001, 777002}
        assert by_chat[777001][1] is None
        assert by_chat[777002][1] == 42
        for text, _ in by_chat.values():
            assert "[CẢNH BÁO VẬN HÀNH · PAPER]" in text
            assert "Cầu dao rủi ro kích hoạt" in text
            assert "Lỗ ngày chạm trần E2E" in text
        return {"delivered": summary.delivered, "chats": sorted(by_chat)}

    return asyncio.run(run())


def _replay() -> dict:
    with _pg_connect() as connection, connection.transaction():
        connection.execute(
            "UPDATE outbox_event SET status='PENDING', attempts=0, available_at=now(), "
            "completed_at=NULL WHERE id=%s",
            (UUID(STATE["breaker_later"]),),
        )
    return {}


def _worker_idempotent() -> dict:
    from trading_bot.application.operational_alert_outbox import OperationalAlertOutboxConsumer

    sessions = _sessions()
    workspace = UUID(STATE["workspace"])
    sender = _RecordingSender()
    consumer = OperationalAlertOutboxConsumer(
        sessions,
        sender=sender,  # type: ignore[arg-type]
        workspace_id=workspace,
        max_attempts=2,
    )

    async def run() -> dict:
        summary = await consumer.process_once(workspace_id=workspace)
        assert summary.skipped_duplicate == 1, f"receipt phải chặn gửi trùng: {summary}"
        assert summary.delivered == 0
        assert not sender.sent
        return {"skipped_duplicate": summary.skipped_duplicate}

    return asyncio.run(run())


def _admin_verify() -> dict:
    from trading_bot.adapters.database.repositories.bot_runtime_controls import (
        BotRuntimeControlRepository,
    )
    from trading_bot.application.operational_alert_outbox import list_operational_alerts
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.execution.runtime_control import BotEntryState

    sessions = _sessions()
    workspace = UUID(STATE["workspace"])
    bot = UUID(STATE["bot"])

    async def run() -> dict:
        # RLS admin-web trên bảng control đọc theo membership ACTIVE (app.membership_id).
        async with UnitOfWork(
            sessions,
            workspace_id=workspace,
            actor_membership_id=UUID(STATE["owner"]),
        ) as unit:
            alerts = await list_operational_alerts(
                unit.session, workspace_id=workspace, include_delivered=True, limit=50
            )
            by_event = {str(a.event_id): a for a in alerts}
            dead = by_event[STATE["pause_event"]]
            assert dead.status == "DEAD"
            assert dead.attempts == 2
            assert dead.last_error_code == "ConnectionError"
            assert "CRY-274 E2E" in dead.reason
            assert dead.admin_link.endswith(f"/admin/bots/{bot}")
            delivered = by_event[STATE["breaker_later"]]
            assert delivered.status == "DELIVERED"
            control = await BotRuntimeControlRepository(unit.session).current_bot(
                workspace_id=workspace, bot_id=bot
            )
            assert control is not None
            assert control.entry_state is BotEntryState.PAUSED, (
                "Telegram outage không được đổi trạng thái pause"
            )
            return {"dead_visible": True, "still_paused": True}

    return asyncio.run(run())


def _inspect() -> dict:
    with _pg_connect() as connection:
        receipts = connection.execute(
            "SELECT outbox_event_id FROM consumer_receipt WHERE workspace_id=%s "
            "AND consumer_name=%s",
            (UUID(STATE["workspace"]), CONSUMER),
        ).fetchall()
        audits = connection.execute(
            "SELECT action, reason FROM audit_event WHERE workspace_id=%s "
            "AND action LIKE 'operational_alert.%%'",
            (UUID(STATE["workspace"]),),
        ).fetchall()
    assert len(receipts) == 1, f"1 event đã gửi phải có 1 receipt: {receipts}"
    assert receipts[0][0] == UUID(STATE["breaker_later"])
    actions = sorted(action for action, _ in audits)
    assert actions.count("operational_alert.dead_lettered") == 2
    assert actions.count("operational_alert.delivered") == 1
    for _, reason in audits:
        assert "password" not in (reason or "").lower()
    return {"receipts": len(receipts), "audits": actions}


_PHASES = {
    "seed": _seed,
    "admin_pause": _admin_pause,
    "worker_outage": _worker_outage,
    "unblock": _unblock,
    "worker_deliver": _worker_deliver,
    "replay": _replay,
    "worker_idempotent": _worker_idempotent,
    "admin_verify": _admin_verify,
    "inspect": _inspect,
}

if __name__ == "__main__":
    print(json.dumps(_PHASES[PHASE]()))
