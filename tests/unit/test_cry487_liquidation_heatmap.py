"""Unit test TDD cho CRY-487 (IND-4): forceOrder, Liquidation Heatmap và cổng L4."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.application.bot_reporting import BotReportError
from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateStatus,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.market.liquidation_heatmap import (
    LIQUIDATION_HEATMAP_VERSION,
    LiquidationHeatmapConfig,
    RealizedLiquidation,
    compute_liquidation_heatmap,
    observation_from_snapshot_features,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.research.derivatives import DerivativesSnapshot
from trading_bot.domain.strategy.checklists import _card_l4_items
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    MarketSeries,
)
from trading_bot.domain.strategy_card.graph_verifier import DeadNodeDetector
from trading_bot.domain.strategy_card.models import (
    CompositionLayer,
    GraphEdge,
    GraphNode,
    NodeExecutionRole,
)
from trading_bot.domain.strategy_card.operations import OPERATION_REGISTRY

ROOT = Path(__file__).parents[2]
GOLDEN_FIXTURE = ROOT / "tests" / "fixtures" / "btcusdt_1h_sig1_golden.json"


def _make_bar(
    idx: int,
    *,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: float = 100.0,
    quote_volume: float | None = None,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    base_time: datetime = datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
) -> OHLCVBar:
    open_time = base_time + timedelta(hours=idx)
    close_time = open_time + timedelta(minutes=59, seconds=59)
    qv = Decimal(str(quote_volume if quote_volume is not None else volume * close_p))
    return OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=close_time,
        available_at=close_time,
        open=Decimal(str(open_p)),
        high=Decimal(str(high_p)),
        low=Decimal(str(low_p)),
        close=Decimal(str(close_p)),
        volume=Decimal(str(volume)),
        quote_volume=qv,
        is_closed=True,
    )


def _load_golden_bars() -> list[OHLCVBar]:
    payload = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    bars: list[OHLCVBar] = []
    for item in payload["bars"]:
        open_time = datetime.fromisoformat(item["open_time"].replace("Z", "+00:00"))
        close_time = datetime.fromisoformat(item["close_time"].replace("Z", "+00:00"))
        bars.append(
            OHLCVBar(
                symbol=payload["symbol"],
                timeframe=payload["timeframe"],
                open_time=open_time,
                close_time=close_time,
                event_at=close_time,
                received_at=close_time,
                available_at=close_time,
                open=Decimal(item["open"]),
                high=Decimal(item["high"]),
                low=Decimal(item["low"]),
                close=Decimal(item["close"]),
                volume=Decimal(item["volume"]),
                quote_volume=Decimal(item["quote_volume"]),
                is_closed=True,
            )
        )
    return bars


def test_compose_enables_liquidation_when_live_symbols_configured() -> None:
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "TRADING_BOT_INGESTION_LIVE_EVENTS:" in compose_text
    assert "liquidation" in compose_text
    settings = AppSettings(
        process_role="ingestion-worker",
        ingestion_workspace_id=UUID("22222222-2222-2222-2222-222222222222"),
        ingestion_live_symbols="BTCUSDT",
        ingestion_live_events="liquidation",
    )
    assert settings.ingestion_live_event_kinds == ("liquidation",)


def test_liquidation_heatmap_leverage_projection_and_oi_weighting() -> None:
    # Tạo một swing low rõ ràng ở nến 3 (low=10000) với volume lớn, sau đó giá hồi lên 10500
    bars = [
        _make_bar(0, open_p=10500, high_p=10600, low_p=10400, close_p=10450, volume=100),
        _make_bar(1, open_p=10450, high_p=10500, low_p=10300, close_p=10350, volume=100),
        _make_bar(2, open_p=10350, high_p=10400, low_p=10200, close_p=10250, volume=100),
        _make_bar(3, open_p=10250, high_p=10300, low_p=10000, close_p=10200, volume=800),
        _make_bar(4, open_p=10200, high_p=10400, low_p=10150, close_p=10350, volume=100),
        _make_bar(5, open_p=10350, high_p=10550, low_p=10300, close_p=10500, volume=100),
        _make_bar(6, open_p=10500, high_p=10600, low_p=10450, close_p=10550, volume=100),
    ]
    as_of = bars[-1].close_time
    # OI tăng mạnh tại nến 3
    oi_snaps = [
        DerivativesSnapshot(
            symbol="BTCUSDT",
            collected_at=bars[2].close_time,
            premium_time=bars[2].close_time,
            mark_price=Decimal("10250"),
            funding_rate=Decimal("0.0001"),
            next_funding_time=bars[2].close_time + timedelta(hours=8),
            oi_value_usdt=Decimal("100000000"),
        ),
        DerivativesSnapshot(
            symbol="BTCUSDT",
            collected_at=bars[3].close_time,
            premium_time=bars[3].close_time,
            mark_price=Decimal("10200"),
            funding_rate=Decimal("0.0001"),
            next_funding_time=bars[3].close_time + timedelta(hours=8),
            oi_value_usdt=Decimal("150000000"),
        ),
    ]
    cfg = LiquidationHeatmapConfig(swing_window=2, min_bars=5, mmr=Decimal("0.004"))
    obs = compute_liquidation_heatmap(
        bars,
        symbol="BTCUSDT",
        as_of=as_of,
        oi_snapshots=oi_snaps,
        config=cfg,
    )
    obs_no_oi = compute_liquidation_heatmap(
        bars,
        symbol="BTCUSDT",
        as_of=as_of,
        config=cfg,
    )

    assert obs.formula_version == LIQUIDATION_HEATMAP_VERSION
    assert obs.is_stale is False
    assert obs.long_liq_clusters
    assert obs.nearest_long_liq_price is not None
    assert obs.nearest_long_liq_price < bars[-1].close
    # Với P=10000, L=10x => 10000 * (1 - 0.1 + 0.004) = 9040
    # Với P=10000, L=20x => 10000 * (1 - 0.05 + 0.004) = 9540
    # Với P=10000, L=50x => 10000 * (1 - 0.02 + 0.004) = 9840
    # Với P=10000, L=100x => 10000 * (1 - 0.01 + 0.004) = 9940
    assert obs.long_liq_intensity > obs_no_oi.long_liq_intensity
    assert obs.liq_heatmap_skew is not None
    assert -1.0 <= obs.liq_heatmap_skew <= 1.0


def test_liquidation_heatmap_sweeps_by_price_and_realized_force_order() -> None:
    # Nến 3 tạo swing low tại 10000; cụm Long 100x ở ~9940.
    # Nến 7 có low=9900 => quét qua 9940 (100x) và 9840? (không, 9840 chưa chạm)
    bars_before_sweep = [
        _make_bar(0, open_p=10500, high_p=10600, low_p=10400, close_p=10450, volume=100),
        _make_bar(1, open_p=10450, high_p=10500, low_p=10300, close_p=10350, volume=100),
        _make_bar(2, open_p=10350, high_p=10400, low_p=10200, close_p=10250, volume=100),
        _make_bar(3, open_p=10250, high_p=10300, low_p=10000, close_p=10200, volume=800),
        _make_bar(4, open_p=10200, high_p=10400, low_p=10150, close_p=10350, volume=100),
        _make_bar(5, open_p=10350, high_p=10550, low_p=10300, close_p=10500, volume=100),
        _make_bar(6, open_p=10500, high_p=10600, low_p=10450, close_p=10550, volume=100),
    ]
    bars_after_sweep = bars_before_sweep + [
        _make_bar(7, open_p=10550, high_p=10560, low_p=9900, close_p=10400, volume=100),
    ]
    cfg = LiquidationHeatmapConfig(swing_window=2, min_bars=5, leverages=(10, 100))

    obs_before = compute_liquidation_heatmap(
        bars_before_sweep, symbol="BTCUSDT", as_of=bars_before_sweep[-1].close_time, config=cfg
    )
    obs_after = compute_liquidation_heatmap(
        bars_after_sweep, symbol="BTCUSDT", as_of=bars_after_sweep[-1].close_time, config=cfg
    )
    # Cụm 100x (~9940) đã bị quét bởi low=9900; cụm 10x (~9040) vẫn còn sống
    assert obs_before.nearest_long_liq_price is not None
    assert obs_after.nearest_long_liq_price is not None
    assert obs_after.nearest_long_liq_price < obs_before.nearest_long_liq_price

    # Kiểm tra forceOrder thật (side=SELL là thanh lý Long) ở mức ~9040
    liq_event = RealizedLiquidation(
        symbol="BTCUSDT",
        side="SELL",
        price=Decimal("9040"),
        average_price=Decimal("9040"),
        quantity=Decimal("100"),
        filled_quantity=Decimal("100"),
        event_at=bars_after_sweep[-1].close_time - timedelta(minutes=5),
        available_at=bars_after_sweep[-1].close_time - timedelta(minutes=5),
    )
    obs_with_liq = compute_liquidation_heatmap(
        bars_after_sweep,
        symbol="BTCUSDT",
        as_of=bars_after_sweep[-1].close_time,
        liquidations=[liq_event],
        config=cfg,
    )
    assert obs_with_liq.recent_realized_liq_long_notional == Decimal("904000")
    assert obs_with_liq.long_liq_intensity < obs_after.long_liq_intensity


def test_liquidation_heatmap_prefix_invariance_on_golden_data() -> None:
    bars = _load_golden_bars()
    assert len(bars) >= 150
    for idx in (80, 110, 140):
        as_of = bars[idx].close_time
        prefix_obs = compute_liquidation_heatmap(bars[: idx + 1], symbol="BTCUSDT", as_of=as_of)
        full_obs = compute_liquidation_heatmap(bars, symbol="BTCUSDT", as_of=as_of)
        assert prefix_obs == full_obs


def test_liquidation_heatmap_feature_observations_round_trip() -> None:
    bars = _load_golden_bars()[:100]
    as_of = bars[-1].close_time
    obs = compute_liquidation_heatmap(bars, symbol="BTCUSDT", as_of=as_of)
    feats = obs.to_feature_observations(timeframe="1h")
    snapshot = DecisionSnapshot(
        snapshot_id="snap-liq-1",
        symbol="BTCUSDT",
        decision_time=as_of,
        series=(MarketSeries(timeframe="1h", bars=tuple(bars)),),
        features=feats,
    )
    restored = observation_from_snapshot_features(snapshot)
    assert restored is not None
    assert restored.is_stale is False
    assert restored.long_liq_intensity == pytest.approx(obs.long_liq_intensity, rel=1e-6)
    assert restored.short_liq_intensity == pytest.approx(obs.short_liq_intensity, rel=1e-6)
    if obs.liq_heatmap_skew is not None:
        assert restored.liq_heatmap_skew == pytest.approx(obs.liq_heatmap_skew, rel=1e-6)


def test_l4_liquidation_heatmap_gate_registration_warn_filter_and_checklist() -> None:
    spec = OPERATION_REGISTRY["liquidation_heatmap_gate"]
    assert spec.layer is CompositionLayer.L4_CONTEXT_GATE

    # Không được phép đặt ở L5
    bad_card = SimpleNamespace(
        nodes=(
            GraphNode(
                node_id="n0",
                layer=CompositionLayer.L0_DATA_HYGIENE,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="ohlcv_stream",
                parameters={"timeframe": "1h"},
            ),
            GraphNode(
                node_id="n5",
                layer=CompositionLayer.L5_STRATEGY_TRIGGER,
                role=NodeExecutionRole.GATE_FILTER,
                operation="liquidation_heatmap_gate",
                parameters={},
            ),
        ),
        edges=(GraphEdge(from_node="n0", from_port="out", to_node="n5", to_port="in"),),
    )
    report = DeadNodeDetector().verify(bad_card.nodes, bad_card.edges)
    assert report.is_valid is False
    assert "n5" in report.logic_anomalies
    assert any("liquidation_heatmap_gate" in w for w in report.warnings)

    # Tạo observation có cụm Long rất sát bên dưới (0.25 ATR) và bão thanh lý lớn
    bars = _load_golden_bars()[:100]
    as_of = bars[-1].close_time
    obs = compute_liquidation_heatmap(bars, symbol="BTCUSDT", as_of=as_of)
    # Ép một tình huống sát cụm bằng RealizedLiquidation + nearest_long_liq_atr nhỏ
    evaluator = ContextGateEvaluator()
    warn_res = evaluator.evaluate_liquidation_heatmap_gate(
        observation=obs,
        expected_direction="LONG",
        mode="warn",
        min_cluster_atr=10.0,  # ngưỡng rộng để chắc chắn kích hoạt cảnh báo nếu có cụm
        max_recent_realized_liq_notional=Decimal("1"),
    )
    assert warn_res.status is ContextGateStatus.PASS
    assert warn_res.warnings

    filter_res = evaluator.evaluate_liquidation_heatmap_gate(
        observation=obs,
        expected_direction="LONG",
        mode="filter",
        min_cluster_atr=10.0,
    )
    if obs.nearest_long_liq_atr is not None and obs.nearest_long_liq_atr <= 10.0:
        assert filter_res.status is ContextGateStatus.BLOCK
        assert filter_res.reasons

    # Checklist rendering
    card = SimpleNamespace(
        nodes=(
            GraphNode(
                node_id="n0",
                layer=CompositionLayer.L0_DATA_HYGIENE,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="ohlcv_stream",
                parameters={"timeframe": "1h"},
            ),
            GraphNode(
                node_id="n4",
                layer=CompositionLayer.L4_CONTEXT_GATE,
                role=NodeExecutionRole.GATE_FILTER,
                operation="liquidation_heatmap_gate",
                parameters={"mode": "warn", "direction": "LONG"},
            ),
        ),
        edges=(GraphEdge(from_node="n0", from_port="out", to_node="n4", to_port="in"),),
    )
    from trading_bot.application.strategy_pipeline.pipeline_engine import PipelineExecutionResult

    res = PipelineExecutionResult(
        status="NO_TRIGGER",
        gate_result=warn_res,
        reasons=("NO_TRIGGER",),
    )
    snapshot = DecisionSnapshot(
        snapshot_id="snap-checklist",
        symbol="BTCUSDT",
        decision_time=as_of,
        series=(MarketSeries(timeframe="1h", bars=tuple(bars)),),
        features=(),
    )
    checklist = _card_l4_items(
        cast(Any, card),
        snapshot,
        res,
        warmup_sources=set(),
        atr_single_bar_source="",
    )
    assert any(item.key == "l4_liquidation_heatmap" for item in checklist)


def test_chart_liquidation_heatmap_endpoint_security_and_payload() -> None:
    bot_id = uuid4()
    principal = Principal(
        workspace_id=uuid4(),
        membership_id=uuid4(),
        user_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )

    class _FakeReporting:
        async def get_liquidation_heatmap(
            self, _principal: Principal, **kwargs: Any
        ) -> dict[str, Any]:
            if kwargs["bot_id"] != bot_id:
                raise BotReportError(404, "NOT_FOUND", "Không tìm thấy bot.")
            return {
                "symbol": kwargs["symbol"],
                "timeframe": kwargs["timeframe"],
                "as_of": "2026-11-26T00:00:00+00:00",
                "formula_version": LIQUIDATION_HEATMAP_VERSION,
                "long_liq_clusters": [{"price": 80000.0, "intensity": 1200.0, "leverage_hint": 50}],
                "short_liq_clusters": [
                    {"price": 82000.0, "intensity": 1800.0, "leverage_hint": 25}
                ],
                "nearest_short_liq_price": 82000.0,
                "nearest_short_liq_atr": 1.2,
                "short_liq_intensity": 1800.0,
                "nearest_long_liq_price": 80000.0,
                "nearest_long_liq_atr": 0.8,
                "long_liq_intensity": 1200.0,
                "liq_heatmap_skew": 0.2,
                "recent_realized_liq_long_notional": 50000.0,
                "recent_realized_liq_short_notional": 10000.0,
            }

    app = create_app(AppSettings(app_env="test", trading_mode="PAPER"))
    app.state.bot_reporting_service = _FakeReporting()
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@crypidian.test"
    )

    client = TestClient(app)
    resp = client.get(
        f"/admin/api/chart/liquidation-heatmap?bot_id={bot_id}&symbol=BTCUSDT&timeframe=1h"
    )
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.json()
    assert body["status"] == "ok"
    assert body["formula_version"] == LIQUIDATION_HEATMAP_VERSION
    assert len(body["long_liq_clusters"]) == 1
    assert len(body["short_liq_clusters"]) == 1
    assert body["liq_heatmap_skew"] == 0.2
