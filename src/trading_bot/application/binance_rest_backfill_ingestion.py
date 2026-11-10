"""Điều phối backfill REST và lưu nến/checkpoint trong cùng Unit of Work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from trading_bot.application.binance_rest_backfill import (
    BinanceRestBackfillRunner,
    RestBackfillBatch,
    RestBackfillCheckpoint,
    RestBackfillError,
    RestBackfillRequest,
)
from trading_bot.domain.market.ingestion import IngestionCursor, MarketRecordVersion, MarketStream
from trading_bot.domain.market.models import OHLCVBar


class RestBackfillIngestionError(RuntimeError):
    """Checkpoint hoặc dữ liệu append không khớp với yêu cầu backfill."""


class RestBackfillRepository(Protocol):
    """Các thao tác persistence phải dùng chung transaction của caller."""

    async def get_cursor(
        self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
    ) -> IngestionCursor | None: ...

    async def append_bar(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        bar: OHLCVBar,
        version: MarketRecordVersion,
    ) -> object: ...

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None: ...


class BinanceRestBackfillIngestionService:
    """Nạp trang REST vào kho market và lưu cursor có thể khôi phục.

    Caller phải giữ `repository` trong một transaction/Unit of Work. Nến và checkpoint được
    flush cùng transaction để retry sau rollback chỉ append lại cùng idempotency key.
    """

    def __init__(
        self,
        *,
        runner: BinanceRestBackfillRunner,
        repository: RestBackfillRepository,
        workspace_id: UUID,
        instrument_id: UUID,
    ) -> None:
        if not isinstance(workspace_id, UUID) or not isinstance(instrument_id, UUID):
            raise ValueError("workspace_id và instrument_id phải là UUID.")
        self._runner = runner
        self._repository = repository
        self._workspace_id = workspace_id
        self._instrument_id = instrument_id

    @staticmethod
    def cursor_stream(request: RestBackfillRequest) -> MarketStream:
        """Phân vùng cursor theo request để các job lịch sử không dùng chung tiến độ."""
        return MarketStream(
            source=f"BINANCE_RB_{request.fingerprint}",
            symbol=request.symbol,
            timeframe=request.interval,
        )

    async def run_batch(
        self,
        request: RestBackfillRequest,
        *,
        now: datetime,
        max_pages: int = 10,
        checkpoint: RestBackfillCheckpoint | None = None,
    ) -> RestBackfillBatch:
        """Tải batch tiếp theo, append nến rồi flush checkpoint cùng Unit of Work."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        instant = now.astimezone(UTC)
        cursor_stream = self.cursor_stream(request)
        saved_cursor = await self._repository.get_cursor(
            workspace_id=self._workspace_id,
            stream=cursor_stream,
            as_of=instant,
        )
        if checkpoint is not None and saved_cursor is not None:
            persisted = _checkpoint_from_cursor(saved_cursor, request)
            if persisted.to_json() != checkpoint.to_json():
                raise RestBackfillIngestionError("Checkpoint truyền vào cũ hơn trạng thái đã lưu.")
        elif checkpoint is None and saved_cursor is not None:
            checkpoint = _checkpoint_from_cursor(saved_cursor, request)

        batch = await self._runner.run_batch(
            request,
            now=instant,
            max_pages=max_pages,
            checkpoint=checkpoint,
        )
        for bar in batch.bars:
            versioned_bar, version = _make_version(
                workspace_id=self._workspace_id,
                bar=bar,
            )
            await self._repository.append_bar(
                workspace_id=self._workspace_id,
                instrument_id=self._instrument_id,
                bar=versioned_bar,
                version=version,
            )
        await self._repository.save_cursor(
            workspace_id=self._workspace_id,
            cursor=IngestionCursor(
                stream=cursor_stream,
                token=batch.checkpoint.to_json().decode("utf-8"),
                covered_until=batch.checkpoint.next_open_time,
                updated_at=instant,
            ),
        )
        return batch


def _checkpoint_from_cursor(
    cursor: IngestionCursor, request: RestBackfillRequest
) -> RestBackfillCheckpoint:
    try:
        checkpoint = RestBackfillCheckpoint.from_json(cursor.token.encode("utf-8"))
    except (UnicodeEncodeError, ValueError, RestBackfillError) as error:
        raise RestBackfillIngestionError("Checkpoint persistence không hợp lệ.") from error
    if checkpoint.request_fingerprint != request.fingerprint:
        raise RestBackfillIngestionError("Checkpoint persistence thuộc request khác.")
    if cursor.covered_until != checkpoint.next_open_time:
        raise RestBackfillIngestionError("Cursor persistence không khớp checkpoint.")
    return checkpoint


def _make_version(*, workspace_id: UUID, bar: OHLCVBar) -> tuple[OHLCVBar, MarketRecordVersion]:
    content_checksum = _bar_content_checksum(bar)
    versioned_bar = replace(bar, checksum=content_checksum)
    logical_key = (
        f"{bar.source}:{bar.symbol}:{bar.timeframe}:{bar.open_time.astimezone(UTC).isoformat()}"
    )
    idempotency_key = logical_key
    stream = MarketStream(bar.source, bar.symbol, bar.timeframe)
    record_id = uuid5(
        NAMESPACE_URL,
        f"crypidian-market-bar:{workspace_id}:{bar.source}:{logical_key}",
    )
    return versioned_bar, MarketRecordVersion(
        record_id=record_id,
        stream=stream,
        logical_key=logical_key,
        idempotency_key=idempotency_key,
        revision=1,
        content_checksum=content_checksum,
        event_at=bar.event_at,
        received_at=bar.received_at,
        available_at=bar.available_at,
    )


def _bar_content_checksum(bar: OHLCVBar) -> str:
    payload = {
        "source": bar.source,
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "open_time": bar.open_time.astimezone(UTC).isoformat(),
        "close_time": bar.close_time.astimezone(UTC).isoformat(),
        "event_at": bar.event_at.astimezone(UTC).isoformat(),
        "received_at": bar.received_at.astimezone(UTC).isoformat(),
        "available_at": bar.available_at.astimezone(UTC).isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "quote_volume": None if bar.quote_volume is None else str(bar.quote_volume),
        "source_cursor": bar.source_cursor,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
