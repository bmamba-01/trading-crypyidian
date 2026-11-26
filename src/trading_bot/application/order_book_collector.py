"""Collector nhẹ thu ảnh chụp sổ lệnh L2 ``/fapi/v1/depth`` Binance USDⓈ-M (DATA-2).

Tuân thủ các ràng buộc vận hành:
- Kiểm quyền Source Registry ``PAPER_DEMO`` (``BINANCE_USDM_REST``) **trước** khi gọi mạng
  (fail-closed khi thiếu quyền).
- Tôn trọng rate-limit weight của Binance USDⓈ-M (weight 5 cho ``limit=100``) theo cửa sổ
  trượt 1 phút.
- Lưu cache trạng thái in-memory và tùy chọn ghi JSON snapshot mới nhất ra đĩa phục vụ
  runtime hoặc màn hình chart.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.order_book import (
    OrderBookConfig,
    OrderBookContext,
    OrderBookSnapshot,
    compute_order_book_context,
    from_port_snapshot,
)
from trading_bot.ports.market_data import MarketDataPage
from trading_bot.ports.market_data import OrderBookSnapshot as PortOrderBookSnapshot

logger = logging.getLogger(__name__)

DEFAULT_DEPTH_LIMIT = 100
_DEPTH_WEIGHT_BY_LIMIT = {
    5: 2,
    10: 2,
    20: 2,
    50: 2,
    100: 5,
    500: 10,
    1000: 20,
}
_MINUTE = timedelta(minutes=1)


class OrderBookMarketDataPort(Protocol):
    """Giao diện tối thiểu để tải L2 depth từ adapter thị trường."""

    async def get_order_book(
        self, symbol: str, *, limit: int = 100
    ) -> MarketDataPage[PortOrderBookSnapshot]: ...


class OrderBookRightsGate(Protocol):
    """Cổng kiểm quyền Source Registry trước khi gọi REST công khai."""

    async def authorize(
        self,
        *,
        request: RestBackfillRequest,
        now: datetime,
    ) -> BackfillRightsEvidence: ...


@dataclass(frozen=True, slots=True)
class OrderBookCollectResult:
    """Kết quả một lần thu thập L2 depth cho một symbol."""

    symbol: str
    status: str  # collected | rights_denied | rate_limited | error
    weight_used: int = 0
    context: OrderBookContext | None = None
    detail: str | None = None


class OrderBookCollector:
    """Service thu thập, kiểm quyền, giới hạn weight và cache sổ lệnh L2 theo symbol."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        market_data: OrderBookMarketDataPort,
        rights_gate: OrderBookRightsGate,
        limit: int = DEFAULT_DEPTH_LIMIT,
        max_weight_per_minute: int = 60,
        snapshot_dir: Path | None = None,
        context_config: OrderBookConfig | None = None,
    ) -> None:
        if limit not in _DEPTH_WEIGHT_BY_LIMIT:
            raise ValueError("limit depth phải là một trong 5, 10, 20, 50, 100, 500, 1000.")
        if max_weight_per_minute < _DEPTH_WEIGHT_BY_LIMIT[limit]:
            raise ValueError("max_weight_per_minute phải >= weight của một lần gọi depth.")
        self._workspace_id = workspace_id
        self._market_data = market_data
        self._rights_gate = rights_gate
        self._limit = limit
        self._weight = _DEPTH_WEIGHT_BY_LIMIT[limit]
        self._max_weight_per_minute = max_weight_per_minute
        self._snapshot_dir = snapshot_dir
        self._context_config = context_config or OrderBookConfig()
        self._weight_window: deque[tuple[datetime, int]] = deque()
        self._snapshots: dict[str, OrderBookSnapshot] = {}
        self._contexts: dict[str, OrderBookContext] = {}

    def _prune_weight_window(self, now: datetime) -> int:
        cutoff = now - _MINUTE
        while self._weight_window and self._weight_window[0][0] <= cutoff:
            self._weight_window.popleft()
        return sum(w for _, w in self._weight_window)

    def get_latest(self, symbol: str) -> OrderBookSnapshot | None:
        """Lấy ảnh chụp L2 mới nhất trong bộ nhớ cho ``symbol``."""
        return self._snapshots.get(symbol)

    def get_context(
        self,
        symbol: str,
        *,
        as_of: datetime,
        atr_14: Decimal | None = None,
    ) -> OrderBookContext:
        """Tính ``OrderBookContext`` từ ảnh chụp mới nhất trong cache (fail-closed nếu stale)."""
        snap = self._snapshots.get(symbol)
        return compute_order_book_context(
            snap, as_of=as_of, atr_14=atr_14, config=self._context_config
        )

    def _write_json_snapshot(self, symbol: str, context: OrderBookContext) -> None:
        if self._snapshot_dir is None:
            return
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        target = self._snapshot_dir / f"{symbol}.json"
        tmp_path = self._snapshot_dir / f".{symbol}.json.tmp"
        tmp_path.write_text(
            json.dumps(context.to_metrics(), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(target)

    async def collect_symbol(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
        atr_14: Decimal | None = None,
    ) -> OrderBookCollectResult:
        """Kiểm quyền ``PAPER_DEMO``, kiểm tra budget weight rồi tải ``/fapi/v1/depth``."""
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        aligned_end = instant.replace(second=0, microsecond=0)
        if aligned_end < instant:
            aligned_end += _MINUTE
        request = RestBackfillRequest(
            symbol=symbol,
            interval="1m",
            start=aligned_end - _MINUTE,
            end=aligned_end,
            purpose=BackfillRightsPurpose.PAPER_DEMO,
        )
        try:
            await self._rights_gate.authorize(request=request, now=instant)
        except PermissionError as exc:
            return OrderBookCollectResult(
                symbol=symbol,
                status="rights_denied",
                detail=str(exc),
            )

        used_weight = self._prune_weight_window(instant)
        if used_weight + self._weight > self._max_weight_per_minute:
            return OrderBookCollectResult(
                symbol=symbol,
                status="rate_limited",
                detail=(
                    f"Vượt giới hạn weight depth/phút ({used_weight + self._weight} > "
                    f"{self._max_weight_per_minute})."
                ),
            )

        try:
            page = await self._market_data.get_order_book(symbol, limit=self._limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lỗi tải L2 depth cho %s: %s", symbol, exc)
            return OrderBookCollectResult(
                symbol=symbol,
                status="error",
                detail=str(exc),
            )

        self._weight_window.append((instant, self._weight))
        if not page.records:
            return OrderBookCollectResult(
                symbol=symbol,
                status="error",
                weight_used=self._weight,
                detail="Phản hồi depth không có bản ghi.",
            )

        domain_snap = from_port_snapshot(page.records[0], as_of=instant)
        ctx = compute_order_book_context(
            domain_snap, as_of=instant, atr_14=atr_14, config=self._context_config
        )
        self._snapshots[symbol] = domain_snap
        self._contexts[symbol] = ctx
        self._write_json_snapshot(symbol, ctx)
        return OrderBookCollectResult(
            symbol=symbol,
            status="collected",
            weight_used=self._weight,
            context=ctx,
        )


__all__ = [
    "DEFAULT_DEPTH_LIMIT",
    "OrderBookCollectResult",
    "OrderBookCollector",
]
