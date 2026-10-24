from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy.sql.elements import BinaryExpression
from tests.unit.bot_scope_fakes import BotScopeSession

from trading_bot.adapters.database.models import Base
from trading_bot.adapters.database.models.entities import StrategyRelease, TradingAccount
from trading_bot.adapters.database.models.paper_entities import (
    ExchangeOrderRecord,
    FillRecord,
    OrderIntentRecord,
    PositionEventRecord,
    TradeCostRecord,
    TradeRecord,
)
from trading_bot.adapters.database.models.risk_entities import (
    RiskDecisionRecord,
    RiskReservationRecord,
    TradePlanRecord,
)
from trading_bot.adapters.database.repositories.paper_ledger import (
    PaperLedgerConflict,
    PaperLedgerRepository,
    sum_cost_components,
)

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACCOUNT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SETTINGS_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
RELEASE_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
RISK_DECISION_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
RISK_RESERVATION_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
NOW = datetime(2026, 10, 22, tzinfo=UTC)


def test_paper_ledger_models_have_tenant_lineage_and_exact_state_checks() -> None:
    expected = {"order_intent", "exchange_order", "fill", "position_event", "trade", "trade_cost"}
    assert expected <= set(Base.metadata.tables)
    for model in (
        OrderIntentRecord,
        ExchangeOrderRecord,
        FillRecord,
        PositionEventRecord,
        TradeRecord,
        TradeCostRecord,
    ):
        table = model.__table__
        assert table.c.workspace_id.nullable is False
        assert table.c.account_id.nullable is False
        assert table.c.mode.server_default is not None
        assert any(
            {element.parent.name for element in constraint.elements}
            >= {"workspace_id", "account_id"}
            for constraint in table.foreign_key_constraints
        )
    intent_checks = _check_sql(OrderIntentRecord)
    assert "status = 'FILLED'" in intent_checks
    assert "filled_quantity = quantity" in intent_checks
    assert "PARTIALLY_FILLED" in intent_checks
    assert "filled_quantity > 0" in intent_checks
    assert OrderIntentRecord.__table__.c.quote_asset.type.length == 16
    fill_checks = _check_sql(FillRecord)
    assert "fill_role IN ('ENTRY', 'EXIT')" in fill_checks
    assert FillRecord.__table__.c.fill_role.nullable is False
    trade_checks = _check_sql(TradeRecord)
    assert "status = 'OPEN'" in trade_checks
    assert "closed_at IS NULL" in trade_checks
    assert TradeRecord.__table__.c.quote_asset.type.length == 16


def _check_sql(model: type) -> str:
    return " ".join(
        str(check.sqltext) for check in model.__table__.constraints if hasattr(check, "sqltext")
    )


class FakeUow:
    def __init__(
        self,
        *,
        account_mode: str = "PAPER",
        decision_approved: bool = True,
        decision_plan_hash: str = "a" * 64,
        reservation_status: str = "RESERVED",
        reservation_decision_id: UUID = RISK_DECISION_ID,
        reservation_plan_hash: str = "a" * 64,
        reservation_expires_at: datetime | None = None,
        existing_intent: object | None = None,
        plan_payload: dict[str, str] | None = None,
        strategy_release_name: str = "h01-v1",
        strategy_release_hash: str = "b" * 64,
    ) -> None:
        self.workspace_id = WORKSPACE_ID
        self.request_id = uuid4()
        self.events: list[object] = []
        reservation_expiry = reservation_expires_at or datetime.now(UTC) + timedelta(minutes=5)
        self.session = FakeSession(
            workspace_id=WORKSPACE_ID,
            account=SimpleNamespace(id=ACCOUNT_ID, workspace_id=WORKSPACE_ID, mode=account_mode),
            decision=SimpleNamespace(
                id=RISK_DECISION_ID,
                workspace_id=WORKSPACE_ID,
                account_id=ACCOUNT_ID,
                approved=decision_approved,
                plan_hash=decision_plan_hash,
                payload={"candidate_expires_at": reservation_expiry.isoformat()},
            ),
            reservation=SimpleNamespace(
                id=RISK_RESERVATION_ID,
                workspace_id=WORKSPACE_ID,
                account_id=ACCOUNT_ID,
                risk_decision_id=reservation_decision_id,
                plan_hash=reservation_plan_hash,
                status=reservation_status,
                expires_at=reservation_expiry,
            ),
            plan=SimpleNamespace(
                workspace_id=WORKSPACE_ID,
                account_id=ACCOUNT_ID,
                plan_hash="a" * 64,
                symbol="BTCUSDT",
                payload=plan_payload
                or {
                    "quantity": "1",
                    "direction": "LONG",
                    "settings_version_id": str(SETTINGS_ID),
                    "strategy_release_id": "h01-v1",
                    "release_content_hash": "b" * 64,
                },
            ),
            strategy_release=SimpleNamespace(
                id=RELEASE_ID,
                workspace_id=WORKSPACE_ID,
                name=strategy_release_name,
                content_hash=strategy_release_hash,
                status="PUBLISHED",
            ),
            existing_intent=existing_intent,
        )

    def enqueue(self, event: object) -> None:
        self.events.append(event)


