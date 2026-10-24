"""Kho lưu dữ liệu thị trường có tenant scope và truy vấn point-in-time."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    IngestionCursor as IngestionCursorRow,
)
from trading_bot.adapters.database.models import (
    IngestionGap as IngestionGapRow,
)
from trading_bot.adapters.database.models import (
    MarketBar,
    MarketFunding,
    MarketInstrument,
    MarketOpenInterest,
    MarketRawReference,
)
from trading_bot.domain.market.ingestion import (
    IngestionCursor,
    IngestionDisposition,
    IngestionGap,
    IngestionRangeStatus,
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
    assess_ingestion_range,
)
from trading_bot.domain.market.models import OHLCVBar


class MarketRepositoryConflict(RuntimeError):
    """Từ chối xung đột idempotency, lineage rỗng hoặc cursor lùi."""


@dataclass(frozen=True, slots=True)
class MarketAppendResult:
    """Kết quả append bất biến và định danh bản ghi đang được giữ."""

    disposition: IngestionDisposition
    record_id: UUID


class MarketDataRepository:
    """Mỗi thao tác nhận workspace tường minh; transaction do caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_instrument(
        self,
        *,
        workspace_id: UUID,
        observation: InstrumentObservation,
        idempotency_key: str,
        received_at: datetime | None = None,
        available_at: datetime | None = None,
    ) -> MarketAppendResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key không được để trống.")
        received_at = received_at or datetime.now(UTC)
        available_at = available_at or received_at
        _validate_causal_timestamps(observation.observed_at, received_at, available_at)
        instrument = observation.instrument
        checksum = _instrument_checksum(observation)
        existing = await self._session.scalar(
            select(MarketInstrument).where(
                MarketInstrument.workspace_id == workspace_id,
                MarketInstrument.source == observation.source,
                MarketInstrument.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.checksum == checksum:
                return MarketAppendResult(IngestionDisposition.DUPLICATE, existing.id)
            raise MarketRepositoryConflict("idempotency key metadata đã có payload khác.")
        latest = await self._session.scalar(
            select(MarketInstrument)
            .where(
                MarketInstrument.workspace_id == workspace_id,
                MarketInstrument.source == observation.source,
                MarketInstrument.symbol == instrument.symbol,
            )
            .order_by(MarketInstrument.revision.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is not None and observation.observed_at <= latest.event_at:
            raise MarketRepositoryConflict("metadata mới phải sau phiên bản trước.")
        row = MarketInstrument(
            workspace_id=workspace_id,
            source=observation.source,
            idempotency_key=idempotency_key,
            symbol=instrument.symbol,
            base_asset=instrument.base_asset,
            quote_asset=instrument.quote_asset,
            tick_size=instrument.tick_size,
            quantity_step=instrument.quantity_step,
            min_notional=instrument.min_notional,
            status=instrument.status,
            revision=latest.revision + 1 if latest else 1,
            checksum=checksum,
            supersedes_id=latest.id if latest else None,
            event_at=observation.observed_at,
            received_at=received_at,
            available_at=available_at,
        )
        duplicate = await self._insert_or_get_duplicate(
            row=row,
            lookup=select(MarketInstrument).where(
                MarketInstrument.workspace_id == workspace_id,
                MarketInstrument.source == observation.source,
                MarketInstrument.idempotency_key == idempotency_key,
            ),
            matches=lambda existing: existing.checksum == checksum,
            conflict_message="idempotency key metadata có payload khác hoặc revision xung đột.",
        )
        if duplicate is not None:
            return duplicate
        disposition = IngestionDisposition.REVISION if latest else IngestionDisposition.ACCEPT
        return MarketAppendResult(disposition, row.id)

    async def append_bar(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        bar: OHLCVBar,
        version: MarketRecordVersion,
        raw_reference_id: UUID | None = None,
    ) -> MarketAppendResult:
        _validate_bar_version(bar, version)
        existing = await self._session.scalar(
            select(MarketBar).where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.source == version.stream.source,
                MarketBar.idempotency_key == version.idempotency_key,
            )
        )
        if existing is not None:
            if _same_version(existing, version):
                return MarketAppendResult(IngestionDisposition.DUPLICATE, existing.id)
            raise MarketRepositoryConflict("idempotency key của bar đã có payload khác.")
        latest = await self._session.scalar(
            select(MarketBar)
            .where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.logical_key == version.logical_key,
            )
            .order_by(MarketBar.revision.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is None:
            if version.revision != 1 or version.supersedes_record_id is not None:
                raise MarketRepositoryConflict("revision đầu phải là 1 và không có parent.")
            disposition = IngestionDisposition.ACCEPT
        else:
            if (
                version.revision != latest.revision + 1
                or version.supersedes_record_id != latest.id
                or version.available_at <= latest.available_at
            ):
                raise MarketRepositoryConflict("revision mới phải nối tiếp bản ghi gần nhất.")
            disposition = IngestionDisposition.REVISION
        row = MarketBar(
            id=version.record_id,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            source=version.stream.source,
            logical_key=version.logical_key,
            idempotency_key=version.idempotency_key,
            revision=version.revision,
            checksum=version.content_checksum,
            supersedes_id=version.supersedes_record_id,
            raw_reference_id=raw_reference_id,
            source_cursor=bar.source_cursor,
            open_time=bar.open_time,
            close_time=bar.close_time,
            event_at=bar.event_at,
            received_at=bar.received_at,
            available_at=bar.available_at,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            is_closed=bar.is_closed,
        )
        duplicate = await self._insert_or_get_duplicate(
            row=row,
            lookup=select(MarketBar).where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.source == version.stream.source,
                MarketBar.idempotency_key == version.idempotency_key,
            ),
            matches=lambda existing: _same_version(existing, version),
            conflict_message="idempotency key hoặc revision bar xung đột khi ghi đồng thời.",
        )
        if duplicate is not None:
            return duplicate
        return MarketAppendResult(disposition, row.id)

    async def list_bars(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
        closed_only: bool = False,
    ) -> list[OHLCVBar]:
        _validate_range(start, end, as_of)
        query = select(MarketBar).where(
            MarketBar.workspace_id == workspace_id,
            MarketBar.symbol == symbol,
            MarketBar.timeframe == timeframe,
            MarketBar.open_time >= start,
            MarketBar.open_time < end,
            MarketBar.available_at <= as_of,
        )
        if closed_only:
            query = query.where(MarketBar.is_closed.is_(True))
        records = await self._session.scalars(
            query.order_by(MarketBar.open_time, MarketBar.revision.desc())
        )
        latest_by_key: dict[str, MarketBar] = {}
        for record in records.all():
            latest_by_key.setdefault(record.logical_key, record)
        return [_to_domain_bar(row) for row in latest_by_key.values()]

    async def get_instrument(
        self, *, workspace_id: UUID, source: str, symbol: str, as_of: datetime
    ) -> MarketInstrument | None:
        _require_aware(as_of, "as_of")
        return await self._session.scalar(
            select(MarketInstrument)
            .where(
                MarketInstrument.workspace_id == workspace_id,
                MarketInstrument.source == source,
                MarketInstrument.symbol == symbol,
                MarketInstrument.available_at <= as_of,
            )
            .order_by(MarketInstrument.revision.desc())
            .limit(1)
        )

    async def append_raw_reference(
        self,
        *,
        workspace_id: UUID,
        source: str,
        idempotency_key: str,
        object_key: str,
        checksum: str,
        byte_length: int,
        content_type: str,
    ) -> MarketAppendResult:
        existing = await self._session.scalar(
            select(MarketRawReference).where(
                MarketRawReference.workspace_id == workspace_id,
                MarketRawReference.source == source,
                MarketRawReference.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.checksum == checksum and existing.object_key == object_key:
                return MarketAppendResult(IngestionDisposition.DUPLICATE, existing.id)
            raise MarketRepositoryConflict("idempotency key raw reference có payload khác.")
        row = MarketRawReference(
            workspace_id=workspace_id,
            source=source,
            idempotency_key=idempotency_key,
            object_key=object_key,
            checksum=checksum,
            byte_length=byte_length,
            content_type=content_type,
        )
        duplicate = await self._insert_or_get_duplicate(
            row=row,
            lookup=select(MarketRawReference).where(
                MarketRawReference.workspace_id == workspace_id,
                MarketRawReference.source == source,
                MarketRawReference.idempotency_key == idempotency_key,
            ),
            matches=lambda existing: (
                existing.checksum == checksum
                and existing.object_key == object_key
                and existing.byte_length == byte_length
                and existing.content_type == content_type
            ),
            conflict_message=(
                "idempotency key raw reference có payload khác hoặc bị ghi đồng thời xung đột."
            ),
        )
        if duplicate is not None:
            return duplicate
        return MarketAppendResult(IngestionDisposition.ACCEPT, row.id)

    async def get_cursor(
        self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
    ) -> IngestionCursor | None:
        _require_aware(as_of, "as_of")
        row = await self._session.scalar(
            select(IngestionCursorRow).where(
                IngestionCursorRow.workspace_id == workspace_id,
                IngestionCursorRow.source == stream.source,
                IngestionCursorRow.symbol == stream.symbol,
                IngestionCursorRow.timeframe == stream.timeframe,
                IngestionCursorRow.updated_at <= as_of,
            )
        )
        if row is None:
            return None
        return IngestionCursor(
            stream=stream,
            token=row.token,
            covered_until=row.covered_until,
            updated_at=row.updated_at,
        )

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None:
        stream = cursor.stream
        current = await self._session.scalar(
            select(IngestionCursorRow)
            .where(
                IngestionCursorRow.workspace_id == workspace_id,
                IngestionCursorRow.source == stream.source,
                IngestionCursorRow.symbol == stream.symbol,
                IngestionCursorRow.timeframe == stream.timeframe,
            )
            .with_for_update()
        )
        if current is None:
            self._session.add(
                IngestionCursorRow(
                    workspace_id=workspace_id,
                    source=stream.source,
                    symbol=stream.symbol,
                    timeframe=stream.timeframe,
                    token=cursor.token,
                    covered_until=cursor.covered_until,
                    updated_at=cursor.updated_at,
                )
            )
        else:
            if (
                cursor.covered_until < current.covered_until
                or cursor.updated_at < current.updated_at
            ):
                raise MarketRepositoryConflict(
                    "cursor không được lùi covered_until hoặc updated_at."
                )
            current.token = cursor.token
            current.covered_until = cursor.covered_until
            current.updated_at = cursor.updated_at
        await self._session.flush()

    async def append_gap(self, *, workspace_id: UUID, gap: IngestionGap) -> UUID:
        stream = gap.stream
        existing = await self._session.scalar(
            select(IngestionGapRow).where(
                IngestionGapRow.workspace_id == workspace_id,
                IngestionGapRow.source == stream.source,
                IngestionGapRow.symbol == stream.symbol,
                IngestionGapRow.timeframe == stream.timeframe,
                IngestionGapRow.gap_start == gap.gap_start,
                IngestionGapRow.gap_end == gap.gap_end,
            )
        )
        if existing is not None:
            return existing.id
        row = IngestionGapRow(
            workspace_id=workspace_id,
            source=stream.source,
            symbol=stream.symbol,
            timeframe=stream.timeframe,
            gap_start=gap.gap_start,
            gap_end=gap.gap_end,
            detected_at=gap.detected_at,
            resolved_at=gap.resolved_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def resolve_gap(
        self, *, workspace_id: UUID, gap: IngestionGap, resolved_at: datetime
    ) -> UUID:
        """Ghi nhận một lần lấp khoảng trống, không cho đổi trạng thái đã resolve."""
        _require_aware(resolved_at, "resolved_at")
        if resolved_at < gap.detected_at:
            raise ValueError("resolved_at không được trước detected_at.")
        row = await self._session.scalar(
            select(IngestionGapRow)
            .where(
                IngestionGapRow.workspace_id == workspace_id,
                IngestionGapRow.source == gap.stream.source,
                IngestionGapRow.symbol == gap.stream.symbol,
                IngestionGapRow.timeframe == gap.stream.timeframe,
                IngestionGapRow.gap_start == gap.gap_start,
                IngestionGapRow.gap_end == gap.gap_end,
            )
            .with_for_update()
        )
        if row is None:
            raise MarketRepositoryConflict("Không tìm thấy gap trong workspace/stream đã yêu cầu.")
        if row.resolved_at is not None:
            if row.resolved_at == resolved_at:
                return row.id
            raise MarketRepositoryConflict("Gap đã được resolve và không thể sửa thời điểm.")
        row.resolved_at = resolved_at
        await self._session.flush()
        return row.id

    async def list_gaps(
        self,
        *,
        workspace_id: UUID,
        stream: MarketStream,
        range_start: datetime,
        range_end: datetime,
        as_of: datetime,
    ) -> tuple[IngestionGap, ...]:
        _validate_range(range_start, range_end, as_of)
        records = await self._session.scalars(
            select(IngestionGapRow)
            .where(
                IngestionGapRow.workspace_id == workspace_id,
                IngestionGapRow.source == stream.source,
                IngestionGapRow.symbol == stream.symbol,
                IngestionGapRow.timeframe == stream.timeframe,
                IngestionGapRow.gap_start < range_end,
                IngestionGapRow.gap_end > range_start,
                IngestionGapRow.detected_at <= as_of,
                or_(IngestionGapRow.resolved_at.is_(None), IngestionGapRow.resolved_at > as_of),
            )
            .order_by(IngestionGapRow.gap_start)
        )
        return tuple(
            IngestionGap(
                stream=stream,
                gap_start=row.gap_start,
                gap_end=row.gap_end,
                detected_at=row.detected_at,
                resolved_at=row.resolved_at,
            )
            for row in records.all()
        )

    async def assess_range(
        self,
        *,
        workspace_id: UUID,
        stream: MarketStream,
        range_start: datetime,
        range_end: datetime,
        as_of: datetime,
        expected_interval: timedelta,
        max_event_age: timedelta,
        backfill_complete: bool = False,
    ) -> IngestionRangeStatus:
        bars = await self.list_bars(
            workspace_id=workspace_id,
            symbol=stream.symbol,
            timeframe=stream.timeframe,
            start=range_start,
            end=range_end,
            as_of=as_of,
        )
        cursor = await self.get_cursor(workspace_id=workspace_id, stream=stream, as_of=as_of)
        gaps = await self.list_gaps(
            workspace_id=workspace_id,
            stream=stream,
            range_start=range_start,
            range_end=range_end,
            as_of=as_of,
        )
        return assess_ingestion_range(
            stream=stream,
            bars=tuple(bars),
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            expected_interval=expected_interval,
            max_event_age=max_event_age,
            cursor=cursor,
            gaps=gaps,
            backfill_complete=backfill_complete,
        )

    async def append_funding_rate(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        version: MarketRecordVersion,
        funding_time: datetime,
        funding_rate: Decimal,
    ) -> MarketAppendResult:
        return await self._append_point_record(
            model=MarketFunding,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            version=version,
            point_field="funding_time",
            point_time=funding_time,
            value_field="funding_rate",
            value=funding_rate,
        )

    async def append_open_interest(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        version: MarketRecordVersion,
        observed_at: datetime,
        quantity: Decimal,
    ) -> MarketAppendResult:
        return await self._append_point_record(
            model=MarketOpenInterest,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            version=version,
            point_field="observed_at",
            point_time=observed_at,
            value_field="quantity",
            value=quantity,
        )

    async def _append_point_record(
        self,
        *,
        model: Any,
        workspace_id: UUID,
        instrument_id: UUID,
        version: MarketRecordVersion,
        point_field: str,
        point_time: datetime,
        value_field: str,
        value: Decimal,
    ) -> MarketAppendResult:
        _require_aware(point_time, point_field)
        if not value.is_finite():
            raise ValueError("Market numeric value phải hữu hạn.")
        existing = await self._session.scalar(
            select(model).where(
                model.workspace_id == workspace_id,
                model.source == version.stream.source,
                model.idempotency_key == version.idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.checksum == version.content_checksum
                and existing.revision == version.revision
                and existing.supersedes_id == version.supersedes_record_id
                and getattr(existing, point_field) == point_time
                and getattr(existing, value_field) == value
            ):
                return MarketAppendResult(IngestionDisposition.DUPLICATE, existing.id)
            raise MarketRepositoryConflict("idempotency key market record có payload khác.")
        latest = await self._session.scalar(
            select(model)
            .where(
                model.workspace_id == workspace_id,
                getattr(model, point_field) == point_time,
                model.instrument_id == instrument_id,
                model.source == version.stream.source,
            )
            .order_by(model.revision.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is None:
            if version.revision != 1 or version.supersedes_record_id is not None:
                raise MarketRepositoryConflict("revision đầu phải là 1 và không có parent.")
            disposition = IngestionDisposition.ACCEPT
        else:
            if (
                version.revision != latest.revision + 1
                or version.supersedes_record_id != latest.id
                or version.available_at <= latest.available_at
            ):
                raise MarketRepositoryConflict("revision mới phải nối tiếp bản ghi gần nhất.")
            disposition = IngestionDisposition.REVISION
        row = model(
            id=version.record_id,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            source=version.stream.source,
            idempotency_key=version.idempotency_key,
            revision=version.revision,
            checksum=version.content_checksum,
            supersedes_id=version.supersedes_record_id,
            **{point_field: point_time, value_field: value},
            event_at=version.event_at,
            received_at=version.received_at,
            available_at=version.available_at,
        )
        duplicate = await self._insert_or_get_duplicate(
            row=row,
            lookup=select(model).where(
                model.workspace_id == workspace_id,
                model.source == version.stream.source,
                model.idempotency_key == version.idempotency_key,
            ),
            matches=lambda existing: (
                existing.checksum == version.content_checksum
                and existing.revision == version.revision
                and existing.supersedes_id == version.supersedes_record_id
                and getattr(existing, point_field) == point_time
                and getattr(existing, value_field) == value
            ),
            conflict_message=(
                "idempotency key hoặc revision market record xung đột khi ghi đồng thời."
            ),
        )
        if duplicate is not None:
            return duplicate
        return MarketAppendResult(disposition, row.id)

    async def _insert_or_get_duplicate(
        self,
        *,
        row: Any,
        lookup: Any,
        matches: Any,
        conflict_message: str,
    ) -> MarketAppendResult | None:
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._session.scalar(lookup)
            if existing is not None and matches(existing):
                return MarketAppendResult(IngestionDisposition.DUPLICATE, existing.id)
            raise MarketRepositoryConflict(conflict_message) from error
        return None


def _instrument_checksum(observation: InstrumentObservation) -> str:
    instrument = observation.instrument
    payload = {
        "source": observation.source,
        "observed_at": observation.observed_at.isoformat(),
        "symbol": instrument.symbol,
        "base_asset": instrument.base_asset,
        "quote_asset": instrument.quote_asset,
        "tick_size": str(instrument.tick_size),
        "quantity_step": str(instrument.quantity_step),
        "min_notional": str(instrument.min_notional),
        "status": instrument.status,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _same_version(record: MarketBar, version: MarketRecordVersion) -> bool:
    return (
        record.logical_key == version.logical_key
        and record.checksum == version.content_checksum
        and record.revision == version.revision
        and record.supersedes_id == version.supersedes_record_id
        and record.available_at == version.available_at
    )


def _validate_causal_timestamps(
    event_at: datetime, received_at: datetime, available_at: datetime
) -> None:
    for name, value in (
        ("event_at", event_at),
        ("received_at", received_at),
        ("available_at", available_at),
    ):
        _require_aware(value, name)
    if event_at > received_at or received_at > available_at:
        raise ValueError("Timestamp phải thỏa event_at <= received_at <= available_at.")


def _validate_bar_version(bar: OHLCVBar, version: MarketRecordVersion) -> None:
    if (version.stream.source, version.stream.symbol, version.stream.timeframe) != (
        bar.source,
        bar.symbol,
        bar.timeframe,
    ):
        raise ValueError("Version phải thuộc cùng source/symbol/timeframe với bar.")
    if (version.event_at, version.received_at, version.available_at) != (
        bar.event_at,
        bar.received_at,
        bar.available_at,
    ):
        raise ValueError("Timestamp lineage phải khớp OHLCVBar.")
    if bar.checksum is not None and bar.checksum != version.content_checksum:
        raise ValueError("Checksum version phải khớp OHLCVBar.")


def _to_domain_bar(row: MarketBar) -> OHLCVBar:
    return OHLCVBar(
        symbol=row.symbol,
        timeframe=row.timeframe,
        open_time=row.open_time,
        close_time=row.close_time,
        event_at=row.event_at,
        received_at=row.received_at,
        available_at=row.available_at,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        is_closed=row.is_closed,
        source=row.source,
        source_cursor=row.source_cursor,
        checksum=row.checksum,
    )


def _validate_range(start: datetime, end: datetime, as_of: datetime) -> None:
    for name, value in (("start", start), ("end", end), ("as_of", as_of)):
        _require_aware(value, name)
    if start >= end:
        raise ValueError("range start phải trước end.")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
