from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from trading_bot.adapters.database.models.learning_source_bar_entities import (
    BotDecisionSourceBarRecord,
)
from trading_bot.adapters.database.repositories.bot_decision_source_bars import (
    BotDecisionSourceBarConflict,
    append_decision_source_bars,
)
from trading_bot.application.bot_dispatcher import BotDecisionStatus
from trading_bot.application.decision_snapshot_builder import build_decision_snapshot_from_bars
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BASE_TIME = datetime(2026, 11, 8, 0, tzinfo=UTC)
SOURCE = "BINANCE_USDM_REST_BACKFILL"


class _Mappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.added: list[object] = []
        self.statements: list[object] = []
        self.flush_count = 0

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)

    def add_all(self, records: list[object]) -> None:
        self.added.extend(records)

    async def flush(self) -> None:
        self.flush_count += 1


def _bar(timeframe: str, index: int) -> OHLCVBar:
    interval = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    opened = BASE_TIME + interval * index
    closed = opened + interval - timedelta(milliseconds=1)
    record_id = uuid5(WORKSPACE_ID, f"{timeframe}:{opened.isoformat()}")
    checksum = f"{index + 1:064x}"
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal("100") + index,
        high=Decimal("102") + index,
        low=Decimal("99") + index,
        close=Decimal("101") + index,
        volume=Decimal("2"),
        quote_volume=Decimal("202"),
        is_closed=True,
        source=SOURCE,
        source_cursor=f"{timeframe}:{opened.isoformat()}",
        checksum=checksum,
        raw_reference=None,
        source_identity=MarketBarSourceIdentity(
            workspace_id=WORKSPACE_ID,
            record_id=record_id,
            logical_key=f"market:{timeframe}:{opened.isoformat()}",
            revision=1,
            supersedes_record_id=None,
            checksum=checksum,
        ),
    )


def _snapshot():
    bars_by_timeframe = {
        "1h": tuple(_bar("1h", index) for index in range(8)),
        "4h": tuple(_bar("4h", index) for index in range(2)),
    }
    decision_time = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)
    return build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe=bars_by_timeframe,
        position_is_flat=True,
        source_identity_required=True,
    )


def _market_row(bar: OHLCVBar, **overrides: object) -> dict[str, object]:
    assert bar.source_identity is not None
    row: dict[str, object] = {
        "id": bar.source_identity.record_id,
        "workspace_id": bar.source_identity.workspace_id,
        "logical_key": bar.source_identity.logical_key,
        "revision": bar.source_identity.revision,
        "supersedes_id": bar.source_identity.supersedes_record_id,
        "checksum": bar.source_identity.checksum,
        "raw_reference_id": None,
        "source": bar.source,
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "open_time": bar.open_time,
        "close_time": bar.close_time,
        "available_at": bar.available_at,
        "is_closed": bar.is_closed,
    }
    row.update(overrides)
    return row


def _session_for(snapshot, *, overrides: dict[UUID, dict[str, object]] | None = None):
    rows = []
    by_id = overrides or {}
    for series in snapshot.series:
        for bar in series.bars:
            assert bar.source_identity is not None
            rows.append(_market_row(bar, **by_id.get(bar.source_identity.record_id, {})))
    return _Session(rows)


def _decision(snapshot, *, status: BotDecisionStatus = BotDecisionStatus.CANDIDATE):
    return SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        bot_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        decision_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        decision_snapshot_id=snapshot.snapshot_id,
        status=status,
    )


@pytest.mark.asyncio
async def test_append_decision_source_bars_persists_ordered_multi_timeframe_metadata() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    session = _session_for(snapshot)

    await append_decision_source_bars(session, decision, snapshot)  # type: ignore[arg-type]

    assert session.flush_count == 1
    assert len(session.added) == 10
    assert all(type(row) is BotDecisionSourceBarRecord for row in session.added)
    assert [row.timeframe for row in session.added] == ["1h"] * 8 + ["4h"] * 2
    assert [row.ordinal for row in session.added] == list(range(8)) + [0, 1]
    expected = tuple(
        (lineage, series, bar, ordinal)
        for lineage, series in zip(snapshot.timeframe_lineage, snapshot.series, strict=True)
        for ordinal, bar in enumerate(series.bars)
    )
    for row, (lineage, series, bar, ordinal) in zip(session.added, expected, strict=True):
        assert row.workspace_id == WORKSPACE_ID
        assert row.bot_id == decision.bot_id
        assert row.bot_decision_id == decision.decision_id
        assert row.decision_snapshot_id == snapshot.snapshot_id
        assert row.timeframe == lineage.timeframe == bar.timeframe
        assert row.feature_snapshot_id == lineage.feature_snapshot_id
        assert row.market_bar_id == bar.source_identity.record_id
        assert row.source_event_id == lineage.source_event_ids[ordinal]
        assert row.source_event_digest == lineage.source_event_digest
        assert row.ordinal == ordinal
        assert row.timeframe_bar_count == len(series.bars)
        assert row.snapshot_timeframe_count == len(snapshot.series)
    assert tuple(session.statements[0].selected_columns.keys()) == (
        "id",
        "workspace_id",
        "logical_key",
        "revision",
        "supersedes_id",
        "checksum",
        "raw_reference_id",
        "source",
        "symbol",
        "timeframe",
        "open_time",
        "close_time",
        "available_at",
        "is_closed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [BotDecisionStatus.ABSTAIN, BotDecisionStatus.VETO])
async def test_append_decision_source_bars_accepts_baseline_without_scorer_event(
    status: BotDecisionStatus,
) -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot, status=status)
    session = _session_for(snapshot)

    await append_decision_source_bars(session, decision, snapshot)  # type: ignore[arg-type]

    assert len(session.added) == 10
    assert all(row.bot_decision_id == decision.decision_id for row in session.added)


