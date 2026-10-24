from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from trading_bot.adapters.database.repositories.feature_snapshots import (
    FeatureSnapshotRepository,
    FeatureSnapshotRepositoryConflict,
)
from trading_bot.domain.market.feature_snapshot import (
    FeatureCalculator,
    FeatureSettings,
)
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BASE_TIME = datetime(2026, 10, 20, tzinfo=UTC)


def make_snapshot(*, computed_at: datetime | None = None):
    bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1h",
            open_time=BASE_TIME + timedelta(hours=index),
            close_time=BASE_TIME + timedelta(hours=index + 1),
            event_at=BASE_TIME + timedelta(hours=index + 1),
            received_at=BASE_TIME + timedelta(hours=index + 1, minutes=1),
            available_at=BASE_TIME + timedelta(hours=index + 1, minutes=1),
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=Decimal(10),
            is_closed=True,
            source="fixture",
            checksum=f"checksum-{index}",
        )
        for index in range(4)
    )
    settings = FeatureSettings(
        code_hash="a" * 64,
        expected_interval=timedelta(hours=1),
        max_event_age=timedelta(hours=2),
        regime_history=timedelta(hours=2),
        sma_period=2,
        ema_periods=(2,),
        atr_period=2,
        rsi_period=2,
        adx_period=2,
        bollinger_period=2,
        realized_volatility_period=2,
        efficiency_ratio_period=2,
        zscore_period=2,
    )
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)
    return FeatureCalculator(settings).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"event-{index}" for index in range(len(bars))),
        computed_at=computed_at or market.as_of + timedelta(seconds=1),
    )


class FakeSession:
    def __init__(
        self,
        scalar_results: list[object | None] | None = None,
        *,
        flush_error: Exception | None = None,
    ) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []
        self.flush_error = flush_error

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.scalar_results)

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_append_persists_immutable_snapshot_payload_and_full_lineage() -> None:
    snapshot = make_snapshot()
    repository = FeatureSnapshotRepository(FakeSession([None]))  # type: ignore[arg-type]

    result = await repository.append(workspace_id=WORKSPACE_ID, snapshot=snapshot)

    assert result.snapshot_id == snapshot.snapshot_id
    assert result.content_hash == snapshot.content_hash
    stored = repository._session.added[0]
    assert stored.workspace_id == WORKSPACE_ID
    assert stored.as_of == snapshot.as_of
    assert stored.computed_at == snapshot.computed_at
    assert stored.schema_hash == snapshot.schema_hash
    assert stored.formula_hash == snapshot.formula_hash
    assert stored.code_hash == snapshot.code_hash
    assert stored.input_bar_ids == list(snapshot.input_bar_ids)
    assert stored.input_event_ids == list(snapshot.input_event_ids)
    assert stored.features == dict(snapshot.features)
    assert stored.canonical_json == snapshot.canonical_json()


@pytest.mark.asyncio
async def test_same_workspace_and_hash_is_idempotent() -> None:
    snapshot = make_snapshot()
    existing = SimpleNamespace(
        id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        workspace_id=WORKSPACE_ID,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        canonical_json=snapshot.canonical_json(),
    )
    session = FakeSession([existing])
    repository = FeatureSnapshotRepository(session)  # type: ignore[arg-type]

    result = await repository.append(workspace_id=WORKSPACE_ID, snapshot=snapshot)

    assert result.duplicate is True
    assert result.record_id == existing.id
    assert session.added == []


@pytest.mark.asyncio
async def test_hash_collision_or_same_key_with_different_payload_is_conflict() -> None:
    snapshot = make_snapshot()
    existing = SimpleNamespace(
        id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        workspace_id=WORKSPACE_ID,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        canonical_json='{"different":true}',
    )
    repository = FeatureSnapshotRepository(FakeSession([existing]))  # type: ignore[arg-type]

    with pytest.raises(FeatureSnapshotRepositoryConflict, match="hash|payload"):
        await repository.append(workspace_id=WORKSPACE_ID, snapshot=snapshot)


