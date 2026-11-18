"""Bộ kiểm thử TDD (RED → GREEN) cho Strategy Cards mở rộng, Engine L0–L6 và Kiểm định Trực giao."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.application.decision_snapshot_builder import build_decision_snapshot_from_bars
from trading_bot.application.strategy_catalog import PRODUCT_CARD_IDS, strategy_registry
from trading_bot.application.strategy_market_requirements import (
    ENTRY_TIMEFRAME_BY_RELEASE,
    FEATURES_BY_RELEASE,
)
from trading_bot.application.strategy_pipeline.card_strategy import strategy_card_release_id
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.application.strategy_pipeline.microscope import MicroscopeReplayService
from trading_bot.application.strategy_pipeline.pipeline_engine import StrategyPipelineEngine
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy.contracts import NoTrade, StrategyCandidate, StrategySelection
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
from trading_bot.domain.strategy_card.schema import StrategyCard

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")

EXPECTED_CARD_IDS = (
    "SC-BINANCE-USDM-VOL-BREAKOUT-001",
    "SC-BINANCE-USDM-MEAN-REVERT-002",
    "SC-BINANCE-USDM-TSMOM-003",
    "SC-BINANCE-USDM-BBKC-SQUEEZE-004",
    "SC-BINANCE-USDM-TREND-PULLBACK-005",
    "SC-BINANCE-USDM-VWAP-REVERT-006",
    "SC-BINANCE-USDM-MACD-ADX-007",
    "SC-BINANCE-USDM-TAKER-FLOW-008",
)


def _bar(
    index: int,
    timeframe: str,
    close: float,
    *,
    open_p: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 120.0,
    taker_buy: float | None = 60.0,
    start: datetime = datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
) -> OHLCVBar:
    step = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}[timeframe]
    dt = timedelta(minutes=step)
    opened = start + index * dt
    closed = opened + dt - timedelta(microseconds=1)
    c = Decimal(str(round(close, 4)))
    o = Decimal(str(round(close if open_p is None else open_p, 4)))
    h = Decimal(str(round(max(close, float(o)) + 0.8 if high is None else high, 4)))
    low_d = Decimal(str(round(min(close, float(o)) - 0.8 if low is None else low, 4)))
    chk = f"{(index + 10):064x}"
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=o,
        high=h,
        low=low_d,
        close=c,
        volume=Decimal(str(volume)),
        quote_volume=Decimal(str(round(volume * close, 4))),
        taker_buy_base_volume=None if taker_buy is None else Decimal(str(taker_buy)),
        is_closed=True,
        checksum=chk,
        source_identity=MarketBarSourceIdentity(
            workspace_id=WORKSPACE_ID,
            record_id=UUID(int=50_000 + index + step),
            logical_key=f"BTCUSDT:{timeframe}:{int(opened.timestamp())}",
            revision=1,
            supersedes_record_id=None,
            checksum=chk,
        ),
    )


def test_all_8_strategy_cards_exist_have_no_dead_nodes_and_are_enabled() -> None:
    cards = build_default_demo_cards()
    assert tuple(c.card_id for c in cards) == EXPECTED_CARD_IDS
    assert PRODUCT_CARD_IDS == EXPECTED_CARD_IDS

    detector = DeadNodeDetector()
    registry = strategy_registry()
    for card in cards:
        report = detector.verify(card.nodes, card.edges)
        assert report.is_valid, (
            f"{card.card_id}: dead={report.dead_nodes}, anomalies={report.logic_anomalies}"
        )
        assert report.dead_nodes == []
        rel_id = strategy_card_release_id(card)
        assert registry.get_release(rel_id).strategy_id == card.card_id
        assert rel_id in FEATURES_BY_RELEASE
        assert rel_id in ENTRY_TIMEFRAME_BY_RELEASE


@pytest.mark.asyncio
async def test_admin_service_runs_real_orthogonality_check_and_detects_collinearity() -> None:
    principal = Principal(
        workspace_id=WORKSPACE_ID,
        membership_id=MEMBERSHIP_ID,
        user_id=USER_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    from tests.fixtures.sig6_consensus_bars import consensus_bars_provider

    service = StrategyCardAdminService(bars_provider=consensus_bars_provider)
    views = await service.list_strategy_cards(principal)
    assert len(views) == len(EXPECTED_CARD_IDS)
    for view in views:
        assert view.dead_nodes_count == 0
        assert view.is_verified is True, f"{view.card_id}: {view.consensus_warnings}"

    # Thẻ có 2 node đa cộng tuyến gần như đồng nhất (|r| >= 0.85) phải bị phát hiện thật!
    ps = TypedPort(name="ohlcv_in", data_type=DataType.PRICE_SERIES)
    pn = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
    pf = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)
    collinear_card = StrategyCard(
        card_id="SC-TEST-COLLINEAR-999",
        name="Collinear Duplicate Indicators Card",
        version="1.0.0",
        author="Test",
        created_at=datetime(2026, 11, 16, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L2_INDICATORS,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            GraphNode(
                "l0_clean",
                CompositionLayer.L0_DATA_HYGIENE,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "data_hygiene_check",
                outputs=(ps,),
            ),
            GraphNode(
                "l2_ema_a",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "exponential_moving_average",
                parameters={"period": 20},
                inputs=(ps,),
                outputs=(pn,),
            ),
            GraphNode(
                "l2_sma_b",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "simple_moving_average",
                parameters={"period": 20},
                inputs=(ps,),
                outputs=(pn,),
            ),
            GraphNode(
                "l4_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "volatility_regime_gate",
                parameters={"min_atr": 0.1},
                inputs=(ps, pn),
                outputs=(pf,),
            ),
            GraphNode(
                "l5_trigger",
                CompositionLayer.L5_STRATEGY_TRIGGER,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "rejection_breakout_trigger",
                parameters={"threshold": 0.5},
                inputs=(pn, pf),
                outputs=(pf,),
            ),
            GraphNode(
                "l6_zones",
                CompositionLayer.L6_ZONES,
                NodeExecutionRole.TERMINAL_ZONE,
                "atr_zone_calculator",
                parameters={"risk_reward": 2.0},
                inputs=(pf,),
            ),
        ],
        edges=[
            GraphEdge("l0_clean", "ohlcv_in", "l2_ema_a", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l2_sma_b", "ohlcv_in"),
            GraphEdge("l2_ema_a", "val", "l4_gate", "val"),
            GraphEdge("l2_sma_b", "val", "l5_trigger", "val"),
            GraphEdge("l4_gate", "flag", "l5_trigger", "flag"),
            GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
        ],
        parameters=[],
        compatibility=CompatibilityMap(
            target_venues=("BINANCE_USDM",), target_symbols=("BTCUSDT",), timeframes=("1h",)
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"), assumed_slippage_bps=Decimal("2.0")
        ),
    )
    collinear_service = StrategyCardAdminService(
        in_memory_cards=[collinear_card],
        bars_provider=consensus_bars_provider,
    )
    collinear_views = await collinear_service.list_strategy_cards(principal)
    assert collinear_views[0].has_consensus_warning is True
    assert collinear_views[0].is_verified is False


def test_card_002_mean_reversion_computes_real_zscore_and_blocks_single_bar_warmup() -> None:
    cards = {c.card_id: c for c in build_default_demo_cards()}
    card2 = cards["SC-BINANCE-USDM-MEAN-REVERT-002"]
    rel2 = strategy_card_release_id(card2)
    selection = StrategySelection("s-1", (rel2,), published=True)
    strategy = strategy_registry().resolve(rel2, selection)

    # 1 nến đơn lẻ trên StrategyCardStrategy -> fail-closed WARMUP!
    single_bar = _bar(0, "15m", 101.0, open_p=100.0, high=102.0, low=90.0)
    micro_snap = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id=rel2,
        decision_time=single_bar.available_at,
        series_quality_as_of=single_bar.available_at,
        bars_by_timeframe={"15m": (single_bar,)},
        position_is_flat=True,
        source_identity_required=True,
    )
    assert isinstance(strategy.evaluate(micro_snap, selection), NoTrade)

    # 205 nến đi ngang quanh 100 rồi nến cuối rút râu đáy (zscore <= -1.5) -> LONG
    bars = [_bar(i, "15m", 100.0 + ((i % 2) - 0.5) * 0.4, high=100.5, low=99.5) for i in range(204)]
    bars.append(_bar(204, "15m", 97.6, open_p=97.3, high=97.9, low=93.5))
    snap_long = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id=rel2,
        decision_time=bars[-1].available_at,
        series_quality_as_of=bars[-1].available_at,
        bars_by_timeframe={"15m": tuple(bars)},
        position_is_flat=True,
        source_identity_required=True,
    )
    res_long = strategy.evaluate(snap_long, selection)
    assert isinstance(res_long, StrategyCandidate), res_long
    assert res_long.direction == "long"


def test_cards_003_to_008_execute_and_short_circuit_l4_cleanly() -> None:
    cards = {c.card_id: c for c in build_default_demo_cards()}
    registry = strategy_registry()
    engine = StrategyPipelineEngine()

    # Taker-Flow Card 008: 205 nến 15m tích lũy + 15 nến tăng + phá đỉnh taker; 205 nến 1h
    bars_15m = [
        _bar(i, "15m", 100.0 + (i % 3) * 0.05, high=100.6, low=99.4, volume=100.0, taker_buy=52.0)
        for i in range(204)
    ]
    for offset in range(15):
        idx = 189 + offset
        bars_15m[idx] = _bar(
            idx,
            "15m",
            100.0 + offset * 0.25,
            high=100.5 + offset * 0.25,
            low=99.7 + offset * 0.25,
            volume=150.0,
            taker_buy=95.0,
        )
    bars_15m.append(
        _bar(204, "15m", 106.5, open_p=103.5, high=106.8, low=103.4, volume=260.0, taker_buy=215.0)
    )
    start_1h = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    bars_1h = tuple(
        _bar(i, "1h", 90.0 + i * 0.2, volume=300.0, taker_buy=170.0, start=start_1h)
        for i in range(260)
    )
    rel8 = strategy_card_release_id(cards["SC-BINANCE-USDM-TAKER-FLOW-008"])
    selection8 = StrategySelection("s-8", (rel8,), published=True)
    dt8 = bars_15m[-1].available_at
    snap8 = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id=rel8,
        decision_time=dt8,
        series_quality_as_of=dt8,
        bars_by_timeframe={
            "15m": tuple(bars_15m),
            "1h": tuple(b for b in bars_1h if b.available_at <= dt8),
        },
        position_is_flat=True,
        source_identity_required=True,
    )
    cand8 = registry.resolve(rel8, selection8).evaluate(snap8, selection8)
    assert isinstance(cand8, StrategyCandidate), cand8
    assert cand8.direction == "long"

    # Ngắt mạch L4 macro_blackout cho từng card mới 003..008
    for cid in EXPECTED_CARD_IDS[2:]:
        card = cards[cid]
        bars_map = {
            tf: (tuple(_bar(i, tf, 100.0 + i * 0.1, start=start_1h) for i in range(30)))
            for tf in FEATURES_BY_RELEASE[strategy_card_release_id(card)]
        }
        blackout_dt = max(tf_bars[-1].available_at for tf_bars in bars_map.values())
        # Đẩy các khung ngắn hơn tới sát blackout_dt để không bị tính là stale.
        fresh_bars_map = {}
        for tf, tf_bars in bars_map.items():
            shift = blackout_dt - tf_bars[-1].available_at
            fresh_bars_map[tf] = tuple(
                _bar(i, tf, 100.0 + i * 0.1, start=start_1h + shift) for i in range(30)
            )
        blackout_snap = build_decision_snapshot_from_bars(
            workspace_id=WORKSPACE_ID,
            symbol="BTCUSDT",
            strategy_release_id=strategy_card_release_id(card),
            decision_time=blackout_dt,
            series_quality_as_of=blackout_dt,
            bars_by_timeframe=fresh_bars_map,
            macro_blackout=True,
            source_identity_required=True,
        )
        pipe_res = engine.execute(card, blackout_snap)
        assert pipe_res.status == "CONTEXT_GATE_BLOCKED"
        assert pipe_res.trigger_result is None


def test_microscope_inspects_all_8_cards_across_presets_cleanly() -> None:
    microscope = MicroscopeReplayService()
    presets = microscope.get_presets()
    assert len(presets) >= 3
    for card in build_default_demo_cards():
        for preset in presets:
            trace = microscope.inspect_bar(card, preset.ohlcv, preset.context)
            assert trace.card_id == card.card_id
            assert len(trace.layer_traces) == len(card.nodes)
            if preset.id == "preset-low-vol-block":
                assert trace.is_short_circuited is True
