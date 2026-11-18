"""Unit tests cho SC-1 (CRY-32): Engine Strategy Card tính đúng thứ thẻ khai báo.

Kiểm tra các yêu cầu nghiệm thu của SC-1:
- Bảng đăng ký operation tường minh; từ chối operation chưa đăng ký hoặc sai tầng.
- Cổng biến động theo tỷ lệ ATR/giá (``min_atr_pct``, ``max_atr_pct``).
- Cổng xu hướng (``trend_alignment_gate``) không tự pass khi thiếu EMA.
- Ngòi nổ ngưỡng tổng quát (``threshold_trigger``) bắt buộc khai báo ngưỡng và hướng.
- Ngòi nổ giao cắt (``crossover_trigger``) chỉ kích hoạt khi có giao cắt thật.
- ``max_hold_bars`` và ``candidate_validity_minutes`` đọc từ thẻ.
- Engine chọn đúng timeframe của thẻ và áp dụng guard dữ liệu như H0x.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.application.strategy_pipeline.card_strategy import StrategyCardStrategy
from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateResult,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    PIPELINE_ENGINE_VERSION,
    StrategyPipelineEngine,
)
from trading_bot.application.strategy_pipeline.planner_bridge import StrategyPipelinePlannerBridge
from trading_bot.application.strategy_pipeline.trigger_evaluator import (
    TriggerEvaluator,
    TriggerSignal,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    NoTrade,
    SeriesQuality,
    StrategySelection,
)
from trading_bot.domain.strategy_card.graph_verifier import DeadNodeDetector
from trading_bot.domain.strategy_card.models import (
    CompatibilityMap,
    CompositionLayer,
    DataType,
    ExecutionProfile,
    GraphEdge,
    GraphNode,
    NodeExecutionRole,
    TypedPort,
)
from trading_bot.domain.strategy_card.operations import OPERATION_REGISTRY, resolve_operation
from trading_bot.domain.strategy_card.schema import StrategyCard


def _bar(
    *,
    timeframe: str = "1h",
    open_p: str = "100",
    high_p: str = "102",
    low_p: str = "98",
    close_p: str = "101",
    is_closed: bool = True,
    available_offset: timedelta = timedelta(0),
    open_time: datetime | None = None,
) -> OHLCVBar:
    ot = open_time or datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    ct = ot + timedelta(hours=1) - timedelta(microseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=ot,
        close_time=ct,
        event_at=ct,
        received_at=ct,
        available_at=ct + available_offset,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
        volume=Decimal("100"),
        is_closed=is_closed,
    )


def _card_with_ops(
    *,
    l4_op: str = "volatility_regime_gate",
    l4_params: dict[str, object] | None = None,
    l5_op: str = "threshold_trigger",
    l5_params: dict[str, object] | None = None,
    l6_params: dict[str, object] | None = None,
    timeframes: tuple[str, ...] = ("1h",),
) -> StrategyCard:
    p_series = TypedPort("ohlcv", DataType.PRICE_SERIES, "series")
    p_num = TypedPort("val", DataType.NUMERIC_SCALAR, "scalar")
    p_flag = TypedPort("flag", DataType.BOOLEAN_FLAG, "flag")
    nodes = [
        GraphNode(
            "l0",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            {},
            (),
            (p_series,),
        ),
        GraphNode(
            "l1_wick",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "lower_wick_ratio",
            {},
            (p_series,),
            (p_num,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            {"period": 14},
            (p_series,),
            (p_num,),
        ),
        GraphNode(
            "l4_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            l4_op,
            l4_params or {"min_atr_pct": 0.01, "max_atr_pct": 0.1},
            (p_num,),
            (p_flag,),
        ),
        GraphNode(
            "l5_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            l5_op,
            l5_params if l5_params is not None else {"threshold": 0.4, "direction": "LONG"},
            (p_flag, p_num),
            (p_flag,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "execution_zone_builder",
            l6_params
            or {
                "sl_multiplier": 2.0,
                "rr_ratio": 2.0,
                "max_hold_bars": 24,
                "candidate_validity_minutes": 45,
            },
            (p_flag,),
            (),
        ),
    ]
    edges = [
        GraphEdge("l0", "ohlcv", "l1_wick", "ohlcv"),
        GraphEdge("l0", "ohlcv", "l2_atr", "ohlcv"),
        GraphEdge("l2_atr", "val", "l4_gate", "val"),
        GraphEdge("l4_gate", "flag", "l5_trigger", "flag"),
        GraphEdge("l1_wick", "val", "l5_trigger", "val"),
        GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
    ]
    return StrategyCard(
        card_id="SC-TEST-SC1-001",
        name="SC1 Reference Card",
        version="1.0.0",
        author="Quant",
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L2_INDICATORS,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=nodes,
        edges=edges,
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=("BTCUSDT",),
            timeframes=timeframes,
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.0"),
        ),
    )


def test_operation_registry_rejects_unregistered_and_wrong_layer_operations() -> None:
    assert PIPELINE_ENGINE_VERSION == "l0-l6-v4"
    assert "volatility_regime_gate" in OPERATION_REGISTRY
    assert resolve_operation("magic_unknown_op") is None
    assert resolve_operation("lower_wick_ratio", layer=CompositionLayer.L2_INDICATORS) is None

    bad_card = _card_with_ops(l5_op="unregistered_magic_trigger")
    report = DeadNodeDetector().verify(bad_card.nodes, bad_card.edges)
    assert report.is_valid is False
    assert "l5_trigger" in report.logic_anomalies

    bar = _bar()
    snap = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=bar.close_time,
        snapshot_id="s-1",
        series=(
            MarketSeries(
                timeframe="1h", bars=(bar,), quality=SeriesQuality.READY, backfill_complete=True
            ),
        ),
        features=(),
    )
    res = StrategyPipelineEngine().execute(bad_card, snap)
    assert res.status == "ERROR"
    assert any("UNREGISTERED_OPERATION" in r for r in res.reasons)


def test_volatility_gate_supports_atr_pct_of_price() -> None:
    evaluator = ContextGateEvaluator()
    low_res = evaluator.evaluate_volatility_gate(
        current_atr=Decimal("0.5"),
        current_price=Decimal("100"),
        min_atr_pct=Decimal("0.01"),
        max_atr_pct=Decimal("0.05"),
    )
    assert low_res.status == ContextGateStatus.BLOCK
    assert any("min_atr_pct" in r for r in low_res.reasons)

    high_res = evaluator.evaluate_volatility_gate(
        current_atr=Decimal("6.0"),
        current_price=Decimal("100"),
        min_atr_pct=Decimal("0.01"),
        max_atr_pct=Decimal("0.05"),
    )
    assert high_res.status == ContextGateStatus.BLOCK
    assert any("max_atr_pct" in r for r in high_res.reasons)

    ok_res = evaluator.evaluate_volatility_gate(
        current_atr=Decimal("2.0"),
        current_price=Decimal("100"),
        min_atr_pct=Decimal("0.01"),
        max_atr_pct=Decimal("0.05"),
    )
    assert ok_res.status == ContextGateStatus.PASS
    assert ok_res.metrics["atr_pct"] == 0.02


def test_trend_gate_blocks_when_ema_missing_instead_of_auto_passing() -> None:
    evaluator = ContextGateEvaluator()
    gate_node = GraphNode(
        "l4_trend",
        CompositionLayer.L4_CONTEXT_GATE,
        NodeExecutionRole.GATE_FILTER,
        "trend_alignment_gate",
        {"trend": "BULLISH"},
    )
    bar = _bar()
    snap = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=bar.close_time,
        snapshot_id="s-trend",
        series=(MarketSeries(timeframe="1h", bars=(bar,)),),
        features=(),
    )
    res = evaluator.evaluate_gates([gate_node], {}, snap, latest_bar=bar)
    assert res.status == ContextGateStatus.BLOCK
    assert any("Thiếu giá trị EMA" in r for r in res.reasons)


def test_threshold_trigger_requires_explicit_threshold_and_direction() -> None:
    evaluator = TriggerEvaluator()
    gate_ok = ContextGateResult(status=ContextGateStatus.PASS)
    missing_params_node = GraphNode(
        "l5_t",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        NodeExecutionRole.STATELESS_TRANSFORM,
        "threshold_trigger",
        {},
    )
    res = evaluator.evaluate([missing_params_node], {"l1_wick": Decimal("0.9")}, gate_ok)
    assert res.signal == TriggerSignal.NO_SIGNAL


def test_crossover_trigger_checks_real_crossover_and_direction() -> None:
    evaluator = TriggerEvaluator()
    gate_ok = ContextGateResult(status=ContextGateStatus.PASS)
    node_long = GraphNode(
        "l5_cross",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        NodeExecutionRole.STATELESS_TRANSFORM,
        "crossover_trigger",
        {"direction": "LONG"},
    )
    # Không giao cắt -> NO_SIGNAL
    no_cross = evaluator.evaluate(
        [node_long],
        {
            "fast_prev": Decimal("10"),
            "fast_curr": Decimal("11"),
            "slow_prev": Decimal("9"),
            "slow_curr": Decimal("9.5"),
        },
        gate_ok,
    )
    assert no_cross.signal == TriggerSignal.NO_SIGNAL

    # Giao cắt tăng -> LONG_ENTRY
    bull_cross = evaluator.evaluate(
        [node_long],
        {
            "fast_prev": Decimal("9"),
            "fast_curr": Decimal("11"),
            "slow_prev": Decimal("10"),
            "slow_curr": Decimal("10"),
        },
        gate_ok,
    )
    assert bull_cross.signal == TriggerSignal.LONG_ENTRY

    # Giao cắt giảm với direction=SHORT -> SHORT_ENTRY
    node_short = GraphNode(
        "l5_cross",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        NodeExecutionRole.STATELESS_TRANSFORM,
        "crossover_trigger",
        {"direction": "SHORT"},
    )
    bear_cross = evaluator.evaluate(
        [node_short],
        {
            "fast_prev": Decimal("11"),
            "fast_curr": Decimal("9"),
            "slow_prev": Decimal("10"),
            "slow_curr": Decimal("10"),
        },
        gate_ok,
    )
    assert bear_cross.signal == TriggerSignal.SHORT_ENTRY


def test_card_timeframe_selection_h0x_guards_and_l6_recipe_fields() -> None:
    card = _card_with_ops(timeframes=("1h",))
    bar_15m = _bar(timeframe="15m")
    bar_1h = _bar(timeframe="1h", open_p="100", high_p="101", low_p="97", close_p="100.5")
    atr_feat = FeatureObservation(
        feature_id="1h:atr_14:snap",
        name="atr_14",
        timeframe="1h",
        value=2.0,
        previous_value=2.0,
        available_at=bar_1h.close_time,
        quality=FeatureQuality.READY,
    )
    # Chuỗi 15m đứng trước series[0], nhưng thẻ khai báo 1h -> engine phải chọn chuỗi 1h.
    snap = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=bar_1h.close_time,
        snapshot_id="snap-tf",
        series=(
            MarketSeries(
                timeframe="15m",
                bars=(bar_15m,),
                quality=SeriesQuality.READY,
                backfill_complete=True,
            ),
            MarketSeries(
                timeframe="1h", bars=(bar_1h,), quality=SeriesQuality.READY, backfill_complete=True
            ),
        ),
        features=(atr_feat,),
        position_is_flat=True,
    )
    strategy = StrategyCardStrategy(card)
    selection = StrategySelection("s-1", (strategy.release_id,), published=True)
    candidate = strategy.evaluate(snap, selection)
    assert not isinstance(candidate, NoTrade)
    assert candidate.recipe.max_hold_bars == 24
    assert candidate.expires_at == bar_1h.close_time + timedelta(minutes=45)

    # Guard position_is_flat=False
    non_flat = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=bar_1h.close_time,
        snapshot_id="snap-nf",
        series=snap.series,
        features=(atr_feat,),
        position_is_flat=False,
    )
    blocked = strategy.evaluate(non_flat, selection)
    assert isinstance(blocked, NoTrade)
    assert blocked.code == "STRATEGY_CARD_POSITION_NOT_FLAT"

    # Guard nến chưa đóng cửa
    unclosed_bar = _bar(timeframe="1h", is_closed=False)
    unclosed_snap = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=unclosed_bar.close_time,
        snapshot_id="snap-uc",
        series=(
            MarketSeries(
                timeframe="1h",
                bars=(unclosed_bar,),
                quality=SeriesQuality.READY,
                backfill_complete=True,
            ),
        ),
        features=(atr_feat,),
        position_is_flat=True,
    )
    uc_res = StrategyPipelineEngine().execute(card, unclosed_snap)
    assert uc_res.status == "DATA_HYGIENE_FAILED"

    # Guard look-ahead (available_at > decision_time)
    future_bar = _bar(timeframe="1h", available_offset=timedelta(minutes=5))
    future_snap = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=future_bar.close_time,
        snapshot_id="snap-la",
        series=(
            MarketSeries(
                timeframe="1h",
                bars=(future_bar,),
                quality=SeriesQuality.READY,
                backfill_complete=True,
            ),
        ),
        features=(atr_feat,),
        position_is_flat=True,
    )
    la_res = StrategyPipelineEngine().execute(card, future_snap)
    assert la_res.status == "DATA_HYGIENE_FAILED"

    # Kiểm tra StrategyPipelinePlannerBridge đọc trực tiếp max_hold_bars
    engine_res = StrategyPipelineEngine().execute(card, snap)
    assert engine_res.candidate is not None
    bridged = StrategyPipelinePlannerBridge.to_contract_candidate(engine_res.candidate, card, snap)
    assert bridged.recipe.max_hold_bars == 24
    assert bridged.expires_at == bar_1h.close_time + timedelta(minutes=45)
