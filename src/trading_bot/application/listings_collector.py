"""Collector exchangeInfo Binance (AI-4 lát 3): kiểm quyền → tải → chuẩn hóa → lưu lần kiểm.

Chạy trong ``ingestion-worker``. Nguồn ``BINANCE_USDM_EXCHANGE_INFO`` cần policy Source Registry
``verified`` cấp ``PAPER_DEMO``. Mỗi lần kiểm lưu một hàng; danh sách hợp đồng chỉ lưu khi hash
khác lần trước (``changed``). Phản hồi sai hợp đồng hoặc bị giới hạn thì không lưu gì.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from trading_bot.application.source_use_gate import SourceUseGate
from trading_bot.domain.research.listings import (
    LISTINGS_SOURCE_ID,
    entries_hash,
    parse_exchange_info,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

logger = logging.getLogger(__name__)
DEFAULT_REFRESH = timedelta(minutes=30)


class ListingsFetcher(Protocol):
    async def fetch_exchange_info(self) -> Any: ...


class ListingsStore(Protocol):
    def gate(self, source_id: str) -> SourceUseGate: ...

    async def last_check(self) -> tuple[datetime, str] | None:
        """``(fetched_at, entries_sha256)`` của lần kiểm gần nhất."""
        ...

    async def insert(self, record: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ListingsTickResult:
    status: str  # changed | unchanged | skipped_recent | rights_denied | error
    entry_count: int = 0
    detail: str = ""


class ListingsCollector:
    def __init__(
        self,
        *,
        workspace_id: UUID,
        fetcher: ListingsFetcher,
        store_scope: Callable[[], AbstractAsyncContextManager[ListingsStore]],
        refresh: timedelta = DEFAULT_REFRESH,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace_id = workspace_id
        self._fetcher = fetcher
        self._store_scope = store_scope
        self._refresh = refresh
        self._clock = clock

    async def collect_once(self, now: datetime) -> ListingsTickResult:
        try:
            async with self._store_scope() as store:
                last = await store.last_check()
                if last is not None and now - last[0] < self._refresh:
                    return ListingsTickResult("skipped_recent")
                await store.gate(LISTINGS_SOURCE_ID).authorize(SourceUsePurpose.PAPER_DEMO)
            entries = parse_exchange_info(await self._fetcher.fetch_exchange_info())
            fetched_at = self._clock()
            digest = entries_hash(entries)
            async with self._store_scope() as store:
                evidence = await store.gate(LISTINGS_SOURCE_ID).authorize(
                    SourceUsePurpose.PAPER_DEMO
                )
                previous = await store.last_check()
                changed = previous is None or previous[1] != digest
                await store.insert(
                    {
                        "id": uuid4(),
                        "workspace_id": self._workspace_id,
                        "fetched_at": fetched_at,
                        "policy_fingerprint": evidence.policy_fingerprint,
                        "entries_sha256": digest,
                        "entry_count": len(entries),
                        "changed": changed,
                        "entries": [e.payload() for e in entries] if changed else None,
                    }
                )
        except PermissionError as error:
            return ListingsTickResult("rights_denied", detail=str(error))
        except Exception as error:  # noqa: BLE001 - lỗi mạng/hợp đồng: thử lại lượt sau
            logger.warning("listings collector lỗi: %s", type(error).__name__)
            return ListingsTickResult("error", detail=type(error).__name__)
        return ListingsTickResult("changed" if changed else "unchanged", len(entries))


async def run_listings_collector(
    collector: ListingsCollector,
    stopping: Any,
    *,
    tick_seconds: float = 300.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_tick: Callable[[ListingsTickResult], None] | None = None,
) -> None:
    import asyncio

    while not stopping.is_set():
        result = await collector.collect_once(clock())
        if on_tick is not None:
            on_tick(result)
        if result.status != "skipped_recent":
            logger.info("listings: %s (%d hợp đồng)", result.status, result.entry_count)
        try:
            await asyncio.wait_for(stopping.wait(), timeout=tick_seconds)
        except TimeoutError:
            continue


__all__ = ["ListingsCollector", "ListingsTickResult", "run_listings_collector"]
