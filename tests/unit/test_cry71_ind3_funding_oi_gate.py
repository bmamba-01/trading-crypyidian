"""Unit tests TDD cho CRY-71 (IND-3): Funding & Open Interest làm cổng bối cảnh L4.

Bao phủ:
1. Feature point-in-time cho funding cực trị và biến động OI (bất biến tiền tố / không nhìn trước).
2. Cơ chế fail-closed khi thiếu mẫu quá khứ, dữ liệu quá cũ (stale) hoặc thiếu OI.
3. Cổng bối cảnh L4 ở hai chế độ ``warn`` (cảnh báo) và ``filter`` (lọc cứng) chống chiều lệnh.
4. Bất biến kiến trúc: CHỈ lọc/cảnh báo ở tầng L4, TUYỆT ĐỐI KHÔNG làm ngòi nổ L5.
5. Tích hợp với StrategyPipelineEngine, PlannerBridge, Checklist L0–L6 và tương thích ngược.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    PIPELINE_ENGINE_VERSION,
    StrategyPipelineEngine,
)
from trading_bot.application.strategy_pipeline.planner_bridge import StrategyPipelinePlannerBridge
from trading_bot.domain.market.derivatives_context import (
    DERIVATIVES_CONTEXT_FORMULA_VERSION,
    DerivativesContextConfig,
    FundingCrowdingState,
    OpenInterestRegime,
    compute_derivatives_context,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.research.derivatives import DerivativesSnapshot
from trading_bot.domain.strategy.checklists import _strategy_card_checklist
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
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
from trading_bot.domain.strategy_card.operations import resolve_operation
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash

BASE_TIME = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def _make_deriv_snapshot(
    *,
    hour_offset: int,
    funding_rate: str,
    oi_value_usdt: str | None = "1000000000",
    oi_value_24h_ago: str | None = None,
    symbol: str = "BTCUSDT",
) -> DerivativesSnapshot:
    collected_at = BASE_TIME + timedelta(hours=hour_offset)
    return DerivativesSnapshot(
        symbol=symbol,
        collected_at=collected_at,
        mark_price=Decimal("90000"),
        funding_rate=Decimal(funding_rate),
        next_funding_time=collected_at + timedelta(hours=8),
        premium_time=collected_at,
        oi_contracts=Decimal("10000") if oi_value_usdt is not None else None,
        oi_value_usdt=Decimal(oi_value_usdt) if oi_value_usdt is not None else None,
        oi_at=collected_at if oi_value_usdt is not None else None,
        oi_value_24h_ago=Decimal(oi_value_24h_ago) if oi_value_24h_ago is not None else None,
    )


def _make_bar(
    *,
    open_p: str = "100",
    high_p: str = "102",
    low_p: str = "90",
    close_p: str = "101",
    close_time: datetime | None = None,
) -> OHLCVBar:
    ct = close_time or (BASE_TIME + timedelta(hours=12))
    ot = ct - timedelta(minutes=15) + timedelta(microseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="15m",
        open_time=ot,
        close_time=ct,
        event_at=ct,
        received_at=ct,
        available_at=ct,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
        volume=Decimal("150"),
        is_closed=True,
    )


def _make_snapshot(
    bar: OHLCVBar,
    *,
    features: tuple[FeatureObservation, ...] = (),
) -> DecisionSnapshot:
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
        snapshot_id=f"snap-ind3-{int(bar.close_time.timestamp())}",
        series=(series,),
        features=features,
        position_is_flat=True,
        macro_blackout=False,
        macro_evidence_ids=("macro-1",),
        macro_evidence_available_at=bar.close_time,
        macro_is_fresh=True,
    )


def _make_card_with_funding_oi_gate(
    *,
    mode: str = "filter",
    direction: str = "LONG",
    fail_closed_on_missing: bool = False,
    block_on_oi_anomaly: bool = False,
    trigger_threshold: float = 0.5,
) -> StrategyCard:
    port_series = TypedPort(name="price", data_type=DataType.PRICE_SERIES)
    port_scalar = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
    port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)

    nodes = [
        GraphNode(
            node_id="l0_cleaner",
            layer=CompositionLayer.L0_DATA_HYGIENE,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="sanitize_ohlcv",
            outputs=(port_series,),
        ),
        GraphNode(
            node_id="l1_wick",
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="wick_rejection_strength",
            inputs=(port_series,),
            outputs=(port_scalar,),
        ),
        GraphNode(
            node_id="l2_atr",
            layer=CompositionLayer.L2_INDICATORS,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="atr_calculator",
            parameters={"default_atr": 10.0},
            inputs=(port_series,),
            outputs=(port_scalar,),
        ),
        GraphNode(
            node_id="l4_funding_oi",
            layer=CompositionLayer.L4_CONTEXT_GATE,
            role=NodeExecutionRole.GATE_FILTER,
            operation="funding_oi_context_gate",
            parameters={
                "mode": mode,
                "direction": direction,
                "fail_closed_on_missing": fail_closed_on_missing,
                "block_on_oi_anomaly": block_on_oi_anomaly,
            },
            inputs=(port_series,),
            outputs=(port_flag,),
        ),
        GraphNode(
            node_id="l5_trigger",
            layer=CompositionLayer.L5_STRATEGY_TRIGGER,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="rejection_breakout_trigger",
            parameters={"threshold": trigger_threshold, "direction": direction},
            inputs=(port_scalar, port_flag),
            outputs=(port_flag,),
        ),
        GraphNode(
            node_id="l6_zones",
            layer=CompositionLayer.L6_ZONES,
            role=NodeExecutionRole.TERMINAL_ZONE,
            operation="atr_zone_calculator",
            parameters={"sl_multiplier": 2.0, "rr_ratio": 2.0},
            inputs=(port_flag,),
            outputs=(),
        ),
    ]
    edges = [
        GraphEdge("l0_cleaner", "price", "l1_wick", "price"),
        GraphEdge("l0_cleaner", "price", "l2_atr", "price"),
        GraphEdge("l0_cleaner", "price", "l4_funding_oi", "price"),
        GraphEdge("l1_wick", "val", "l5_trigger", "val"),
        GraphEdge("l4_funding_oi", "flag", "l5_trigger", "flag"),
        GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
    ]
    return StrategyCard(
        card_id="SC-IND3-FUNDING-OI-TEST",
        name="Hammer with Funding & OI Context Gate",
        version="1.0.0",
        author="Crypidian Core Team",
        created_at=BASE_TIME,
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
            timeframes=("15m",),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.0"),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Feature point-in-time & Prefix Invariance
# ---------------------------------------------------------------------------


def test_derivatives_context_prefix_invariance_no_lookahead() -> None:
    """Thêm snapshot tương lai (> as_of) tuyệt đối không làm thay đổi kết quả tại as_of."""
    normal_rates = [
        "0.00010",
        "0.00011",
        "0.00009",
        "0.00012",
        "0.00008",
        "0.00010",
        "0.00011",
        "0.00009",
        "0.00010",
        "0.00040",  # t=9: đột biến dương (crowded long)
    ]
    prefix = [
        _make_deriv_snapshot(
            hour_offset=idx,
            funding_rate=rate,
            oi_value_usdt=str(1_000_000_000 + idx * 10_000_000),
        )
        for idx, rate in enumerate(normal_rates)
    ]
    future = [
        _make_deriv_snapshot(hour_offset=10, funding_rate="-0.00200", oi_value_usdt="500000000"),
        _make_deriv_snapshot(hour_offset=11, funding_rate="-0.00300", oi_value_usdt="300000000"),
        _make_deriv_snapshot(
            hour_offset=5, funding_rate="0.00999", oi_value_usdt="9999999999", symbol="ETHUSDT"
        ),
    ]
    as_of = BASE_TIME + timedelta(hours=9, minutes=30)

    obs_prefix = compute_derivatives_context(prefix, symbol="BTCUSDT", as_of=as_of)
    obs_extended = compute_derivatives_context(prefix + future, symbol="BTCUSDT", as_of=as_of)

    assert obs_prefix == obs_extended
    assert obs_prefix.formula_version == DERIVATIVES_CONTEXT_FORMULA_VERSION
    assert obs_prefix.is_stale is False
    assert obs_prefix.crowding_state is FundingCrowdingState.CROWDED_LONG
    assert obs_prefix.funding_zscore is not None
    assert obs_prefix.funding_zscore > 2.0
    assert obs_prefix.funding_percentile == pytest.approx(1.0)
    assert obs_prefix.oi_change_pct == pytest.approx(Decimal("0.09"))
    assert obs_prefix.oi_regime is OpenInterestRegime.EXPANDING
    assert obs_prefix.oi_anomaly is False


def test_derivatives_context_crowded_short_and_oi_contracting_anomaly() -> None:
    """Phát hiện crowded short và OI giảm đột biến (anomaly) dùng oi_value_24h_ago."""
    rates = [
        "-0.00005",
        "-0.00004",
        "-0.00006",
        "-0.00005",
        "-0.00003",
        "-0.00005",
        "-0.00006",
        "-0.00004",
        "-0.00060",  # t=8: cực âm
    ]
    snaps = [
        _make_deriv_snapshot(
            hour_offset=idx,
            funding_rate=rate,
            oi_value_usdt="800000000" if idx == 8 else "1000000000",
            oi_value_24h_ago="1000000000" if idx == 8 else None,
        )
        for idx, rate in enumerate(rates)
    ]
    as_of = BASE_TIME + timedelta(hours=8, minutes=15)

    obs = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=as_of)
    assert obs.is_stale is False
    assert obs.crowding_state is FundingCrowdingState.CROWDED_SHORT
    assert obs.funding_zscore is not None
    assert obs.funding_zscore < -2.0
    assert obs.funding_percentile == pytest.approx(0.0)
    assert obs.oi_change_pct == Decimal("-0.2")
    assert obs.oi_regime is OpenInterestRegime.CONTRACTING
    assert obs.oi_anomaly is True


# ---------------------------------------------------------------------------
# 2. Fail-closed khi thiếu mẫu hoặc dữ liệu quá cũ (stale)
# ---------------------------------------------------------------------------


def test_derivatives_context_fail_closed_when_insufficient_samples() -> None:
    """Ít hơn min_samples quan sát quá khứ → trả None/UNKNOWN cho funding crowding."""
    snaps = [
        _make_deriv_snapshot(hour_offset=idx, funding_rate="0.00080", oi_value_usdt="1000000000")
        for idx in range(4)
    ]
    as_of = BASE_TIME + timedelta(hours=3, minutes=10)
    obs = compute_derivatives_context(
        snaps,
        symbol="BTCUSDT",
        as_of=as_of,
        config=DerivativesContextConfig(min_samples=8),
    )

    assert obs.is_stale is False
    assert obs.funding_rate == Decimal("0.00080")
    assert obs.funding_zscore is None
    assert obs.funding_percentile is None
    assert obs.crowding_state is FundingCrowdingState.UNKNOWN


def test_derivatives_context_fail_closed_when_stale_or_empty() -> None:
    """Dữ liệu cũ hơn max_age hoặc rỗng → fail-closed toàn bộ chỉ số về None/UNKNOWN."""
    snaps = [
        _make_deriv_snapshot(hour_offset=idx, funding_rate="0.00010", oi_value_usdt="1000000000")
        for idx in range(10)
    ]
    stale_as_of = BASE_TIME + timedelta(hours=15)  # trễ 6h > mặc định 3h

    obs_stale = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=stale_as_of)
    assert obs_stale.is_stale is True
    assert obs_stale.funding_rate is None
    assert obs_stale.funding_zscore is None
    assert obs_stale.funding_percentile is None
    assert obs_stale.crowding_state is FundingCrowdingState.UNKNOWN
    assert obs_stale.oi_change_pct is None
    assert obs_stale.oi_regime is OpenInterestRegime.UNKNOWN
    assert obs_stale.oi_anomaly is False

    obs_empty = compute_derivatives_context([], symbol="BTCUSDT", as_of=stale_as_of)
    assert obs_empty.is_stale is True
    assert obs_empty.sample_count == 0
    assert obs_empty.crowding_state is FundingCrowdingState.UNKNOWN

    with pytest.raises(ValueError, match="múi giờ"):
        compute_derivatives_context(
            snaps,
            symbol="BTCUSDT",
            as_of=datetime(2026, 11, 20, 12, 0),  # noqa: DTZ001
        )


def test_to_feature_observations_preserves_quality_and_available_at() -> None:
    """FeatureObservation xuất từ DerivativesContextObservation giữ đúng quality và available_at."""
    snaps = [
        _make_deriv_snapshot(
            hour_offset=idx,
            funding_rate="0.00010" if idx < 9 else "0.00045",
            oi_value_usdt="1000000000",
        )
        for idx in range(10)
    ]
    as_of = BASE_TIME + timedelta(hours=9, minutes=30)
    obs = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=as_of)
    feats = obs.to_feature_observations(timeframe="15m")
    by_name = {f.name: f for f in feats}

    assert by_name["derivatives_funding_rate"].quality is FeatureQuality.READY
    assert by_name["derivatives_crowding_code"].value == 1.0
    assert all(f.available_at <= as_of for f in feats)

    stale_obs = compute_derivatives_context(
        snaps, symbol="BTCUSDT", as_of=BASE_TIME + timedelta(hours=20)
    )
    stale_feats = {f.name: f for f in stale_obs.to_feature_observations(timeframe="15m")}
    assert stale_feats["derivatives_funding_rate"].quality is FeatureQuality.STALE
    assert stale_feats["derivatives_is_stale"].value == 1.0


# ---------------------------------------------------------------------------
# 3. Cổng bối cảnh L4 (chỉ lọc / cảnh báo)
# ---------------------------------------------------------------------------


def test_l4_funding_oi_gate_blocks_contra_direction_in_filter_mode_and_warns_in_warn_mode() -> None:
    """Crowded long chặn LONG ở chế độ filter, cảnh báo ở chế độ warn, và cho qua SHORT."""
    snaps = [
        _make_deriv_snapshot(
            hour_offset=idx,
            funding_rate="0.00010" if idx < 9 else "0.00060",
            oi_value_usdt="1000000000",
            oi_value_24h_ago="1000000000",
        )
        for idx in range(10)
    ]
    as_of = BASE_TIME + timedelta(hours=9, minutes=15)
    obs = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=as_of)
    evaluator = ContextGateEvaluator()

    res_filter_long = evaluator.evaluate_funding_oi_context_gate(
        observation=obs, expected_direction="LONG", mode="filter"
    )
    assert res_filter_long.status is ContextGateStatus.BLOCK
    assert any("FUNDING_CROWDED_LONG_CONTRA" in r for r in res_filter_long.reasons)

    res_warn_long = evaluator.evaluate_funding_oi_context_gate(
        observation=obs, expected_direction="LONG", mode="warn"
    )
    assert res_warn_long.status is ContextGateStatus.PASS
    assert res_warn_long.reasons == ()
    assert any("FUNDING_CROWDED_LONG_CONTRA" in w for w in res_warn_long.warnings)

    res_filter_short = evaluator.evaluate_funding_oi_context_gate(
        observation=obs, expected_direction="SHORT", mode="filter"
    )
    assert res_filter_short.status is ContextGateStatus.PASS
    assert res_filter_short.reasons == ()


def test_l4_funding_oi_gate_pipeline_short_circuit_and_warning_propagation() -> None:
    """Pipeline ngắt mạch L4 ở chế độ filter; ở chế độ warn vẫn sinh candidate kèm l4_warnings."""
    snaps = [
        _make_deriv_snapshot(
            hour_offset=idx,
            funding_rate="0.00010" if idx < 9 else "0.00060",
            oi_value_usdt="1000000000",
        )
        for idx in range(10)
    ]
    as_of = BASE_TIME + timedelta(hours=9, minutes=15)
    obs = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=as_of)
    bar = _make_bar(close_time=as_of)
    snapshot = _make_snapshot(bar, features=obs.to_feature_observations(timeframe="15m"))
    engine = StrategyPipelineEngine()

    # Chế độ filter: chặn tại L4, không chạy L5
    card_filter = _make_card_with_funding_oi_gate(mode="filter", direction="LONG")
    res_block = engine.execute(card_filter, snapshot)
    assert res_block.status == "CONTEXT_GATE_BLOCKED"
    assert res_block.candidate is None
    assert res_block.trigger_result is None
    assert any("FUNDING_CROWDED_LONG_CONTRA" in r for r in res_block.reasons)

    # Chế độ warn: L4 PASS kèm warnings, L5 sinh candidate, PlannerBridge bảo toàn l4_warnings
    card_warn = _make_card_with_funding_oi_gate(mode="warn", direction="LONG")
    res_warn = engine.execute(card_warn, snapshot)
    assert res_warn.status == "SUCCESS"
    assert res_warn.candidate is not None
    assert res_warn.gate_result is not None
    assert len(res_warn.gate_result.warnings) >= 1
    assert "gate_warnings" in res_warn.candidate.metadata

    contract_cand = StrategyPipelinePlannerBridge.to_contract_candidate(
        res_warn.candidate, card_warn, snapshot
    )
    meta_dict = dict(contract_cand.card_metadata)
    assert "l4_warnings" in meta_dict
    assert "FUNDING_CROWDED_LONG_CONTRA" in meta_dict["l4_warnings"]


def test_l4_funding_oi_gate_missing_data_opt_in_fail_closed() -> None:
    """Khi thiếu dữ liệu phái sinh: fail_closed_on_missing=False chỉ cảnh báo, True thì BLOCK."""
    bar = _make_bar()
    snapshot = _make_snapshot(bar, features=())
    engine = StrategyPipelineEngine()

    card_soft = _make_card_with_funding_oi_gate(
        mode="filter", direction="LONG", fail_closed_on_missing=False
    )
    res_soft = engine.execute(card_soft, snapshot)
    assert res_soft.status == "SUCCESS"
    assert res_soft.gate_result is not None
    assert any("DERIVATIVES_CONTEXT_UNAVAILABLE" in w for w in res_soft.gate_result.warnings)

    card_strict = _make_card_with_funding_oi_gate(
        mode="filter", direction="LONG", fail_closed_on_missing=True
    )
    res_strict = engine.execute(card_strict, snapshot)
    assert res_strict.status == "CONTEXT_GATE_BLOCKED"
    assert any("DERIVATIVES_CONTEXT_UNAVAILABLE" in r for r in res_strict.reasons)


# ---------------------------------------------------------------------------
# 4. Bất biến: CHỈ lọc/cảnh báo L4, TUYỆT ĐỐI KHÔNG làm ngòi nổ L5
# ---------------------------------------------------------------------------


def test_funding_oi_operation_is_strictly_l4_and_rejected_at_l5() -> None:
    """Operation funding/OI chỉ hợp lệ ở L4, bị từ chối ở L5 bởi registry, verifier và engine."""
    assert (
        resolve_operation("funding_oi_context_gate", layer=CompositionLayer.L4_CONTEXT_GATE)
        is not None
    )
    assert (
        resolve_operation("funding_oi_context_gate", layer=CompositionLayer.L5_STRATEGY_TRIGGER)
        is None
    )
    assert (
        resolve_operation("derivatives_context_gate", layer=CompositionLayer.L5_STRATEGY_TRIGGER)
        is None
    )

    port_series = TypedPort(name="price", data_type=DataType.PRICE_SERIES)
    port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)
    illegal_card = StrategyCard(
        card_id="SC-ILLEGAL-L5-FUNDING",
        name="Illegal L5 Funding Trigger",
        version="1.0.0",
        author="Test",
        created_at=BASE_TIME,
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            GraphNode(
                node_id="l0_cleaner",
                layer=CompositionLayer.L0_DATA_HYGIENE,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="sanitize_ohlcv",
                outputs=(port_series,),
            ),
            GraphNode(
                node_id="l5_illegal_funding",
                layer=CompositionLayer.L5_STRATEGY_TRIGGER,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="funding_oi_context_gate",
                inputs=(port_series,),
                outputs=(port_flag,),
            ),
            GraphNode(
                node_id="l6_zones",
                layer=CompositionLayer.L6_ZONES,
                role=NodeExecutionRole.TERMINAL_ZONE,
                operation="atr_zone_calculator",
                inputs=(port_flag,),
            ),
        ],
        edges=[
            GraphEdge("l0_cleaner", "price", "l5_illegal_funding", "price"),
            GraphEdge("l5_illegal_funding", "flag", "l6_zones", "flag"),
        ],
        compatibility=CompatibilityMap(timeframes=("15m",)),
        execution_profile=ExecutionProfile(Decimal("0.0005"), Decimal("2")),
    )

    report = DeadNodeDetector().verify(illegal_card.nodes, illegal_card.edges)
    assert report.is_valid is False

    res = StrategyPipelineEngine().execute(illegal_card, _make_snapshot(_make_bar()))
    assert res.status == "ERROR"
    assert res.candidate is None
    assert any("UNREGISTERED_OPERATION" in r for r in res.reasons)


def test_l4_funding_oi_pass_never_generates_l5_signal_by_itself() -> None:
    """L4 funding/OI PASS nhưng L5 không đủ ngưỡng → NO_TRIGGER, không bao giờ tự vào lệnh."""
    snaps = [
        _make_deriv_snapshot(hour_offset=idx, funding_rate="0.00010", oi_value_usdt="1000000000")
        for idx in range(10)
    ]
    as_of = BASE_TIME + timedelta(hours=9, minutes=15)
    obs = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=as_of)
    bar = _make_bar(close_time=as_of)
    snapshot = _make_snapshot(bar, features=obs.to_feature_observations(timeframe="15m"))

    card = _make_card_with_funding_oi_gate(mode="filter", direction="LONG", trigger_threshold=0.99)
    res = StrategyPipelineEngine().execute(card, snapshot)
    assert res.gate_result is not None
    assert res.gate_result.status is ContextGateStatus.PASS
    assert res.status == "NO_TRIGGER"
    assert res.candidate is None


# ---------------------------------------------------------------------------
# 5. Checklist L0–L6 & Tương thích ngược
# ---------------------------------------------------------------------------


def test_strategy_card_checklist_renders_l4_funding_oi_context_item() -> None:
    """Checklist L0–L6 hiển thị rõ trạng thái cổng L4 funding/OI."""
    snaps = [
        _make_deriv_snapshot(
            hour_offset=idx,
            funding_rate="0.00010" if idx < 9 else "0.00060",
            oi_value_usdt="1000000000",
        )
        for idx in range(10)
    ]
    as_of = BASE_TIME + timedelta(hours=9, minutes=15)
    obs = compute_derivatives_context(snaps, symbol="BTCUSDT", as_of=as_of)
    bar = _make_bar(close_time=as_of)
    snapshot = _make_snapshot(bar, features=obs.to_feature_observations(timeframe="15m"))

    card_filter = _make_card_with_funding_oi_gate(mode="filter", direction="LONG")
    items = _strategy_card_checklist(card_filter, snapshot)
    deriv_items = [item for item in items if item.key == "l4_funding_oi_context"]
    assert len(deriv_items) == 1
    assert deriv_items[0].passed is False


def test_backward_compatibility_demo_cards_and_engine_version_unchanged() -> None:
    """Giữ nguyên PIPELINE_ENGINE_VERSION và mọi demo card hợp lệ không đổi hash."""
    assert PIPELINE_ENGINE_VERSION == "l0-l6-v4"
    cards = build_default_demo_cards()
    assert len(cards) >= 1
    for card in cards:
        assert len(compute_canonical_hash(card)) == 64
        assert DeadNodeDetector().verify(card.nodes, card.edges).is_valid is True
