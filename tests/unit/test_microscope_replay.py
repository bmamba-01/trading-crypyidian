"""Unit tests TDD cho WP-SC.6: Kính hiển vi soi vết lịch sử (Microscope Replay Workspace).

Kiểm thử:
1. inspect_bar với nến hợp lệ (Mẫu 1 Hammer -> L4 PASS, L5 LONG, L6 Zones đầy đủ).
2. inspect_bar với nến biến động thấp (Mẫu 2 -> L4 BLOCK do ATR < 15, Ngắt mạch tức thì).
3. inspect_bar với nến Shooting Star (Mẫu 3 -> L4 PASS, L5 SHORT, L6 Zones cấu trúc hình học Short).
4. inspect_bar với nến hỏng dữ liệu L0 (High < Low -> ngắt mạch ngay tại L0, L1-L6 SKIPPED).
5. Tính toàn vẹn của DTO LayerTrace và MicroscopeTrace (to_dict, serialization, hash).
6. Presets mẫu nến điển hình (get_presets).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.application.strategy_pipeline.microscope import (
    MicroscopeReplayService,
    MicroscopeTrace,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings
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


@pytest.fixture
def test_strategy_card() -> StrategyCard:
    """Tạo StrategyCard đầy đủ 7 tầng cho kiểm thử Microscope."""
    port_series = TypedPort(name="ohlcv_in", data_type=DataType.PRICE_SERIES)
    port_scalar = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
    port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)

    node_l0 = GraphNode(
        node_id="l0_hygiene",
        layer=CompositionLayer.L0_DATA_HYGIENE,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="data_hygiene_check",
        inputs=(),
        outputs=(port_series,),
    )
    node_l1_lower = GraphNode(
        node_id="l1_wick",
        layer=CompositionLayer.L1_CANDLE_ANATOMY,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="lower_wick_ratio",
        inputs=(port_series,),
        outputs=(port_scalar,),
    )
    node_l1_upper = GraphNode(
        node_id="l1_upper_wick",
        layer=CompositionLayer.L1_CANDLE_ANATOMY,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="upper_wick_ratio",
        inputs=(port_series,),
        outputs=(port_scalar,),
    )
    node_l2_atr = GraphNode(
        node_id="l2_atr",
        layer=CompositionLayer.L2_INDICATORS,
        role=NodeExecutionRole.STATEFUL_WINDOW,
        operation="average_true_range",
        parameters={"period": 14},
        inputs=(port_series,),
        outputs=(port_scalar,),
    )
    node_l3_algo = GraphNode(
        node_id="l3_score",
        layer=CompositionLayer.L3_ALGORITHMS,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="score_normalizer",
        inputs=(port_scalar,),
        outputs=(port_scalar,),
    )
    node_l4_gate = GraphNode(
        node_id="l4_gate",
        layer=CompositionLayer.L4_CONTEXT_GATE,
        role=NodeExecutionRole.GATE_FILTER,
        operation="volatility_regime_gate",
        parameters={"min_atr": 5.0, "max_atr": 100.0},
        inputs=(port_series,),
        outputs=(port_flag,),
    )
    node_l5_trigger = GraphNode(
        node_id="l5_trigger",
        layer=CompositionLayer.L5_STRATEGY_TRIGGER,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="rejection_breakout_trigger",
        parameters={"threshold": 0.5, "direction": "LONG"},
        inputs=(port_scalar, port_flag),
        outputs=(port_flag,),
    )
    node_l6_zones = GraphNode(
        node_id="l6_zones",
        layer=CompositionLayer.L6_ZONES,
        role=NodeExecutionRole.TERMINAL_ZONE,
        operation="execution_zone_builder",
        parameters={"sl_multiplier": 2.0, "rr_ratio": 2.0},
        inputs=(port_flag,),
        outputs=(),
    )

    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_wick", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_upper_wick", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l2_atr", "val", "l3_score", "val"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l4_gate", "ohlcv_in"),
        GraphEdge("l1_wick", "val", "l5_trigger", "val"),
        GraphEdge("l4_gate", "flag", "l5_trigger", "flag"),
        GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
    ]

    return StrategyCard(
        card_id="SC-TEST-MICROSCOPE-001",
        name="Microscope Replay Test Strategy Card",
        version="1.0.0",
        author="Crypidian Core Quant Team",
        created_at=datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L2_INDICATORS,
            CompositionLayer.L3_ALGORITHMS,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            node_l0,
            node_l1_lower,
            node_l1_upper,
            node_l2_atr,
            node_l3_algo,
            node_l4_gate,
            node_l5_trigger,
            node_l6_zones,
        ],
        edges=edges,
        parameters=[
            ParameterBound("min_atr", 1.0, 50.0, 1.0, 5.0),
        ],
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


class TestMicroscopeReplayService:
    """Bộ kiểm thử cho dịch vụ MicroscopeReplayService."""

    def test_preset_candles_list(self) -> None:
        """Kiểm tra danh sách các mẫu nến điển hình có sẵn (Presets)."""
        service = MicroscopeReplayService()
        presets = service.get_presets()

        assert len(presets) >= 3
        preset_ids = [p.id for p in presets]
        assert "preset-hammer-long" in preset_ids
        assert "preset-low-vol-block" in preset_ids
        assert "preset-shooting-star-short" in preset_ids

        # Kiểm tra nội dung Mẫu 1
        p1 = next(p for p in presets if p.id == "preset-hammer-long")
        assert "Hammer" in p1.title
        assert p1.ohlcv["high"] > p1.ohlcv["low"]

        # Kiểm tra nội dung Mẫu 2
        p2 = next(p for p in presets if p.id == "preset-low-vol-block")
        assert "biến động thấp" in p2.title or "ATR < 15" in p2.title

        # Kiểm tra nội dung Mẫu 3
        p3 = next(p for p in presets if p.id == "preset-shooting-star-short")
        assert "Shooting Star" in p3.title

    def test_inspect_bar_preset_hammer_success(self, test_strategy_card: StrategyCard) -> None:
        """Kiểm thử Mẫu 1: Nến Hammer -> L4 PASS, L5 LONG, L6 sinh ExecutionZones đầy đủ."""
        service = MicroscopeReplayService()
        p1 = next(p for p in service.get_presets() if p.id == "preset-hammer-long")

        trace = service.inspect_bar(
            card=test_strategy_card,
            ohlcv=p1.ohlcv,
            context=p1.context,
        )

        assert isinstance(trace, MicroscopeTrace)
        assert trace.card_id == test_strategy_card.card_id
        assert not trace.is_short_circuited
        assert trace.short_circuit_reason is None
        assert trace.context_gate_passed is True
        assert trace.trigger_signal == "LONG_ENTRY"
        assert trace.candidate_id is not None
        assert len(trace.candidate_id) == 64  # SHA-256

        # Kiểm tra Execution Zones
        assert trace.zones is not None
        assert trace.zones["entry_price"] == Decimal("101.0")
        assert trace.zones["stop_loss"] < trace.zones["entry_price"]
        assert trace.zones["take_profit"] > trace.zones["entry_price"]
        assert trace.zones["risk_reward_ratio"] == Decimal("2.0")

        # Kiểm tra dấu vết 7 tầng
        layers_in_trace = {t.layer for t in trace.layer_traces}
        assert CompositionLayer.L0_DATA_HYGIENE in layers_in_trace
        assert CompositionLayer.L1_CANDLE_ANATOMY in layers_in_trace
        assert CompositionLayer.L2_INDICATORS in layers_in_trace
        assert CompositionLayer.L4_CONTEXT_GATE in layers_in_trace
        assert CompositionLayer.L5_STRATEGY_TRIGGER in layers_in_trace
        assert CompositionLayer.L6_ZONES in layers_in_trace

        # Xác nhận tất cả các tầng đều SUCCESS / PASS / TRIGGERED
        statuses = {t.status for t in trace.layer_traces}
        assert "FAILED" not in statuses
        assert "BLOCK" not in statuses
        assert "SKIPPED" not in statuses

    def test_inspect_bar_preset_low_volatility_blocks_pipeline(
        self, test_strategy_card: StrategyCard
    ) -> None:
        """Kiểm thử Mẫu 2: Nến biến động thấp -> L4 BLOCK do ATR < 15, Ngắt mạch tức thì."""
        service = MicroscopeReplayService()
        p2 = next(p for p in service.get_presets() if p.id == "preset-low-vol-block")

        trace = service.inspect_bar(
            card=test_strategy_card,
            ohlcv=p2.ohlcv,
            context=p2.context,
        )

        assert trace.is_short_circuited is True
        assert trace.short_circuit_reason is not None
        assert "L4" in trace.short_circuit_reason or "ATR" in trace.short_circuit_reason
        assert trace.context_gate_passed is False
        assert trace.trigger_signal in {"NO_SIGNAL", "NONE"}
        assert trace.zones is None
        assert trace.candidate_id is None

        # Tầng L4 phải có trạng thái BLOCK
        l4_traces = [t for t in trace.layer_traces if t.layer == CompositionLayer.L4_CONTEXT_GATE]
        assert any(t.status == "BLOCK" for t in l4_traces)

        # BẢO ĐẢM SHORT-CIRCUIT: Tầng L5 và L6 phải là SKIPPED
        l5_traces = [
            t for t in trace.layer_traces if t.layer == CompositionLayer.L5_STRATEGY_TRIGGER
        ]
        l6_traces = [t for t in trace.layer_traces if t.layer == CompositionLayer.L6_ZONES]
        assert all(t.status == "SKIPPED" for t in l5_traces)
        assert all(t.status == "SKIPPED" for t in l6_traces)

    def test_inspect_bar_preset_shooting_star_short(self, test_strategy_card: StrategyCard) -> None:
        """Kiểm thử Mẫu 3: Nến Shooting Star -> L4 PASS, L5 SHORT, L6 Zones hợp lệ."""
        service = MicroscopeReplayService()
        p3 = next(p for p in service.get_presets() if p.id == "preset-shooting-star-short")

        trace = service.inspect_bar(
            card=test_strategy_card,
            ohlcv=p3.ohlcv,
            context=p3.context,
        )

        assert not trace.is_short_circuited
        assert trace.context_gate_passed is True
        assert trace.trigger_signal == "SHORT_ENTRY"
        assert trace.zones is not None
        assert trace.candidate_id is not None

        # Với SHORT: Stop Loss phải nằm TRÊN Entry, Take Profit phải nằm DƯỚI Entry
        entry = trace.zones["entry_price"]
        sl = trace.zones["stop_loss"]
        tp = trace.zones["take_profit"]
        assert sl > entry
        assert tp < entry

    def test_inspect_bar_corrupted_candle_l0_short_circuit(
        self, test_strategy_card: StrategyCard
    ) -> None:
        """Kiểm thử nến hỏng L0 (High < Low) -> Ngắt mạch ngay tại L0, L1->L6 SKIPPED."""
        service = MicroscopeReplayService()
        corrupted_ohlcv = {
            "open": Decimal("100.0"),
            "high": Decimal("90.0"),  # Hỏng: High < Low
            "low": Decimal("105.0"),
            "close": Decimal("95.0"),
            "volume": Decimal("50.0"),
        }

        trace = service.inspect_bar(
            card=test_strategy_card,
            ohlcv=corrupted_ohlcv,
        )

        assert trace.is_short_circuited is True
        assert trace.short_circuit_reason is not None
        assert "L0" in trace.short_circuit_reason or "High < Low" in trace.short_circuit_reason
        assert trace.context_gate_passed is False
        assert trace.trigger_signal == "NONE"
        assert trace.zones is None
        assert trace.candidate_id is None

        # Tầng L0 phải báo FAILED
        l0_traces = [t for t in trace.layer_traces if t.layer == CompositionLayer.L0_DATA_HYGIENE]
        assert any(t.status == "FAILED" for t in l0_traces)

        # Tất cả các tầng từ L1 đến L6 đều phải là SKIPPED
        downstream_traces = [
            t for t in trace.layer_traces if t.layer != CompositionLayer.L0_DATA_HYGIENE
        ]
        assert all(t.status == "SKIPPED" for t in downstream_traces)

    def test_trace_to_dict_serialization(self, test_strategy_card: StrategyCard) -> None:
        """Kiểm tra hàm to_dict của MicroscopeTrace trả về JSON-serializable dict an toàn."""
        service = MicroscopeReplayService()
        p1 = next(p for p in service.get_presets() if p.id == "preset-hammer-long")
        trace = service.inspect_bar(
            card=test_strategy_card,
            ohlcv=p1.ohlcv,
            context=p1.context,
        )

        data = trace.to_dict()
        assert isinstance(data, dict)
        assert data["card_id"] == test_strategy_card.card_id
        assert data["is_short_circuited"] is False
        assert data["context_gate_passed"] is True
        assert data["trigger_signal"] == "LONG_ENTRY"
        assert isinstance(data["layer_traces"], list)
        assert len(data["layer_traces"]) > 0
        assert "entry_price" in data["zones"]


# ---------------------------------------------------------------------------
# Test Web Router & REST API Endpoints
# ---------------------------------------------------------------------------
WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


class _MockUnauthService:
    async def authenticate(self, _cookie: str | None) -> AuthIdentity:
        raise AuthenticationFailure(401, "Yêu cầu đăng nhập.")


def _create_authenticated_client() -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=WORKSPACE_ID,
        membership_id=MEMBERSHIP_ID,
        user_id=USER_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="admin@crypidian.test"
    )
    return TestClient(app)


def _create_unauthenticated_client() -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    app.state.auth_service = _MockUnauthService()
    return TestClient(app)


class TestMicroscopeWebRouter:
    """Kiểm thử router giao diện HTML và REST API của Microscope Replay."""

    def test_microscope_page_requires_auth(self) -> None:
        """Truy cập trang microscope khi chưa xác thực trả về 401."""
        client = _create_unauthenticated_client()
        res = client.get(
            "/admin/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope",
            follow_redirects=False,
        )
        assert res.status_code == 401

    def test_microscope_page_not_found(self) -> None:
        """Truy cập mã card không tồn tại trả về 404."""
        client = _create_authenticated_client()
        res = client.get("/admin/strategy-cards/NON-EXISTENT-CARD-999/microscope")
        assert res.status_code == 404
        assert res.headers["content-type"].startswith("text/html")
        assert "Không tìm thấy nội dung trong phạm vi được cấp quyền." in res.text
        assert "NON-EXISTENT-CARD-999" not in res.text

    def test_microscope_page_renders_successfully(self) -> None:
        """Hiển thị trang Microscope Replay đầy đủ components, form và 7 tầng L0-L6."""
        client = _create_authenticated_client()
        res = client.get("/admin/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        html = res.text

        # Xác nhận tiêu đề và breadcrumb
        assert "Kính hiển vi Soi vết Lịch sử (Microscope Replay)" in html
        assert "Quay lại danh sách Chiến lược" in html

        # Xác nhận thông tin Card
        assert "SC-BINANCE-USDM-VOL-BREAKOUT-001" in html
        assert "Donchian Trend &amp; Volatility Breakout" in html or "Donchian Trend" in html

        # Xác nhận Presets
        assert "Mẫu nến Điển hình có sẵn (Presets)" in html
        assert "Mẫu 1 — Nến Hammer đảo chiều tăng" in html
        assert "Mẫu 2 — Nến biến động thấp" in html
        assert "Mẫu 3 — Nến Shooting Star" in html

        # Xác nhận 7 tầng L0->L6
        assert "Tầng L0" in html
        assert "Data Hygiene" in html
        assert "Tầng L1" in html
        assert "Candle Anatomy" in html
        assert "Tầng L2" in html
        assert "Indicators" in html
        assert "Tầng L3" in html
        assert "Algorithms" in html
        assert "Tầng L4" in html
        assert "Context Gate" in html
        assert "Tầng L5" in html
        assert "Strategy Trigger" in html
        assert "Tầng L6" in html
        assert "Execution Zones" in html

    def test_inspect_bar_api_requires_auth(self) -> None:
        """API inspect-bar yêu cầu xác thực 401."""
        client = _create_unauthenticated_client()
        res = client.post(
            "/admin/api/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/inspect-bar",
            json={"ohlcv": {"open": 100, "high": 102, "low": 90, "close": 101, "volume": 150}},
        )
        assert res.status_code == 401

    def test_inspect_bar_api_success(self) -> None:
        """API inspect-bar trả về JSON MicroscopeTrace hợp lệ."""
        client = _create_authenticated_client()
        res = client.post(
            "/admin/api/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/inspect-bar",
            json={
                "ohlcv": {
                    "open": 100.0,
                    "high": 102.0,
                    "low": 90.0,
                    "close": 101.0,
                    "volume": 150.0,
                },
                "context": {"direction": "LONG", "min_atr": 5.0},
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        trace = data["trace"]
        assert trace["card_id"] == "SC-BINANCE-USDM-VOL-BREAKOUT-001"
        assert trace["is_short_circuited"] is False
        assert trace["context_gate_passed"] is True
        assert trace["trigger_signal"] == "LONG_ENTRY"
        assert trace["zones"] is not None
        assert "entry_price" in trace["zones"]

    def test_strategy_cards_page_contains_microscope_button(self) -> None:
        """Trang danh sách Strategy Cards chứa nút liên kết Soi kính hiển vi."""
        client = _create_authenticated_client()
        res = client.get("/admin/strategy-cards")
        assert res.status_code == 200
        html = res.text
        assert "Soi kính hiển vi (Microscope)" in html
        assert "/admin/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope" in html
