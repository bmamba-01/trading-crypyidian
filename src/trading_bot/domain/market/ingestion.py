"""Hợp đồng domain cho ingestion thị trường, cursor và lineage phiên bản."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from trading_bot.domain.market.models import Instrument, OHLCVBar
from trading_bot.domain.market.validation import BarSeriesQuality, inspect_bar_series

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class MarketStream:
    """Danh tính nguồn và chuỗi cụ thể của một luồng thị trường."""

    source: str
    symbol: str
    timeframe: str

    def __post_init__(self) -> None:
        for name in ("source", "symbol", "timeframe"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")


@dataclass(frozen=True, slots=True)
class InstrumentObservation:
    """Quan sát quy cách hợp đồng kèm mốc thời gian nguồn metadata."""

    instrument: Instrument
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source không được để trống.")
        _require_aware(self.observed_at, "observed_at")

    def is_fresh(self, *, as_of: datetime, max_age: timedelta) -> bool:
        """Metadata tương lai hoặc quá tuổi không được dùng cho quyết định."""
        _require_aware(as_of, "as_of")
        if max_age < timedelta(0):
            raise ValueError("max_age không được âm.")
        return self.observed_at <= as_of and as_of - self.observed_at <= max_age


@dataclass(frozen=True, slots=True)
class ClockObservation:
    """Đối chiếu giờ do nguồn công bố với thời điểm nhận ở collector."""

    source: str
    source_time: datetime
    observed_at: datetime
    tolerance: timedelta

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source không được để trống.")
        _require_aware(self.source_time, "source_time")
        _require_aware(self.observed_at, "observed_at")
        if self.tolerance < timedelta(0):
            raise ValueError("tolerance không được âm.")

    @property
    def is_within_tolerance(self) -> bool:
        return abs(self.source_time - self.observed_at) <= self.tolerance


@dataclass(frozen=True, slots=True)
class IngestionCursor:
    """Tiến độ đã thu thập của một stream; token được xem là opaque."""

    stream: MarketStream
    token: str
    covered_until: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("token cursor không được để trống.")
        _require_aware(self.covered_until, "covered_until")
        _require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class IngestionGap:
    """Khoảng nửa mở chưa được lấp đầy trong một stream."""

    stream: MarketStream
    gap_start: datetime
    gap_end: datetime
    detected_at: datetime
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.gap_start, "gap_start")
        _require_aware(self.gap_end, "gap_end")
        _require_aware(self.detected_at, "detected_at")
        if self.gap_start >= self.gap_end:
            raise ValueError("gap_start phải trước gap_end.")
        if self.resolved_at is not None:
            _require_aware(self.resolved_at, "resolved_at")
            if self.resolved_at < self.detected_at:
                raise ValueError("resolved_at không được trước detected_at.")

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def overlaps(self, *, range_start: datetime, range_end: datetime) -> bool:
        _require_aware(range_start, "range_start")
        _require_aware(range_end, "range_end")
        return self.is_open and self.gap_start < range_end and range_start < self.gap_end


@dataclass(frozen=True, slots=True)
class IngestionRangeStatus:
    """Kết quả quality có stream context, cursor và gap tường minh."""

    stream: MarketStream
    range_start: datetime
    range_end: datetime
    as_of: datetime
    quality: BarSeriesQuality
    cursor: IngestionCursor | None
    open_gaps: tuple[IngestionGap, ...]

    @property
    def is_complete(self) -> bool:
        return (
            self.quality.is_usable
            and self.quality.backfill_complete is True
            and self.range_end <= self.as_of
            and self.cursor is not None
            and self.cursor.stream == self.stream
            and self.cursor.covered_until >= self.range_end
            and self.cursor.updated_at <= self.as_of
            and not self.open_gaps
        )


def assess_ingestion_range(
    *,
    stream: MarketStream,
    bars: tuple[OHLCVBar, ...],
    as_of: datetime,
    range_start: datetime,
    range_end: datetime,
    expected_interval: timedelta,
    max_event_age: timedelta,
    cursor: IngestionCursor | None,
    gaps: tuple[IngestionGap, ...] = (),
    backfill_complete: bool = False,
) -> IngestionRangeStatus:
    """Tính completeness cho một stream kể cả khi chưa có bản ghi nến."""
    _require_aware(as_of, "as_of")
    if cursor is not None and cursor.stream != stream:
        raise ValueError("Cursor không thuộc stream được yêu cầu.")
    if any(gap.stream != stream for gap in gaps):
        raise ValueError("Gap không thuộc stream được yêu cầu.")
    if any(bar.symbol != stream.symbol or bar.timeframe != stream.timeframe for bar in bars):
        raise ValueError("Bar không thuộc symbol/timeframe của stream được yêu cầu.")

    quality = inspect_bar_series(
        bars,
        as_of=as_of,
        expected_interval=expected_interval,
        max_event_age=max_event_age,
        expected_range_start=range_start,
        expected_range_end=range_end,
        backfill_complete=backfill_complete,
        expected_streams=((stream.symbol, stream.timeframe),),
    )
    open_gaps = tuple(
        gap for gap in gaps if gap.overlaps(range_start=range_start, range_end=range_end)
    )
    return IngestionRangeStatus(
        stream=stream,
        range_start=range_start,
        range_end=range_end,
        as_of=as_of,
        quality=quality,
        cursor=cursor,
        open_gaps=open_gaps,
    )


@dataclass(frozen=True, slots=True)
class MarketRecordVersion:
    """Bản ghi nguồn bất biến; correction tạo revision mới có parent rõ ràng."""

    record_id: UUID
    stream: MarketStream
    logical_key: str
    idempotency_key: str
    revision: int
    content_checksum: str
    event_at: datetime
    received_at: datetime
    available_at: datetime
    supersedes_record_id: UUID | None = None

    def __post_init__(self) -> None:
        for name in ("logical_key", "idempotency_key"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        if self.revision < 1:
            raise ValueError("revision phải bắt đầu từ 1.")
        if _SHA256_PATTERN.fullmatch(self.content_checksum) is None:
            raise ValueError("content_checksum phải là SHA-256 dạng hex chữ thường.")
        for name in ("event_at", "received_at", "available_at"):
            _require_aware(getattr(self, name), name)
        if self.event_at > self.received_at or self.received_at > self.available_at:
            raise ValueError("Timestamp phải thỏa event_at <= received_at <= available_at.")
        if self.revision == 1 and self.supersedes_record_id is not None:
            raise ValueError("Revision đầu tiên không có supersedes_record_id.")
        if self.revision > 1 and self.supersedes_record_id is None:
            raise ValueError("Revision sửa đổi cần supersedes_record_id.")


class IngestionDisposition(StrEnum):
    """Kết quả phân loại append trước khi repository ghi dữ liệu."""

    ACCEPT = "ACCEPT"
    DUPLICATE = "DUPLICATE"
    REVISION = "REVISION"
    CONFLICT = "CONFLICT"


def classify_market_ingestion(
    existing_versions: tuple[MarketRecordVersion, ...], candidate: MarketRecordVersion
) -> IngestionDisposition:
    """Phân loại retry/correction mà không sửa hoặc ghi đè phiên bản cũ."""
    same_source_key = tuple(
        version
        for version in existing_versions
        if version.stream.source == candidate.stream.source
        and version.idempotency_key == candidate.idempotency_key
    )
    if same_source_key:
        if (
            len(same_source_key) == 1
            and same_source_key[0].stream == candidate.stream
            and same_source_key[0].logical_key == candidate.logical_key
            and same_source_key[0].content_checksum == candidate.content_checksum
            and same_source_key[0].revision == candidate.revision
            and same_source_key[0].supersedes_record_id == candidate.supersedes_record_id
        ):
            return IngestionDisposition.DUPLICATE
        return IngestionDisposition.CONFLICT
    if any(version.record_id == candidate.record_id for version in existing_versions):
        return IngestionDisposition.CONFLICT

    history = tuple(
        sorted(
            (
                version
                for version in existing_versions
                if version.stream == candidate.stream
                and version.logical_key == candidate.logical_key
            ),
            key=lambda version: version.revision,
        )
    )
    if not history:
        if candidate.revision == 1 and candidate.supersedes_record_id is None:
            return IngestionDisposition.ACCEPT
        return IngestionDisposition.CONFLICT

    expected_revisions = tuple(range(1, len(history) + 1))
    actual_revisions = tuple(version.revision for version in history)
    if actual_revisions != expected_revisions:
        return IngestionDisposition.CONFLICT

    latest = history[-1]
    if (
        candidate.revision == latest.revision + 1
        and candidate.supersedes_record_id == latest.record_id
        and candidate.available_at > latest.available_at
    ):
        return IngestionDisposition.REVISION
    return IngestionDisposition.CONFLICT
