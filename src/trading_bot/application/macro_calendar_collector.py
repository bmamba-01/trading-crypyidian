"""Collector lịch vĩ mô (AI-4 lát 1): kiểm quyền → tải → parse → lưu một hàng bất biến/lần tải.

Chạy trong ``ingestion-worker`` (role ghi duy nhất của ``macro_calendar_fetch``). Mỗi nguồn cần
policy Source Registry ``verified`` cấp ``PAPER_DEMO``; thiếu quyền thì bỏ qua nguồn đó (không
tải). Lỗi mạng/parse của một nguồn không chặn nguồn khác; H06 sẽ thấy lịch cũ → STALE.
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
from trading_bot.domain.research.macro_calendar import (
    BEA_SOURCE_ID,
    FOMC_SOURCE_ID,
    MACRO_SOURCE_IDS,
    MacroEvent,
    events_hash,
    parse_bea_release_dates,
    parse_fomc_calendar,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

logger = logging.getLogger(__name__)
DEFAULT_REFRESH = timedelta(hours=6)


class MacroCalendarFetcher(Protocol):
    async def fetch(self, source_id: str) -> tuple[str, Any]: ...


class MacroCalendarStore(Protocol):
    """Trong transaction đã gắn ``app.workspace_id``."""

    def gate(self, source_id: str) -> SourceUseGate: ...

    async def last_fetched_at(self, source_id: str) -> datetime | None: ...

    async def insert(self, record: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class MacroFetchResult:
    source_id: str
    status: str  # stored | skipped_recent | rights_denied | error
    event_count: int = 0
    detail: str = ""


def parse_source(source_id: str, raw: Any) -> list[MacroEvent]:
    if source_id == BEA_SOURCE_ID:
        return parse_bea_release_dates(raw)
    if source_id == FOMC_SOURCE_ID:
        return parse_fomc_calendar(raw)
    raise ValueError(f"Nguồn macro chưa hỗ trợ: {source_id}")


class MacroCalendarCollector:
    def __init__(
        self,
        *,
        workspace_id: UUID,
        fetcher: MacroCalendarFetcher,
        store_scope: Callable[[], AbstractAsyncContextManager[MacroCalendarStore]],
        sources: tuple[str, ...] = MACRO_SOURCE_IDS,
        refresh: timedelta = DEFAULT_REFRESH,
    ) -> None:
        self._workspace_id = workspace_id
        self._fetcher = fetcher
        self._store_scope = store_scope
        self._sources = sources
        self._refresh = refresh

    async def collect_once(self, now: datetime) -> list[MacroFetchResult]:
        results = []
        for source_id in self._sources:
            try:
                results.append(await self._collect_source(source_id, now))
            except PermissionError as error:
                results.append(MacroFetchResult(source_id, "rights_denied", detail=str(error)))
            except Exception as error:  # noqa: BLE001 - một nguồn lỗi không chặn nguồn khác
                logger.warning("macro calendar %s lỗi: %s", source_id, type(error).__name__)
                results.append(MacroFetchResult(source_id, "error", detail=type(error).__name__))
        return results

    async def _collect_source(self, source_id: str, now: datetime) -> MacroFetchResult:
        async with self._store_scope() as store:
            last = await store.last_fetched_at(source_id)
            if last is not None and now - last < self._refresh:
                return MacroFetchResult(source_id, "skipped_recent")
            evidence = await store.gate(source_id).authorize(SourceUsePurpose.PAPER_DEMO)
        url, raw = await self._fetcher.fetch(source_id)
        events = parse_source(source_id, raw)
        fetched_at = datetime.now(UTC)
        async with self._store_scope() as store:
            # Kiểm lại quyền sau khi tải: policy bị thu hồi giữa chừng thì không lưu.
            evidence = await store.gate(source_id).authorize(SourceUsePurpose.PAPER_DEMO)
            await store.insert(
                {
                    "id": uuid4(),
                    "workspace_id": self._workspace_id,
                    "source_id": source_id,
                    "fetched_at": fetched_at,
                    "source_url": url,
                    "policy_fingerprint": evidence.policy_fingerprint,
                    "events_sha256": events_hash(events),
                    "event_count": len(events),
                    "coverage_end": max(e.scheduled_at for e in events),
                    "events": [e.payload() for e in events],
                }
            )
        return MacroFetchResult(source_id, "stored", len(events))


async def run_macro_collector(
    collector: MacroCalendarCollector,
    stopping: Any,
    *,
    tick_seconds: float = 600.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_tick: Callable[[list[MacroFetchResult]], None] | None = None,
) -> None:
    """Lặp tới khi ``stopping`` được set; mỗi tick chỉ tải nguồn đã quá hạn làm mới."""
    import asyncio

    while not stopping.is_set():
        results = await collector.collect_once(clock())
        if on_tick is not None:
            on_tick(results)
        for result in results:
            if result.status in {"stored", "rights_denied", "error"}:
                logger.info(
                    "macro calendar %s: %s (%d sự kiện)",
                    result.source_id,
                    result.status,
                    result.event_count,
                )
        try:
            await asyncio.wait_for(stopping.wait(), timeout=tick_seconds)
        except TimeoutError:
            continue


__all__ = [
    "MacroCalendarCollector",
    "MacroFetchResult",
    "parse_source",
    "run_macro_collector",
]
