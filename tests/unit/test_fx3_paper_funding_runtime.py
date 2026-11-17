"""FX-3 lát 2 (CRY-198): runtime PAPER tự ghi funding đã chốt cho vị thế đang mở."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.test_paper_execution import _plan
from tests.unit.test_paper_lifecycle_checkpoints import (
    CallbackUow,
    MemoryCheckpoints,
    MemoryLedger,
    _gateway,
)
from tests.unit.test_rt1_paper_market_dispatch import _FakeApplication, _row

from trading_bot.adapters.database.repositories.funding_settlements import (
    FundingSettlementRepository,
)
from trading_bot.application import paper_lifecycle
from trading_bot.application.paper_lifecycle import (
    PaperLifecycleConflict,
    PaperLifecycleCoordinator,
)
from trading_bot.application.paper_market_event_loop import (
    FUNDING_MARK_PRICE_MISSING_REASON,
    FUNDING_RATE_MISSING_REASON,
    FundingMoment,
    PaperMarketEventLoop,
    _Scope,
    funding_moments_from,
    market_event_scope,
    paper_bar_from_row,
)
from trading_bot.domain.execution.funding import FundingSettlement
from trading_bot.domain.execution.paper import PaperBar

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
WORKSPACE = UUID("11111111-1111-1111-1111-111111111111")


# --- coordinator ---------------------------------------------------------------------------------
class FundingLedger(MemoryLedger):
    """Ledger giả có fill theo thời gian và trade_cost idempotent theo cost_key."""

    def __init__(self) -> None:
        super().__init__()
        self.fill_rows: list[tuple[str, Decimal, datetime]] = []
        self.costs: dict[str, dict[str, Any]] = {}

    async def append_fill(self, _uow: object, **values: Any) -> bool:
        created = await super().append_fill(_uow, **values)
        if created:
            self.fill_rows.append(("ENTRY", values["quantity"], values["occurred_at"]))
        return created

    async def open_quantity_at(self, _uow: object, *, intent_id: object, at: datetime) -> Decimal:
        del intent_id
        total = Decimal(0)
        for role, quantity, occurred_at in self.fill_rows:
            if role == "ENTRY" and occurred_at < at:
                total += quantity
            elif role == "EXIT" and occurred_at <= at:
                total -= quantity
        return total

    async def append_trade_cost(self, _uow: object, **values: Any) -> bool:
        key = values["cost_key"]
        if key in self.costs:
            assert self.costs[key] == values
            return False
        self.costs[key] = values
        return True


def _bar(minute: int) -> PaperBar:
    opened = NOW + timedelta(minutes=minute)
    return PaperBar(
        bar_key=f"bar-{minute}",
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        available_at=opened + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
    )


def _settlement(rate: str, *, minute: int = 10, mark: str = "200") -> FundingSettlement:
    return FundingSettlement(
        symbol="BTCUSDT",
        funding_time=NOW + timedelta(minutes=minute),
        funding_rate=Decimal(rate),
        mark_price=Decimal(mark),
    )


async def _open_position(direction: str = "long") -> tuple[Any, ...]:
    """Mở vị thế qua coordinator thật; trả coordinator, checkpoint, ledger, id vị thế, account."""
    account_id = uuid4()
    initial = _gateway()
    order = initial.submit(_plan(), f"funding-{direction}", submitted_at=NOW, eligible_after=NOW)
    checkpoints = MemoryCheckpoints(initial.export_state())
    ledger = FundingLedger()
    live = _gateway()
    coordinator = PaperLifecycleCoordinator(live, ledger, checkpoints)  # type: ignore[arg-type]
    uow = CallbackUow()
    await coordinator.apply_entry_market_event(
        uow,  # type: ignore[arg-type]
        account_id=account_id,
        paper_intent_id=order.intent_id,
        ledger_intent_id=uuid4(),
        bar=_bar(1),
    )
    uow.commit()
    if direction == "short":
        # Đảo hướng trong snapshot (plan kiểm thử chỉ có long) để kiểm dấu dòng tiền short.
        shorted = _gateway()
        shorted.replace_state(checkpoints.rows[-1].snapshot)
        position = shorted.position(order.intent_id)
        assert position is not None
        shorted._positions[order.intent_id] = replace(position, direction="short")
        checkpoints.rows[-1].snapshot = shorted.export_state()
    return coordinator, checkpoints, ledger, order.intent_id, account_id, live


async def _apply(coordinator: Any, position_id: str, account_id: UUID, settlement: Any) -> Any:
    uow = CallbackUow()
    result = await coordinator.apply_funding_settlement(
        uow,
        account_id=account_id,
        paper_position_id=position_id,
        ledger_intent_id=uuid4(),
        trade_id=uuid4(),
        settlement=settlement,
        currency="USDT",
        fencing_token=None,
    )
    uow.commit()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "rate", "expected_sign"),
    [
        ("long", "0.0001", 1),
        ("long", "-0.0001", -1),
        ("short", "0.0001", -1),
        ("short", "-0.0001", 1),
    ],
)
async def test_funding_sign_follows_direction_and_rate(
    direction: str, rate: str, expected_sign: int
) -> None:
    coordinator, checkpoints, ledger, position_id, account_id, live = await _open_position(
        direction
    )
    position = live.position(position_id)
    assert position is not None
    result = await _apply(coordinator, position_id, account_id, _settlement(rate))

    expected = Decimal("200") * position.quantity * position.contract_multiplier * Decimal(rate)
    assert result.applied
    assert result.quantity == position.quantity
    assert result.amount == expected * (1 if direction == "long" else -1)
    assert (result.amount > 0) == (expected_sign > 0)
    (cost,) = ledger.costs.values()
    assert cost["cost_type"] == "FUNDING"
    assert cost["amount"] == result.amount
    assert cost["source_event_id"] == _settlement(rate).event_id
    assert cost["occurred_at"] == _settlement(rate).funding_time
    # Gateway thật chỉ đổi sau commit; checkpoint nằm ở scope funding riêng.
    published = live.position(position_id)
    assert published is not None
    assert published.funding_cost == result.amount
    assert published.funding_event_ids == (_settlement(rate).event_id,)
    latest = checkpoints.rows[-1]
    assert latest.event_at == _settlement(rate).funding_time
    assert latest.event_scope == paper_lifecycle._funding_event_scope(position_id, "BTCUSDT")
    assert latest.event_scope != market_event_scope(position_id, "BTCUSDT")


@pytest.mark.asyncio
async def test_funding_retry_is_idempotent() -> None:
    coordinator, checkpoints, ledger, position_id, account_id, live = await _open_position()
    first = await _apply(coordinator, position_id, account_id, _settlement("0.0003"))
    revisions = len(checkpoints.rows)
    again = await _apply(coordinator, position_id, account_id, _settlement("0.0003"))

    assert first.applied
    assert not again.applied
    assert len(ledger.costs) == 1
    assert len(checkpoints.rows) == revisions
    position = live.position(position_id)
    assert position is not None
    assert position.funding_cost == first.amount


@pytest.mark.asyncio
async def test_funding_uses_quantity_open_at_funding_time() -> None:
    coordinator, _checkpoints, ledger, position_id, account_id, _live = await _open_position()
    entry_quantity = ledger.fill_rows[0][1]
    # Một nửa đã đóng đúng mốc 10 phút (cuối nến kết thúc tại mốc) → tính trên phần còn lại.
    ledger.fill_rows.append(("EXIT", entry_quantity / 2, NOW + timedelta(minutes=10)))
    result = await _apply(coordinator, position_id, account_id, _settlement("0.001"))
    assert result.quantity == entry_quantity / 2


@pytest.mark.asyncio
async def test_funding_rejects_position_opened_after_funding_time() -> None:
    coordinator, _checkpoints, ledger, position_id, account_id, _live = await _open_position()
    with pytest.raises(PaperLifecycleConflict, match="không mở tại mốc"):
        # Entry khớp ở nến mở 1 phút: mốc 1 phút không thu (vị thế mở sau mốc).
        await _apply(coordinator, position_id, account_id, _settlement("0.001", minute=1))
    assert not ledger.costs


@pytest.mark.asyncio
async def test_funding_amount_is_rounded_to_ledger_scale() -> None:
    coordinator, _checkpoints, _ledger, position_id, account_id, _live = await _open_position()
    result = await _apply(
        coordinator, position_id, account_id, _settlement("0.0000123456789", mark="101.123456789")
    )
    assert result.amount == result.amount.quantize(Decimal("1e-18"))
    assert -result.amount.as_tuple().exponent <= 18  # type: ignore[operator]


def test_funding_scope_and_keys_are_short_and_distinct_from_market_scope() -> None:
    event_id = _settlement("0.0001").event_id
    assert paper_lifecycle._funding_event_scope("paper:key", "BTCUSDT") != (
        paper_lifecycle._market_event_scope("paper:key", "BTCUSDT")
    )
    for key in (
        paper_lifecycle._funding_event_key("i" * 240, event_id),
        paper_lifecycle._funding_cost_key("i" * 240, event_id),
    ):
        assert len(key) < 240


# --- funding moments từ dữ liệu -----------------------------------------------------------------
def test_funding_moments_merge_settled_rows_and_schedule() -> None:
    t1 = NOW + timedelta(hours=8)
    t2 = NOW + timedelta(hours=16)
    t3 = NOW + timedelta(hours=24)
    moments = funding_moments_from(
        symbol="BTCUSDT",
        settled=(
            SimpleNamespace(
                funding_time=t1, funding_rate=Decimal("0.0001"), mark_price=Decimal("5")
            ),
            SimpleNamespace(funding_time=t2, funding_rate=Decimal("0.0002"), mark_price=None),
        ),
        scheduled=(t1, t3),
    )
    assert [moment.funding_time for moment in moments] == [t1, t2, t3]
    assert moments[0].settlement is not None
    assert moments[0].settlement.mark_price == Decimal("5")
    assert moments[1].settlement is None
    assert moments[1].missing_reason == FUNDING_MARK_PRICE_MISSING_REASON
    assert moments[2].settlement is None
    assert moments[2].missing_reason == FUNDING_RATE_MISSING_REASON


class _RecordingSession:
    def __init__(self, scalars: list[Any]) -> None:
        self.statements: list[str] = []
        self._scalars = list(scalars)

    def _compile(self, statement: Any) -> None:
        self.statements.append(
            str(
                statement.compile(  # type: ignore[no-untyped-call]
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )

    async def execute(self, statement: Any) -> Any:
        self._compile(statement)
        return SimpleNamespace(all=lambda: [])

    async def scalar(self, statement: Any) -> Any:
        self._compile(statement)
        return self._scalars.pop(0) if self._scalars else None


def test_funding_repository_reads_point_in_time_only() -> None:
    t1 = NOW + timedelta(hours=8)
    session = _RecordingSession([t1, NOW + timedelta(days=9)])
    repository = FundingSettlementRepository(session)  # type: ignore[arg-type]
    now = NOW + timedelta(hours=9)
    asyncio.run(
        repository.settled_after(workspace_id=WORKSPACE, symbol="BTCUSDT", after=NOW, now=now)
    )
    scheduled = asyncio.run(
        repository.scheduled_after(workspace_id=WORKSPACE, symbol="BTCUSDT", after=NOW, now=now)
    )
    # Mốc tương lai (> now) không được coi là đã qua.
    assert scheduled == (t1,)
    settled_sql, first_schedule_sql, *_ = session.statements
    assert "market_funding.available_at <=" in settled_sql
    assert "market_funding.funding_time <=" in settled_sql
    assert "DISTINCT ON (market_funding.funding_time)" in settled_sql
    assert "market_funding.source = 'BINANCE_USDM_REST'" in settled_sql
    assert "market_instrument.symbol = 'BTCUSDT'" in settled_sql
    assert "market_mark_price.available_at <=" in first_schedule_sql
    assert "market_mark_price.next_funding_time >" in first_schedule_sql
    assert "LIMIT 1" in first_schedule_sql


# --- vòng sự kiện thị trường ---------------------------------------------------------------------
class _FundingApplication(_FakeApplication):
    """Ghi lại thứ tự lệnh entry/monitor/funding để kiểm nến không bị bỏ qua hay đảo thứ tự."""

    def __init__(self) -> None:
        super().__init__(stop=Decimal("1"))
        self.calls: list[tuple[str, datetime]] = []
        self.funding: list[FundingSettlement] = []

    async def apply_entry_event(self, command: Any) -> Any:
        self.calls.append(("entry", command.bar.opened_at))
        return await super().apply_entry_event(command)

    async def monitor_position(self, command: Any) -> Any:
        self.calls.append(("monitor", command.bar.opened_at))
        return await super().monitor_position(command)

    async def apply_funding(self, command: Any) -> Any:
        assert command.trade_id == self.trade_id
        assert command.paper_position_id == "paper:key"
        self.calls.append(("funding", command.settlement.funding_time))
        applied = command.settlement not in self.funding
        self.funding.append(command.settlement)
        return SimpleNamespace(applied=applied)


class _FundingLoop(PaperMarketEventLoop):
    """Vòng kịch bản: con trỏ nến theo scope thị trường, con trỏ funding theo scope riêng."""

    def __init__(self, app: _FundingApplication, rows: list[SimpleNamespace]) -> None:
        super().__init__(application=app, sessions=object(), workspace_id=WORKSPACE)  # type: ignore[arg-type]
        self.app = app
        self.rows = rows
        self.now = rows[-1].available_at
        self.moments: dict[datetime, FundingMoment] = {}
        self.moment_queries: list[datetime] = []

    def add_moment(
        self, at: datetime, *, rate: str | None = "0.0001", mark: str | None = "100"
    ) -> None:
        if rate is not None and mark is not None:
            settlement = FundingSettlement("BTCUSDT", at, Decimal(rate), Decimal(mark))
            self.moments[at] = FundingMoment(at, settlement, None)
        else:
            reason = (
                FUNDING_RATE_MISSING_REASON if rate is None else FUNDING_MARK_PRICE_MISSING_REASON
            )
            self.moments[at] = FundingMoment(at, None, reason)

    async def _load_scopes(self, account: Any) -> tuple[datetime, tuple[_Scope, ...]]:
        markets = [at for kind, at in self.app.calls if kind != "funding"]
        fundings = [at for kind, at in self.app.calls if kind == "funding"]
        entries = [at for kind, at in self.app.calls if kind == "entry"]
        return self.now, (
            _Scope(
                intent_id=UUID(int=1),
                paper_intent_id="paper:key",
                symbol="BTCUSDT",
                timeframe="1m",
                feature_snapshot_id="f" * 64,
                trade_id=self.app.trade_id if entries else None,
                entry_pending=not entries,
                cursor=max(markets, default=None),
                trade_opened_at=min(entries, default=None),
                funding_cursor=max(fundings, default=None),
            ),
        )

    async def _bars(
        self, scope: _Scope, *, now: datetime, at_or_after: datetime | None
    ) -> tuple[PaperBar, ...]:
        bars = []
        for row in self.rows:
            bar = paper_bar_from_row(row)
            if bar is None or row.available_at > now:
                continue
            if scope.cursor is not None and bar.opened_at <= scope.cursor:
                continue
            if at_or_after is not None and bar.opened_at < at_or_after:
                continue
            bars.append(bar)
        return tuple(bars)

    async def _previous_bar(self, scope: _Scope, *, before: datetime) -> PaperBar | None:
        earlier = [
            bar
            for bar in (paper_bar_from_row(row) for row in self.rows)
            if bar is not None and bar.opened_at < before
        ]
        return max(earlier, key=lambda bar: bar.opened_at, default=None)

    async def _funding_moments(
        self, scope: _Scope, *, after: datetime, now: datetime
    ) -> tuple[FundingMoment, ...]:
        self.moment_queries.append(after)
        return tuple(moment for at, moment in sorted(self.moments.items()) if after < at <= now)


def _rows(count: int) -> list[SimpleNamespace]:
    return [_row(minute) for minute in range(-1, count)]


def test_funding_is_applied_before_first_bar_at_or_after_funding_time() -> None:
    app = _FundingApplication()
    rows = _rows(5)  # _row(-1) lịch sử, entry ở _row(0), monitor _row(1.._row(4)
    loop = _FundingLoop(app, rows)
    funding_time = rows[3].open_time  # = _row(2).open_time
    loop.add_moment(funding_time)
    asyncio.run(loop.run_for_account(app.account.account_id))

    assert app.calls == [
        ("entry", rows[1].open_time),
        ("monitor", rows[2].open_time),
        ("funding", funding_time),
        ("monitor", rows[3].open_time),
        ("monitor", rows[4].open_time),
        ("monitor", rows[5].open_time),
    ]
    # Moment chỉ đọc sau mốc vào lệnh (vị thế mở sau mốc thì không trả funding).
    assert loop.moment_queries == [rows[1].open_time]
    assert loop.counters.funding_events == 1
    assert loop.status()["state"] == "running"

    # Tick sau: cursor nến (scope thị trường) không bị mốc funding kéo lệch, không áp lại.
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert [kind for kind, _ in app.calls].count("funding") == 1
    assert [kind for kind, _ in app.calls].count("monitor") == 4
    assert loop.moment_queries[-1] == funding_time


def test_missing_settlement_holds_bars_until_rate_arrives() -> None:
    app = _FundingApplication()
    rows = _rows(5)
    loop = _FundingLoop(app, rows)
    funding_time = rows[3].open_time
    loop.now = funding_time + timedelta(minutes=5)
    loop.add_moment(funding_time, rate=None)
    asyncio.run(loop.run_for_account(app.account.account_id))

    # Giữ nến từ mốc trở đi: nến trước mốc vẫn được xử lý.
    assert app.calls == [("entry", rows[1].open_time), ("monitor", rows[2].open_time)]
    status = loop.status()
    assert status["state"] == "running"
    assert status["funding_holding"] == 1
    assert loop.counters.funding_holds == 1

    loop.add_moment(funding_time)
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert app.calls[2:] == [
        ("funding", funding_time),
        ("monitor", rows[3].open_time),
        ("monitor", rows[4].open_time),
        ("monitor", rows[5].open_time),
    ]
    assert loop.status()["funding_holding"] == 0


@pytest.mark.parametrize(
    ("rate", "mark", "reason"),
    [
        (None, "100", FUNDING_RATE_MISSING_REASON),
        ("0.0001", None, FUNDING_MARK_PRICE_MISSING_REASON),
    ],
)
def test_missing_settlement_after_timeout_degrades_and_continues(
    rate: str | None, mark: str | None, reason: str
) -> None:
    app = _FundingApplication()
    rows = _rows(5)
    loop = _FundingLoop(app, rows)
    funding_time = rows[3].open_time
    loop.now = funding_time + timedelta(minutes=15)
    loop.add_moment(funding_time, rate=rate, mark=mark)
    asyncio.run(loop.run_for_account(app.account.account_id))

    assert [kind for kind, _ in app.calls] == ["entry", "monitor", "monitor", "monitor", "monitor"]
    status = loop.status()
    assert status["state"] == "degraded"
    assert status["last_error_code"] == reason
    assert status["funding_missing"] == 1

    # Mức đã chốt về muộn: ghi ngay (khối lượng tại mốc do ledger tính), hết degraded.
    loop.add_moment(funding_time)
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert app.calls[-1] == ("funding", funding_time)
    status = loop.status()
    assert status["funding_missing"] == 0
    assert status["state"] == "running"


def test_funding_error_stops_scope_without_skipping_bars() -> None:
    app = _FundingApplication()

    async def boom(_command: Any) -> Any:
        from trading_bot.application.paper_runtime_application import (
            PaperRuntimeApplicationError,
        )

        raise PaperRuntimeApplicationError("PAPER_COMMAND_REJECTED")

    rows = _rows(4)
    loop = _FundingLoop(app, rows)
    loop.add_moment(rows[3].open_time)
    app.apply_funding = boom  # type: ignore[method-assign]
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert app.calls == [("entry", rows[1].open_time), ("monitor", rows[2].open_time)]
    assert loop.status()["state"] == "degraded"
    assert loop.status()["last_error_code"] == "PAPER_COMMAND_REJECTED"
