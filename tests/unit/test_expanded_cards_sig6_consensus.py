"""Kiểm thử SIG-6 ``card_consensus`` và ``PriceBar`` cho đầy đủ 8 Strategy Cards (CRY-435)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from tests.fixtures.sig6_consensus_bars import consensus_bars

from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.application.strategy_pipeline.consensus import (
    CONSENSUS_VERSION,
    PriceBar,
    card_consensus,
    card_node_series,
)
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.market.models import OHLCVBar
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

PRINCIPAL = Principal(
    workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
    membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
    user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
    roles=frozenset({Role.OWNER}),
    mfa_verified_at=datetime.now(UTC),
)

EXPECTED_TOTAL_CONDITIONS = {
    "SC-BINANCE-USDM-VOL-BREAKOUT-001": 2,
    "SC-BINANCE-USDM-MEAN-REVERT-002": 5,
    "SC-BINANCE-USDM-TSMOM-003": 4,
    "SC-BINANCE-USDM-BBKC-SQUEEZE-004": 4,
    "SC-BINANCE-USDM-TREND-PULLBACK-005": 6,
    "SC-BINANCE-USDM-VWAP-REVERT-006": 5,
    "SC-BINANCE-USDM-MACD-ADX-007": 5,
    "SC-BINANCE-USDM-TAKER-FLOW-008": 5,
}


def _build_multi_regime_ohlcv_bars(
    n: int = 300,
    *,
    timeframe: str = "15m",
    broken_at: int | None = None,
) -> list[OHLCVBar]:
    """Sinh chuỗi OHLCVBar đa chế độ tất định kích hoạt đủ L1–L3 của cả 8 thẻ."""
    step = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(timeframe, 15)
    dt = timedelta(minutes=step)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    close = 100.0
    for idx in range(n):
        if idx < 60:
            drift = math.sin(idx / 5.0) * 1.1 + math.cos(idx / 2.5) * 0.6
            spread = 0.8 + abs(math.sin(idx)) * 0.6
            vol = 100.0 + 30.0 * abs(math.sin(idx * 0.7))
            tbuy_ratio = 0.45 + 0.15 * math.sin(idx * 0.8)
        elif idx < 95:
            drift = 0.05 * math.sin(idx * 1.3)
            spread = 0.12
            vol = 80.0
            tbuy_ratio = 0.50 + 0.05 * math.sin(idx)
        elif idx == 95:
            drift = 5.5
            spread = 0.6
            vol = 420.0
            tbuy_ratio = 0.88
        elif idx < 150:
            if idx % 14 == 0:
                drift = -1.6
            elif idx % 14 == 1:
                drift = 1.9
            else:
                drift = 0.55 + 0.25 * math.sin(idx / 3.0)
            spread = 0.6 + 0.4 * abs(math.cos(idx))
            vol = 130.0 + (180.0 if idx in (118, 132) else 40.0 * abs(math.sin(idx)))
            tbuy_ratio = 0.82 if idx in (95, 118, 132) else (0.55 + 0.15 * math.sin(idx * 0.6))
        elif idx < 230:
            drift = -0.45 + 0.8 * math.sin(idx / 4.0)
            spread = 0.7 + 0.5 * abs(math.sin(idx * 1.1))
            vol = 140.0 + (220.0 if idx == 190 else 50.0 * abs(math.cos(idx)))
            tbuy_ratio = 0.15 if idx == 190 else (0.42 + 0.15 * math.cos(idx * 0.7))
        else:
            drift = 0.4 + 1.4 * math.sin(idx / 4.5)
            spread = 0.9 + 0.6 * abs(math.sin(idx * 0.9))
            vol = 150.0 + 90.0 * abs(math.sin(idx * 0.6))
            tbuy_ratio = 0.52 + 0.28 * math.sin(idx * 0.85)

        open_ = close
        close = max(10.0, open_ + drift)
        if idx == 95:
            high = close + 0.2
            low = open_ - 0.15
        elif 96 <= idx < 150 and idx % 14 == 1:
            high = close + 0.3
            low = min(open_, close) - 2.2
        else:
            high = max(open_, close) + spread * (0.4 + 0.5 * abs(math.sin(idx * 0.9)))
            low = min(open_, close) - spread * (0.4 + 0.5 * abs(math.cos(idx * 1.1)))

        if broken_at == idx:
            # Dùng PriceBar ở test bẩn vì OHLCVBar chặn High < Low ngay khi khởi tạo.
            raise ValueError("Dùng PriceBar cho kiểm thử nến bẩn L0.")

        opened = start + idx * dt
        closed = opened + dt - timedelta(microseconds=1)
        tbuy = vol * tbuy_ratio
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe=timeframe,
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=Decimal(f"{open_:.4f}"),
                high=Decimal(f"{high:.4f}"),
                low=Decimal(f"{low:.4f}"),
                close=Decimal(f"{close:.4f}"),
                volume=Decimal(f"{vol:.4f}"),
                taker_buy_base_volume=Decimal(f"{tbuy:.4f}"),
                is_closed=True,
            )
        )
    return bars


def _to_rich_price_bars(ohlcv_bars: list[OHLCVBar]) -> list[PriceBar]:
    return [
        PriceBar(
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            open_time=b.open_time,
            taker_buy_base_volume=b.taker_buy_base_volume,
        )
        for b in ohlcv_bars
    ]


def test_price_bar_keeps_four_arg_compatibility_and_supports_optional_fields() -> None:
    pos = PriceBar(Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"))
    kw = PriceBar(
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
    )
    assert pos == kw
    assert pos.volume is None
    assert pos.open_time is None
    assert pos.taker_buy_base_volume is None

    ts = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    rich = PriceBar(
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("250"),
        open_time=ts,
        taker_buy_base_volume=Decimal("150"),
    )
    assert rich.volume == Decimal("250")
    assert rich.open_time == ts
    assert rich.taker_buy_base_volume == Decimal("150")


def test_all_8_cards_on_minimal_120_price_bars_never_inflate_with_zero_constants() -> None:
    """Trên PriceBar 4 trường (thiếu volume/open_time/taker), node thiếu dữ liệu không bị đếm ảo."""
    bars = consensus_bars(120)
    cards = {c.card_id: c for c in build_default_demo_cards()}
    assert set(cards) == set(EXPECTED_TOTAL_CONDITIONS)

    expected_active_120 = {
        "SC-BINANCE-USDM-VOL-BREAKOUT-001": 2,
        "SC-BINANCE-USDM-MEAN-REVERT-002": 5,  # l3_zscore được tính thật, không phải hằng 0
        "SC-BINANCE-USDM-TSMOM-003": 3,  # l3_tsmom cần > 252 nến
        "SC-BINANCE-USDM-BBKC-SQUEEZE-004": 3,  # l3_squeeze cần volume
        "SC-BINANCE-USDM-TREND-PULLBACK-005": 5,  # sine wave không khớp nến pullback
        "SC-BINANCE-USDM-VWAP-REVERT-006": 3,  # thiếu open_time + volume cho VWAP
        "SC-BINANCE-USDM-MACD-ADX-007": 5,  # l3_macd_score được tính thật
        "SC-BINANCE-USDM-TAKER-FLOW-008": 3,  # thiếu volume + taker_buy_base_volume
    }

    for card_id, card in cards.items():
        summary = card_consensus(card, bars)
        assert summary.version == CONSENSUS_VERSION
        assert summary.status == "ok", f"{card_id}: {summary}"
        assert summary.sample_size == 120
        assert summary.total_conditions == EXPECTED_TOTAL_CONDITIONS[card_id]
        assert summary.independent_conditions == expected_active_120[card_id], (
            f"{card_id}: got {summary.independent_conditions}/{summary.total_conditions}, "
            f"inactive={summary.inactive_conditions}"
        )
        assert summary.warnings == ()
        assert summary.high_correlation_pairs == ()

    # Card 002 và 007 có L3 chạy hoàn chỉnh trên PriceBar 120: chuỗi L3 phải biến thiên thật.
    s2 = card_node_series(cards["SC-BINANCE-USDM-MEAN-REVERT-002"], bars)
    assert len(set(s2["l3_zscore"])) > 10
    s7 = card_node_series(cards["SC-BINANCE-USDM-MACD-ADX-007"], bars)
    assert len(set(s7["l3_macd_score"])) >= 2

    # Card 006 và 008 ghi rõ các node VWAP / Taker chưa đo được trên PriceBar 4 trường.
    s6_sum = card_consensus(cards["SC-BINANCE-USDM-VWAP-REVERT-006"], bars)
    assert s6_sum.inactive_conditions == ("l2_vwap", "l3_vwap_dev")
    s8_sum = card_consensus(cards["SC-BINANCE-USDM-TAKER-FLOW-008"], bars)
    assert s8_sum.inactive_conditions == ("l2_taker_flow", "l3_flow_breakout")


def test_all_8_cards_on_full_ohlcv_and_rich_price_bars_compute_all_nodes_cleanly() -> None:
    """Trên OHLCVBar thật và PriceBar mở rộng 300 nến đa chế độ, cả 8/8 thẻ đạt x/y đầy đủ."""
    cards = build_default_demo_cards()
    ohlcv_15m = _build_multi_regime_ohlcv_bars(300, timeframe="15m")
    rich_15m = _to_rich_price_bars(ohlcv_15m)

    for bars_label, bars in (("OHLCVBar", ohlcv_15m), ("RichPriceBar", rich_15m)):
        for card in cards:
            summary = card_consensus(card, bars)
            expected_y = EXPECTED_TOTAL_CONDITIONS[card.card_id]
            assert summary.status == "ok", f"{bars_label}/{card.card_id}: {summary}"
            assert summary.sample_size == 300
            assert summary.total_conditions == expected_y
            assert summary.independent_conditions == expected_y, (
                f"{bars_label}/{card.card_id}: {summary.independent_conditions}/{expected_y}, "
                f"inactive={summary.inactive_conditions}, pairs={summary.high_correlation_pairs}"
            )
            assert summary.inactive_conditions == ()
            assert summary.warnings == ()

            series = card_node_series(card, bars)
            for node_id, values in series.items():
                assert max(values) - min(values) > 1e-9, (
                    f"{bars_label}/{card.card_id}/{node_id} bị hằng số"
                )

    # Kiểm tra riêng 120 nến OHLCVBar (khớp khung 15m của Card 006 VWAP & Card 008 Taker Flow).
    ohlcv_120 = ohlcv_15m[80:200]
    card6 = next(c for c in cards if c.card_id == "SC-BINANCE-USDM-VWAP-REVERT-006")
    sum6 = card_consensus(card6, ohlcv_120)
    assert sum6.status == "ok"
    assert sum6.independent_conditions == 5
    assert sum6.total_conditions == 5
    assert sum6.inactive_conditions == ()

    card8 = next(c for c in cards if c.card_id == "SC-BINANCE-USDM-TAKER-FLOW-008")
    sum8 = card_consensus(card8, ohlcv_120)
    assert sum8.status == "ok"
    assert sum8.independent_conditions == 5
    assert sum8.total_conditions == 5
    assert sum8.inactive_conditions == ()


def test_l3_node_series_has_no_lookahead_and_skips_dirty_bars() -> None:
    """Chuỗi L1–L3 không nhìn trước nến tương lai và bỏ qua nến vi phạm L0."""
    bars = _to_rich_price_bars(_build_multi_regime_ohlcv_bars(150, timeframe="15m"))
    dirty = PriceBar(
        open=Decimal("100"),
        high=Decimal("99"),
        low=Decimal("101"),  # High < Low vi phạm L0
        close=Decimal("100"),
        volume=Decimal("100"),
        open_time=bars[70].open_time,
        taker_buy_base_volume=Decimal("50"),
    )
    bars_with_dirty = [*bars[:70], dirty, *bars[71:]]

    for card_id in (
        "SC-BINANCE-USDM-MEAN-REVERT-002",
        "SC-BINANCE-USDM-VWAP-REVERT-006",
        "SC-BINANCE-USDM-TAKER-FLOW-008",
    ):
        card = next(c for c in build_default_demo_cards() if c.card_id == card_id)
        full = card_node_series(card, bars_with_dirty)
        assert {len(v) for v in full.values()} == {149}
        prefix = card_node_series(card, bars_with_dirty[:90])
        for node_id, values in prefix.items():
            assert full[node_id][: len(values)] == values


def test_card_consensus_detects_l3_collinearity_and_handles_constant_zero_safely() -> None:
    """Phát hiện đa cộng tuyến ở L3 và không công nhận trực giao khi toàn bộ chuỗi phẳng."""
    ps = TypedPort(name="ohlcv_in", data_type=DataType.PRICE_SERIES)
    pn = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
    pf = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)

    collinear_l3 = StrategyCard(
        card_id="SC-TEST-L3-COLLINEAR",
        name="Collinear L3 Z-Score Card",
        version="1.0.0",
        author="Test",
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L3_ALGORITHMS,
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
                "l3_z20",
                CompositionLayer.L3_ALGORITHMS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "zscore_normalizer",
                parameters={"window": 20},
                inputs=(ps,),
                outputs=(pn,),
            ),
            GraphNode(
                "l3_z21",
                CompositionLayer.L3_ALGORITHMS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "zscore_normalizer",
                parameters={"window": 21},
                inputs=(ps,),
                outputs=(pn,),
            ),
            GraphNode(
                "l4_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "volatility_regime_gate",
                parameters={"min_atr": 0.1},
                inputs=(ps,),
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
            GraphEdge("l0_clean", "ohlcv_in", "l3_z20", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l3_z21", "ohlcv_in"),
            GraphEdge("l3_z20", "val", "l5_trigger", "val"),
            GraphEdge("l3_z21", "val", "l5_trigger", "val"),
            GraphEdge("l4_gate", "flag", "l5_trigger", "flag"),
            GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
        ],
        parameters=[],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",), target_symbols=("BTCUSDT",), timeframes=("15m",)
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"), assumed_slippage_bps=Decimal("2.0")
        ),
    )

    summary_col = card_consensus(collinear_l3, consensus_bars(120))
    assert summary_col.status == "warning"
    assert summary_col.total_conditions == 2
    assert summary_col.independent_conditions == 1
    assert ("l3_z20", "l3_z21") in {p[:2] for p in summary_col.high_correlation_pairs}

    # Chuỗi nến phẳng hoàn toàn (không biến thiên) không được báo "ok".
    flat_bars = [
        PriceBar(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100")) for _ in range(120)
    ]
    card_mean_revert = next(
        c for c in build_default_demo_cards() if c.card_id == "SC-BINANCE-USDM-MEAN-REVERT-002"
    )
    flat_summary = card_consensus(card_mean_revert, flat_bars)
    assert flat_summary.status == "insufficient_data"
    assert flat_summary.independent_conditions is None

    # Nếu nến bẩn L0 bị loại làm số mẫu sạch < MIN_CONSENSUS_SAMPLES → insufficient_data.
    mostly_dirty = [*consensus_bars(48), *consensus_bars(10, broken_at=0)[:1] * 10]
    dirty_summary = card_consensus(collinear_l3, mostly_dirty)
    assert dirty_summary.status == "insufficient_data"
    assert dirty_summary.sample_size == 48
    assert dirty_summary.independent_conditions is None

    # Card có 1 node biến thiên + 1 node hằng 0 (thiếu taker) trả 1/2, không phải 2/2.
    one_real_one_zero = StrategyCard(
        card_id="SC-TEST-ONE-REAL-ONE-ZERO",
        name="One Active One Constant Card",
        version="1.0.0",
        author="Test",
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
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
                "l1_wick",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "lower_wick_ratio",
                inputs=(ps,),
                outputs=(pn,),
            ),
            GraphNode(
                "l2_taker",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "taker_flow_imbalance",
                parameters={"window": 3},
                inputs=(ps,),
                outputs=(pn,),
            ),
            GraphNode(
                "l4_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "volatility_regime_gate",
                parameters={"min_atr": 0.1},
                inputs=(ps,),
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
            GraphEdge("l0_clean", "ohlcv_in", "l1_wick", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l2_taker", "ohlcv_in"),
            GraphEdge("l1_wick", "val", "l5_trigger", "val"),
            GraphEdge("l2_taker", "val", "l5_trigger", "val"),
            GraphEdge("l4_gate", "flag", "l5_trigger", "flag"),
            GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
        ],
        parameters=[],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",), target_symbols=("BTCUSDT",), timeframes=("15m",)
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"), assumed_slippage_bps=Decimal("2.0")
        ),
    )
    mixed_summary = card_consensus(one_real_one_zero, consensus_bars(120))
    assert mixed_summary.status == "ok"
    assert mixed_summary.total_conditions == 2
    assert mixed_summary.independent_conditions == 1
    assert mixed_summary.inactive_conditions == ("l2_taker",)


@pytest.mark.asyncio
async def test_admin_service_reports_full_independence_for_all_8_cards_with_rich_bars() -> None:
    async def rich_provider(_principal: Principal, _symbol: str, timeframe: str) -> list[PriceBar]:
        return _to_rich_price_bars(_build_multi_regime_ohlcv_bars(300, timeframe=timeframe))

    service = StrategyCardAdminService(bars_provider=rich_provider)
    views = await service.list_strategy_cards(PRINCIPAL)
    assert len(views) == 8
    for view in views:
        expected = EXPECTED_TOTAL_CONDITIONS[view.card_id]
        assert view.is_verified is True
        assert view.consensus_status == "ok"
        assert view.has_consensus_warning is False
        assert view.consensus_sample_size == 300
        assert view.total_conditions == expected
        assert view.independent_conditions == expected
