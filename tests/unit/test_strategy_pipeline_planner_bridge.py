"""Unit tests cho StrategyPipelinePlannerBridge.

Kiểm tra:
- Khả năng chuyển đổi PipelineCandidate từ L0-L6 sang contracts.StrategyCandidate chuẩn.
- Các thuộc tính bắt buộc của TradePlanner: ExitRecipe, SHA-256 release_content_hash,
  thời gian hiệu lực, reference_price Decimal.
- Tính toán canonical candidate_id SHA-256 nhất quán.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_bot.application.strategy_pipeline.pipeline_engine import (
    StrategyCandidate as PipelineCandidate,
)
from trading_bot.application.strategy_pipeline.planner_bridge import (
    StrategyPipelinePlannerBridge,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import DecisionSnapshot, MarketSeries
from trading_bot.domain.strategy_card.models import (
    CompositionLayer,
    ExecutionProfile,
    GraphNode,
    NodeExecutionRole,
)
from trading_bot.domain.strategy_card.schema import StrategyCard


@pytest.fixture
def sample_snapshot() -> DecisionSnapshot:
    dt = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    bar = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="15m",
        open_time=datetime(2026, 11, 5, 11, 45, 0, tzinfo=UTC),
        close_time=dt,
        event_at=dt,
        received_at=dt,
        available_at=dt,
        open=Decimal("65000.0"),
        high=Decimal("65500.0"),
        low=Decimal("64900.0"),
        close=Decimal("65400.0"),
        volume=Decimal("120.5"),
        is_closed=True,
    )
    series = MarketSeries(timeframe="15m", bars=(bar,))
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=dt,
        snapshot_id="snap_btc_01",
        series=(series,),
        features=(),
    )


@pytest.fixture
def sample_card() -> StrategyCard:
    return StrategyCard(
        card_id="card_pinbar_01",
        name="Pinbar Rejection Strategy",
        version="1.0.0",
        author="Quant Test",
        created_at=datetime(2026, 11, 5, tzinfo=UTC),
        description="Chiến lược bắt đáy râu nến",
        layers=[
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            GraphNode(
                node_id="n1",
                layer=CompositionLayer.L1_CANDLE_ANATOMY,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="wick_rejection",
            ),
            GraphNode(
                node_id="n5",
                layer=CompositionLayer.L5_STRATEGY_TRIGGER,
                role=NodeExecutionRole.GATE_FILTER,
                operation="threshold",
            ),
            GraphNode(
                node_id="n6",
                layer=CompositionLayer.L6_ZONES,
                role=NodeExecutionRole.TERMINAL_ZONE,
                operation="atr_zones",
                parameters={"sl_multiplier": 2.0, "rr_ratio": 2.5},
            ),
        ],
        edges=[],
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("1.0"),
        ),
    )


def test_planner_bridge_converts_candidate_correctly(
    sample_snapshot: DecisionSnapshot, sample_card: StrategyCard
) -> None:
    dt = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    pipeline_cand = PipelineCandidate(
        candidate_id="cand_orig_01",
        strategy_card_id="card_pinbar_01",
        content_hash="a" * 64,
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=Decimal("65400.0"),
        stop_loss=Decimal("64400.0"),
        take_profit=Decimal("67900.0"),
        risk_reward_ratio=Decimal("2.5"),
        confidence=Decimal("0.95"),
        signal_at=dt,
        evidence_nodes=("n1", "n5"),
        metadata={"atr": "500.0"},
    )

    contract_cand = StrategyPipelinePlannerBridge.to_contract_candidate(
        pipeline_candidate=pipeline_cand,
        card=sample_card,
        snapshot=sample_snapshot,
    )

    assert contract_cand.strategy_id == "card_pinbar_01"
    assert contract_cand.direction == "long"
    assert contract_cand.reference_price == Decimal("65400.0")
    assert contract_cand.atr_value == 500.0
    assert contract_cand.recipe.stop_atr_multiple == 2.0
    assert contract_cand.recipe.take_profit_atr_multiple == 5.0  # 2.0 * 2.5
    assert contract_cand.evidence_feature_ids == ("n1", "n5")
    assert len(contract_cand.release_content_hash) == 64
    assert len(contract_cand.candidate_id) == 64  # SHA-256 hash


def test_planner_bridge_short_direction_conversion(
    sample_snapshot: DecisionSnapshot, sample_card: StrategyCard
) -> None:
    dt = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    pipeline_cand = PipelineCandidate(
        candidate_id="cand_short_01",
        strategy_card_id="card_pinbar_01",
        content_hash="b" * 64,
        symbol="BTCUSDT",
        direction="SHORT",
        entry_price=Decimal("65000.0"),
        stop_loss=Decimal("66000.0"),
        take_profit=Decimal("63000.0"),
        risk_reward_ratio=Decimal("2.0"),
        confidence=Decimal("0.90"),
        signal_at=dt,
        evidence_nodes=("n5",),
        metadata={"atr": "500.0"},
    )

    contract_cand = StrategyPipelinePlannerBridge.to_contract_candidate(
        pipeline_candidate=pipeline_cand,
        card=sample_card,
        snapshot=sample_snapshot,
    )

    assert contract_cand.direction == "short"
    assert contract_cand.reference_price == Decimal("65000.0")
    assert contract_cand.recipe.stop_atr_multiple == 2.0
    assert contract_cand.recipe.take_profit_atr_multiple == 4.0
    assert contract_cand.reason_code == "QUANTSMART_L6_ZONE_CONFIRMED"
