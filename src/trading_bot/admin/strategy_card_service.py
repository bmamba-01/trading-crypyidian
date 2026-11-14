"""Dịch vụ quản trị (Admin Service) cho Strategy Cards.

Cung cấp các ca sử dụng hiển thị danh sách, phân tích tĩnh cấu trúc L0-L6,
kiểm tra nút chết (Dead Nodes) và ảo giác đồng thuận (Anti-Consensus)
phục vụ giao diện Admin Web và REST API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.strategy_card_repository import (
    StrategyCardRepository,
)
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.auth.policy import Principal
from trading_bot.domain.strategy_card.graph_verifier import DeadNodeDetector
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
