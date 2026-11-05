"""Unit tests cho WP-SC.1: Domain Models, Candle Anatomy Primitives & Strategy Card Schema.

Bao quát:
1. Thư viện giải phẫu nến (Candle Anatomy Primitives): Hammer, Doji phẳng (H=L), Marubozu.
2. Kiểm tra tính bất biến của Domain Models (Frozen Dataclasses).
3. Thẩm định đồ thị DAG không chu trình (Acyclic check) và phát hiện chu trình (Cyclic dependency).
4. Tính tất định của mã băm chuẩn hóa (Canonical SHA-256 Content Hash).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.domain.strategy_card.anatomy import (
    body_ratio,
    close_location,
    lower_wick_ratio,
    upper_wick_ratio,
    wick_rejection_strength,
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
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash


# ---------------------------------------------------------------------------
# 1. Tests cho Thư viện Giải phẫu Nến (Candle Anatomy Primitives)
# ---------------------------------------------------------------------------
class TestCandleAnatomyPrimitives:
    """Kiểm tra các hàm đo đạc hình học nến nguyên tử."""

    def test_hammer_candle_geometry(self) -> None:
        """Kiểm tra mẫu nến Hammer tăng giá: Râu dưới dài, râu trên ngắn, thân nhỏ."""
        open_price = Decimal("100.0")
        high = Decimal("102.0")
        low = Decimal("90.0")
        close = Decimal("101.0")
        # Biên độ nến = 102 - 90 = 12
        # Thân nến = |101 - 100| = 1 -> body_ratio = 1 / 12
        # Râu dưới = min(100, 101) - 90 = 10 -> lower_wick_ratio = 10 / 12
        # Râu trên = 102 - max(100, 101) = 1 -> upper_wick_ratio = 1 / 12
        # close_location = (101 - 90) / 12 = 11 / 12

        lw = lower_wick_ratio(open_price, high, low, close)
        uw = upper_wick_ratio(open_price, high, low, close)
        br = body_ratio(open_price, high, low, close)
        cl = close_location(open_price, high, low, close)
        wrs = wick_rejection_strength(open_price, high, low, close)

        expected_lw = Decimal("10") / Decimal("12")
        expected_uw = Decimal("1") / Decimal("12")
        expected_br = Decimal("1") / Decimal("12")
        expected_cl = Decimal("11") / Decimal("12")
        expected_wrs = expected_lw * expected_cl * (Decimal("1") - expected_br)

        assert lw == expected_lw
        assert uw == expected_uw
        assert br == expected_br
        assert cl == expected_cl
        assert wrs == expected_wrs
        assert lw > Decimal("0.8")
        assert wrs > Decimal("0.6")

    def test_flat_doji_candle_zero_division_guard(self) -> None:
        """Kiểm tra nến Doji phẳng (H == L): Phải chống lỗi chia cho 0 an toàn."""
        open_price = Decimal("100.0")
        high = Decimal("100.0")
        low = Decimal("100.0")
        close = Decimal("100.0")

        # Khi High == Low: các tỷ lệ wick/body trả về 0, close_location trả về 0.5 (trung lập)
        lw = lower_wick_ratio(open_price, high, low, close)
        uw = upper_wick_ratio(open_price, high, low, close)
        br = body_ratio(open_price, high, low, close)
        cl = close_location(open_price, high, low, close)
        wrs = wick_rejection_strength(open_price, high, low, close)

        assert lw == Decimal("0")
        assert uw == Decimal("0")
        assert br == Decimal("0")
        assert cl == Decimal("0.5")
        assert wrs == Decimal("0")

    def test_bullish_marubozu_candle(self) -> None:
        """Kiểm tra nến Bullish Marubozu: Thân chiếm 100% biên độ, không có râu."""
        open_price = Decimal("100.0")
        high = Decimal("110.0")
        low = Decimal("100.0")
        close = Decimal("110.0")

        lw = lower_wick_ratio(open_price, high, low, close)
        uw = upper_wick_ratio(open_price, high, low, close)
        br = body_ratio(open_price, high, low, close)
        cl = close_location(open_price, high, low, close)
        wrs = wick_rejection_strength(open_price, high, low, close)

        assert lw == Decimal("0")
        assert uw == Decimal("0")
        assert br == Decimal("1")
        assert cl == Decimal("1")
        assert wrs == Decimal("0")  # (1 - body_ratio) = 0 nên wrs = 0

    def test_shooting_star_candle(self) -> None:
        """Kiểm tra nến Shooting Star: Râu trên dài, râu dưới rất ngắn, đóng gần đáy."""
        open_price = Decimal("100.0")
        high = Decimal("115.0")
        low = Decimal("99.0")
        close = Decimal("101.0")
        # Biên độ = 115 - 99 = 16
        # Thân = |101 - 100| = 1 -> body_ratio = 1 / 16
        # Râu trên = 115 - max(100, 101) = 14 -> upper_wick_ratio = 14 / 16
        # Râu dưới = min(100, 101) - 99 = 1 -> lower_wick_ratio = 1 / 16
        # close_location = (101 - 99) / 16 = 2 / 16

        lw = lower_wick_ratio(open_price, high, low, close)
        uw = upper_wick_ratio(open_price, high, low, close)
        br = body_ratio(open_price, high, low, close)
        cl = close_location(open_price, high, low, close)

        assert uw == Decimal("14") / Decimal("16")
        assert lw == Decimal("1") / Decimal("16")
        assert br == Decimal("1") / Decimal("16")
        assert cl == Decimal("2") / Decimal("16")
        assert uw > Decimal("0.8")

    def test_dragonfly_and_gravestone_doji(self) -> None:
        """Kiểm tra Dragonfly Doji (mở=đóng=đỉnh) và Gravestone Doji (mở=đóng=đáy)."""
        # Dragonfly Doji
        df_open = Decimal("100.0")
        df_high = Decimal("100.0")
        df_low = Decimal("90.0")
        df_close = Decimal("100.0")
        assert lower_wick_ratio(df_open, df_high, df_low, df_close) == Decimal("1")
        assert upper_wick_ratio(df_open, df_high, df_low, df_close) == Decimal("0")
        assert body_ratio(df_open, df_high, df_low, df_close) == Decimal("0")
        assert close_location(df_open, df_high, df_low, df_close) == Decimal("1")

        # Gravestone Doji
        gs_open = Decimal("90.0")
        gs_high = Decimal("100.0")
        gs_low = Decimal("90.0")
        gs_close = Decimal("90.0")
        assert lower_wick_ratio(gs_open, gs_high, gs_low, gs_close) == Decimal("0")
        assert upper_wick_ratio(gs_open, gs_high, gs_low, gs_close) == Decimal("1")
        assert body_ratio(gs_open, gs_high, gs_low, gs_close) == Decimal("0")
        assert close_location(gs_open, gs_high, gs_low, gs_close) == Decimal("0")


# ---------------------------------------------------------------------------
# 2. Tests cho Domain Models (Frozen Dataclasses)
# ---------------------------------------------------------------------------
class TestStrategyCardDomainModels:
    """Kiểm tra tính bất biến và khởi tạo của các cấu trúc miền."""

    def test_models_immutability(self) -> None:
        """Kiểm tra các frozen dataclass không thể bị sửa đổi sau khi tạo."""
        port = TypedPort(name="in_close", data_type=DataType.PRICE_SERIES)
        with pytest.raises(FrozenInstanceError):
            port.name = "new_name"  # type: ignore[misc]

        node = GraphNode(
            node_id="node_l1",
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="wick_rejection_strength",
            parameters={"threshold": 0.6},
            inputs=(port,),
            outputs=(TypedPort(name="out_signal", data_type=DataType.NUMERIC_SCALAR),),
        )
        with pytest.raises(FrozenInstanceError):
            node.operation = "other_op"  # type: ignore[misc]

        edge = GraphEdge(
            from_node="node_l1",
            from_port="out_signal",
            to_node="node_l4",
            to_port="in_signal",
        )
        with pytest.raises(FrozenInstanceError):
            edge.from_node = "modified"  # type: ignore[misc]

        bound = ParameterBound(
            name="lookback",
            min_value=10.0,
            max_value=100.0,
            step=1.0,
            default_value=20.0,
        )
        with pytest.raises(FrozenInstanceError):
            bound.step = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fixtures dùng chung
# ---------------------------------------------------------------------------
@pytest.fixture
def valid_sample_card() -> StrategyCard:
    """Tạo StrategyCard mẫu hợp lệ với đồ thị DAG không chu trình."""
    port_series = TypedPort(name="price", data_type=DataType.PRICE_SERIES)
    port_scalar = TypedPort(name="val", data_type=DataType.NUMERIC_SCALAR)
    port_flag = TypedPort(name="flag", data_type=DataType.BOOLEAN_FLAG)

    node_l0 = GraphNode(
        node_id="l0_cleaner",
        layer=CompositionLayer.L0_DATA_HYGIENE,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="sanitize_ohlcv",
        parameters={},
        inputs=(),
        outputs=(port_series,),
    )
    node_l1 = GraphNode(
        node_id="l1_wick",
        layer=CompositionLayer.L1_CANDLE_ANATOMY,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="wick_rejection_strength",
        parameters={},
        inputs=(port_series,),
        outputs=(port_scalar,),
    )
    node_l4 = GraphNode(
        node_id="l4_regime",
        layer=CompositionLayer.L4_CONTEXT_GATE,
        role=NodeExecutionRole.GATE_FILTER,
        operation="volatility_regime_gate",
        parameters={"min_atr": 10.0},
        inputs=(port_series,),
        outputs=(port_flag,),
    )
    node_l5 = GraphNode(
        node_id="l5_trigger",
        layer=CompositionLayer.L5_STRATEGY_TRIGGER,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="rejection_breakout_trigger",
        parameters={"threshold": 0.7},
        inputs=(port_scalar, port_flag),
        outputs=(port_flag,),
    )
    node_l6 = GraphNode(
        node_id="l6_zones",
        layer=CompositionLayer.L6_ZONES,
        role=NodeExecutionRole.TERMINAL_ZONE,
        operation="atr_zone_calculator",
        parameters={"atr_multiplier": 2.0},
        inputs=(port_flag,),
        outputs=(),
    )

    edges = [
        GraphEdge("l0_cleaner", "price", "l1_wick", "price"),
        GraphEdge("l0_cleaner", "price", "l4_regime", "price"),
        GraphEdge("l1_wick", "val", "l5_trigger", "val"),
        GraphEdge("l4_regime", "flag", "l5_trigger", "flag"),
        GraphEdge("l5_trigger", "flag", "l6_zones", "flag"),
    ]

    return StrategyCard(
        card_id="SC-BINANCE-USDM-VOL-BREAKOUT-001",
        name="Volume-Confirmed Wick Rejection Breakout",
        version="1.0.0",
        author="Crypidian Quant Lab",
        created_at=datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[node_l0, node_l1, node_l4, node_l5, node_l6],
        edges=edges,
        parameters=[
            ParameterBound(
                name="atr_multiplier",
                min_value=1.0,
                max_value=5.0,
                step=0.1,
                default_value=2.0,
            )
        ],
        compatibility=CompatibilityMap(
            target_venues=("BINANCE_USDM",),
            target_symbols=("BTCUSDT", "ETHUSDT"),
            timeframes=("15m", "1h"),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.5"),
        ),
    )


# ---------------------------------------------------------------------------
# 3. Tests cho StrategyCard Schema & DAG Validation
# ---------------------------------------------------------------------------
class TestStrategyCardSchemaAndDAG:
    """Kiểm tra thẩm định đồ thị và schema Pydantic v2."""

    def test_valid_dag_card_validation(self, valid_sample_card: StrategyCard) -> None:
        """Đồ thị hợp lệ không có chu trình phải qua validation thành công."""
        assert valid_sample_card.card_id == "SC-BINANCE-USDM-VOL-BREAKOUT-001"
        assert len(valid_sample_card.nodes) == 5
        assert len(valid_sample_card.edges) == 5

    def test_dag_cycle_detection_rejects_cyclic_graph(
        self, valid_sample_card: StrategyCard
    ) -> None:
        """Đồ thị có chu trình (l5_trigger -> l1_wick) phải bị từ chối bằng ValidationError."""
        cyclic_edges = list(valid_sample_card.edges) + [
            # Tạo chu trình: l5_trigger quay ngược lại l1_wick
            GraphEdge("l5_trigger", "flag", "l1_wick", "price")
        ]

        card_dict = valid_sample_card.model_dump()
        card_dict["edges"] = cyclic_edges

        with pytest.raises(ValidationError) as exc_info:
            StrategyCard.model_validate(card_dict)

        assert "chu trình" in str(exc_info.value).lower() or "cycle" in str(exc_info.value).lower()

    def test_dag_rejects_self_loop(self, valid_sample_card: StrategyCard) -> None:
        """Cạnh tự trỏ vào chính mình (Self-loop) phải bị từ chối."""
        self_loop_edges = list(valid_sample_card.edges) + [
            GraphEdge("l1_wick", "val", "l1_wick", "price")
        ]
        card_dict = valid_sample_card.model_dump()
        card_dict["edges"] = self_loop_edges

        with pytest.raises(ValidationError) as exc_info:
            StrategyCard.model_validate(card_dict)

        assert "chu trình" in str(exc_info.value).lower() or "cycle" in str(exc_info.value).lower()

    def test_dag_rejects_missing_node_reference(self, valid_sample_card: StrategyCard) -> None:
        """Cạnh tham chiếu đến node không tồn tại trong danh sách nodes phải bị từ chối."""
        invalid_edge = GraphEdge("non_existent_node", "out", "l1_wick", "price")
        card_dict = valid_sample_card.model_dump()
        card_dict["edges"] = list(valid_sample_card.edges) + [invalid_edge]

        with pytest.raises(ValidationError) as exc_info:
            StrategyCard.model_validate(card_dict)

        assert (
            "không tồn tại" in str(exc_info.value).lower()
            or "not found" in str(exc_info.value).lower()
        )

    def test_dag_rejects_duplicate_node_ids(self, valid_sample_card: StrategyCard) -> None:
        """Danh sách nodes chứa duplicate node_id phải bị từ chối."""
        dup_node = GraphNode(
            node_id="l1_wick",  # Trùng node_id
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="dup_op",
            parameters={},
            inputs=(),
            outputs=(),
        )
        card_dict = valid_sample_card.model_dump()
        card_dict["nodes"] = list(valid_sample_card.nodes) + [dup_node]

        with pytest.raises(ValidationError) as exc_info:
            StrategyCard.model_validate(card_dict)

        assert "trùng" in str(exc_info.value).lower() or "duplicate" in str(exc_info.value).lower()

    def test_dag_diamond_structure_allowed(self) -> None:
        """Đồ thị phân nhánh/hội tụ kim cương (Diamond: A->B, A->C, B->D, C->D) là DAG hợp lệ."""
        node_a = GraphNode(
            node_id="A",
            layer=CompositionLayer.L0_DATA_HYGIENE,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="source",
        )
        node_b = GraphNode(
            node_id="B",
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="branch_1",
        )
        node_c = GraphNode(
            node_id="C",
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="branch_2",
        )
        node_d = GraphNode(
            node_id="D",
            layer=CompositionLayer.L6_ZONES,
            role=NodeExecutionRole.TERMINAL_ZONE,
            operation="sink",
        )

        edges = [
            GraphEdge("A", "out", "B", "in"),
            GraphEdge("A", "out", "C", "in"),
            GraphEdge("B", "out", "D", "in1"),
            GraphEdge("C", "out", "D", "in2"),
        ]

        card = StrategyCard(
            card_id="SC-DIAMOND-TEST",
            name="Diamond DAG Test",
            version="1.0.0",
            author="Quant Test",
            created_at=datetime(2026, 11, 5, tzinfo=UTC),
            layers=[
                CompositionLayer.L0_DATA_HYGIENE,
                CompositionLayer.L1_CANDLE_ANATOMY,
                CompositionLayer.L6_ZONES,
            ],
            nodes=[node_a, node_b, node_c, node_d],
            edges=edges,
            execution_profile=ExecutionProfile(
                assumed_taker_fee=Decimal("0.0005"),
                assumed_slippage_bps=Decimal("1.0"),
            ),
        )

        assert len(card.nodes) == 4
        assert len(card.edges) == 4

    def test_dag_indirect_long_cycle_rejected(self) -> None:
        """Chu trình gián tiếp dài hơn: A -> B -> C -> D -> B phải bị phát hiện và từ chối."""
        node_a = GraphNode(
            node_id="A",
            layer=CompositionLayer.L0_DATA_HYGIENE,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="op_a",
        )
        node_b = GraphNode(
            node_id="B",
            layer=CompositionLayer.L1_CANDLE_ANATOMY,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="op_b",
        )
        node_c = GraphNode(
            node_id="C",
            layer=CompositionLayer.L2_INDICATORS,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="op_c",
        )
        node_d = GraphNode(
            node_id="D",
            layer=CompositionLayer.L3_ALGORITHMS,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="op_d",
        )

        edges = [
            GraphEdge("A", "out", "B", "in"),
            GraphEdge("B", "out", "C", "in"),
            GraphEdge("C", "out", "D", "in"),
            GraphEdge("D", "out", "B", "in"),  # Tạo chu trình: B -> C -> D -> B
        ]

        with pytest.raises(ValidationError) as exc_info:
            StrategyCard(
                card_id="SC-INDIRECT-CYCLE",
                name="Indirect Cycle Test",
                version="1.0.0",
                author="Quant Test",
                created_at=datetime(2026, 11, 5, tzinfo=UTC),
                layers=[
                    CompositionLayer.L0_DATA_HYGIENE,
                    CompositionLayer.L1_CANDLE_ANATOMY,
                    CompositionLayer.L2_INDICATORS,
                    CompositionLayer.L3_ALGORITHMS,
                ],
                nodes=[node_a, node_b, node_c, node_d],
                edges=edges,
                execution_profile=ExecutionProfile(
                    assumed_taker_fee=Decimal("0.0005"),
                    assumed_slippage_bps=Decimal("1.0"),
                ),
            )

        assert "chu trình" in str(exc_info.value).lower() or "cycle" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 4. Tests cho Tính Tất Định của Canonical SHA-256 Hash
# ---------------------------------------------------------------------------
class TestCanonicalHash:
    """Kiểm tra tính tất định và bất biến thứ tự của mã băm SHA-256."""

    def test_canonical_hash_property_equals_function(self, valid_sample_card: StrategyCard) -> None:
        """Property card.canonical_hash phải trùng khớp với compute_canonical_hash(card)."""
        assert valid_sample_card.canonical_hash == compute_canonical_hash(valid_sample_card)

    def test_canonical_hash_deterministic_and_order_invariant(
        self, valid_sample_card: StrategyCard
    ) -> None:
        """Hai card cùng cấu trúc nhưng đảo thứ tự nodes/edges/parameters phải cùng SHA-256."""
        # Tạo card thứ hai đảo ngược thứ tự danh sách nodes, edges, parameters, layers
        reversed_card = StrategyCard(
            card_id=valid_sample_card.card_id,
            name=valid_sample_card.name,
            version=valid_sample_card.version,
            author=valid_sample_card.author,
            created_at=valid_sample_card.created_at,
            layers=list(reversed(valid_sample_card.layers)),
            nodes=list(reversed(valid_sample_card.nodes)),
            edges=list(reversed(valid_sample_card.edges)),
            parameters=list(reversed(valid_sample_card.parameters)),
            compatibility=valid_sample_card.compatibility,
            execution_profile=valid_sample_card.execution_profile,
        )

        hash1 = compute_canonical_hash(valid_sample_card)
        hash2 = compute_canonical_hash(reversed_card)

        # Hash phải là chuỗi hex SHA-256 64 ký tự
        assert len(hash1) == 64
        assert len(hash2) == 64
        assert all(c in "0123456789abcdef" for c in hash1)

        # Bất biến thứ tự: Dù thứ tự node/edge bị đảo, hash vẫn giống hệt nhau
        assert hash1 == hash2

    def test_canonical_hash_sensitivity_to_changes(self, valid_sample_card: StrategyCard) -> None:
        """Bất kỳ sự thay đổi nhỏ nào trong card (ví dụ phí taker fee) đều phải sinh ra hash mới."""
        modified_card = StrategyCard(
            card_id=valid_sample_card.card_id,
            name=valid_sample_card.name,
            version=valid_sample_card.version,
            author=valid_sample_card.author,
            created_at=valid_sample_card.created_at,
            layers=valid_sample_card.layers,
            nodes=valid_sample_card.nodes,
            edges=valid_sample_card.edges,
            parameters=valid_sample_card.parameters,
            compatibility=valid_sample_card.compatibility,
            execution_profile=ExecutionProfile(
                assumed_taker_fee=Decimal("0.0006"),  # Đổi từ 0.0005 thành 0.0006
                assumed_slippage_bps=Decimal("2.5"),
            ),
        )

        hash1 = compute_canonical_hash(valid_sample_card)
        hash_modified = compute_canonical_hash(modified_card)

        assert hash1 != hash_modified
