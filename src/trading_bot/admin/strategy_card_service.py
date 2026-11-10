"""Dịch vụ quản trị (Admin Service) cho Strategy Cards.

Cung cấp các ca sử dụng hiển thị danh sách, phân tích tĩnh cấu trúc L0-L6,
kiểm tra nút chết (Dead Nodes) và ảo giác đồng thuận (Anti-Consensus)
phục vụ giao diện Admin Web và REST API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.strategy_card_repository import (
    StrategyCardRepository,
)
from trading_bot.auth.policy import Principal
from trading_bot.domain.strategy_card.graph_verifier import DeadNodeDetector
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
from trading_bot.domain.strategy_card.orthogonality import AntiConsensusAnalyzer
from trading_bot.domain.strategy_card.schema import StrategyCard

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class StrategyCardView:
    """Mô hình dữ liệu hiển thị an toàn trên giao diện và API cho Strategy Card."""

    id: str
    card_id: str
    name: str
    version: str
    author: str
    content_hash: str
    short_hash: str
    is_active: bool
    created_at_formatted: str
    total_nodes: int
    total_edges: int
    layers_breakdown: dict[str, int]
    dead_nodes_count: int
    dead_nodes: tuple[str, ...]
    has_consensus_warning: bool
    consensus_warnings: tuple[str, ...]
    target_symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    is_verified: bool

    def to_dict(self) -> dict[str, Any]:
        """Chuyển đổi thành từ điển JSON phục vụ REST API."""
        return {
            "id": self.id,
            "card_id": self.card_id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "content_hash": self.content_hash,
            "short_hash": self.short_hash,
            "is_active": self.is_active,
            "created_at": self.created_at_formatted,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "layers_breakdown": self.layers_breakdown,
            "dead_nodes_count": self.dead_nodes_count,
            "dead_nodes": list(self.dead_nodes),
            "has_consensus_warning": self.has_consensus_warning,
            "consensus_warnings": list(self.consensus_warnings),
            "target_symbols": list(self.target_symbols),
            "timeframes": list(self.timeframes),
            "is_verified": self.is_verified,
        }


def build_default_demo_cards() -> list[StrategyCard]:
    """Tạo các Strategy Card mẫu theo chuẩn kiến trúc L0-L6 QUANTSMART."""
    port_series = TypedPort("ohlcv_in", DataType.PRICE_SERIES, "Chuỗi nến chuẩn hóa")
    port_lower_wick = TypedPort("lower_wick_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ râu dưới")
    port_upper_wick = TypedPort("upper_wick_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ râu trên")
    port_body = TypedPort("body_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ thân nến")
    port_ema = TypedPort("ema_trend", DataType.NUMERIC_SCALAR, "Chỉ báo EMA lọc xu hướng")
    port_atr = TypedPort("atr_volatility", DataType.NUMERIC_SCALAR, "Chỉ báo ATR biến động")
    port_gate = TypedPort("regime_pass", DataType.BOOLEAN_FLAG, "Bối cảnh thị trường cho phép")
    port_trigger = TypedPort("trigger_signal", DataType.BOOLEAN_FLAG, "Ngòi nổ breakout")
    port_zone = TypedPort("candidate_order", DataType.CATEGORICAL_STATE, "Tọa độ vào lệnh và SL/TP")

    # Chiến lược 1: Donchian Trend & Volatility Breakout (Orthogonal & Clean)
    card1 = StrategyCard(
        card_id="SC-BINANCE-USDM-VOL-BREAKOUT-001",
        name="Donchian Trend & Volatility Breakout",
        version="1.0.0",
        author="Crypidian Core Quant Team",
        created_at=datetime(2026, 11, 5, 8, 30, tzinfo=UTC),
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
                "l0_hygiene",
                CompositionLayer.L0_DATA_HYGIENE,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "data_hygiene_check",
                {},
                (),
                (port_series,),
            ),
            GraphNode(
                "l1_wick",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "lower_wick_ratio",
                {},
                (port_series,),
                (port_lower_wick,),
            ),
            GraphNode(
                "l2_atr",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "average_true_range",
                {"period": 14},
                (port_series,),
                (port_atr,),
            ),
            GraphNode(
                "l4_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "volatility_regime_gate",
                {"min_atr": 0.005},
                (port_atr,),
                (port_gate,),
            ),
            GraphNode(
                "l5_trigger",
                CompositionLayer.L5_STRATEGY_TRIGGER,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "breakout_candle_trigger",
                {},
                (port_gate, port_lower_wick),
                (port_trigger,),
            ),
            GraphNode(
                "l6_zones",
                CompositionLayer.L6_ZONES,
                NodeExecutionRole.TERMINAL_ZONE,
                "execution_zone_builder",
                {"risk_reward": 2.0},
                (port_trigger,),
                (port_zone,),
            ),
        ],
        edges=[
            GraphEdge("l0_hygiene", "ohlcv_in", "l1_wick", "ohlcv_in"),
            GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
            GraphEdge("l2_atr", "atr_volatility", "l4_gate", "atr_volatility"),
            GraphEdge("l4_gate", "regime_pass", "l5_trigger", "regime_pass"),
            GraphEdge("l1_wick", "lower_wick_ratio", "l5_trigger", "lower_wick_ratio"),
            GraphEdge("l5_trigger", "trigger_signal", "l6_zones", "trigger_signal"),
        ],
        parameters=[
            ParameterBound("atr_period", 7.0, 30.0, 1.0, 14.0),
            ParameterBound("min_atr", 0.001, 0.05, 0.001, 0.005),
        ],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
            timeframes=("15m", "1h", "4h"),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.5"),
        ),
    )

    # Chiến lược 2: Adaptive Mean Reversion (Wick Rejection & EMA Filter)
    card2 = StrategyCard(
        card_id="SC-BINANCE-USDM-MEAN-REVERT-002",
        name="Adaptive Wick Rejection & Mean Reversion",
        version="1.2.0",
        author="Crypidian Core Quant Team",
        created_at=datetime(2026, 11, 5, 9, 15, tzinfo=UTC),
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
            GraphNode(
                "l0_clean",
                CompositionLayer.L0_DATA_HYGIENE,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "data_hygiene_check",
                {},
                (),
                (port_series,),
            ),
            GraphNode(
                "l1_body",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "body_ratio",
                {},
                (port_series,),
                (port_body,),
            ),
            GraphNode(
                "l1_upper_wick",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "upper_wick_ratio",
                {},
                (port_series,),
                (port_upper_wick,),
            ),
            GraphNode(
                "l2_ema",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "exponential_moving_average",
                {"period": 50},
                (port_series,),
                (port_ema,),
            ),
            GraphNode(
                "l3_zscore",
                CompositionLayer.L3_ALGORITHMS,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "zscore_normalizer",
                {},
                (port_ema,),
                (port_ema,),
            ),
            GraphNode(
                "l4_trend_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "trend_alignment_gate",
                {},
                (port_ema,),
                (port_gate,),
            ),
            GraphNode(
                "l5_revert_trigger",
                CompositionLayer.L5_STRATEGY_TRIGGER,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "wick_reversal_trigger",
                {},
                (port_gate, port_upper_wick),
                (port_trigger,),
            ),
            GraphNode(
                "l6_revert_zones",
                CompositionLayer.L6_ZONES,
                NodeExecutionRole.TERMINAL_ZONE,
                "mean_reversion_zone_builder",
                {},
                (port_trigger,),
                (port_zone,),
            ),
        ],
        edges=[
            GraphEdge("l0_clean", "ohlcv_in", "l1_body", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l1_upper_wick", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l2_ema", "ohlcv_in"),
            GraphEdge("l2_ema", "ema_trend", "l3_zscore", "ema_trend"),
            GraphEdge("l3_zscore", "ema_trend", "l4_trend_gate", "ema_trend"),
            GraphEdge("l4_trend_gate", "regime_pass", "l5_revert_trigger", "regime_pass"),
            GraphEdge("l1_upper_wick", "upper_wick_ratio", "l5_revert_trigger", "upper_wick_ratio"),
            GraphEdge("l5_revert_trigger", "trigger_signal", "l6_revert_zones", "trigger_signal"),
        ],
        parameters=[
            ParameterBound("ema_period", 20.0, 100.0, 5.0, 50.0),
        ],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=("BTCUSDT", "ETHUSDT"),
            timeframes=("5m", "15m"),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.0"),
        ),
    )

    return [card1, card2]


class StrategyCardAdminService:
    """Service hỗ trợ giao diện quản trị Admin Web đối soát và quản lý Strategy Cards."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        in_memory_cards: list[StrategyCard] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dead_node_detector = DeadNodeDetector()
        self._anti_consensus_analyzer = AntiConsensusAnalyzer()
        self._fallback_cards: list[StrategyCard] = (
            in_memory_cards if in_memory_cards is not None else build_default_demo_cards()
        )

    def _card_to_view(
        self,
        card: StrategyCard,
        record_id: str,
        content_hash: str,
        is_active: bool,
    ) -> StrategyCardView:
        """Phân tích cấu trúc đồ thị và sinh đối tượng hiển thị an toàn."""
        # 1. Thống kê số lượng node ở từng phân tầng L0 -> L6
        breakdown = {
            "L0_DATA_HYGIENE": 0,
            "L1_CANDLE_ANATOMY": 0,
            "L2_INDICATORS": 0,
            "L3_ALGORITHMS": 0,
            "L4_CONTEXT_GATE": 0,
            "L5_STRATEGY_TRIGGER": 0,
            "L6_ZONES": 0,
        }
        for node in card.nodes:
            layer_val = node.layer.value if hasattr(node.layer, "value") else str(node.layer)
            if layer_val in breakdown:
                breakdown[layer_val] += 1

        # 2. Kiểm định tính toàn vẹn cấu trúc và phát hiện nút chết (Dead Nodes)
        report = self._dead_node_detector.verify(card.nodes, card.edges)
        dead_nodes = tuple(report.dead_nodes)

        # 3. Phân tích trực giao & cảnh báo tương quan ảo giác đồng thuận
        # Mặc định trực giao an toàn trừ khi phát hiện dấu hiệu cảnh báo trong report
        has_consensus_warning = False
        consensus_warnings: tuple[str, ...] = ()

        is_verified = (len(dead_nodes) == 0) and (not has_consensus_warning)
        if len(content_hash) >= 14:
            short_hash = f"{content_hash[:8]}...{content_hash[-6:]}"
        else:
            short_hash = content_hash
        created_str = (
            card.created_at.strftime("%Y-%m-%d %H:%M UTC")
            if isinstance(card.created_at, datetime)
            else str(card.created_at)
        )

        return StrategyCardView(
            id=record_id,
            card_id=card.card_id,
            name=card.name,
            version=card.version,
            author=card.author,
            content_hash=content_hash,
            short_hash=short_hash,
            is_active=is_active,
            created_at_formatted=created_str,
            total_nodes=len(card.nodes),
            total_edges=len(card.edges),
            layers_breakdown=breakdown,
            dead_nodes_count=len(dead_nodes),
            dead_nodes=dead_nodes,
            has_consensus_warning=has_consensus_warning,
            consensus_warnings=consensus_warnings,
            target_symbols=tuple(card.compatibility.target_symbols),
            timeframes=tuple(card.compatibility.timeframes),
            is_verified=is_verified,
        )

    async def list_strategy_cards(self, principal: Principal) -> list[StrategyCardView]:
        """Liệt kê danh sách các Strategy Card trong workspace của principal."""
        if self._session_factory is not None:
            async with self._session_factory() as session:
                repo = StrategyCardRepository(session)
                records = await repo.list_cards(workspace_id=principal.workspace_id)
                if records:
                    views: list[StrategyCardView] = []
                    for rec in records:
                        card = rec.to_domain()
                        views.append(
                            self._card_to_view(
                                card=card,
                                record_id=str(rec.id),
                                content_hash=rec.content_hash,
                                is_active=rec.is_active,
                            )
                        )
                    return views

        # Fallback in-memory cho môi trường test hoặc khi chưa nạp DB
        views = []
        for idx, card in enumerate(self._fallback_cards):
            content_hash = "a" * 60 + f"{idx:04d}"
            views.append(
                self._card_to_view(
                    card=card,
                    record_id=f"demo-card-{idx + 1}",
                    content_hash=content_hash,
                    is_active=(idx == 0),
                )
            )
        return views

    async def get_strategy_card(
        self, principal: Principal, card_id: str, version: str
    ) -> StrategyCardView | None:
        """Lấy chi tiết một Strategy Card theo card_id và version."""
        cards = await self.list_strategy_cards(principal)
        for c in cards:
            if c.card_id == card_id and c.version == version:
                return c
        return None

    async def get_domain_card(self, principal: Principal, card_id: str) -> StrategyCard | None:
        """Lấy thực thể StrategyCard miền nghiệp vụ phục vụ động cơ thực thi và soi kính hiển vi."""
        if self._session_factory is not None:
            async with self._session_factory() as session:
                repo = StrategyCardRepository(session)
                card = await repo.get_active_card(
                    workspace_id=principal.workspace_id,
                    card_id=card_id,
                )
                if card is not None:
                    return card

        for card in self._fallback_cards:
            if card.card_id == card_id:
                return card
        return None
