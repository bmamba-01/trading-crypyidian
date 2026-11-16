"""Dịch vụ quản trị (Admin Service) cho Strategy Cards.

Cung cấp các ca sử dụng hiển thị danh sách, phân tích tĩnh cấu trúc L0-L6,
kiểm tra nút chết (Dead Nodes) và ảo giác đồng thuận (Anti-Consensus)
phục vụ giao diện Admin Web và REST API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.strategy_card_repository import (
    StrategyCardRepository,
)
from trading_bot.application.strategy_pipeline.consensus import (
    ConsensusSummary,
    PriceBar,
    card_consensus,
)
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.auth.policy import Principal
from trading_bot.domain.strategy_card.graph_verifier import DeadNodeDetector
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash

if TYPE_CHECKING:
    pass

# Nguồn nến đã đóng (tăng dần) cho phân tích đồng thuận: (principal, symbol, timeframe) → nến.
CardBarsProvider = Callable[[Principal, str, str], Awaitable[Sequence[PriceBar]]]


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
    consensus_status: str
    consensus_sample_size: int
    independent_conditions: int | None
    total_conditions: int
    consensus_scope: str
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
            "consensus_status": self.consensus_status,
            "consensus_sample_size": self.consensus_sample_size,
            "independent_conditions": self.independent_conditions,
            "total_conditions": self.total_conditions,
            "consensus_scope": self.consensus_scope,
            "target_symbols": list(self.target_symbols),
            "timeframes": list(self.timeframes),
            "is_verified": self.is_verified,
        }


class StrategyCardAdminService:
    """Service hỗ trợ giao diện quản trị Admin Web đối soát và quản lý Strategy Cards."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        in_memory_cards: list[StrategyCard] | None = None,
        bars_provider: CardBarsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dead_node_detector = DeadNodeDetector()
        self._bars_provider = bars_provider
        self._fallback_cards: list[StrategyCard] = (
            in_memory_cards if in_memory_cards is not None else build_default_demo_cards()
        )

    def _card_to_view(
        self,
        card: StrategyCard,
        record_id: str,
        content_hash: str,
        is_active: bool,
        consensus: ConsensusSummary,
        consensus_scope: str,
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

        # 3. Đồng thuận giả (G7): đo trên chuỗi đầu ra node thật; thiếu nến = "chưa đủ dữ liệu",
        # không mặc định là trực giao.
        has_consensus_warning = consensus.status == "warning"
        consensus_warnings = consensus.warnings

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
            consensus_status=consensus.status,
            consensus_sample_size=consensus.sample_size,
            independent_conditions=consensus.independent_conditions,
            total_conditions=consensus.total_conditions,
            consensus_scope=consensus_scope,
            target_symbols=tuple(card.compatibility.target_symbols),
            timeframes=tuple(card.compatibility.timeframes),
            is_verified=is_verified,
        )

    async def _consensus(
        self, principal: Principal, card: StrategyCard
    ) -> tuple[ConsensusSummary, str]:
        """Đo đồng thuận giả trên mã/khung đầu tiên card khai báo, từ nến đã lưu nội bộ."""
        symbols, timeframes = card.compatibility.target_symbols, card.compatibility.timeframes
        if not symbols or not timeframes:
            return card_consensus(card, []), "—"
        symbol, timeframe = symbols[0], timeframes[0]
        bars: Sequence[PriceBar] = ()
        if self._bars_provider is not None:
            bars = await self._bars_provider(principal, symbol, timeframe)
        return card_consensus(card, bars), f"{symbol} · {timeframe}"

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
                        consensus, scope = await self._consensus(principal, card)
                        views.append(
                            self._card_to_view(
                                card=card,
                                record_id=str(rec.id),
                                content_hash=rec.content_hash,
                                is_active=rec.is_active,
                                consensus=consensus,
                                consensus_scope=scope,
                            )
                        )
                    return views

        # Fallback in-memory cho môi trường test hoặc khi chưa nạp DB
        views = []
        # Thẻ mẫu chưa lưu DB: hash là SHA-256 canonical thật của nội dung thẻ (không bịa) và
        # không thẻ nào được coi là đang kích hoạt vì chưa có hàng công bố.
        for idx, card in enumerate(self._fallback_cards):
            consensus, scope = await self._consensus(principal, card)
            views.append(
                self._card_to_view(
                    card=card,
                    record_id=f"demo-card-{idx + 1}",
                    content_hash=compute_canonical_hash(card),
                    is_active=False,
                    consensus=consensus,
                    consensus_scope=scope,
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
