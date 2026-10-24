from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.execution.paper import (
    IntentConflict,
    OrderStatus,
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    RiskLimits,
    TradePlan,
    TradePlanner,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _settings() -> PlannerSettings:
    return PlannerSettings(
        workspace_id="workspace-a",
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        published=True,
        active_release_ids=("h01-v1",),
        leverage=Decimal("2"),
        exchange=ExchangeRules(
            Decimal("0.1"),
            Decimal("0.01"),
            Decimal("1"),
            Decimal("5"),
            Decimal("10000"),
            Decimal("1"),
        ),
        limits=RiskLimits(
            Decimal("0.01"),
            Decimal("250"),
            Decimal("10000"),
            Decimal("5"),
            3,
            Decimal("5000"),
            Decimal("0.5"),
            Decimal("200"),
            Decimal("500"),
            timedelta(seconds=30),
            timedelta(minutes=5),
        ),
        release_content_hashes=(("h01-v1", "a" * 64),),
    )


def _plan() -> TradePlan:
    candidate = StrategyCandidate(
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash="a" * 64,
        settings_version_id="settings-v1",
        snapshot_id="snapshot-1",
        symbol="BTCUSDT",
        direction="long",
        reference_price=Decimal("100"),
        atr_value=2.0,
        signal_at=NOW,
        eligible_after=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(minutes=10),
        recipe=ExitRecipe("exit-1", 2),
        evidence_feature_ids=("feature-1",),
        evidence_bar_keys=("BTCUSDT:1m:bar-1",),
        macro_evidence_ids=(),
        geometry_version=None,
        reason_code="H01_LONG_ENTRY",
        detail_vi="Tín hiệu kiểm thử.",
    )
    account = AccountSnapshot(
        "workspace-a", "paper-a", "settings-v1", NOW, Decimal("10000"), Decimal("1000"), True
    )
    plan = TradePlanner().plan(
        candidate,
        _settings(),
        account,
        (),
        CostModel("cost-v1", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        now=NOW + timedelta(seconds=3),
    )
    assert isinstance(plan, TradePlan)
    return plan


def _bar(
    key: str,
    minute: int,
    *,
    open_price: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> PaperBar:
    opened = NOW + timedelta(minutes=minute)
    return PaperBar(
        bar_key=key,
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        available_at=opened + timedelta(minutes=1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def _gateway() -> PaperExecutionGateway:
    return PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fill-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.002"),
            fee_rate=Decimal("0.001"),
            max_fill_quantity_per_event=Decimal("0.4"),
        )
    )


def test_submit_is_idempotent_and_rejects_reusing_key_for_different_plan() -> None:
    gateway = _gateway()
    first = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    repeated = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )

    assert repeated == first
    changed = replace(_plan(), plan_hash="f" * 64)
    with pytest.raises(IntentConflict, match="idempotency"):
        gateway.submit(
            changed, "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
        )


def test_entry_waits_for_next_eligible_bar_then_partial_fill_covers_stop() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )

    skipped = gateway.apply_market_event(_bar("bar-before", 0), available_quantity=Decimal("1"))
    assert skipped[0].filled_quantity == 0
    partial = gateway.apply_market_event(_bar("bar-next", 1), available_quantity=Decimal("0.4"))[0]

    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.fill_policy_version == "paper-fill-v1"
    assert partial.filled_quantity == Decimal("0.4")
    assert partial.remaining_quantity == Decimal("0.6")
    position = gateway.position(order.intent_id)
    assert position is not None
    assert position.quantity == position.protective_stop_quantity == Decimal("0.4")
    assert (
        gateway.apply_market_event(_bar("bar-next", 1), available_quantity=Decimal("1"))[0]
        == partial
    )


def test_cancel_remainder_preserves_partial_position_and_stop_coverage() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    gateway.apply_market_event(_bar("bar-1", 1), available_quantity=Decimal("0.4"))

    cancelled = gateway.cancel_unfilled(order.intent_id)
    position = gateway.position(order.intent_id)

    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.remaining_quantity == Decimal("0.6")
    assert position is not None
    assert position.quantity == position.protective_stop_quantity == Decimal("0.4")


def test_unknown_order_is_not_filled_until_reconciled_and_retry_is_idempotent() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    unknown = gateway.mark_unknown(order.intent_id)

    assert unknown.status is OrderStatus.UNKNOWN
    assert (
        gateway.submit(
            _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
        )
        == unknown
    )
    gateway.apply_market_event(_bar("bar-1", 1), available_quantity=Decimal("1"))
    assert gateway.order(order.intent_id) == unknown
    resolved = gateway.resolve_unknown(order.intent_id, order_absent=True)
    assert resolved.status is OrderStatus.CANCELLED


def test_entry_fee_and_slippage_are_recorded_once_per_fill_event() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    first = gateway.apply_market_event(_bar("bar-1", 1), available_quantity=Decimal("0.4"))[0]
    repeated = gateway.apply_market_event(_bar("bar-1", 1), available_quantity=Decimal("0.4"))[0]
    position = gateway.position(order.intent_id)

    assert repeated == first
    assert position is not None
    assert len(first.fills) == 1
    assert first.fills[0].fill_policy_version == "paper-fill-v1"
    assert first.fills[0].fee_cost == first.fills[0].execution_price * Decimal("0.4") * Decimal(
        "0.001"
    )
    assert first.fills[0].slippage_cost == (
        first.fills[0].execution_price - first.fills[0].reference_price
    ) * Decimal("0.4")
    assert position.fee_cost == first.fills[0].fee_cost
    assert position.slippage_cost == first.fills[0].slippage_cost


def test_funding_credit_reduces_cost_and_increases_net_realized_pnl() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-funding", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    gateway.apply_market_event(_bar("bar-funding-entry", 1), available_quantity=Decimal("1"))
    position = gateway.position(order.intent_id)
    assert position is not None

    credited = gateway.apply_funding_event(position.position_id, "funding-credit", Decimal("-2"))
    retried = gateway.apply_funding_event(position.position_id, "funding-credit", Decimal("-2"))

    assert credited.funding_cost == Decimal("-2")
    assert credited.net_realized_pnl == (Decimal("2") - credited.fee_cost - credited.slippage_cost)
    assert retried == credited


def test_pause_blocks_new_entries_but_does_not_disable_position_protection() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    gateway.apply_market_event(_bar("bar-1", 1), available_quantity=Decimal("0.4"))
    gateway.pause_entries()

    with pytest.raises(IntentConflict, match="paused"):
        gateway.submit(_plan(), "intent-2", submitted_at=NOW)
    assert gateway.order(order.intent_id).status is OrderStatus.CANCELLED
    position = gateway.position(order.intent_id)
    assert position is not None
    result = gateway.monitor_position(position, _bar("bar-stop", 2, low="95"))
    assert result.exit_reason == "STOP_LOSS"
    assert result.position.is_closed
    assert gateway.monitor_position(result.position, _bar("bar-stop", 2, low="95")) == result


def test_reduce_only_close_clamps_to_open_quantity_and_never_reverses() -> None:
    gateway = _gateway()
    order = gateway.submit(
        _plan(), "intent-1", submitted_at=NOW, eligible_after=NOW + timedelta(seconds=2)
    )
    gateway.apply_market_event(_bar("bar-1", 1), available_quantity=Decimal("0.4"))
    position = gateway.position(order.intent_id)
    assert position is not None

    closed = gateway.close_position(
        position.position_id,
        Decimal("1"),
        reference_price=Decimal("101"),
        closed_at=NOW + timedelta(minutes=2),
        idempotency_key="close-1",
    )

    assert closed.filled_quantity == Decimal("0.4")
    assert closed.reduce_only
    assert closed.position.quantity == Decimal("0")
    assert closed.position.is_closed
    assert closed.position.direction == "long"
    assert (
        gateway.close_position(
            position.position_id,
            Decimal("1"),
            reference_price=Decimal("101"),
            closed_at=NOW + timedelta(minutes=2),
            idempotency_key="close-1",
        )
        == closed
    )
    with pytest.raises(IntentConflict, match="idempotency"):
        gateway.close_position(
            position.position_id,
            Decimal("0.2"),
            reference_price=Decimal("101"),
            closed_at=NOW + timedelta(minutes=2),
            idempotency_key="close-1",
        )