class FakeSession(BotScopeSession):
    def __init__(
        self,
        *,
        workspace_id: UUID,
        account: object,
        decision: object,
        reservation: object,
        plan: object,
        strategy_release: object,
        existing_intent: object | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.account = account
        self.decision = decision
        self.reservation = reservation
        self.plan = plan
        self.strategy_release = strategy_release
        self.existing_intent = existing_intent
        self.added: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        if self.is_bot_query(statement):
            return self.bot_identity(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is TradingAccount:
            return self.account
        if entity is StrategyRelease:
            return self.strategy_release
        if entity is RiskDecisionRecord:
            return self.decision
        if entity is RiskReservationRecord:
            return self.reservation
        if entity is TradePlanRecord:
            return self.plan
        if entity is OrderIntentRecord and self.existing_intent is not None:
            if self._matches(self.existing_intent, statement):
                return self.existing_intent
        rows = [row for row in self.added if isinstance(row, entity)]
        return next((row for row in rows if self._matches(row, statement)), None)

    async def scalars(self, statement: object) -> object:
        entity = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.added if isinstance(row, entity)]
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    @staticmethod
    def _matches(row: object, statement: object) -> bool:
        criteria: dict[str, object] = {}

        def visit(expression: object) -> None:
            if isinstance(expression, BinaryExpression):
                key = getattr(expression.left, "key", None)
                value = getattr(expression.right, "value", None)
                if isinstance(key, str):
                    criteria[key] = value
            elif hasattr(expression, "get_children"):
                for child in expression.get_children():
                    visit(child)

        for clause in statement._where_criteria:
            visit(clause)
        return all(getattr(row, key, object()) == value for key, value in criteria.items())


def _intent_args(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "idempotency_key": "intent-1",
        "plan_hash": "a" * 64,
        "settings_version_id": SETTINGS_ID,
        "strategy_release_id": RELEASE_ID,
        "strategy_release_hash": "b" * 64,
        "feature_snapshot_id": "c" * 64,
        "candidate_id": "d" * 64,
        "risk_decision_id": RISK_DECISION_ID,
        "risk_reservation_id": RISK_RESERVATION_ID,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quote_asset": "USDT",
        "quantity": Decimal("1"),
        "created_at": NOW,
        "evidence": {"bar_keys": ["bar-1"]},
    }
    values.update(overrides)
    return values


async def _accepted_intent(uow: FakeUow, **overrides: object) -> OrderIntentRecord:
    result = await PaperLedgerRepository().create_intent(uow, **_intent_args(**overrides))  # type: ignore[arg-type]
    assert not result.duplicate
    return next(row for row in uow.session.added if isinstance(row, OrderIntentRecord))


@pytest.mark.asyncio
async def test_intent_requires_approved_decision_and_reserved_matching_reservation() -> None:
    repository = PaperLedgerRepository()
    for uow in (
        FakeUow(decision_approved=False),
        FakeUow(decision_plan_hash="x" * 64),
        FakeUow(reservation_status="UNKNOWN"),
        FakeUow(reservation_status="RELEASED"),
        FakeUow(reservation_decision_id=uuid4()),
        FakeUow(reservation_plan_hash="x" * 64),
        FakeUow(strategy_release_name="other-release"),
        FakeUow(strategy_release_hash="x" * 64),
    ):
        with pytest.raises(PaperLedgerConflict):
            await repository.create_intent(uow, **_intent_args())  # type: ignore[arg-type]
        assert not uow.session.added
        assert not uow.events


@pytest.mark.asyncio
async def test_intent_conflict_identifies_mismatched_trade_plan_field() -> None:
    uow = FakeUow(
        plan_payload={
            "quantity": "2",
            "direction": "LONG",
            "settings_version_id": str(SETTINGS_ID),
            "strategy_release_id": "h01-v1",
            "release_content_hash": "b" * 64,
        }
    )

    with pytest.raises(PaperLedgerConflict, match="trường không khớp: quantity"):
        await PaperLedgerRepository().create_intent(uow, **_intent_args())  # type: ignore[arg-type]

    assert not uow.session.added
    assert not uow.events


@pytest.mark.asyncio
async def test_intent_rejects_expired_reservation_at_exact_boundary() -> None:
    expired = FakeUow(reservation_expires_at=NOW)

    with pytest.raises(PaperLedgerConflict, match="hết hạn"):
        await PaperLedgerRepository(clock=lambda: NOW).create_intent(expired, **_intent_args())  # type: ignore[arg-type]

    assert not expired.session.added
    assert not expired.events


@pytest.mark.asyncio
async def test_intent_create_and_exact_retry_preserve_lineage_and_exchange_order() -> None:
    uow = FakeUow(
        plan_payload={
            "quantity": "1",
            "direction": "long",
            "settings_version_id": str(SETTINGS_ID),
            "strategy_release_id": "h01-v1",
            "release_content_hash": "b" * 64,
        }
    )
    result = await PaperLedgerRepository().create_intent(uow, **_intent_args())  # type: ignore[arg-type]
    assert not result.duplicate
    intent = next(row for row in uow.session.added if isinstance(row, OrderIntentRecord))
    order = next(row for row in uow.session.added if isinstance(row, ExchangeOrderRecord))
    assert intent.workspace_id == WORKSPACE_ID
    assert intent.account_id == ACCOUNT_ID
    assert intent.mode == "PAPER"
    assert intent.quote_asset == "USDT"
    assert intent.risk_decision_id == RISK_DECISION_ID
    assert intent.risk_reservation_id == RISK_RESERVATION_ID
    assert order.intent_id == result.intent_id
    assert order.status == "NEW"
    assert len(uow.events) == 1


@pytest.mark.asyncio
async def test_intent_retry_rejects_changed_evidence_or_creation_time() -> None:
    original = _intent_args()
    existing = SimpleNamespace(
        id=uuid4(),
        bot_id=uuid5(NAMESPACE_URL, str(ACCOUNT_ID)),
        workspace_id=WORKSPACE_ID,
        account_id=ACCOUNT_ID,
        idempotency_key=original["idempotency_key"],
        **{
            key: value
            for key, value in original.items()
            if key not in {"account_id", "idempotency_key", "created_at", "evidence"}
        },
        created_at=NOW,
        evidence={"bar_keys": ["bar-1"]},
    )
    for override in (
        {"evidence": {"bar_keys": ["changed"]}},
        {"created_at": NOW + timedelta(seconds=1)},
    ):
        with pytest.raises(PaperLedgerConflict, match="payload"):
            await PaperLedgerRepository().create_intent(
                FakeUow(existing_intent=existing), **_intent_args(**override)
            )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_partial_fills_then_cancel_remainder_and_costs_are_recorded_once() -> None:
    uow = FakeUow()
    intent = await _accepted_intent(uow)
    repository = PaperLedgerRepository()
    base = {
        "intent_id": intent.id,
        "bar_key": "bar-2",
        "fill_policy_version": "paper-v1",
        "price": Decimal("100"),
        "fee_amount": Decimal("0.10"),
        "slippage_amount": Decimal("0.02"),
        "occurred_at": NOW + timedelta(minutes=1),
    }
    assert await repository.append_fill(uow, fill_key="fill-1", quantity=Decimal("0.4"), **base)
    assert await repository.append_fill(
        uow,
        fill_key="fill-2",
        quantity=Decimal("0.3"),
        **{**base, "bar_key": "bar-3", "occurred_at": NOW + timedelta(minutes=2)},
    )
    assert intent.filled_quantity == Decimal("0.7")
    assert intent.status == "PARTIALLY_FILLED"
    order = next(row for row in uow.session.added if isinstance(row, ExchangeOrderRecord))
    assert order.status == "PARTIALLY_FILLED"
    trades = [row for row in uow.session.added if isinstance(row, TradeRecord)]
    assert len(trades) == 1
    assert trades[0].opened_at == NOW + timedelta(minutes=1)
    cost_rows = [row for row in uow.session.added if isinstance(row, TradeCostRecord)]
    assert len(cost_rows) == 4
    assert {row.currency for row in cost_rows} == {"USDT"}
    assert await repository.cancel_remainder(
        uow, intent_id=intent.id, occurred_at=NOW + timedelta(minutes=3)
    )
    assert not await repository.cancel_remainder(
        uow, intent_id=intent.id, occurred_at=NOW + timedelta(minutes=4)
    )
    assert intent.status == "CANCELLED"
    assert order.status == "CANCELLED"


@pytest.mark.asyncio
async def test_fill_retry_checks_timestamp_and_conflicting_replay_fails() -> None:
    uow = FakeUow()
    intent = await _accepted_intent(uow)
    repository = PaperLedgerRepository()
    args = {
        "intent_id": intent.id,
        "fill_key": "fill-1",
        "bar_key": "bar-2",
        "fill_policy_version": "paper-v1",
        "quantity": Decimal("0.5"),
        "price": Decimal("100"),
        "fee_amount": Decimal("0.1"),
        "slippage_amount": Decimal("0.02"),
        "occurred_at": NOW + timedelta(minutes=1),
    }
    assert await repository.append_fill(uow, **args)
    order = next(row for row in uow.session.added if isinstance(row, ExchangeOrderRecord))
    assert order.status == "PARTIALLY_FILLED"
    assert not await repository.append_fill(uow, **args)
    with pytest.raises(PaperLedgerConflict, match="Fill key"):
        await repository.append_fill(uow, **{**args, "occurred_at": NOW + timedelta(minutes=2)})


@pytest.mark.asyncio
async def test_exit_fill_is_idempotent_records_costs_and_keeps_entry_quantity_unchanged() -> None:
    uow = FakeUow()
    intent = await _accepted_intent(
        uow,
        evidence={"bar_keys": ["bar-1"], "paper_execution_profile_hash": "e" * 64},
    )
    repository = PaperLedgerRepository()
    entry = {
        "intent_id": intent.id,
        "fill_key": "entry-1",
        "bar_key": "bar-entry",
        "fill_policy_version": "paper-v1",
        "quantity": Decimal("1"),
        "price": Decimal("100"),
        "fee_amount": Decimal("0.10"),
        "slippage_amount": Decimal("0.02"),
        "occurred_at": NOW + timedelta(minutes=1),
    }
    assert await repository.append_fill(uow, **entry)
    before_filled = intent.filled_quantity
    before_status = intent.status
    args = {
        "intent_id": intent.id,
        "fill_key": "exit-1",
        "bar_key": "bar-exit",
        "fill_policy_version": "paper-v1",
        "quantity": Decimal("0.4"),
        "price": Decimal("110"),
        "fee_amount": Decimal("0.11"),
        "slippage_amount": Decimal("0.03"),
        "occurred_at": NOW + timedelta(minutes=2),
    }

    assert await repository.append_exit_fill(uow, **args)
    assert not await repository.append_exit_fill(uow, **args)
    with pytest.raises(PaperLedgerConflict, match="Fill key"):
        await repository.append_exit_fill(uow, **{**args, "quantity": Decimal("0.3")})

    fills = [row for row in uow.session.added if isinstance(row, FillRecord)]
    costs = [row for row in uow.session.added if isinstance(row, TradeCostRecord)]
    exit_events = [
        row
        for row in uow.session.added
        if isinstance(row, PositionEventRecord) and row.event_key == "exit-fill:exit-1"
    ]
    assert [row.fill_role for row in fills] == ["ENTRY", "EXIT"]
    assert all(row.lineage["paper_execution_profile_hash"] == "e" * 64 for row in fills)
    assert intent.filled_quantity == before_filled
    assert intent.status == before_status
    assert len(costs) == 4
    assert len(exit_events) == 1
    assert exit_events[0].event_type == "REDUCED"
    assert exit_events[0].quantity == Decimal("0.6")


@pytest.mark.asyncio
async def test_exit_fill_rejects_quantity_above_remaining_position() -> None:
    uow = FakeUow()
    intent = await _accepted_intent(uow)
    repository = PaperLedgerRepository()
    await repository.append_fill(
        uow,
        intent_id=intent.id,
        fill_key="entry-1",
        bar_key="bar-entry",
        fill_policy_version="paper-v1",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee_amount=Decimal("0.1"),
        slippage_amount=Decimal("0.01"),
        occurred_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(PaperLedgerConflict, match="vượt lượng vị thế còn mở"):
        await repository.append_exit_fill(
            uow,
            intent_id=intent.id,
            fill_key="oversized-exit",
            bar_key="bar-exit",
            fill_policy_version="paper-v1",
            quantity=Decimal("1.1"),
            price=Decimal("110"),
            fee_amount=Decimal("0.1"),
            slippage_amount=Decimal("0.01"),
            occurred_at=NOW + timedelta(minutes=2),
        )

    assert len([row for row in uow.session.added if isinstance(row, FillRecord)]) == 1


@pytest.mark.asyncio
async def test_position_event_retry_checks_payload_and_time() -> None:
    uow = FakeUow()
    intent = await _accepted_intent(uow)
    repository = PaperLedgerRepository()
    args = {
        "intent_id": intent.id,
        "event_key": "pos-1",
        "event_type": "STOP_UPDATED",
        "quantity": Decimal("1"),
        "occurred_at": NOW + timedelta(minutes=1),
        "payload": {"stop": "99"},
    }
    assert await repository.append_position_event(uow, **args)
    assert not await repository.append_position_event(uow, **args)
    with pytest.raises(PaperLedgerConflict, match="event key"):
        await repository.append_position_event(uow, **{**args, "payload": {"stop": "98"}})


@pytest.mark.asyncio
async def test_trade_costs_use_quote_asset_close_exactly_and_reject_double_close() -> None:
    uow = FakeUow()
    intent = await _accepted_intent(uow, quote_asset="USDC")
    repository = PaperLedgerRepository()
    await repository.append_fill(
        uow,
        intent_id=intent.id,
        fill_key="fill-1",
        bar_key="bar-2",
        fill_policy_version="paper-v1",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee_amount=Decimal("0.10"),
        slippage_amount=Decimal("0.02"),
        occurred_at=NOW + timedelta(minutes=1),
    )
    trade = next(row for row in uow.session.added if isinstance(row, TradeRecord))
    assert trade.quote_asset == "USDC"
    currencies = {row.currency for row in uow.session.added if isinstance(row, TradeCostRecord)}
    assert currencies == {"USDC"}
    await repository.append_trade_cost(
        uow,
        trade_id=trade.id,
        cost_key="funding-1",
        cost_type="FUNDING",
        amount=Decimal("-0.03"),
        currency="USDC",
        source_event_id="fund-1",
        occurred_at=NOW + timedelta(minutes=2),
    )
    assert (trade.fee_total, trade.funding_total, trade.slippage_total) == (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    assert trade.total_cost == Decimal("0")
    assert trade.net_pnl == Decimal("0")
    with pytest.raises(PaperLedgerConflict, match="quote asset"):
        await repository.append_trade_cost(
            uow,
            trade_id=trade.id,
            cost_key="mixed",
            cost_type="FEE",
            amount=Decimal("1"),
            currency="USDT",
            source_event_id="fee-2",
            occurred_at=NOW + timedelta(minutes=2),
        )
    closed = await repository.close_trade(
        uow,
        trade_id=trade.id,
        gross_pnl=Decimal("1.00"),
        closed_at=NOW + timedelta(minutes=4),
    )
    assert (closed.fee_total, closed.funding_total, closed.slippage_total) == (
        Decimal("0.10"),
        Decimal("-0.03"),
        Decimal("0.02"),
    )
    assert closed.total_cost == Decimal("0.09")
    assert closed.net_pnl == Decimal("0.91")
    with pytest.raises(PaperLedgerConflict, match="đã đóng"):
        await repository.close_trade(
            uow,
            trade_id=trade.id,
            gross_pnl=Decimal("1"),
            closed_at=NOW + timedelta(minutes=5),
        )


def test_cost_aggregation_rejects_mixed_currency() -> None:
    costs = [SimpleNamespace(cost_type="FEE", amount=Decimal("1"), currency="USDT")]
    with pytest.raises(PaperLedgerConflict, match="currency"):
        sum_cost_components(costs, quote_asset="USDC")  # type: ignore[arg-type]
