"""TG-3: luồng worker gửi kết quả trade PAPER và tổng hợp định kỳ với repository giả.

Fake repository mô phỏng khóa duy nhất của delivery ledger để kiểm idempotency: chạy lại tick
hoặc tạo worker mới (khởi động lại) trên cùng "DB" không sinh tin trùng.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.telegram_signals import SignalSubscriptionTarget
from trading_bot.adapters.database.repositories.telegram_trade_results import (
    PaperReportClaim,
    TradeEvent,
    TradeNotificationClaim,
)
from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.application.telegram_trade_results import (
    BotSummaryRow,
    TradeFillFact,
    TradeNotificationKind,
    TradeResultSource,
)
from trading_bot.workers.telegram_trade_result_worker import (
    ChatPacer,
    TelegramTradeResultWorker,
)

NOW = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOT_A = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
BOT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-cccccccccccc")
SUB_A = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SUB_B = UUID("cccccccc-cccc-4ccc-8ccc-dddddddddddd")
SUB_ETH_ONLY = UUID("cccccccc-cccc-4ccc-8ccc-eeeeeeeeeeee")
TRADE = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CHAT = 987654321
OTHER_CHAT = 123456789


@dataclass
class Row:
    id: UUID
    key: tuple[Any, ...]
    bot_id: UUID | None
    status: str = "PENDING"
    attempts: int = 0
    available_at: datetime = NOW
    lease_owner: str | None = None
    last_error_code: str | None = None
    sent_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class FakeDb:
    """Trạng thái dùng chung giữa các instance repository (như một PostgreSQL thật)."""

    def __init__(self) -> None:
        self.targets: list[SignalSubscriptionTarget] = [
            SignalSubscriptionTarget(SUB_A, BOT_A, CHAT),
            SignalSubscriptionTarget(SUB_B, BOT_B, CHAT),
        ]
        self.events: dict[UUID, list[TradeEvent]] = {
            BOT_A: [
                TradeEvent(TRADE, TradeNotificationKind.OPENED, "BTCUSDT", "LONG"),
                TradeEvent(TRADE, TradeNotificationKind.CLOSED, "BTCUSDT", "LONG"),
            ],
            BOT_B: [],
        }
        self.notifications: dict[tuple[Any, ...], Row] = {}
        self.reports: dict[tuple[Any, ...], Row] = {}
        self.source_missing = False
        self.uow_scopes: list[UUID | None] = []


def _source(bot_id: UUID, kind: TradeNotificationKind) -> TradeResultSource:
    opened = NOW - timedelta(hours=1)
    return TradeResultSource(
        kind=kind,
        bot_id=bot_id,
        bot_name="Bot A",
        trade_id=TRADE,
        symbol="BTCUSDT",
        side="LONG",
        quote_asset="USDT",
        status="CLOSED" if kind is TradeNotificationKind.CLOSED else "OPEN",
        opened_at=opened,
        closed_at=NOW if kind is TradeNotificationKind.CLOSED else None,
        net_pnl=Decimal("12.5"),
        fills=(
            TradeFillFact("ENTRY", Decimal("0.01"), Decimal("68000"), opened),
            TradeFillFact("EXIT", Decimal("0.01"), Decimal("69250"), NOW),
        ),
        reserved_risk=Decimal("5"),
        exit_reason="TAKE_PROFIT",
    )


class FakeUow:
    def __init__(self, db: FakeDb, **kwargs: Any) -> None:
        self.session = db
        db.uow_scopes.append(kwargs.get("bot_id"))

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSignalRepository:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    async def list_enabled_subscriptions(
        self, *, workspace_id: UUID
    ) -> tuple[SignalSubscriptionTarget, ...]:
        assert workspace_id == WORKSPACE
        return tuple(self.db.targets)


class FakeRepository:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    async def recent_trade_events(self, *, bot_id: UUID, since: datetime, **_: Any) -> Any:
        assert since == NOW - timedelta(minutes=60)
        return tuple(self.db.events.get(bot_id, ()))

    async def enqueue_trade_notification(self, **kwargs: Any) -> bool:
        key = (kwargs["subscription_id"], kwargs["trade_id"], kwargs["kind"])
        if key in self.db.notifications:
            return False
        self.db.notifications[key] = Row(uuid4(), key, kwargs["bot_id"])
        return True

    def _claimable(self, rows: dict[tuple[Any, ...], Row], now: datetime) -> Row | None:
        for row in rows.values():
            if row.status == "PENDING" and row.available_at <= now and row.attempts < 5:
                return row
        return None

    async def claim_trade_notification(
        self, *, bot_id: UUID, worker_id: str, now: datetime, **_: Any
    ) -> TradeNotificationClaim | None:
        candidates = {k: v for k, v in self.db.notifications.items() if v.bot_id == bot_id}
        row = self._claimable(candidates, now)
        if row is None:
            return None
        if self.db.source_missing:
            row.status = "DEAD"
            row.last_error_code = "TRADE_RESULT_SOURCE_UNAVAILABLE"
            return None
        row.status, row.lease_owner = "PROCESSING", worker_id
        row.attempts += 1
        subscription_id, trade_id, kind = row.key
        target = next(t for t in self.db.targets if t.subscription_id == subscription_id)
        chat = target.chat_id
        return TradeNotificationClaim(
            notification_id=row.id,
            bot_id=bot_id,
            subscription_id=subscription_id,
            trade_id=trade_id,
            kind=kind,
            attempts=row.attempts,
            chat_id=chat,
            source=_source(bot_id, kind),
            thread_id=target.report_thread_id,
        )

    async def bot_summary(self, *, bot_id: UUID, window_start: datetime, **_: Any) -> Any:
        assert window_start == datetime(2026, 11, 15, 4, tzinfo=UTC)
        return BotSummaryRow(
            bot_id=bot_id,
            bot_name="Bot A" if bot_id == BOT_A else "Bot B",
            quote_asset="USDT",
            window_closed_count=1,
            window_net_pnl=Decimal("12.5") if bot_id == BOT_A else Decimal("-2"),
            total_closed_count=3,
            total_winning_count=2,
            total_net_pnl=Decimal("12.5") if bot_id == BOT_A else Decimal("-2"),
            open_positions=0,
            max_drawdown=Decimal("1"),
        )

    async def enqueue_report(self, *, chat_id: int, window_start: datetime, **kwargs: Any) -> bool:
        key = (chat_id, window_start)
        if key in self.db.reports:
            return False
        self.db.reports[key] = Row(uuid4(), key, None, extra={"window_end": kwargs["window_end"]})
        return True

    async def claim_report(
        self, *, worker_id: str, now: datetime, **_: Any
    ) -> PaperReportClaim | None:
        row = self._claimable(self.db.reports, now)
        if row is None:
            return None
        row.status, row.lease_owner = "PROCESSING", worker_id
        row.attempts += 1
        chat_id, window_start = row.key
        return PaperReportClaim(
            row.id, chat_id, window_start, row.extra["window_end"], row.attempts
        )

    def _row(self, delivery_id: UUID, report: bool) -> Row:
        rows = self.db.reports if report else self.db.notifications
        return next(row for row in rows.values() if row.id == delivery_id)

    async def renew(self, *, delivery_id: UUID, worker_id: str, report: bool, **_: Any) -> bool:
        row = self._row(delivery_id, report)
        return row.status == "PROCESSING" and row.lease_owner == worker_id

    async def mark_sent(self, *, delivery_id: UUID, sent_at: datetime, report: bool, **_: Any):
        row = self._row(delivery_id, report)
        row.status, row.sent_at, row.lease_owner = "SENT", sent_at, None
        return True

    async def mark_failed(
        self,
        *,
        delivery_id: UUID,
        available_at: datetime,
        error_code: str,
        dead: bool,
        report: bool,
        **_: Any,
    ) -> bool:
        row = self._row(delivery_id, report)
        row.status = "DEAD" if dead else "PENDING"
        row.available_at, row.last_error_code, row.lease_owner = available_at, error_code, None
        return True

    async def mark_filtered(self, *, delivery_id: UUID, error_code: str, report: bool, **_: Any):
        row = self._row(delivery_id, report)
        row.status, row.last_error_code, row.lease_owner = "FILTERED", error_code, None
        return True


class FailingSender(RecordingTelegramSender):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def send_message(self, chat_id: int, text: str) -> None:
        raise self.error


def _worker(
    db: FakeDb,
    sender: RecordingTelegramSender | None = None,
    *,
    sleeps: list[float] | None = None,
    **overrides: Any,
) -> tuple[TelegramTradeResultWorker, RecordingTelegramSender]:
    sender = sender or RecordingTelegramSender()
    recorded = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    options: dict[str, Any] = {
        "session_factory": object(),
        "workspace_id": WORKSPACE,
        "sender": sender,
        "worker_id": "tg3-test",
        "now": lambda: NOW,
        "pacer": ChatPacer(now=lambda: NOW, sleep=sleep),
        "unit_of_work": lambda _sessions, **kwargs: FakeUow(db, **kwargs),
        "signal_repository": FakeSignalRepository,
        "repository": FakeRepository,
    }
    options.update(overrides)
    return TelegramTradeResultWorker(**options), sender


@pytest.mark.asyncio
async def test_tick_sends_open_close_and_one_summary_per_chat_then_nothing_on_rerun() -> None:
    db = FakeDb()
    sleeps: list[float] = []
    worker, sender = _worker(db, sleeps=sleeps)

    sent = await worker.tick()

    assert sent == 3
    texts = [text for _, text in sender.messages]
    assert [chat for chat, _ in sender.messages] == [CHAT, CHAT, CHAT]
    assert "Mở vị thế PAPER: BTCUSDT · LONG" in texts[0]
    assert "Đóng lệnh PAPER: BTCUSDT · LONG" in texts[1]
    assert "PnL ròng (sau phí): +12.5 USDT" in texts[1]
    assert "Bội số R: +2.50R" in texts[1]
    assert "Lý do thoát: Chạm mục tiêu chốt lời (TAKE_PROFIT)" in texts[1]
    # Một tin tổng hợp cho chat dù chat theo dõi hai bot; xếp theo PnL ròng.
    assert texts[2].index("1. Bot A") < texts[2].index("2. Bot B")
    assert {row.status for row in db.notifications.values()} == {"SENT"}
    assert [row.status for row in db.reports.values()] == ["SENT"]
    # Giãn cách 1,1 giây giữa các tin cùng chat (đồng hồ giả đứng yên).
    assert sleeps == pytest.approx([1.1, 2.2])

    assert await worker.tick() == 0
    restarted, restarted_sender = _worker(db)
    assert await restarted.tick() == 0
    assert restarted_sender.messages == []
    assert len(db.notifications) == 2
    assert len(db.reports) == 1


@pytest.mark.asyncio
async def test_symbol_filter_of_subscription_is_respected() -> None:
    db = FakeDb()
    db.targets = [
        SignalSubscriptionTarget(SUB_A, BOT_A, CHAT),
        SignalSubscriptionTarget(
            SUB_ETH_ONLY, BOT_A, OTHER_CHAT, TelegramSignalFilters(symbols=("ETHUSDT",))
        ),
    ]
    worker, sender = _worker(db, report_interval=None)

    assert await worker.tick() == 2
    assert {key[0] for key in db.notifications} == {SUB_A}
    assert {chat for chat, _ in sender.messages} == {CHAT}


@pytest.mark.asyncio
async def test_trade_result_messages_are_not_throttled_like_signals() -> None:
    db = FakeDb()
    trades = [uuid4() for _ in range(10)]
    db.events[BOT_A] = [
        TradeEvent(trade_id, TradeNotificationKind.OPENED, "BTCUSDT", "LONG") for trade_id in trades
    ]
    worker, sender = _worker(db, report_interval=None)

    assert await worker.tick() == 10
    assert len(sender.messages) == 10


@pytest.mark.asyncio
async def test_telegram_retry_after_is_persisted_and_not_marked_sent() -> None:
    db = FakeDb()
    sender = FailingSender(TelegramApiError(code="TELEGRAM_RATE_LIMITED", retry_after=17))
    worker, _ = _worker(db, sender, report_interval=None)

    assert await worker.tick() == 0
    rows = list(db.notifications.values())
    assert {row.status for row in rows} == {"PENDING"}
    assert {row.last_error_code for row in rows} == {"TELEGRAM_RATE_LIMITED"}
    assert {row.available_at for row in rows} == {NOW + timedelta(seconds=17)}


@pytest.mark.asyncio
async def test_failure_at_max_attempts_becomes_dead() -> None:
    db = FakeDb()
    worker, _ = _worker(
        db, FailingSender(RuntimeError("boom")), max_attempts=1, report_interval=None
    )

    await worker.tick()

    assert {row.status for row in db.notifications.values()} == {"DEAD"}
    assert {row.last_error_code for row in db.notifications.values()} == {
        "TELEGRAM_RESULT_RUNTIMEERROR"
    }


@pytest.mark.asyncio
async def test_missing_ledger_source_is_dead_without_message() -> None:
    db = FakeDb()
    db.source_missing = True
    worker, sender = _worker(db, report_interval=None)

    assert await worker.tick() == 0
    assert sender.messages == []
    assert "DEAD" in {row.status for row in db.notifications.values()}


@pytest.mark.asyncio
async def test_report_for_chat_without_active_subscription_is_filtered() -> None:
    db = FakeDb()
    worker, _ = _worker(db, trade_results_enabled=False)
    await worker.tick()
    db.reports.clear()
    db.reports[(OTHER_CHAT, datetime(2026, 11, 15, 4, tzinfo=UTC))] = Row(
        uuid4(),
        (OTHER_CHAT, datetime(2026, 11, 15, 4, tzinfo=UTC)),
        None,
        extra={"window_end": datetime(2026, 11, 15, 8, tzinfo=UTC)},
    )
    db.reports[(CHAT, datetime(2026, 11, 15, 4, tzinfo=UTC))] = Row(
        uuid4(), (CHAT, "x"), None, status="SENT"
    )

    await worker.tick()

    statuses = {key[0]: row.status for key, row in db.reports.items()}
    assert statuses[OTHER_CHAT] == "FILTERED"


@pytest.mark.asyncio
async def test_disabled_results_and_reports_do_nothing() -> None:
    db = FakeDb()
    worker, sender = _worker(db, trade_results_enabled=False, report_interval=None)

    assert await worker.tick() == 0
    assert db.uow_scopes == []
    assert sender.messages == []


@pytest.mark.asyncio
async def test_bot_scoped_units_of_work_for_trade_rows_and_workspace_for_reports() -> None:
    db = FakeDb()
    worker, _ = _worker(db)

    await worker.tick()

    assert BOT_A in db.uow_scopes
    assert BOT_B in db.uow_scopes
    assert None in db.uow_scopes


@pytest.mark.asyncio
async def test_custom_send_functions_receive_view_models() -> None:
    db = FakeDb()
    views: list[Any] = []

    async def capture(_sender: Any, chat_id: int, view: Any) -> None:
        views.append((chat_id, view))

    worker, sender = _worker(db, trade_result_send=capture, paper_report_send=capture)

    assert await worker.tick() == 3
    assert sender.messages == []
    assert [type(view).__name__ for _, view in views] == [
        "TradeResultView",
        "TradeResultView",
        "PaperReportView",
    ]


@pytest.mark.asyncio
async def test_chat_pacer_shares_slots_between_workers_of_same_chat() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    pacer = ChatPacer(now=lambda: NOW, sleep=sleep)
    await pacer.wait(CHAT)
    await pacer.wait(OTHER_CHAT)
    await pacer.wait(CHAT)
    pacer.mark_sent(CHAT)
    await pacer.wait(CHAT)

    assert sleeps == pytest.approx([1.1, 2.2])


@pytest.mark.parametrize(
    "overrides",
    [
        {"worker_id": " "},
        {"tick_seconds": 0},
        {"max_event_age": timedelta(0)},
        {"report_interval": timedelta(seconds=10)},
        {"max_attempts": 0},
    ],
)
def test_worker_rejects_invalid_configuration(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="phải|không hợp lệ|tối thiểu"):
        _worker(FakeDb(), **overrides)


@pytest.mark.asyncio
async def test_trade_results_and_summary_go_to_report_topic_when_configured() -> None:
    """TG-4: binding có topic PAPER report → mở/đóng lệnh và tổng hợp đều vào topic đó."""
    db = FakeDb()
    db.targets = [
        SignalSubscriptionTarget(SUB_A, BOT_A, CHAT, report_thread_id=202),
        SignalSubscriptionTarget(SUB_B, BOT_B, CHAT, report_thread_id=202),
    ]
    worker, sender = _worker(db)

    assert await worker.tick() == 3
    assert sender.threads == [("message", CHAT, 202)] * 3


@pytest.mark.asyncio
async def test_trade_results_stay_in_main_chat_without_topic_config() -> None:
    db = FakeDb()
    worker, sender = _worker(db)

    assert await worker.tick() == 3
    assert sender.threads == [("message", CHAT, None)] * 3


@pytest.mark.asyncio
async def test_report_card_text_fallback_stays_in_report_topic(monkeypatch: Any) -> None:
    from trading_bot.adapters.telegram import card_delivery

    def broken(_view: object) -> bytes:
        raise ValueError("render lỗi")

    monkeypatch.setattr(card_delivery, "render_trade_card", broken)
    monkeypatch.setattr(card_delivery, "render_summary_card", broken)
    db = FakeDb()
    db.targets = [SignalSubscriptionTarget(SUB_A, BOT_A, CHAT, report_thread_id=303)]
    worker, sender = _worker(
        db,
        trade_result_send=card_delivery.send_trade_result_card,
        paper_report_send=card_delivery.send_paper_report_card,
    )

    assert await worker.tick() == 3
    assert sender.threads == [("message", CHAT, 303)] * 3
