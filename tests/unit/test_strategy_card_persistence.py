"""Unit tests cho WP-SC.4: Strategy Card Persistence & Repository.

Bao quát:
1. ORM Model StrategyCardRecord:
   - Roundtrip chuyển đổi from_domain và to_domain bảo toàn canonical hash.
   - Thẩm định và từ chối payload không hợp lệ hoặc không phải StrategyCard.
   - Phát hiện và từ chối khi payload JSONB hoặc content_hash bị can thiệp trái phép (tampered).
   - Kiểm tra đối soát chỉ mục denormalized (card_id, version).
2. StrategyCardRepository:
   - Lưu trữ thẻ mới (save_card) kèm advisory lock và RLS workspace.
   - Xử lý lưu trùng lặp idempotent khi cùng payload và hash.
   - Từ chối ghi đè thẻ đã xuất bản khi hash khác (StrategyCardConflict).
   - Truy vấn get_card, list_cards (lọc is_active), get_active_card và set_active_card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.models.strategy_card_entities import StrategyCardRecord
from trading_bot.adapters.database.repositories.strategy_card_repository import (
    StrategyCardConflict,
    StrategyCardRepository,
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

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
OTHER_WORKSPACE_ID = UUID("11111111-2222-3333-4444-555555555555")


def create_sample_card(
    card_id: str = "SC-BINANCE-USDM-VOL-BREAKOUT-001",
    version: str = "1.0.0",
    name: str = "Donchian Trend & Volatility Breakout",
) -> StrategyCard:
    """Tạo StrategyCard mẫu hợp lệ phục vụ kiểm thử persistence."""
    port_series = TypedPort("ohlcv_in", DataType.PRICE_SERIES, "Nến đầu vào")
    port_wick = TypedPort("wick_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ râu")
    port_gate = TypedPort("gate_status", DataType.BOOLEAN_FLAG, "Cờ bối cảnh")
    port_trigger = TypedPort("trigger_signal", DataType.BOOLEAN_FLAG, "Tín hiệu ngòi nổ")
    port_zone = TypedPort("candidate_out", DataType.CATEGORICAL_STATE, "Tọa độ lệnh")

    node_l0 = GraphNode(
        node_id="l0_hygiene",
        layer=CompositionLayer.L0_DATA_HYGIENE,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="check_data_causality",
        parameters={},
        inputs=(),
        outputs=(port_series,),
    )
    node_l1 = GraphNode(
        node_id="l1_wick",
        layer=CompositionLayer.L1_CANDLE_ANATOMY,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="lower_wick_ratio",
        parameters={},
        inputs=(port_series,),
        outputs=(port_wick,),
    )
    node_l4 = GraphNode(
        node_id="l4_gate",
        layer=CompositionLayer.L4_CONTEXT_GATE,
        role=NodeExecutionRole.GATE_FILTER,
        operation="volatility_regime_gate",
        parameters={"atr_threshold": 1.5},
        inputs=(port_wick,),
        outputs=(port_gate,),
    )
    node_l5 = GraphNode(
        node_id="l5_trigger",
        layer=CompositionLayer.L5_STRATEGY_TRIGGER,
        role=NodeExecutionRole.STATELESS_TRANSFORM,
        operation="breakout_trigger",
        parameters={},
        inputs=(port_gate,),
        outputs=(port_trigger,),
    )
    node_l6 = GraphNode(
        node_id="l6_zones",
        layer=CompositionLayer.L6_ZONES,
        role=NodeExecutionRole.TERMINAL_ZONE,
        operation="atr_zone_builder",
        parameters={},
        inputs=(port_trigger,),
        outputs=(port_zone,),
    )

    edges = (
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_wick", "ohlcv_in"),
        GraphEdge("l1_wick", "wick_ratio", "l4_gate", "wick_ratio"),
        GraphEdge("l4_gate", "gate_status", "l5_trigger", "gate_status"),
        GraphEdge("l5_trigger", "trigger_signal", "l6_zones", "trigger_signal"),
    )

    return StrategyCard(
        card_id=card_id,
        name=name,
        version=version,
        author="Crypidian Core Quant Team",
        created_at=NOW,
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[node_l0, node_l1, node_l4, node_l5, node_l6],
        edges=list(edges),
        parameters=[
            ParameterBound("atr_mult", 1.0, 5.0, 0.5, 2.0),
        ],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=("BTCUSDT", "ETHUSDT"),
            timeframes=("15m", "1h"),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.5"),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Tests cho ORM Model StrategyCardRecord
# ---------------------------------------------------------------------------
class TestStrategyCardRecord:
    """Kiểm tra ORM mapping, serialization, deserialization và integrity check."""

    def test_from_domain_and_to_domain_roundtrip(self) -> None:
        """Kiểm tra chuyển đổi hai chiều bảo toàn 100% dữ liệu và mã băm."""
        card = create_sample_card()
        expected_hash = compute_canonical_hash(card)

        record = StrategyCardRecord.from_domain(
            workspace_id=WORKSPACE_ID,
            card=card,
            is_active=True,
        )

        assert record.workspace_id == WORKSPACE_ID
        assert record.card_id == card.card_id
        assert record.name == card.name
        assert record.version == card.version
        assert record.content_hash == expected_hash
        assert record.is_active is True
        assert isinstance(record.card_payload, dict)
        assert record.card_payload["card_id"] == card.card_id

        # Tái tạo lại domain
        restored = record.to_domain()
        assert restored.card_id == card.card_id
        assert restored.name == card.name
        assert restored.version == card.version
        assert len(restored.nodes) == len(card.nodes)
        assert len(restored.edges) == len(card.edges)
        assert compute_canonical_hash(restored) == expected_hash

    def test_from_domain_rejects_invalid_types(self) -> None:
        """Từ chối khi truyền tham số sai kiểu dữ liệu."""
        card = create_sample_card()
        with pytest.raises(ValueError, match="StrategyCard"):
            StrategyCardRecord.from_domain(workspace_id=WORKSPACE_ID, card=cast(Any, {}))

        with pytest.raises(ValueError, match="UUID"):
            StrategyCardRecord.from_domain(workspace_id=cast(Any, "not-a-uuid"), card=card)

    def test_to_domain_detects_tampered_content_hash(self) -> None:
        """Phát hiện mã băm lưu trữ bị can thiệp trái phép không khớp với payload."""
        card = create_sample_card()
        record = StrategyCardRecord.from_domain(WORKSPACE_ID, card)
        # Giả lập can thiệp sửa content_hash trong cơ sở dữ liệu
        record.content_hash = "f" * 64

        with pytest.raises(ValueError, match="sai lệch hoặc đã bị can thiệp trái phép"):
            record.to_domain()

    def test_to_domain_detects_tampered_payload_content(self) -> None:
        """Phát hiện nội dung JSONB bị thay đổi sau khi lưu."""
        card = create_sample_card()
        record = StrategyCardRecord.from_domain(WORKSPACE_ID, card)
        # Giả lập sửa đổi tên hoặc logic trong payload
        tampered_payload = dict(record.card_payload)
        tampered_payload["name"] = "Sửa đổi trái phép"
        record.card_payload = tampered_payload

        with pytest.raises(ValueError, match="sai lệch hoặc đã bị can thiệp trái phép"):
            record.to_domain()

    def test_to_domain_detects_denormalized_field_mismatch(self) -> None:
        """Phát hiện cột chỉ mục ngoài bảng (card_id, version) bị lệch so với JSONB payload."""
        card = create_sample_card()
        record = StrategyCardRecord.from_domain(WORKSPACE_ID, card)
        record.card_id = "SC-DIFFERENT-ID"

        with pytest.raises(ValueError, match="card_id không khớp"):
            record.to_domain()

        record2 = StrategyCardRecord.from_domain(WORKSPACE_ID, card)
        record2.version = "9.9.9"

        with pytest.raises(ValueError, match="version không khớp"):
            record2.to_domain()


# ---------------------------------------------------------------------------
# 2. Fake Database Session & Tests cho StrategyCardRepository
# ---------------------------------------------------------------------------
class FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class FakeSession:
    """Mock AsyncSession tối giản cho phép kiểm tra câu lệnh SQL, transaction lock và RLS."""

    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        rows_results: list[list[object]] | None = None,
    ) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.rows_results = iter(rows_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []

    async def execute(self, statement: object, params: object | None = None) -> object:
        del params
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace()

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.scalar_results, None)

    async def scalars(self, statement: object) -> FakeResult:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return FakeResult(next(self.rows_results, []))

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None


class TestStrategyCardRepository:
    """Kiểm tra tầng Repository quản lý Strategy Card."""

    @pytest.mark.asyncio
    async def test_save_card_success(self) -> None:
        """Lưu thẻ chiến lược mới với advisory lock và bind workspace."""
        session = FakeSession(
            scalar_results=[None, None]  # Không trùng hash, không trùng (card_id, version)
        )
        repo = StrategyCardRepository(session)  # type: ignore[arg-type]
        card = create_sample_card()

        record = await repo.save_card(
            workspace_id=WORKSPACE_ID,
            card=card,
            is_active=False,
        )

        assert record.card_id == card.card_id
        assert record.version == card.version
        assert record.workspace_id == WORKSPACE_ID
        assert len(session.added) == 1
        assert session.added[0] is record

        # Xác minh đã gọi advisory lock và RLS workspace
        joined_statements = " ".join(session.statements)
        assert "set_config('app.workspace_id'" in joined_statements
        assert "pg_advisory_xact_lock" in joined_statements

    @pytest.mark.asyncio
    async def test_save_card_idempotent_duplicate(self) -> None:
        """Ghi lại cùng một thẻ đã tồn tại trả về bản ghi cũ mà không add mới."""
        card = create_sample_card()
        existing_record = StrategyCardRecord.from_domain(WORKSPACE_ID, card)
        session = FakeSession(scalar_results=[existing_record])
        repo = StrategyCardRepository(session)  # type: ignore[arg-type]

        result = await repo.save_card(workspace_id=WORKSPACE_ID, card=card)

        assert result is existing_record
        assert len(session.added) == 0  # Không add bản ghi mới

    @pytest.mark.asyncio
    async def test_save_card_rejects_version_conflict_with_different_hash(self) -> None:
        """Từ chối sửa đổi thẻ đã xuất bản (cùng card_id và version nhưng hash khác)."""
        card = create_sample_card()
        # Giả lập: Không trùng hash nhưng trùng (card_id, version) với hash khác
        conflict_record = StrategyCardRecord.from_domain(WORKSPACE_ID, card)
        conflict_record.content_hash = "different" * 8

        session = FakeSession(scalar_results=[None, conflict_record])
        repo = StrategyCardRepository(session)  # type: ignore[arg-type]

        with pytest.raises(StrategyCardConflict, match="không thể sửa đổi"):
            await repo.save_card(workspace_id=WORKSPACE_ID, card=card)

    @pytest.mark.asyncio
    async def test_get_card_found_and_not_found(self) -> None:
        """Đọc thẻ chiến lược theo card_id và version."""
        card = create_sample_card()
        record = StrategyCardRecord.from_domain(WORKSPACE_ID, card)

        session_found = FakeSession(scalar_results=[record])
        repo_found = StrategyCardRepository(session_found)  # type: ignore[arg-type]
        result = await repo_found.get_card(
            workspace_id=WORKSPACE_ID, card_id=card.card_id, version=card.version
        )
        assert result is not None
        assert result.card_id == card.card_id

        session_not_found = FakeSession(scalar_results=[None])
        repo_not_found = StrategyCardRepository(session_not_found)  # type: ignore[arg-type]
        result_none = await repo_not_found.get_card(
            workspace_id=WORKSPACE_ID, card_id="NON_EXISTENT", version="1.0.0"
        )
        assert result_none is None

    @pytest.mark.asyncio
    async def test_list_cards(self) -> None:
        """Liệt kê danh sách thẻ trong workspace."""
        card1 = create_sample_card("CARD-1", "1.0.0")
        card2 = create_sample_card("CARD-2", "1.0.0")
        rec1 = StrategyCardRecord.from_domain(WORKSPACE_ID, card1, is_active=True)
        rec2 = StrategyCardRecord.from_domain(WORKSPACE_ID, card2, is_active=False)

        session = FakeSession(rows_results=[[rec1, rec2]])
        repo = StrategyCardRepository(session)  # type: ignore[arg-type]

        cards = await repo.list_cards(workspace_id=WORKSPACE_ID)
        assert len(cards) == 2
        assert cards[0].card_id == "CARD-1"
        assert cards[1].card_id == "CARD-2"

    @pytest.mark.asyncio
    async def test_get_active_card(self) -> None:
        """Lấy thẻ đang kích hoạt của một loại chiến lược."""
        card = create_sample_card()
        record = StrategyCardRecord.from_domain(WORKSPACE_ID, card, is_active=True)

        session = FakeSession(scalar_results=[record])
        repo = StrategyCardRepository(session)  # type: ignore[arg-type]

        active_card = await repo.get_active_card(workspace_id=WORKSPACE_ID, card_id=card.card_id)
        assert active_card is not None
        assert active_card.card_id == card.card_id

    @pytest.mark.asyncio
    async def test_set_active_card(self) -> None:
        """Kích hoạt một phiên bản chiến lược cụ thể."""
        card = create_sample_card(version="2.0.0")
        record = StrategyCardRecord.from_domain(WORKSPACE_ID, card, is_active=False)

        session = FakeSession(scalar_results=[record])
        repo = StrategyCardRepository(session)  # type: ignore[arg-type]

        activated = await repo.set_active_card(
            workspace_id=WORKSPACE_ID, card_id=card.card_id, version="2.0.0"
        )
        assert activated.is_active is True
        # Xác minh câu lệnh update hạ cờ các card cũ
        joined_statements = " ".join(session.statements)
        assert "UPDATE strategy_cards" in joined_statements
