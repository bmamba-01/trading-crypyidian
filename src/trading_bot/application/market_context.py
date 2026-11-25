"""Dịch vụ đọc read-model bối cảnh thị trường đã kiểm quyền hiển thị (CRY-374)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from trading_bot.application.live_kline_ingestion import LIVE_BAR_SOURCE
from trading_bot.domain.portfolio.market_context import (
    MarketContextObservation,
    MarketContextReport,
    build_market_context_report,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose


class MarketContextRepository(Protocol):
    """Đọc trong transaction/workspace của caller; không dùng clock của process."""

    async def database_clock(self, *, workspace_id: UUID) -> datetime: ...

    async def display_permitted(
        self, *, workspace_id: UUID, source_id: str, at: datetime
    ) -> bool: ...

    async def list_in_window(
        self, *, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> tuple[MarketContextObservation, ...]: ...


class MarketContextReadModelService:
    """Kiểm quyền trước khi đọc dữ liệu; từ chối thì không trích xuất quan sát."""

    def __init__(
        self,
        repository: MarketContextRepository,
        *,
        source_id: str = LIVE_BAR_SOURCE,
        window: timedelta = timedelta(hours=24),
        staleness_threshold: timedelta = timedelta(hours=2),
    ) -> None:
        if not source_id.strip():
            raise ValueError("source_id không được rỗng.")
        if window <= timedelta(0) or staleness_threshold <= timedelta(0):
            raise ValueError("window và staleness_threshold phải dương.")
        self._repository = repository
        self._source_id = source_id
        self._window = window
        self._staleness_threshold = staleness_threshold

    async def get_report(self, *, workspace_id: UUID, symbol: str) -> MarketContextReport:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not symbol.strip():
            raise ValueError("symbol không được rỗng.")
        now = await self._repository.database_clock(workspace_id=workspace_id)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("database clock phải có múi giờ.")
        instant = now.astimezone(UTC)
        permitted = await self._repository.display_permitted(
            workspace_id=workspace_id, source_id=self._source_id, at=instant
        )
        # Từ chối quyền thì không đọc market data: tránh lộ dữ liệu qua log/lỗi.
        observations: tuple[MarketContextObservation, ...] = ()
        if permitted:
            observations = await self._repository.list_in_window(
                workspace_id=workspace_id,
                symbol=symbol,
                start=instant - self._window,
                end=instant,
            )
        return build_market_context_report(
            workspace_id=str(workspace_id),
            symbol=symbol,
            observations=observations,
            purpose=SourceUsePurpose.DISPLAY if permitted else SourceUsePurpose.METADATA,
            now=instant,
            window=self._window,
            staleness_threshold=self._staleness_threshold,
        )
