"""Unit tests TDD cho WP-SC.3: Strategy Pipeline Runtime Engine (L0->L6 & Context Gate).

Kiểm thử toàn diện:
1. Tầng L0 Data Hygiene: Phát hiện và chặn nến hỏng (High < Low, High < Open, Low > Close).
2. Tầng L4 Context Gate Short-Circuit: Chặn khi ATR quá thấp, EMA ngược chiều, hoặc Macro Blackout.
   Bảo đảm L5 Trigger tuyệt đối KHÔNG được đánh giá khi L4 là BLOCK.
3. Tầng L5 Trigger Evaluator: Phát hiện tín hiệu LONG / SHORT / NO_SIGNAL và phòng thủ L4.
4. Tầng L6 Zone Builder: Tính toán ExecutionZones (Entry, SL, TP, R:R) cho cả hai chiều mua/bán.
5. Luồng hoàn chỉnh L0 -> L6: Sinh StrategyCandidate hợp lệ khi mọi tầng đều đạt chuẩn.
6. Tính tất định (Determinism): Cùng snapshot và card luôn sinh ra cùng một candidate_id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateResult,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    StrategyCandidate,
    StrategyPipelineEngine,
)
from trading_bot.application.strategy_pipeline.trigger_evaluator import (
    TriggerEvaluator,
    TriggerSignal,
)
from trading_bot.application.strategy_pipeline.zone_builder import (
    ExecutionZones,
    ZoneBuilder,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    MarketSeries,
    SeriesQuality,
)
from trading_bot.domain.strategy_card.models import (
    CompatibilityMap,
    CompositionLayer,
    DataType,
    ExecutionProfile,
    GraphEdge,
    GraphNode,
    NodeExecutionRole,
    ParameterBound,
    TypedPort,
)
from trading_bot.domain.strategy_card.schema import StrategyCard


# ---------------------------------------------------------------------------
# Helper tạo dữ liệu kiểm thử
# ---------------------------------------------------------------------------
def _create_bar(
    *,
    open_p: Decimal,
    high_p: Decimal,
    low_p: Decimal,
    close_p: Decimal,
    volume: Decimal = Decimal("100"),
    open_time: datetime | None = None,
) -> OHLCVBar:
    """Tạo một nến OHLCVBar hợp lệ cho kiểm thử."""
    ot = open_time or datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC)
    ct = ot + timedelta(minutes=15) - timedelta(microseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="15m",
        open_time=ot,
        close_time=ct,
        event_at=ct,
        received_at=ct,
        available_at=ct,
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=volume,
        is_closed=True,
    )


def _create_snapshot(
    bar: OHLCVBar,
    *,
    macro_blackout: bool = False,
    extra_features: tuple[FeatureObservation, ...] = (),
) -> DecisionSnapshot:
    """Tạo DecisionSnapshot điểm thời gian từ một nến gần nhất."""
    series = MarketSeries(
        timeframe="15m",
        bars=(bar,),
        quality=SeriesQuality.READY,
        expected_interval=timedelta(minutes=15),
        max_event_age=timedelta(minutes=30),
        backfill_complete=True,
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=bar.close_time,
        snapshot_id=f"snap-{int(bar.close_time.timestamp())}",
        series=(series,),
        features=extra_features,
        position_is_flat=True,
        macro_blackout=macro_blackout,
        macro_evidence_ids=("macro-ev-1",),
        macro_evidence_available_at=bar.close_time,
        macro_is_fresh=True,
    )


@pytest.fixture
def standard_strategy_card() -> StrategyCard:
    """Tạo StrategyCard chuẩn gồm đầy đủ 6 tầng L0->L6."""
    port_series = TypedPort(name="price", data_type=DataType.PRICE_SERIES)
    port_scalar = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
    port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)

    node_l0 = GraphNode(
        node_id="l0_cleaner",
        layer=CompositionLayer.L0_DATA_HYGIENE,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="sanitize_ohlcv",
        inputs=(),
        outputs=(port_series,),
    )
    node_l1 = GraphNode(
        node_id="l1_wick",
        layer=CompositionLayer.L1_CANDLE_ANATOMY,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="wick_rejection_strength",
        inputs=(port_series,),
        outputs=(port_scalar,),
    )
    node_l2 = GraphNode(
        node_id="l2_atr",
        layer=CompositionLayer.L2_INDICATORS,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="atr_calculator",
        parameters={"default_atr": 10.0},
        inputs=(port_series,),
        outputs=(port_scalar,),
    )
    node_l4 = GraphNode(
        node_id="l4_regime",
        layer=CompositionLayer.L4_CONTEXT_GATE,
        role=NodeExecutionRole.GATE_FILTER,
        operation="volatility_regime_gate",
        parameters={"min_atr": 5.0, "max_atr": 100.0},
        inputs=(port_series,),
        outputs=(port_flag,),
    )
    node_l5 = GraphNode(
        node_id="l5_trigger",
        layer=CompositionLayer.L5_STRATEGY_TRIGGER,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="rejection_breakout_trigger",
        parameters={"threshold": 0.5, "direction": "LONG"},
        inputs=(port_scalar, port_flag),
        outputs=(port_flag,),
    )
    node_l6 = GraphNode(
        node_id="l6_zones",
        layer=CompositionLayer.L6_ZONES,
        role=NodeExecutionRole.TERMINAL_ZONE,
        operation="atr_zone_calculator",
        parameters={"sl_multiplier": 2.0, "rr_ratio": 2.0},
        inputs=(port_flag,),
        outputs=(),
    )

    edges = [
        GraphEdge("l0_cleaner", "price", "l1_wick", "price"),
        GraphEdge("l0_cleaner", "price", "l2_atr", "price"),
        GraphEdge("l0_cleaner", "price", "l4_regime", "price"),
        GraphEdge("l1_wick", "val", "l5_trigger", "val"),
        GraphEdge("l4_regime", "flag", "l5_trigger", "flag"),
        GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
    ]

    return StrategyCard(
        card_id="SC-HAMMER-BREAKOUT-001",
        name="Hammer Rejection Breakout Strategy",
        version="1.0.0",
        author="Crypidian Core Team",
        created_at=datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L2_INDICATORS,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[node_l0, node_l1, node_l2, node_l4, node_l5, node_l6],
        edges=edges,
        parameters=[
            ParameterBound(
                name="sl_multiplier",
                min_value=1.0,
                max_value=5.0,
                step=0.1,
                default_value=2.0,
            ),
            ParameterBound(
                name="rr_ratio",
                min_value=1.0,
                max_value=5.0,
                step=0.1,
                default_value=2.0,
            ),
        ],
        compatibility=CompatibilityMap(
            target_venues=("BINANCE_USDM",),
            target_symbols=("BTCUSDT",),
            timeframes=("15m",),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.0"),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Tests cho Tầng L0 Data Hygiene
# ---------------------------------------------------------------------------
class TestL0DataHygiene:
    """Kiểm thử tầng lọc sạch nến L0."""

    def test_corrupted_bar_high_less_than_low_blocked(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Nến có High < Low phải bị chặn ngay tại L0, không thực thi các tầng sau."""
        # Giả lập nến bị hỏng dữ liệu trong stream: High = 90, Low = 100
        corrupted_bar = _create_bar(
            open_p=Decimal("95.0"),
            high_p=Decimal("105.0"),
            low_p=Decimal("90.0"),
            close_p=Decimal("98.0"),
        )
        object.__setattr__(corrupted_bar, "high", Decimal("90.0"))
        object.__setattr__(corrupted_bar, "low", Decimal("100.0"))

        snapshot = _create_snapshot(corrupted_bar)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        assert result.status == "DATA_HYGIENE_FAILED"
        assert result.candidate is None
        assert any("High < Low" in r or "DATA_HYGIENE" in r for r in result.reasons)
        assert result.gate_result is None  # L4 Context Gate chưa hề chạy
        assert result.trigger_result is None  # L5 Trigger chưa hề chạy

    def test_corrupted_bar_high_less_than_open_blocked(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Nến có High < Open phải bị chặn ngay tại L0."""
        corrupted_bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("105.0"),
            low_p=Decimal("95.0"),
            close_p=Decimal("98.0"),
        )
        object.__setattr__(corrupted_bar, "high", Decimal("95.0"))
        object.__setattr__(corrupted_bar, "open", Decimal("102.0"))

        snapshot = _create_snapshot(corrupted_bar)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        assert result.status == "DATA_HYGIENE_FAILED"
        assert result.candidate is None


# ---------------------------------------------------------------------------
# 2. Tests cho Tầng L4 Context Gate & Short-Circuit
# ---------------------------------------------------------------------------
class TestL4ContextGate:
    """Kiểm thử tầng cổng lọc bối cảnh L4 và cơ chế ngắt mạch (short-circuit)."""

    def test_low_volatility_blocks_pipeline_and_skips_l5_trigger(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Khi biến động ATR < min_atr, L4 trả về BLOCK và L5 Trigger KHÔNG được thực thi."""
        # Nến có biên độ cực nhỏ: High = 100.2, Low = 100.0 -> biên độ = 0.2 < min_atr (5.0)
        flat_bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("100.2"),
            low_p=Decimal("100.0"),
            close_p=Decimal("100.1"),
        )
        snapshot = _create_snapshot(flat_bar)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        assert result.status == "CONTEXT_GATE_BLOCKED"
        assert result.candidate is None
        assert result.gate_result is not None
        assert result.gate_result.status == ContextGateStatus.BLOCK
        assert any("min_atr" in r or "Biến động" in r for r in result.gate_result.reasons)
        # BẢO ĐẢM SHORT-CIRCUIT: L5 Trigger tuyệt đối không được đánh giá
        assert result.trigger_result is None

    def test_macro_blackout_blocks_pipeline(self, standard_strategy_card: StrategyCard) -> None:
        """Khi có Macro Blackout, L4 phải ngắt mạch ngay lập tức."""
        bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("110.0"),
            low_p=Decimal("90.0"),
            close_p=Decimal("108.0"),
        )
        snapshot = _create_snapshot(bar, macro_blackout=True)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        assert result.status == "CONTEXT_GATE_BLOCKED"
        assert result.candidate is None
        assert result.gate_result is not None
        assert result.gate_result.status == ContextGateStatus.BLOCK
        assert any("Macro" in r or "MACRO_BLACKOUT" in r for r in result.gate_result.reasons)
        assert result.trigger_result is None

    def test_context_gate_evaluator_direct_evaluation(self) -> None:
        """Kiểm thử trực tiếp ContextGateEvaluator với các điều kiện khác nhau."""
        evaluator = ContextGateEvaluator()

        # ATR quá thấp -> BLOCK
        res_low_atr = evaluator.evaluate_volatility_gate(
            current_atr=Decimal("3.0"), min_atr=Decimal("5.0"), max_atr=Decimal("100.0")
        )
        assert res_low_atr.status == ContextGateStatus.BLOCK
        assert len(res_low_atr.reasons) > 0

        # ATR hợp lệ -> PASS
        res_ok_atr = evaluator.evaluate_volatility_gate(
            current_atr=Decimal("15.0"), min_atr=Decimal("5.0"), max_atr=Decimal("100.0")
        )
        assert res_ok_atr.status == ContextGateStatus.PASS

        # EMA Trend ngược hướng -> BLOCK
        res_ema_counter = evaluator.evaluate_trend_gate(
            current_price=Decimal("95.0"),
            trend_ema=Decimal("100.0"),
            expected_trend="BULLISH",
        )
        assert res_ema_counter.status == ContextGateStatus.BLOCK


