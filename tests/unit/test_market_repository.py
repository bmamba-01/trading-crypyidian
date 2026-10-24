from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from trading_bot.adapters.database.repositories.market_data import (
    MarketDataRepository,
    MarketRepositoryConflict,
    _instrument_checksum,
)
from trading_bot.domain.market.ingestion import (
    IngestionDisposition,
    IngestionGap,
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
)
from trading_bot.domain.market.models import Instrument, OHLCVBar


def instrument_observation(*, observed_at: datetime | None = None) -> InstrumentObservation:
    return InstrumentObservation(
        instrument=Instrument(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
        source="BINANCE_USDM",
        observed_at=observed_at or datetime(2026, 10, 22, tzinfo=UTC),
    )


def bar_and_version(
    *,
    revision: int = 1,
    parent_id: object | None = None,
    open_time: datetime | None = None,
    content_checksum: str = "a" * 64,
    idempotency_key: str = "binance:BTCUSDT:1m:1",
) -> tuple[OHLCVBar, MarketRecordVersion]:
    start = open_time or datetime(2026, 10, 22, tzinfo=UTC)
    event_at = start + timedelta(minutes=1)
    bar = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=start,
        close_time=event_at,
        event_at=event_at,
        received_at=event_at + timedelta(milliseconds=10),
        available_at=event_at + timedelta(milliseconds=20),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10.25000000"),
        is_closed=True,
        source="BINANCE_USDM",
        source_cursor=idempotency_key,
        checksum=content_checksum,
    )
    version = MarketRecordVersion(
        record_id=uuid4(),
        stream=MarketStream("BINANCE_USDM", "BTCUSDT", "1m"),
        logical_key=f"BTCUSDT:1m:{start.isoformat()}",
        idempotency_key=idempotency_key,
        revision=revision,
        content_checksum=content_checksum,
        event_at=bar.event_at,
        received_at=bar.received_at,
        available_at=bar.available_at,
        supersedes_record_id=parent_id,
    )
    return bar, version


class FakeSession:
    def __init__(self, scalar_results: list[object | None] | None = None) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.scalar_results)

    async def scalars(self, statement: object) -> SimpleNamespace:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=lambda: [])

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_bar_retry_is_idempotent_and_keeps_decimal_values() -> None:
    workspace_id = uuid4()
    instrument_id = uuid4()
    bar, version = bar_and_version()
    session = FakeSession([None, None])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    result = await repository.append_bar(
        workspace_id=workspace_id,
        instrument_id=instrument_id,
        bar=bar,
        version=version,
    )

    stored = session.added[-1]
    assert result.disposition is IngestionDisposition.ACCEPT
    assert stored.workspace_id == workspace_id
    assert stored.instrument_id == instrument_id
    assert stored.revision == 1
    assert stored.open == Decimal("100")
    assert stored.volume == Decimal("10.25000000")
    assert stored.available_at == bar.available_at


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_payload_is_conflict() -> None:
    bar, version = bar_and_version(content_checksum="a" * 64)
    existing = SimpleNamespace(
        checksum="b" * 64,
        revision=1,
        supersedes_record_id=None,
        logical_key=version.logical_key,
    )
    repository = MarketDataRepository(FakeSession([existing]))  # type: ignore[arg-type]

    with pytest.raises(MarketRepositoryConflict, match="idempotency"):
        await repository.append_bar(
            workspace_id=uuid4(), instrument_id=uuid4(), bar=bar, version=version
        )


@pytest.mark.asyncio
async def test_correction_appends_revision_with_supersedes_lineage() -> None:
    parent_id = uuid4()
    bar, version = bar_and_version(
        revision=2,
        parent_id=parent_id,
        content_checksum="b" * 64,
        idempotency_key="binance:BTCUSDT:1m:1-correction",
    )
    latest = SimpleNamespace(
        id=parent_id,
        logical_key=version.logical_key,
        revision=1,
        available_at=bar.available_at - timedelta(milliseconds=1),
    )
    session = FakeSession([None, latest])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    result = await repository.append_bar(
        workspace_id=uuid4(), instrument_id=uuid4(), bar=bar, version=version
    )

    stored = session.added[-1]
    assert result.disposition is IngestionDisposition.REVISION
    assert stored.revision == 2
    assert stored.supersedes_id == parent_id


@pytest.mark.asyncio
async def test_as_of_query_excludes_unavailable_rows_and_carries_range_scope() -> None:
    workspace_id = uuid4()
    as_of = datetime(2026, 10, 22, 1, tzinfo=UTC)
    session = FakeSession()
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    bars = await repository.list_bars(
        workspace_id=workspace_id,
        symbol="BTCUSDT",
        timeframe="1m",
        start=as_of - timedelta(hours=1),
        end=as_of,
        as_of=as_of,
    )

    assert bars == []
    sql = session.statements[0]
    assert "market_bar.workspace_id" in sql
    assert "market_bar.available_at <=" in sql
    assert "market_bar.open_time <" in sql


@pytest.mark.asyncio
async def test_instrument_metadata_has_idempotency_and_as_of_visibility() -> None:
    workspace_id = uuid4()
    observation = instrument_observation()
    session = FakeSession([None, None])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    result = await repository.append_instrument(
        workspace_id=workspace_id,
        observation=observation,
        idempotency_key="exchange-info:2026-10-22T00:00Z",
        received_at=observation.observed_at + timedelta(milliseconds=10),
        available_at=observation.observed_at + timedelta(milliseconds=20),
    )

    stored = session.added[-1]
    assert result.disposition is IngestionDisposition.ACCEPT
    assert stored.event_at == observation.observed_at
    assert stored.received_at == observation.observed_at + timedelta(milliseconds=10)
    assert stored.available_at == observation.observed_at + timedelta(milliseconds=20)
    assert stored.workspace_id == workspace_id
    assert stored.idempotency_key == "exchange-info:2026-10-22T00:00Z"