@pytest.mark.asyncio
async def test_append_decision_source_bars_rejects_snapshot_id_mismatch_before_query() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    decision.decision_snapshot_id = "0" * 64
    session = _session_for(snapshot)

    with pytest.raises(BotDecisionSourceBarConflict, match="snapshot"):
        await append_decision_source_bars(session, decision, snapshot)  # type: ignore[arg-type]

    assert session.statements == []
    assert session.added == []


@pytest.mark.asyncio
async def test_append_decision_source_bars_rejects_unpinned_legacy_lineage() -> None:
    snapshot = _snapshot()
    first = replace(snapshot.timeframe_lineage[0], source_identity_pinned=False)
    legacy = replace(snapshot, timeframe_lineage=(first, *snapshot.timeframe_lineage[1:]))
    session = _session_for(legacy)

    with pytest.raises(BotDecisionSourceBarConflict, match="authoritative"):
        await append_decision_source_bars(
            session,
            _decision(legacy),
            legacy,  # type: ignore[arg-type]
        )

    assert session.statements == []
    assert session.added == []


@pytest.mark.asyncio
async def test_append_decision_source_bars_rejects_incomplete_timeframe_bar_set() -> None:
    snapshot = _snapshot()
    series = snapshot.series[0]
    shortened_first_series = replace(
        series,
        bars=series.bars[:-1],
        quote_volumes=series.quote_volumes[:-1] if series.quote_volumes is not None else None,
    )
    incomplete = replace(
        snapshot,
        series=(shortened_first_series, *snapshot.series[1:]),
    )
    session = _session_for(incomplete)

    with pytest.raises(BotDecisionSourceBarConflict, match="lineage nguồn authoritative"):
        await append_decision_source_bars(
            session,
            _decision(incomplete),
            incomplete,  # type: ignore[arg-type]
        )

    assert session.statements == []
    assert session.added == []


@pytest.mark.asyncio
async def test_append_decision_source_bars_rejects_market_bar_reused_across_timeframes() -> None:
    original = _snapshot()
    duplicate_bar = replace(
        original.series[1].bars[0],
        source_identity=original.series[0].bars[0].source_identity,
        checksum=original.series[0].bars[0].checksum,
    )
    duplicate_snapshot = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol=original.symbol,
        strategy_release_id="h01-v1",
        decision_time=original.decision_time,
        series_quality_as_of=original.freshness_as_of or original.decision_time,
        bars_by_timeframe={
            "1h": original.series[0].bars,
            "4h": (duplicate_bar, *original.series[1].bars[1:]),
        },
        position_is_flat=True,
        source_identity_required=True,
    )
    session = _session_for(duplicate_snapshot)

    with pytest.raises(BotDecisionSourceBarConflict, match="Bar trong snapshot"):
        await append_decision_source_bars(
            session,
            _decision(duplicate_snapshot),
            duplicate_snapshot,  # type: ignore[arg-type]
        )

    assert session.statements == []
    assert session.added == []


@pytest.mark.asyncio
async def test_append_decision_source_bars_rejects_missing_market_bar_metadata() -> None:
    snapshot = _snapshot()
    session = _session_for(snapshot)
    session.rows.pop()

    with pytest.raises(BotDecisionSourceBarConflict, match="Thiếu"):
        await append_decision_source_bars(
            session,
            _decision(snapshot),
            snapshot,  # type: ignore[arg-type]
        )

    assert session.added == []
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_append_decision_source_bars_rejects_market_bar_identity_or_event_mismatch() -> None:
    snapshot = _snapshot()
    last = snapshot.series[0].bars[-1]
    assert last.source_identity is not None
    session = _session_for(
        snapshot,
        overrides={last.source_identity.record_id: {"checksum": "f" * 64}},
    )

    with pytest.raises(BotDecisionSourceBarConflict, match="không khớp"):
        await append_decision_source_bars(
            session,
            _decision(snapshot),
            snapshot,  # type: ignore[arg-type]
        )

    assert session.added == []
    assert session.flush_count == 0