# ---------------------------------------------------------------------------
# 3. Tests cho Tầng L5 Trigger Evaluator
# ---------------------------------------------------------------------------
class TestL5TriggerEvaluator:
    """Kiểm thử bộ đánh giá kích hoạt L5 và rào chắn phòng thủ."""

    def test_trigger_evaluator_forbidden_when_gate_is_block(self) -> None:
        """TriggerEvaluator phải từ chối đánh giá nếu ContextGateResult là BLOCK."""
        evaluator = TriggerEvaluator()
        blocked_gate = ContextGateResult(
            status=ContextGateStatus.BLOCK,
            reasons=("ATR too low",),
            metrics={},
        )

        with pytest.raises(PermissionError, match="ContextGate có trạng thái PASS"):
            evaluator.evaluate(
                trigger_nodes=[],
                node_outputs={},
                gate_result=blocked_gate,
            )

    def test_trigger_evaluator_generates_long_signal(self) -> None:
        """TriggerEvaluator tạo tín hiệu LONG khi wick_rejection vượt ngưỡng."""
        evaluator = TriggerEvaluator()
        passed_gate = ContextGateResult(status=ContextGateStatus.PASS)

        port_val = TypedPort("val", DataType.NUMERIC_SCALAR)
        trigger_node = GraphNode(
            node_id="l5_trigger",
            layer=CompositionLayer.L5_STRATEGY_TRIGGER,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="rejection_breakout_trigger",
            parameters={"threshold": 0.5, "direction": "LONG"},
            inputs=(port_val,),
            outputs=(),
        )

        # Giá trị từ L1 là 0.72 > ngưỡng 0.5
        node_outputs = {"l1_wick": Decimal("0.72")}

        res = evaluator.evaluate(
            trigger_nodes=[trigger_node],
            node_outputs=node_outputs,
            gate_result=passed_gate,
        )

        assert res.signal == TriggerSignal.LONG_ENTRY
        assert res.trigger_score == Decimal("0.72")
        assert "l5_trigger" in res.evidence_nodes

    def test_trigger_evaluator_no_signal_when_below_threshold(self) -> None:
        """TriggerEvaluator trả về NO_SIGNAL khi giá trị không đủ kích hoạt."""
        evaluator = TriggerEvaluator()
        passed_gate = ContextGateResult(status=ContextGateStatus.PASS)

        port_val = TypedPort("val", DataType.NUMERIC_SCALAR)
        trigger_node = GraphNode(
            node_id="l5_trigger",
            layer=CompositionLayer.L5_STRATEGY_TRIGGER,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="rejection_breakout_trigger",
            parameters={"threshold": 0.5, "direction": "LONG"},
            inputs=(port_val,),
            outputs=(),
        )

        # Giá trị từ L1 chỉ là 0.3 < 0.5
        node_outputs = {"l1_wick": Decimal("0.3")}

        res = evaluator.evaluate(
            trigger_nodes=[trigger_node],
            node_outputs=node_outputs,
            gate_result=passed_gate,
        )

        assert res.signal == TriggerSignal.NO_SIGNAL