@pytest.mark.asyncio
async def test_instrument_timestamps_must_be_causal() -> None:
    observation = instrument_observation()
    repository = MarketDataRepository(FakeSession([None]))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="event_at <= received_at <= available_at"):
        await repository.append_instrument(
            workspace_id=uuid4(),
            observation=observation,
            idempotency_key="exchange-info:causal",
            received_at=observation.observed_at - timedelta(milliseconds=1),
            available_at=observation.observed_at,
        )


@pytest.mark.asyncio
async def test_concurrent_duplicate_bar_insert_returns_duplicate_after_unique_race() -> None:
    bar, version = bar_and_version()
    duplicate = SimpleNamespace(
        id=version.record_id,
        checksum=version.content_checksum,
        revision=version.revision,
        supersedes_id=version.supersedes_record_id,
        logical_key=version.logical_key,
        event_at=version.event_at,
        received_at=version.received_at,
        available_at=version.available_at,
    )

    class RacingSession(FakeSession):
        async def flush(self) -> None:
            raise IntegrityError("insert", {}, RuntimeError("unique violation"))

    session = RacingSession([None, None, duplicate])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    result = await repository.append_bar(
        workspace_id=uuid4(), instrument_id=uuid4(), bar=bar, version=version
    )

    assert result.disposition is IngestionDisposition.DUPLICATE
    assert result.record_id == version.record_id


@pytest.mark.asyncio
async def test_concurrent_duplicate_instrument_insert_returns_duplicate() -> None:
    observation = instrument_observation()
    duplicate = SimpleNamespace(id=uuid4(), checksum=_instrument_checksum(observation))

    class RacingSession(FakeSession):
        async def flush(self) -> None:
            raise IntegrityError("insert", {}, RuntimeError("unique violation"))

    session = RacingSession([None, None, duplicate])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    result = await repository.append_instrument(
        workspace_id=uuid4(),
        observation=observation,
        idempotency_key="exchange-info:race",
    )

    assert result.disposition is IngestionDisposition.DUPLICATE
    assert result.record_id == duplicate.id


@pytest.mark.asyncio
async def test_concurrent_duplicate_raw_reference_returns_duplicate() -> None:
    duplicate = SimpleNamespace(
        id=uuid4(),
        checksum="a" * 64,
        object_key="market/raw/abc.json",
        byte_length=123,
        content_type="application/json",
    )

    class RacingSession(FakeSession):
        async def flush(self) -> None:
            raise IntegrityError("insert", {}, RuntimeError("unique violation"))

    session = RacingSession([None, duplicate])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    result = await repository.append_raw_reference(
        workspace_id=uuid4(),
        source="BINANCE_USDM",
        idempotency_key="raw:exchange-info:race",
        object_key="market/raw/abc.json",
        checksum="a" * 64,
        byte_length=123,
        content_type="application/json",
    )

    assert result.disposition is IngestionDisposition.DUPLICATE
    assert result.record_id == duplicate.id


@pytest.mark.asyncio
async def test_gap_can_be_resolved_once_with_workspace_and_stream_scope() -> None:
    workspace_id = uuid4()
    stream = MarketStream("BINANCE_USDM", "BTCUSDT", "1m")
    gap_start = datetime(2026, 10, 22, tzinfo=UTC)
    gap = IngestionGap(
        stream=stream,
        gap_start=gap_start,
        gap_end=gap_start + timedelta(minutes=1),
        detected_at=gap_start + timedelta(minutes=2),
    )
    existing = SimpleNamespace(
        id=uuid4(),
        resolved_at=None,
        detected_at=gap.detected_at,
    )
    resolved_at = gap.detected_at + timedelta(minutes=1)
    session = FakeSession([existing])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]

    resolved_id = await repository.resolve_gap(
        workspace_id=workspace_id,
        gap=gap,
        resolved_at=resolved_at,
    )

    assert resolved_id == existing.id
    assert existing.resolved_at == resolved_at


@pytest.mark.asyncio
async def test_gap_resolution_timestamp_cannot_be_changed() -> None:
    workspace_id = uuid4()
    stream = MarketStream("BINANCE_USDM", "BTCUSDT", "1m")
    gap_start = datetime(2026, 10, 22, tzinfo=UTC)
    gap = IngestionGap(
        stream=stream,
        gap_start=gap_start,
        gap_end=gap_start + timedelta(minutes=1),
        detected_at=gap_start + timedelta(minutes=2),
    )
    original_resolution = gap.detected_at + timedelta(minutes=1)
    existing = SimpleNamespace(
        id=uuid4(),
        resolved_at=original_resolution,
        detected_at=gap.detected_at,
    )
    repository = MarketDataRepository(FakeSession([existing]))  # type: ignore[arg-type]

    with pytest.raises(MarketRepositoryConflict, match="không thể sửa"):
        await repository.resolve_gap(
            workspace_id=workspace_id,
            gap=gap,
            resolved_at=original_resolution + timedelta(minutes=1),
        )

    assert existing.resolved_at == original_resolution