@pytest.mark.asyncio
async def test_concurrent_duplicate_insert_rechecks_after_unique_conflict() -> None:
    snapshot = make_snapshot()
    existing = SimpleNamespace(
        id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        workspace_id=WORKSPACE_ID,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        canonical_json=snapshot.canonical_json(),
    )
    session = FakeSession(
        [None, existing], flush_error=IntegrityError("insert", {}, RuntimeError("unique"))
    )
    repository = FeatureSnapshotRepository(session)  # type: ignore[arg-type]

    result = await repository.append(workspace_id=WORKSPACE_ID, snapshot=snapshot)

    assert result.duplicate is True
    assert result.record_id == existing.id


@pytest.mark.asyncio
async def test_get_as_of_restores_snapshot_and_preserves_computation_time() -> None:
    snapshot = make_snapshot()
    row = SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        computed_at=snapshot.computed_at,
        canonical_json=snapshot.canonical_json(),
    )
    repository = FeatureSnapshotRepository(FakeSession([row]))  # type: ignore[arg-type]

    restored = await repository.get_as_of(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=snapshot.computed_at + timedelta(seconds=1),
    )

    assert restored is not None
    assert restored.canonical_json() == snapshot.canonical_json()
    assert restored.snapshot_id == snapshot.snapshot_id
    assert restored.computed_at == snapshot.computed_at


@pytest.mark.asyncio
async def test_as_of_lookup_rejects_corrupted_snapshot_payload_hash() -> None:
    snapshot = make_snapshot()
    row = SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        snapshot_id=snapshot.snapshot_id,
        content_hash="b" * 64,
        computed_at=snapshot.computed_at,
        canonical_json=snapshot.canonical_json(),
    )
    repository = FeatureSnapshotRepository(FakeSession([row]))  # type: ignore[arg-type]

    with pytest.raises(FeatureSnapshotRepositoryConflict, match="Hash"):
        await repository.get_as_of(
            workspace_id=WORKSPACE_ID,
            symbol="BTCUSDT",
            timeframe="1h",
            as_of=snapshot.computed_at + timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_append_rejects_snapshot_hash_mismatch_before_database_access() -> None:
    from dataclasses import replace

    snapshot = replace(make_snapshot(), content_hash="b" * 64)
    session = FakeSession()
    repository = FeatureSnapshotRepository(session)  # type: ignore[arg-type]

    with pytest.raises(FeatureSnapshotRepositoryConflict, match="hash"):
        await repository.append(workspace_id=WORKSPACE_ID, snapshot=snapshot)

    assert session.statements == []


@pytest.mark.asyncio
async def test_as_of_lookup_is_workspace_scoped_and_excludes_later_computation() -> None:
    cutoff = BASE_TIME + timedelta(hours=5)
    session = FakeSession([None])
    repository = FeatureSnapshotRepository(session)  # type: ignore[arg-type]

    result = await repository.get_as_of(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=cutoff,
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
    )

    assert result is None
    sql = session.statements[0].lower()
    assert "feature_snapshot.workspace_id =" in sql
    assert "feature_snapshot.symbol =" in sql
    assert "feature_snapshot.timeframe =" in sql
    assert "feature_snapshot.as_of <=" in sql
    assert "feature_snapshot.computed_at <=" in sql
    assert "feature_snapshot.schema_hash =" in sql
    assert "feature_snapshot.formula_hash =" in sql
    assert "feature_snapshot.code_hash =" in sql
    assert "order by" in sql


def test_feature_snapshot_model_has_workspace_rls_ready_unique_and_asof_indexes() -> None:
    from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord

    table = FeatureSnapshotRecord.__table__
    constraints = {constraint.name for constraint in table.constraints}
    indexes = {index.name for index in table.indexes}

    assert "uq_feature_snapshot_workspace_snapshot" in constraints
    assert "uq_feature_snapshot_workspace_content_hash" in constraints
    assert "ix_feature_snapshot_asof" in indexes
    assert "workspace_id" in table.c