# ---------------------------------------------------------------------------
# 4. Tests cho Tầng L6 Zone Builder
# ---------------------------------------------------------------------------
class TestL6ZoneBuilder:
    """Kiểm thử tính toán vùng giá vào lệnh, dừng lỗ và chốt lời."""

    def test_long_execution_zones_calculation(self) -> None:
        """Kiểm tra tính toán vùng giá cho lệnh LONG."""
        builder = ZoneBuilder()
        entry = Decimal("100.0")
        atr = Decimal("5.0")
        sl_multiplier = Decimal("2.0")  # SL cách 10.0 -> SL = 90.0
        rr_ratio = Decimal("2.0")  # TP gấp đôi khoảng cách SL (20.0) -> TP = 120.0

        zones = builder.build_zones(
            direction="LONG",
            current_price=entry,
            atr=atr,
            sl_multiplier=sl_multiplier,
            rr_ratio=rr_ratio,
        )

        assert zones.entry_price == Decimal("100.0")
        assert zones.stop_loss_price == Decimal("90.0")
        assert zones.take_profit_price == Decimal("120.0")
        assert zones.risk_reward_ratio == Decimal("2.0")

    def test_short_execution_zones_calculation(self) -> None:
        """Kiểm tra tính toán vùng giá cho lệnh SHORT."""
        builder = ZoneBuilder()
        entry = Decimal("100.0")
        atr = Decimal("5.0")
        sl_multiplier = Decimal("1.5")  # SL = 100 + 7.5 = 107.5
        rr_ratio = Decimal("3.0")  # TP = 100 - (7.5 * 3) = 77.5

        zones = builder.build_zones(
            direction="SHORT",
            current_price=entry,
            atr=atr,
            sl_multiplier=sl_multiplier,
            rr_ratio=rr_ratio,
        )

        assert zones.entry_price == Decimal("100.0")
        assert zones.stop_loss_price == Decimal("107.5")
        assert zones.take_profit_price == Decimal("77.5")
        assert zones.risk_reward_ratio == Decimal("3.0")

    def test_execution_zones_validation_invariants(self) -> None:
        """ExecutionZones bảo đảm các bất biến vị trí giá."""
        # Long nhưng SL > Entry -> Lỗi
        with pytest.raises(ValueError, match="LONG: stop_loss_price .* phải nhỏ hơn entry_price"):
            ExecutionZones(
                entry_price=Decimal("100.0"),
                stop_loss_price=Decimal("105.0"),
                take_profit_price=Decimal("120.0"),
                risk_reward_ratio=Decimal("2.0"),
                direction="LONG",
            )


