"""Unit test CRY-216 (SIG-4): limit maker, hết hạn N nến, cooldown chuỗi thua, producer 5m."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from tests.unit.test_risk_engine import _settings as _planner_settings
from tests.unit.test_sig4_acceptance import SETTINGS_HASH, VERSION_ID, WORKSPACE_ID, _published
from tests.unit.test_sig4_scalping_rules import _h18_bars, _snapshot

from trading_bot.application import market_signal_composition
from trading_bot.application.market_signal_composition import PostgresClosedMarketObservationSource
from trading_bot.application.paper_decision_cycle import (
    DecisionCycleOutcome,
    PaperDecisionCycle,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.domain.execution.paper import (
    IntentConflict,
    OrderStatus,
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.execution.paper_state import _order_from_json
from trading_bot.domain.market.models import MarketBarSourceIdentity
from trading_bot.domain.risk.engine import RiskEngine, RiskState
from trading_bot.domain.risk.loss_streak_cooldown import (
    ClosedTradeOutcome,
    LossStreakCooldownPolicy,
    evaluate_loss_streak_cooldown,
)
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    TradePlan,
    TradePlanner,
    calculate_plan_hash,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.rules import release_content_hash
from trading_bot.domain.strategy.scalping_rules import SCALPING_LADDER, H18SweepReclaimRule

ZERO = Decimal("0")
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # Thứ Hai 10:00 UTC (phiên London, ngoài funding)
EXCHANGE = ExchangeRules(
    tick_size=Decimal("0.1"),
    quantity_step=Decimal("0.001"),
    contract_multiplier=Decimal("1"),
    min_notional=Decimal("5"),
    max_notional=Decimal("100000"),
    max_quantity=Decimal("1000"),
)


def _plan(
    *,
    direction: str = "long",
    entry: Decimal = Decimal("100.0"),
    stop: Decimal = Decimal("99.0"),
    target: Decimal = Decimal("102.0"),
    quantity: Decimal = Decimal("1.0"),
    expires_at: datetime | None = None,
) -> TradePlan:
    raw = TradePlan(
        workspace_id="ws-1",
        account_id="acc-1",
        settings_version_id="sv-1",
        settings_hash="sh-1",
        risk_profile_version_id="rp-1",
        strategy_id="H18",
        strategy_release_id="h18-v1",
        release_content_hash="hash-h18",
        snapshot_id="snap-1",
        candidate_id="cand-1",
        symbol="BTCUSDT",
        direction=direction,
        candidate_reason_code="H18_SWEEP_RECLAIM",
        evidence_feature_ids=(),
        evidence_bar_keys=(),
        macro_evidence_ids=(),
        entry_reference_price=entry,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=quantity,
        notional=entry * quantity,
        leverage=Decimal("1"),
        initial_margin=entry * quantity,
        risk_budget=Decimal("100"),
        estimated_loss=abs(entry - stop) * quantity,
        entry_fee_cost=Decimal("0.02"),
        exit_fee_cost=Decimal("0.02"),
        adverse_funding_cost=ZERO,
        entry_slippage_cost=ZERO,
        cost_model_version="vip0-maker",
        exchange_rules=EXCHANGE,
        exit_recipe=ExitRecipe("h18-exit-v1", 1.0, max_hold_bars=12),
        source_signal_at=T0,
        expires_at=expires_at or (T0 + timedelta(hours=1)),
        plan_hash="",
    )
    return replace(raw, plan_hash=calculate_plan_hash(raw))


def _bar(
    index: int,
    *,
    open_p: str = "100.5",
    high_p: str = "101.0",
    low_p: str = "100.0",
    close_p: str = "100.4",
) -> PaperBar:
    opened_at = T0 + timedelta(minutes=5 * index)
    closed_at = opened_at + timedelta(minutes=5)
    return PaperBar(
        bar_key=f"BTCUSDT:5m:{index}",
        symbol="BTCUSDT",
        opened_at=opened_at,
        closed_at=closed_at,
        available_at=closed_at,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
    )


def test_limit_maker_requires_tick_penetration_and_respects_queue_and_maker_fee() -> None:
    """Lệnh limit maker chỉ khớp khi xuyên >= 1 tick, trừ dần hàng đợi và tính phí maker."""
    policy = PaperExecutionPolicy(
        version_id="scalp-maker-v1",
        latency=timedelta(0),
        spread_rate=Decimal("0.0005"),
        slippage_rate=Decimal("0.0005"),
        fee_rate=Decimal("0.0005"),  # taker 0.05%
        maker_fee_rate=Decimal("0.0002"),  # maker 0.02%
        min_maker_penetration_ticks=1,
    )
    gateway = PaperExecutionGateway(policy)
    plan = _plan(direction="long", entry=Decimal("100.0"), quantity=Decimal("2.0"))
    order = gateway.submit(
        plan,
        "maker-long-1",
        submitted_at=T0,
        limit_price=Decimal("100.0"),
        queue_ahead_quantity=Decimal("3.0"),
    )
    assert order.limit_price == Decimal("100.0")
    assert order.queue_ahead_quantity == Decimal("3.0")

    # Nến 1: chỉ chạm đúng 100.0 (chưa xuyên >= 1 tick = 99.9) -> không khớp, hàng đợi giữ nguyên.
    res1 = gateway.apply_market_event(_bar(1, low_p="100.0"), available_quantity=Decimal("10.0"))[0]
    assert res1.status is OrderStatus.ACCEPTED
    assert res1.filled_quantity == ZERO
    assert res1.queue_ahead_quantity == Decimal("3.0")

    # Nến 2: xuyên qua 99.9 (đạt >= 1 tick), thanh khoản 2.0 < hàng đợi 3.0 -> trừ hàng đợi còn 1.0.
    res2 = gateway.apply_market_event(_bar(2, low_p="99.9"), available_quantity=Decimal("2.0"))[0]
    assert res2.status is OrderStatus.ACCEPTED
    assert res2.filled_quantity == ZERO
    assert res2.queue_ahead_quantity == Decimal("1.0")

    # Nến 3: xuyên qua 99.8, thanh khoản 2.5 -> 1.0 trừ hết hàng đợi, còn 1.5 khớp một phần.
    res3 = gateway.apply_market_event(_bar(3, low_p="99.8"), available_quantity=Decimal("2.5"))[0]
    assert res3.status is OrderStatus.PARTIALLY_FILLED
    assert res3.filled_quantity == Decimal("1.5")
    assert res3.remaining_quantity == Decimal("0.5")
    assert res3.queue_ahead_quantity == ZERO
    assert res3.fills[0].is_maker is True
    assert res3.fills[0].execution_price == Decimal("100.0")
    assert res3.fills[0].slippage_cost == ZERO
    # Phí maker = 100.0 * 1.5 * 0.0002 = 0.03
    assert res3.fills[0].fee_cost == Decimal("0.03")

    # Nến 4: xuyên qua 99.9, thanh khoản 1.0 -> khớp nốt 0.5 còn lại.
    res4 = gateway.apply_market_event(_bar(4, low_p="99.9"), available_quantity=Decimal("1.0"))[0]
    assert res4.status is OrderStatus.FILLED
    assert res4.remaining_quantity == ZERO

    # Kiểm tra checkpoint roundtrip giữ nguyên thông tin maker.
    snap = gateway.export_state()
    restored = PaperExecutionGateway.restore(policy, snap)
    restored_order = restored.order(order.intent_id)
    assert restored_order.limit_price == Decimal("100.0")
    assert restored_order.fills[0].is_maker is True


def test_limit_maker_short_penetration_and_legacy_snapshot_compat() -> None:
    """Lệnh short limit maker cần high >= limit + 1 tick; snapshot cũ nạp tương thích ngược."""
    policy = PaperExecutionPolicy(
        version_id="scalp-maker-v1",
        latency=timedelta(0),
        spread_rate=ZERO,
        slippage_rate=ZERO,
        fee_rate=Decimal("0.0005"),
        maker_fee_rate=Decimal("0.0002"),
    )
    gateway = PaperExecutionGateway(policy)
    plan = _plan(
        direction="short",
        entry=Decimal("100.0"),
        stop=Decimal("101.0"),
        target=Decimal("98.0"),
        quantity=Decimal("1.0"),
    )
    gateway.submit(plan, "maker-short-1", submitted_at=T0, limit_price=Decimal("100.0"))

    # Chạm 100.0 chưa xuyên +0.1 -> chưa khớp.
    r1 = gateway.apply_market_event(
        _bar(1, open_p="99.8", high_p="100.0", low_p="99.5", close_p="99.7")
    )[0]
    assert r1.status is OrderStatus.ACCEPTED

    # Xuyên lên 100.1 -> khớp maker.
    r2 = gateway.apply_market_event(
        _bar(2, open_p="99.8", high_p="100.1", low_p="99.5", close_p="99.9")
    )[0]
    assert r2.status is OrderStatus.FILLED
    assert r2.fills[0].execution_price == Decimal("100.0")
    assert r2.fills[0].is_maker is True

    # Snapshot cũ thiếu các trường maker/pending_bars vẫn giải mã bình thường.
    legacy_order = _order_from_json(
        {
            "intent_id": "paper:legacy",
            "idempotency_key": "legacy",
            "plan_hash": "h",
            "fill_policy_version": "v1",
            "symbol": "BTCUSDT",
            "direction": "long",
            "requested_quantity": "1",
            "filled_quantity": "0",
            "remaining_quantity": "1",
            "eligible_at": T0.isoformat(),
            "expires_at": (T0 + timedelta(hours=1)).isoformat(),
            "stop_price": "99",
            "target_price": "102",
            "status": "ACCEPTED",
            "fills": [],
            "seen_bar_keys": [],
        }
    )
    assert legacy_order.limit_price is None
    assert legacy_order.queue_ahead_quantity == ZERO
    assert legacy_order.max_pending_bars is None
    assert legacy_order.pending_bars_elapsed == 0


def test_pending_order_expires_after_n_bars() -> None:
    """Lệnh chờ hết hạn sau N nến không khớp; khớp đủ đúng nến thứ N thì vẫn FILLED."""
    policy = PaperExecutionPolicy(
        version_id="scalp-expire-v1",
        latency=timedelta(0),
        spread_rate=ZERO,
        slippage_rate=ZERO,
        fee_rate=Decimal("0.0005"),
        maker_fee_rate=Decimal("0.0002"),
    )
    gateway = PaperExecutionGateway(policy)
    plan1 = _plan(entry=Decimal("100.0"), quantity=Decimal("1.0"))
    order1 = gateway.submit(
        plan1,
        "expire-after-3",
        submitted_at=T0,
        limit_price=Decimal("100.0"),
        max_pending_bars=3,
    )

    # Nến 1 & 2 không chạm -> còn ACCEPTED.
    assert gateway.apply_market_event(_bar(1, low_p="100.2"))[0].status is OrderStatus.ACCEPTED
    assert gateway.apply_market_event(_bar(2, low_p="100.1"))[0].status is OrderStatus.ACCEPTED
    # Nến 3 không chạm -> hết hạn sau 3 nến.
    res3 = gateway.apply_market_event(_bar(3, low_p="100.0"))[0]
    assert res3.status is OrderStatus.EXPIRED
    assert res3.pending_bars_elapsed == 3
    assert gateway.open_orders() == ()

    # Lệnh thứ 2 khớp đúng ở nến thứ 3 -> FILLED, không bị đánh dấu EXPIRED.
    gateway2 = PaperExecutionGateway(policy)
    order2 = gateway2.submit(
        plan1,
        "fill-on-3rd",
        submitted_at=T0,
        limit_price=Decimal("100.0"),
        max_pending_bars=3,
    )
    gateway2.apply_market_event(_bar(1, low_p="100.2"))
    gateway2.apply_market_event(_bar(2, low_p="100.2"))
    filled_on_3 = gateway2.apply_market_event(_bar(3, low_p="99.8"))[0]
    assert filled_on_3.intent_id == order2.intent_id
    assert filled_on_3.status is OrderStatus.FILLED
    assert order1.intent_id != order2.intent_id

    with pytest.raises(IntentConflict):
        gateway2.submit(plan1, "bad-n", submitted_at=T0, max_pending_bars=0)


def test_loss_streak_cooldown_policy_and_decision_cycle_gate() -> None:
    """Chuỗi thua liên tiếp kích hoạt cooldown và bị RiskEngine/PaperDecisionCycle chặn."""
    snapshot = _snapshot(_h18_bars(), {"atr_14": 0.5})
    now = snapshot.decision_time
    evaluated_at = now + timedelta(seconds=1)

    policy = LossStreakCooldownPolicy(
        max_consecutive_losses=3,
        cooldown_duration=timedelta(minutes=30),
    )
    outcomes = [
        ClosedTradeOutcome(now - timedelta(minutes=60), Decimal("15")),
        ClosedTradeOutcome(now - timedelta(minutes=45), Decimal("-10")),
        ClosedTradeOutcome(now - timedelta(minutes=30), Decimal("-10")),
        ClosedTradeOutcome(now - timedelta(minutes=10), Decimal("-10")),
        # Lệnh tương lai bị bỏ qua khi xét tại now.
        ClosedTradeOutcome(now + timedelta(minutes=5), Decimal("50")),
    ]
    cooldown_state = evaluate_loss_streak_cooldown(outcomes, policy=policy, as_of=now)
    assert cooldown_state.in_cooldown is True
    assert cooldown_state.consecutive_losses == 3
    assert cooldown_state.cooldown_until == now - timedelta(minutes=10) + timedelta(minutes=30)

    # Sau khi hết 30 phút kể từ lệnh thua cuối -> hết cooldown.
    expired_state = evaluate_loss_streak_cooldown(
        outcomes, policy=policy, as_of=now + timedelta(minutes=20)
    )
    assert expired_state.in_cooldown is False

    # Có một lệnh thắng ngắt chuỗi thua -> không cooldown.
    reset_outcomes = [*outcomes[:4], ClosedTradeOutcome(now - timedelta(minutes=5), Decimal("5"))]
    reset_state = evaluate_loss_streak_cooldown(reset_outcomes, policy=policy, as_of=now)
    assert reset_state.in_cooldown is False

    # Kiểm tra PaperDecisionCycle + RiskEngine với H18 candidate thật.
    rule = H18SweepReclaimRule()
    release_hash = release_content_hash(rule)
    planner_settings = replace(
        _planner_settings(),
        workspace_id=str(WORKSPACE_ID),
        settings_version_id=str(VERSION_ID),
        settings_hash=SETTINGS_HASH,
        active_release_ids=(rule.release_id,),
        release_content_hashes=((rule.release_id, release_hash),),
    )
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id=str(VERSION_ID),
        captured_at=now - timedelta(seconds=1),
        equity=Decimal("10000"),
        available_margin=Decimal("1000"),
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id=str(VERSION_ID),
        risk_profile_version_id="risk-v1",
        evaluated_at=evaluated_at,
        account_captured_at=account.captured_at,
        market_data_as_of=now - timedelta(seconds=30),
        equity=account.equity,
        available_margin=account.available_margin,
        open_exposures=(),
        realized_loss_today=Decimal("30"),
        realized_loss_rolling=Decimal("30"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=ZERO,
        loss_streak_cooldown_until=cooldown_state.cooldown_until,
    )
    costs = CostModel(
        "vip0-taker", Decimal("0.0005"), Decimal("0.0005"), Decimal("0"), Decimal("0")
    )
    cycle = PaperDecisionCycle(
        strategy_registry(),
        TradePlanner(),
        RiskEngine(),
        PaperExecutionGateway(
            PaperExecutionPolicy(
                version_id="v1",
                latency=timedelta(0),
                spread_rate=ZERO,
                slippage_rate=ZERO,
                fee_rate=Decimal("0.0005"),
            )
        ),
    )
    rejected = cycle.prepare_published(
        published_settings=_published(),
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        planner_settings=planner_settings,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=costs,
    )
    assert rejected.outcome is DecisionCycleOutcome.RISK_REJECTED
    assert rejected.risk_decision is not None
    assert rejected.risk_decision.reason_code == "LOSS_STREAK_COOLDOWN"

    # Khi cooldown_until đã qua (evaluated_at >= cooldown_until) -> được duyệt.
    approved_state = replace(risk_state, loss_streak_cooldown_until=evaluated_at)
    approved = cycle.prepare_published(
        published_settings=_published(),
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        planner_settings=planner_settings,
        account=account,
        risk_state=approved_state,
        open_exposures=(),
        cost_model=costs,
    )
    assert approved.outcome is DecisionCycleOutcome.RISK_APPROVED
    assert isinstance(approved.strategy_result, StrategyCandidate)
    _ = SCALPING_LADDER


@pytest.mark.asyncio
async def test_producer_observation_source_computes_adx_14_and_efficiency_ratio_40_for_5m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Producer PostgresClosedMarketObservationSource tính adx_14 và ER40 cho 5m."""
    ws_id = uuid4()
    base = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    fake_rows = [
        SimpleNamespace(
            id=uuid4(),
            workspace_id=ws_id,
            symbol="BTCUSDT",
            timeframe="5m",
            open_time=base + timedelta(minutes=5 * i),
            close_time=base + timedelta(minutes=5 * (i + 1)) - timedelta(microseconds=1),
            event_at=base + timedelta(minutes=5 * (i + 1)) - timedelta(microseconds=1),
            received_at=base + timedelta(minutes=5 * (i + 1)),
            available_at=base + timedelta(minutes=5 * (i + 1)),
            open=Decimal("100") + Decimal(i % 4) * Decimal("0.2"),
            high=Decimal("101") + Decimal(i % 4) * Decimal("0.2"),
            low=Decimal("99.5") + Decimal(i % 4) * Decimal("0.2"),
            close=Decimal("100.3") + Decimal(i % 4) * Decimal("0.2"),
            volume=Decimal("10"),
            quote_volume=Decimal("1000"),
            is_closed=True,
            source="BINANCE_USDM",
            source_cursor=str(i),
            checksum=f"{i:064x}",
            raw_reference_id=None,
            logical_key=f"BTCUSDT:5m:{i}",
            revision=1,
            supersedes_id=None,
        )
        for i in range(210)
    ]

    class _FakeUnitOfWork:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.session = object()

        async def __aenter__(self) -> _FakeUnitOfWork:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    saved_snapshots: list[object] = []

    class _FakeRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def append(self, *, workspace_id: UUID, snapshot: object) -> None:
            saved_snapshots.append((workspace_id, snapshot))

    async def _fake_latest_rows(*_args: object, **_kwargs: object) -> Sequence[object]:
        return fake_rows

    monkeypatch.setattr(market_signal_composition, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(market_signal_composition, "_latest_bar_rows", _fake_latest_rows)
    monkeypatch.setattr(market_signal_composition, "FeatureSnapshotRepository", _FakeRepo)

    source = PostgresClosedMarketObservationSource(object())  # type: ignore[arg-type]
    observations = await source.qualified_closed_observations(
        workspace_id=ws_id,
        as_of=base + timedelta(minutes=5 * 210),
        limit=10,
        symbols=("BTCUSDT",),
        timeframes=("5m",),
    )
    assert len(observations) == 1
    snapshot = observations[0].feature_snapshot
    assert snapshot.timeframe == "5m"
    assert snapshot.features["adx_14"] is not None
    assert snapshot.features["efficiency_ratio_40"] is not None
    assert snapshot.warmup["adx_14"] is True
    assert snapshot.warmup["efficiency_ratio_40"] is True
    assert snapshot.data_freshness.name == "FRESH"
    assert snapshot.quality_flags == () or "STALE" not in snapshot.quality_flags
    assert len(saved_snapshots) in (0, 1)
    _ = MarketBarSourceIdentity
