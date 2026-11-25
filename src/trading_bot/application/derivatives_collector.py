"""Collector dữ liệu phái sinh Binance (AI-4 lát 2): kiểm quyền → tải → kiểm hợp đồng → lưu.

Chạy trong ``ingestion-worker`` (role ghi duy nhất của ``derivatives_snapshot``). Nguồn
``BINANCE_USDM_DERIVATIVES_STATS`` cần policy Source Registry ``verified`` cấp ``PAPER_DEMO``;
thiếu quyền thì không tải gì. Lỗi một symbol không chặn symbol khác; Binance báo giới hạn
(418/429) thì dừng cả lượt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from trading_bot.application.source_use_gate import SourceUseGate
from trading_bot.domain.research.derivatives import (
    DERIVATIVES_RETENTION_DAYS,
    DERIVATIVES_SOURCE_ID,
    SourceThrottled,
    build_snapshot,
    payload_hash,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

logger = logging.getLogger(__name__)
DEFAULT_REFRESH = timedelta(minutes=55)


class DerivativesFetcher(Protocol):
    async def fetch_symbol(self, symbol: str) -> dict[str, Any]: ...


class DerivativesStore(Protocol):
    """Trong transaction đã gắn ``app.workspace_id``."""

    def gate(self, source_id: str) -> SourceUseGate: ...

    async def last_collected_at(self) -> datetime | None: ...

    async def prune_expired(self, now: datetime) -> int: ...

    async def insert(self, record: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class DerivativesTickResult:
    status: str  # stored | skipped_recent | rights_denied | throttled | error
    stored: int = 0
    failed: tuple[str, ...] = ()
    detail: str = ""


class DerivativesCollector:
    def __init__(
        self,
        *,
        workspace_id: UUID,
        symbols: Sequence[str],
        fetcher: DerivativesFetcher,
        store_scope: Callable[[], AbstractAsyncContextManager[DerivativesStore]],
        refresh: timedelta = DEFAULT_REFRESH,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not symbols:
            raise ValueError("Collector phái sinh cần ít nhất một symbol.")
        self._workspace_id = workspace_id
        self._symbols = tuple(dict.fromkeys(symbols))
        self._fetcher = fetcher
        self._store_scope = store_scope
        self._refresh = refresh
        self._clock = clock

    async def collect_once(self, now: datetime) -> DerivativesTickResult:
        try:
            async with self._store_scope() as store:
                removed = await store.prune_expired(now)
                if removed:
                    logger.info("derivatives: đã xoá %s ảnh chụp hết hạn", removed)
                last = await store.last_collected_at()
                if last is not None and now - last < self._refresh:
                    return DerivativesTickResult("skipped_recent")
                await store.gate(DERIVATIVES_SOURCE_ID).authorize(SourceUsePurpose.PAPER_DEMO)
        except PermissionError as error:
            return DerivativesTickResult("rights_denied", detail=str(error))
        records: list[dict[str, Any]] = []
        failed: list[str] = []
        for symbol in self._symbols:
            try:
                raw = await self._fetcher.fetch_symbol(symbol)
                collected_at = self._clock()
                snapshot = build_snapshot(symbol, collected_at, raw)
            except SourceThrottled:
                logger.warning("derivatives: Binance giới hạn request, dừng lượt")
                return DerivativesTickResult("throttled", failed=(symbol,))
            except Exception as error:  # noqa: BLE001 - một symbol lỗi không chặn symbol khác
                logger.warning("derivatives %s lỗi: %s", symbol, type(error).__name__)
                failed.append(symbol)
                continue
            records.append({**snapshot.record(), "payload_sha256": payload_hash(raw)})
        if not records:
            return DerivativesTickResult("error", failed=tuple(failed))
        try:
            async with self._store_scope() as store:
                # Kiểm lại quyền trước khi lưu: policy bị thu hồi giữa chừng thì không lưu.
                evidence = await store.gate(DERIVATIVES_SOURCE_ID).authorize(
                    SourceUsePurpose.PAPER_DEMO
                )
                for record in records:
                    await store.insert(
                        {
                            "id": uuid4(),
                            "workspace_id": self._workspace_id,
                            "policy_fingerprint": evidence.policy_fingerprint,
                            "expires_at": now + timedelta(days=DERIVATIVES_RETENTION_DAYS),
                            **record,
                        }
                    )
        except PermissionError as error:
            return DerivativesTickResult("rights_denied", detail=str(error))
        return DerivativesTickResult("stored", len(records), tuple(failed))


async def run_derivatives_collector(
    collector: DerivativesCollector,
    stopping: Any,
    *,
    tick_seconds: float = 300.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_tick: Callable[[DerivativesTickResult], None] | None = None,
) -> None:
    """Lặp tới khi ``stopping`` được set; mỗi tick chỉ tải khi đã quá hạn làm mới."""
    import asyncio

    while not stopping.is_set():
        try:
            result = await collector.collect_once(clock())
        except Exception as error:  # noqa: BLE001 - lỗi DB không được giết vòng lặp
            logger.warning("derivatives collector lỗi: %s", type(error).__name__)
            result = DerivativesTickResult("error", detail=type(error).__name__)
        if on_tick is not None:
            on_tick(result)
        if result.status != "skipped_recent":
            logger.info(
                "derivatives: %s (%d symbol, lỗi %d)",
                result.status,
                result.stored,
                len(result.failed),
            )
        try:
            await asyncio.wait_for(stopping.wait(), timeout=tick_seconds)
        except TimeoutError:
            continue


__all__ = [
    "DerivativesCollector",
    "DerivativesTickResult",
    "run_derivatives_collector",
]