# ---------------------------------------------------------------------------
# 5. Tests Luồng Hoàn Chỉnh L0 -> L6 & Tính Tất Định
# ---------------------------------------------------------------------------
class TestCompletePipelineAndDeterminism:
    """Kiểm thử luồng pipeline hoàn chỉnh từ L0 đến L6 và tính tất định."""

    def test_full_pipeline_success_generates_candidate(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Khi nến đẹp (Hammer), L4 pass và L5 kích hoạt -> L6 sinh ra StrategyCandidate chuẩn."""
        # Nến Hammer tăng giá:
        # Open=100, High=102, Low=90, Close=101 -> Range=12, Lower wick = 10, Body = 1
        # lower_wick_ratio = 10/12, close_location = 11/12, body_ratio = 1/12
        # wick_rejection_strength = 0.833 * 0.916 * (1 - 0.083) = 0.70 > threshold (0.5)
        # ATR = 12 > min_atr (5.0) -> L4 PASS
        hammer_bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("102.0"),
            low_p=Decimal("90.0"),
            close_p=Decimal("101.0"),
        )
        snapshot = _create_snapshot(hammer_bar)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        assert result.status == "SUCCESS"
        assert result.gate_result is not None
        assert result.gate_result.status == ContextGateStatus.PASS
        assert result.trigger_result is not None
        assert result.trigger_result.signal == TriggerSignal.LONG_ENTRY
        assert result.candidate is not None

        candidate = result.candidate
        assert isinstance(candidate, StrategyCandidate)
        assert candidate.strategy_card_id == standard_strategy_card.card_id
        assert candidate.symbol == "BTCUSDT"
        assert candidate.direction == "LONG"
        assert candidate.entry_price == Decimal("101.0")
        # ATR của nến là 12.0. sl_multiplier = 2.0 -> SL distance = 24.0 -> SL = 101 - 24 = 77.0
        assert candidate.stop_loss == Decimal("77.0")
        # TP distance = 24.0 * 2.0 = 48.0 -> TP = 101 + 48 = 149.0
        assert candidate.take_profit == Decimal("149.0")
        assert candidate.risk_reward_ratio == Decimal("2.0")
        assert candidate.confidence > Decimal("0")
        assert len(candidate.candidate_id) == 64  # SHA-256

    def test_pipeline_determinism(self, standard_strategy_card: StrategyCard) -> None:
        """Cùng một snapshot và card luôn sinh ra cùng một candidate_id và nội dung."""
        hammer_bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("102.0"),
            low_p=Decimal("90.0"),
            close_p=Decimal("101.0"),
        )
        snapshot = _create_snapshot(hammer_bar)
        engine = StrategyPipelineEngine()

        res1 = engine.execute(standard_strategy_card, snapshot)
        res2 = engine.execute(standard_strategy_card, snapshot)

        assert res1.candidate is not None
        assert res2.candidate is not None
        assert res1.candidate.candidate_id == res2.candidate.candidate_id
        assert res1.candidate.entry_price == res2.candidate.entry_price
        assert res1.candidate.stop_loss == res2.candidate.stop_loss
        assert res1.candidate.take_profit == res2.candidate.take_profit

    def test_pipeline_safety_invariant_no_order_generated(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Bảo đảm nguyên tắc an toàn: Pipeline chỉ sinh StrategyCandidate, không sinh lệnh sàn."""
        hammer_bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("102.0"),
            low_p=Decimal("90.0"),
            close_p=Decimal("101.0"),
        )
        snapshot = _create_snapshot(hammer_bar)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        # Output chỉ là StrategyCandidate thô để gửi tiếp sang TradePlanner/RiskEngine
        assert isinstance(result.candidate, StrategyCandidate)
        # Không có order id sàn, không gọi connector
        assert not hasattr(result.candidate, "order_id")
        assert not hasattr(result.candidate, "venue_client")

    def test_pipeline_no_trigger_returns_no_candidate(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Khi L4 PASS nhưng điều kiện L5 Trigger không thỏa mãn -> trả về NO_TRIGGER."""
        # Nến Marubozu tăng không có râu dưới: Open=90, Low=90, High=102, Close=102
        # lower_wick_ratio = 0 -> wick_rejection_strength = 0 < threshold (0.5)
        marubozu_bar = _create_bar(
            open_p=Decimal("90.0"),
            high_p=Decimal("102.0"),
            low_p=Decimal("90.0"),
            close_p=Decimal("102.0"),
        )
        snapshot = _create_snapshot(marubozu_bar)
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, snapshot)

        assert result.status == "NO_TRIGGER"
        assert result.candidate is None
        assert result.trigger_result is not None
        assert result.trigger_result.signal == TriggerSignal.NO_SIGNAL

    def test_pipeline_empty_snapshot_series_rejected(
        self, standard_strategy_card: StrategyCard
    ) -> None:
        """Snapshot không có series nến nào phải bị từ chối ngay tại L0."""
        empty_snapshot = DecisionSnapshot(
            symbol="BTCUSDT",
            decision_time=datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
            snapshot_id="empty-snap",
            series=(),
            features=(),
        )
        engine = StrategyPipelineEngine()

        result = engine.execute(standard_strategy_card, empty_snapshot)

        assert result.status == "DATA_HYGIENE_FAILED"
        assert result.candidate is None
        assert any("Không có chuỗi nến" in r for r in result.reasons)

    def test_pipeline_short_breakout_strategy(self) -> None:
        """Chiến lược SHORT: Nến Shooting Star từ chối đỉnh sinh candidate SHORT hợp lệ."""
        port_series = TypedPort(name="price", data_type=DataType.PRICE_SERIES)
        port_scalar = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
        port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)

        node_l0 = GraphNode(
            node_id="l0_cleaner",
            layer=CompositionLayer.L0_DATA_HYGIENE,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="sanitize_ohlcv",
            outputs=(port_series,),
        )
        node_l1 = GraphNode(
            node_id="l1_upper_wick",
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="upper_wick_ratio",
            inputs=(port_series,),
            outputs=(port_scalar,),
        )
        node_l4 = GraphNode(
            node_id="l4_regime",
            layer=CompositionLayer.L4_CONTEXT_GATE,
            role=NodeExecutionRole.GATE_FILTER,
            operation="volatility_regime_gate",
            parameters={"min_atr": 5.0},
            inputs=(port_series,),
            outputs=(port_flag,),
        )
        node_l5 = GraphNode(
            node_id="l5_trigger",
            layer=CompositionLayer.L5_STRATEGY_TRIGGER,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="rejection_breakout_trigger",
            parameters={"threshold": 0.6, "direction": "SHORT"},
            inputs=(port_scalar, port_flag),
            outputs=(port_flag,),
        )
        node_l6 = GraphNode(
            node_id="l6_zones",
            layer=CompositionLayer.L6_ZONES,
            role=NodeExecutionRole.TERMINAL_ZONE,
            operation="atr_zone_calculator",
            parameters={"sl_multiplier": 1.5, "rr_ratio": 2.5},
            inputs=(port_flag,),
            outputs=(),
        )

        short_card = StrategyCard(
            card_id="SC-SHOOTING-STAR-SHORT-001",
            name="Shooting Star Rejection Short Strategy",
            version="1.0.0",
            author="Crypidian Lab",
            created_at=datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC),
            layers=[
                CompositionLayer.L0_DATA_HYGIENE,
                CompositionLayer.L1_CANDLE_ANATOMY,
                CompositionLayer.L4_CONTEXT_GATE,
                CompositionLayer.L5_STRATEGY_TRIGGER,
                CompositionLayer.L6_ZONES,
            ],
            nodes=[node_l0, node_l1, node_l4, node_l5, node_l6],
            edges=[
                GraphEdge("l0_cleaner", "price", "l1_upper_wick", "price"),
                GraphEdge("l0_cleaner", "price", "l4_regime", "price"),
                GraphEdge("l1_upper_wick", "val", "l5_trigger", "val"),
                GraphEdge("l4_regime", "flag", "l5_trigger", "flag"),
                GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
            ],
            parameters=[],
            compatibility=CompatibilityMap(
                target_venues=("BINANCE_USDM",),
                target_symbols=("BTCUSDT",),
                timeframes=("15m",),
            ),
            execution_profile=ExecutionProfile(
                assumed_taker_fee=Decimal("0.0005"),
                assumed_slippage_bps=Decimal("2.0"),
            ),
        )

        # Nến Shooting Star: Open=100, High=120, Low=98, Close=101
        # Range = 22, Upper wick = 120 - 101 = 19 -> upper_wick_ratio = 19/22 = 0.86 > 0.6
        star_bar = _create_bar(
            open_p=Decimal("100.0"),
            high_p=Decimal("120.0"),
            low_p=Decimal("98.0"),
            close_p=Decimal("101.0"),
        )
        snapshot = _create_snapshot(star_bar)
        engine = StrategyPipelineEngine()

        res = engine.execute(short_card, snapshot)

        assert res.status == "SUCCESS"
        assert res.candidate is not None
        assert res.candidate.direction == "SHORT"
        assert res.candidate.entry_price == Decimal("101.0")
        # Với SHORT: Stop Loss phải lớn hơn Entry, Take Profit phải nhỏ hơn Entry
        assert res.candidate.stop_loss > res.candidate.entry_price
        assert res.candidate.take_profit < res.candidate.entry_price
        assert res.candidate.risk_reward_ratio == Decimal("2.5")
