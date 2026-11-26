"""Unit test TDD cho CRY-486: Order Book L2, Bid/Ask Walls, OBI và cổng lọc L4."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
)
from trading_bot.application.order_book_collector import OrderBookCollector
from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    PIPELINE_ENGINE_VERSION,
    StrategyPipelineEngine,
)
from trading_bot.application.strategy_pipeline.planner_bridge import StrategyPipelinePlannerBridge
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.market.order_book import (
    ORDER_BOOK_CONTEXT_VERSION,
    OrderBookConfig,
    OrderBookLevel,
    OrderBookSnapshot,
    compute_order_book_context,
    from_port_snapshot,
    observation_from_snapshot_features,
)
from trading_bot.domain.strategy.checklists import _strategy_card_checklist
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
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
    TypedPort,
)
from trading_bot.domain.strategy_card.operations import resolve_operation
from trading_bot.domain.strategy_card.schema import StrategyCard

_T0 = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)


def _sample_snapshot(
    *,
    as_of: datetime = _T0,
    event_at: datetime | None = None,
    received_at: datetime | None = None,
    bids: tuple[OrderBookLevel, ...] | None = None,
    asks: tuple[OrderBookLevel, ...] | None = None,
) -> OrderBookSnapshot:
    ev = event_at or (as_of - timedelta(seconds=2))
    rx = received_at or (as_of - timedelta(seconds=1))
    default_bids = (
        OrderBookLevel(Decimal("100.0"), Decimal("10")),  # notional 1000
        OrderBookLevel(Decimal("99.8"), Decimal("10")),  # notional 998
        OrderBookLevel(Decimal("99.6"), Decimal("10")),  # notional 996
        OrderBookLevel(Decimal("99.2"), Decimal("120")),  # Bid Wall -0.85%
        OrderBookLevel(Decimal("98.5"), Decimal("5")),  # notional 492.5
    )
    default_asks = (
        OrderBookLevel(Decimal("100.2"), Decimal("5")),  # notional 501
        OrderBookLevel(Decimal("100.4"), Decimal("5")),  # notional 502
        OrderBookLevel(Decimal("100.5"), Decimal("80")),  # Ask Wall +0.4%
        OrderBookLevel(Decimal("100.8"), Decimal("5")),  # notional 504
        OrderBookLevel(Decimal("101.5"), Decimal("4")),  # notional 406
    )
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        as_of=as_of,
        event_at=ev,
        received_at=rx,
        bids=default_bids if bids is None else bids,
        asks=default_asks if asks is None else asks,
    )


def _sample_bars(count: int = 30, end_at: datetime = _T0) -> tuple[OHLCVBar, ...]:
    bars: list[OHLCVBar] = []
    for idx in range(count):
        open_time = end_at - timedelta(hours=count - idx)
        close_time = open_time + timedelta(hours=1)
        base = Decimal("100.0") + Decimal(idx) * Decimal("0.1")
        is_last = idx == count - 1
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=open_time,
                close_time=close_time,
                open=base,
                high=base + Decimal("1.5"),
                low=base - Decimal("1.5") if not is_last else base - Decimal("2.0"),
                close=base + Decimal("0.8") if is_last else base + Decimal("0.2"),
                volume=Decimal("1000"),
                is_closed=True,
                event_at=close_time,
                received_at=close_time,
                available_at=close_time,
            )
        )
    return tuple(bars)


def test_order_book_metrics_obi_and_walls_calculation() -> None:
    snap = _sample_snapshot()
    ctx = compute_order_book_context(
        snap,
        as_of=_T0,
        atr_14=Decimal("1.0"),
        config=OrderBookConfig(wall_median_multiplier=Decimal("3.0")),
    )

    assert ctx.version == ORDER_BOOK_CONTEXT_VERSION
    assert ctx.is_stale is False
    assert ctx.best_bid == Decimal("100.0")
    assert ctx.best_ask == Decimal("100.2")
    assert ctx.mid_price == Decimal("100.1")
    expected_spread_bps = ((Decimal("100.2") - Decimal("100.0")) / Decimal("100.1")) * Decimal(
        "10000"
    )
    assert ctx.spread_bps == expected_spread_bps

    # Trong dải ±50bps quanh 100.1: [99.5995, 100.6005]
    # Bids: 100.0 (1000), 99.8 (998), 99.6 (996) -> 2994
    # Asks: 100.2 (501), 100.4 (502), 100.5 (8040) -> 9043
    assert ctx.obi_50bps is not None
    expected_obi_50 = (Decimal("2994") - Decimal("9043")) / (Decimal("2994") + Decimal("9043"))
    assert ctx.obi_50bps == expected_obi_50

    # Trong dải ±100bps quanh 100.1: [99.099, 101.101]
    # Bids thêm 99.2 (11904) -> 14898
    # Asks thêm 100.8 (504) -> 9547
    assert ctx.obi_100bps is not None
    expected_obi_100 = (Decimal("14898") - Decimal("9547")) / (Decimal("14898") + Decimal("9547"))
    assert ctx.obi_100bps == expected_obi_100

    # Bid wall tại 99.2 (notional 11904 > 3 * median_bid 998)
    assert ctx.nearest_bid_wall_price == Decimal("99.2")
    assert ctx.nearest_bid_wall_notional == Decimal("11904.0")
    assert ctx.bid_wall_distance_atr == Decimal("0.9")

    # Ask wall tại 100.5 (notional 8040 > 3 * median_ask 502)
    assert ctx.nearest_ask_wall_price == Decimal("100.5")
    assert ctx.nearest_ask_wall_notional == Decimal("8040.0")
    assert ctx.ask_wall_distance_atr == Decimal("0.4")


def test_order_book_obi_returns_none_when_band_empty_and_bucket_grouping() -> None:
    # Các mức giá nằm xa ngoài ±50bps và ±100bps so với mid_price = 100.0
    snap = _sample_snapshot(
        bids=(
            OrderBookLevel(Decimal("95.0"), Decimal("10")),
            OrderBookLevel(Decimal("94.9"), Decimal("10")),
            OrderBookLevel(Decimal("94.0"), Decimal("50")),
        ),
        asks=(
            OrderBookLevel(Decimal("105.0"), Decimal("10")),
            OrderBookLevel(Decimal("105.1"), Decimal("10")),
            OrderBookLevel(Decimal("106.0"), Decimal("50")),
        ),
    )
    ctx = compute_order_book_context(
        snap,
        as_of=_T0,
        atr_14=None,
        config=OrderBookConfig(
            wall_median_multiplier=Decimal("2.0"),
            bucket_bps=Decimal("20"),
        ),
    )
    assert ctx.is_stale is False
    assert ctx.obi_50bps is None
    assert ctx.obi_100bps is None
    assert ctx.bid_wall_distance_atr is None
    assert ctx.ask_wall_distance_atr is None
    assert ctx.nearest_bid_wall_price == Decimal("94.0")
    assert ctx.nearest_ask_wall_price == Decimal("106.0")


@pytest.mark.parametrize(
    ("case_name", "kwargs"),
    [
        ("none_snapshot", {"snapshot": None}),
        ("empty_bids", {"bids": ()}),
        ("empty_asks", {"asks": ()}),
        (
            "crossed_book",
            {
                "bids": (OrderBookLevel(Decimal("100.5"), Decimal("10")),),
                "asks": (OrderBookLevel(Decimal("100.2"), Decimal("10")),),
            },
        ),
        (
            "stale_age",
            {
                "received_at": _T0 - timedelta(seconds=61),
                "event_at": _T0 - timedelta(seconds=62),
            },
        ),
        ("future_event_at", {"event_at": _T0 + timedelta(seconds=1)}),
        ("future_received_at", {"received_at": _T0 + timedelta(seconds=1)}),
    ],
)
def test_order_book_fail_closed_cases(case_name: str, kwargs: dict[str, Any]) -> None:
    snap = None if case_name == "none_snapshot" else _sample_snapshot(**kwargs)
    ctx = compute_order_book_context(snap, as_of=_T0, atr_14=Decimal("1.0"))
    assert ctx.is_stale is True
    assert ctx.best_bid is None
    assert ctx.best_ask is None
    assert ctx.mid_price is None
    assert ctx.spread_bps is None
    assert ctx.obi_50bps is None
    assert ctx.obi_100bps is None
    assert ctx.nearest_bid_wall_price is None
    assert ctx.nearest_ask_wall_price is None


def test_order_book_snapshot_features_roundtrip_and_point_in_time() -> None:
    snap = _sample_snapshot()
    ctx = compute_order_book_context(snap, as_of=_T0, atr_14=Decimal("1.0"))
    features = ctx.to_feature_observations()

    future_feat = FeatureObservation(
        feature_id="ob-future",
        name="orderbook_obi_50bps",
        timeframe="4h",
        value=0.99,
        previous_value=None,
        available_at=_T0 + timedelta(seconds=5),
        quality=FeatureQuality.READY,
    )
    decision_snap = DecisionSnapshot(
        snapshot_id="snap-ob-1",
        symbol="BTCUSDT",
        decision_time=_T0,
        series=(
            MarketSeries(
                timeframe="1h",
                bars=_sample_bars(10, _T0),
                quality=SeriesQuality.READY,
            ),
        ),
        features=(*features, future_feat),
        macro_blackout=False,
    )
    restored = observation_from_snapshot_features(decision_snap)
    assert restored is not None
    assert restored.is_stale is False
    assert restored.obi_50bps == pytest.approx(ctx.obi_50bps, rel=Decimal("1e-9"))
    assert restored.ask_wall_distance_atr == pytest.approx(
        ctx.ask_wall_distance_atr, rel=Decimal("1e-9")
    )
    assert restored.bid_wall_distance_atr == pytest.approx(
        ctx.bid_wall_distance_atr, rel=Decimal("1e-9")
    )


@pytest.mark.asyncio
async def test_binance_adapter_depth_parser_and_weight_and_collector(tmp_path: Path) -> None:
    recorded_requests: list[tuple[str, dict[str, str], int]] = []

    class _Limiter:
        async def acquire(self, *, weight: int, category: str | None = None) -> None:
            recorded_requests.append((category or "rest", {}, weight))

        def observe(self, headers: Any) -> None:
            _ = headers

        def configure_requests_per_minute(self, limit: int) -> None:
            _ = limit

    def _handler(request: httpx2.Request) -> httpx2.Response:
        recorded_requests[-1] = (
            request.url.path,
            dict(request.url.params),
            recorded_requests[-1][2],
        )
        return httpx2.Response(
            200,
            json={
                "lastUpdateId": 1027024,
                "E": int((_T0 - timedelta(seconds=1)).timestamp() * 1000),
                "T": int((_T0 - timedelta(seconds=1)).timestamp() * 1000),
                "bids": [["100.00", "10.0"], ["99.50", "150.0"]],
                "asks": [["100.20", "5.0"], ["100.60", "100.0"]],
            },
            headers={"X-MBX-USED-WEIGHT-1M": "5"},
        )

    adapter = BinanceUsdMMarketDataAdapter(
        transport=httpx2.MockTransport(_handler),
        rate_limiter=_Limiter(),
        clock=lambda: _T0,
    )
    async with adapter:
        page = await adapter.get_order_book("BTCUSDT", limit=100)
        assert recorded_requests == [("/fapi/v1/depth", {"symbol": "BTCUSDT", "limit": "100"}, 5)]
        port_snap = page.records[0]
        domain_snap = from_port_snapshot(port_snap, as_of=_T0)
        assert domain_snap.bids[0].price == Decimal("100.00")
        assert domain_snap.asks[0].price == Decimal("100.20")

        # Kiểm tra rights gate fail-closed khi từ chối quyền PAPER_DEMO
        class _DeniedGate:
            async def authorize(self, *, request: Any, now: datetime) -> BackfillRightsEvidence:
                raise PermissionError("Thiếu policy PAPER_DEMO.")

        collector_denied = OrderBookCollector(
            workspace_id=uuid4(),
            market_data=adapter,
            rights_gate=_DeniedGate(),
        )
        denied_res = await collector_denied.collect_symbol("BTCUSDT", now=_T0)
        assert denied_res.status == "rights_denied"
        assert len(recorded_requests) == 1  # Không gọi mạng khi bị từ chối quyền!

        # Kiểm tra collector thành công + cache + JSON snapshot + rate limit weight 5
        ws_id = uuid4()

        class _AllowedGate:
            async def authorize(self, *, request: Any, now: datetime) -> BackfillRightsEvidence:
                assert request.purpose is BackfillRightsPurpose.PAPER_DEMO
                return BackfillRightsEvidence(
                    workspace_id=ws_id,
                    provider="BINANCE_USDM_REST",
                    purpose=BackfillRightsPurpose.PAPER_DEMO,
                    terms_url="https://developers.binance.com/docs",
                    policy_fingerprint="a" * 64,
                    verified_at=now - timedelta(minutes=1),
                    valid_until=now + timedelta(minutes=5),
                )

        collector = OrderBookCollector(
            workspace_id=ws_id,
            market_data=adapter,
            rights_gate=_AllowedGate(),
            snapshot_dir=tmp_path,
            max_weight_per_minute=5,
        )
        ok_res = await collector.collect_symbol("BTCUSDT", now=_T0, atr_14=Decimal("1.0"))
        assert ok_res.status == "collected"
        assert ok_res.weight_used == 5
        assert collector.get_latest("BTCUSDT") is not None
        assert (tmp_path / "BTCUSDT.json").exists()
        saved_json = json.loads((tmp_path / "BTCUSDT.json").read_text(encoding="utf-8"))
        assert saved_json["symbol"] == "BTCUSDT"
        assert saved_json["version"] == ORDER_BOOK_CONTEXT_VERSION

        # Lần gọi ngay lập tức trong cùng phút vượt budget 5 weight -> rate_limited
        limited_res = await collector.collect_symbol("BTCUSDT", now=_T0 + timedelta(seconds=5))
        assert limited_res.status == "rate_limited"
        assert len(recorded_requests) == 2


def test_l4_only_registration_and_gates_warn_filter_behavior() -> None:
    assert (
        resolve_operation("orderbook_wall_gate", layer=CompositionLayer.L4_CONTEXT_GATE) is not None
    )
    assert (
        resolve_operation("order_book_imbalance_gate", layer=CompositionLayer.L4_CONTEXT_GATE)
        is not None
    )
    assert (
        resolve_operation("orderbook_wall_gate", layer=CompositionLayer.L5_STRATEGY_TRIGGER) is None
    )
    assert (
        resolve_operation("order_book_imbalance_gate", layer=CompositionLayer.L5_STRATEGY_TRIGGER)
        is None
    )

    ctx = compute_order_book_context(_sample_snapshot(), as_of=_T0, atr_14=Decimal("1.0"))
    evaluator = ContextGateEvaluator()

    # Ask wall tại 100.5 -> distance_atr = 0.4 <= 0.5 -> cản Long
    warn_wall = evaluator.evaluate_orderbook_wall_gate(
        context=ctx,
        expected_direction="long",
        min_wall_distance_atr=Decimal("0.5"),
        mode="warn",
    )
    assert warn_wall.status is ContextGateStatus.PASS
    assert any("ORDERBOOK_ASK_WALL_NEAR" in w for w in warn_wall.warnings)

    filter_wall = evaluator.evaluate_orderbook_wall_gate(
        context=ctx,
        expected_direction="long",
        min_wall_distance_atr=Decimal("0.5"),
        mode="filter",
    )
    assert filter_wall.status is ContextGateStatus.BLOCK
    assert any("ORDERBOOK_ASK_WALL_NEAR" in r for r in filter_wall.reasons)

    # OBI 50bps = (2994 - 9043)/12037 ~= -0.5025 <= -0.35 -> bất lợi cho Long
    filter_obi = evaluator.evaluate_order_book_imbalance_gate(
        context=ctx,
        expected_direction="long",
        max_adverse_obi=Decimal("0.35"),
        mode="filter",
    )
    assert filter_obi.status is ContextGateStatus.BLOCK
    assert any("ORDERBOOK_ADVERSE_OBI_LONG" in r for r in filter_obi.reasons)

    # Fail-closed khi thiếu dữ liệu nếu opt-in
    missing_fail_closed = evaluator.evaluate_orderbook_wall_gate(
        context=None,
        expected_direction="long",
        mode="filter",
        fail_closed_on_missing=True,
    )
    assert missing_fail_closed.status is ContextGateStatus.BLOCK


def test_pipeline_engine_and_checklist_and_planner_bridge_integration() -> None:
    assert PIPELINE_ENGINE_VERSION == "l0-l6-v4"
    ctx = compute_order_book_context(_sample_snapshot(), as_of=_T0, atr_14=Decimal("1.0"))

    def _build_card(mode: str) -> StrategyCard:
        port_series = TypedPort(name="price", data_type=DataType.PRICE_SERIES)
        port_scalar = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
        port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)
        nodes = [
            GraphNode(
                node_id="n_l0",
                layer=CompositionLayer.L0_DATA_HYGIENE,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="data_hygiene_check",
                outputs=(port_series,),
            ),
            GraphNode(
                node_id="n_l1",
                layer=CompositionLayer.L1_CANDLE_ANATOMY,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="wick_rejection_strength",
                inputs=(port_series,),
                outputs=(port_scalar,),
            ),
            GraphNode(
                node_id="n_l2",
                layer=CompositionLayer.L2_INDICATORS,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="atr",
                parameters={"period": 14, "default_atr": 1.0},
                inputs=(port_series,),
                outputs=(port_scalar,),
            ),
            GraphNode(
                node_id="n_l4_wall",
                layer=CompositionLayer.L4_CONTEXT_GATE,
                role=NodeExecutionRole.GATE_FILTER,
                operation="orderbook_wall_gate",
                parameters={"mode": mode, "direction": "long", "min_wall_distance_atr": "0.5"},
                inputs=(port_series,),
                outputs=(port_flag,),
            ),
            GraphNode(
                node_id="n_l4_obi",
                layer=CompositionLayer.L4_CONTEXT_GATE,
                role=NodeExecutionRole.GATE_FILTER,
                operation="order_book_imbalance_gate",
                parameters={"mode": mode, "direction": "long", "max_adverse_obi": "0.35"},
                inputs=(port_series,),
                outputs=(port_flag,),
            ),
            GraphNode(
                node_id="n_l5",
                layer=CompositionLayer.L5_STRATEGY_TRIGGER,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="rejection_breakout_trigger",
                parameters={"direction": "long", "threshold": 0.2},
                inputs=(port_scalar, port_flag),
                outputs=(port_flag,),
            ),
            GraphNode(
                node_id="n_l6",
                layer=CompositionLayer.L6_ZONES,
                role=NodeExecutionRole.TERMINAL_ZONE,
                operation="atr_zone_calculator",
                parameters={"stop_atr_multiple": "1.5", "risk_reward": "2.0"},
                inputs=(port_flag,),
                outputs=(),
            ),
        ]
        edges = [
            GraphEdge("n_l0", "price", "n_l1", "price"),
            GraphEdge("n_l0", "price", "n_l2", "price"),
            GraphEdge("n_l0", "price", "n_l4_wall", "price"),
            GraphEdge("n_l0", "price", "n_l4_obi", "price"),
            GraphEdge("n_l1", "val", "n_l5", "val"),
            GraphEdge("n_l4_wall", "flag", "n_l5", "flag"),
            GraphEdge("n_l4_obi", "flag", "n_l5", "flag"),
            GraphEdge("n_l5", "flag", "n_l6", "flag"),
        ]
        return StrategyCard(
            card_id="card-cry486-ob",
            name="OrderBook L4 Gate Card",
            version="1.0.0",
            author="Crypidian Core Team",
            created_at=_T0,
            layers=[
                CompositionLayer.L0_DATA_HYGIENE,
                CompositionLayer.L1_CANDLE_ANATOMY,
                CompositionLayer.L2_INDICATORS,
                CompositionLayer.L3_ALGORITHMS,
                CompositionLayer.L4_CONTEXT_GATE,
                CompositionLayer.L5_STRATEGY_TRIGGER,
                CompositionLayer.L6_ZONES,
            ],
            nodes=nodes,
            edges=edges,
            compatibility=CompatibilityMap(
                target_venues=("binance_usdm",),
                target_symbols=("BTCUSDT",),
                timeframes=("1h",),
            ),
            execution_profile=ExecutionProfile(
                assumed_taker_fee=Decimal("0.0005"),
                assumed_slippage_bps=Decimal("2.0"),
            ),
        )

    snapshot = DecisionSnapshot(
        snapshot_id="snap-pipe-ob",
        symbol="BTCUSDT",
        decision_time=_T0,
        series=(
            MarketSeries(
                timeframe="1h",
                bars=_sample_bars(30, _T0),
                quality=SeriesQuality.READY,
            ),
        ),
        features=ctx.to_feature_observations(),
        macro_blackout=False,
    )
    engine = StrategyPipelineEngine()

    res_block = engine.execute(_build_card("filter"), snapshot)
    assert res_block.status == "CONTEXT_GATE_BLOCKED"
    assert res_block.gate_result is not None
    assert any("ORDERBOOK_ASK_WALL_NEAR" in r for r in res_block.reasons)

    res_warn = engine.execute(_build_card("warn"), snapshot)
    assert res_warn.status == "SUCCESS"
    assert res_warn.candidate is not None
    assert "gate_warnings" in res_warn.candidate.metadata

    contract = StrategyPipelinePlannerBridge.to_contract_candidate(
        res_warn.candidate, _build_card("warn"), snapshot
    )
    meta = dict(contract.card_metadata)
    assert "orderbook_wall_gate" in meta.get("l4_gates", "")
    assert "ORDERBOOK_ASK_WALL_NEAR" in meta.get("l4_warnings", "")

    checklist = _strategy_card_checklist(_build_card("filter"), snapshot)
    keys = {item.key: item for item in checklist}
    assert "l4_orderbook_wall" in keys
    assert keys["l4_orderbook_wall"].passed is False
    assert "l4_order_book_imbalance" in keys
    assert keys["l4_order_book_imbalance"].passed is False
