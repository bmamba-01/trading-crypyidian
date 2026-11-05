from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.models.backtest_entities import (
    BacktestDatasetManifestRecord,
)
from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestConflict,
    BacktestDatasetManifestRepository,
)
from trading_bot.application.backtest_dataset_manifest import (
    build_backtest_dataset_manifest,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.models import OHLCVBar

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
WORKSPACE_ID = uuid4()
START = NOW - timedelta(minutes=2)


def manifest():
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1m",
        start=START,
        end=NOW,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time=open_time,
            close_time=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
            event_at=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
            received_at=NOW,
            available_at=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1"),
            is_closed=True,
            source="BINANCE_USDM_REST_BACKFILL",
            source_cursor=open_time.isoformat(),
            checksum=hashlib.sha256(open_time.isoformat().encode()).hexdigest(),
        )
        for open_time in (START, START + timedelta(minutes=1))
    )
    return build_backtest_dataset_manifest(
        request,
        evaluation_start=START,
        warmup_bars=0,
        bars=bars,
        rights_evidence=BackfillRightsEvidence(
            workspace_id=WORKSPACE_ID,
            provider="BINANCE_USDM_REST",
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            terms_url="https://example.invalid/terms",
            policy_fingerprint=hashlib.sha256(b"source-policy-v1").hexdigest(),
            verified_at=NOW - timedelta(days=1),
            checked_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(days=1),
        ),
        prepared_at=NOW,
    )


class FakeSession:
    def __init__(self, scalar_results: list[object | None] | None = None) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []

    async def execute(self, statement: object, params: object | None = None) -> object:
        del params
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace()

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.scalar_results, None)

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


def test_backtest_manifest_record_round_trips_and_checks_canonical_payload() -> None:
    original = manifest()
    record = BacktestDatasetManifestRecord.from_manifest(
        workspace_id=WORKSPACE_ID,
        manifest=original,
    )

    restored = record.to_domain()

    assert restored == original
    assert record.dataset_hash == original.dataset_hash
    assert record.status == "READY"
    assert record.manifest_json["dataset_hash"] == original.dataset_hash

    record.manifest_json = {"status": "tampered"}
    with pytest.raises(ValueError, match="JSONB"):
        record.to_domain()


def test_backtest_manifest_record_detects_denormalized_column_tampering() -> None:
    record = BacktestDatasetManifestRecord.from_manifest(
        workspace_id=WORKSPACE_ID,
        manifest=manifest(),
    )
    record.status = "INCOMPLETE"

    with pytest.raises(ValueError, match="chỉ mục"):
        record.to_domain()


@pytest.mark.asyncio
async def test_append_backtest_manifest_is_workspace_scoped_and_idempotent() -> None:
    value = manifest()
    first_session = FakeSession([None])
    first = await BacktestDatasetManifestRepository(first_session).append(
        workspace_id=WORKSPACE_ID,
        manifest=value,
    )

    assert first.is_duplicate is False
    assert len(first_session.added) == 1
    stored = first_session.added[0]
    assert isinstance(stored, BacktestDatasetManifestRecord)
    assert stored.workspace_id == WORKSPACE_ID
    assert stored.dataset_hash == value.dataset_hash
    assert "pg_advisory_xact_lock" in first_session.statements[1]

    retry_session = FakeSession([stored])
    duplicate = await BacktestDatasetManifestRepository(retry_session).append(
        workspace_id=WORKSPACE_ID,
        manifest=value,
    )

    assert duplicate.is_duplicate is True
    assert duplicate.record.id == stored.id
    assert retry_session.added == []
    assert "app.workspace_id" in retry_session.statements[0]
    assert "backtest_dataset_manifest.workspace_id" in retry_session.statements[-1]


@pytest.mark.asyncio
async def test_append_backtest_manifest_rejects_workspace_or_hash_payload_conflict() -> None:
    value = manifest()
    other_workspace = uuid4()
    with pytest.raises(BacktestDatasetManifestConflict, match="workspace"):
        await BacktestDatasetManifestRepository(FakeSession()).append(
            workspace_id=other_workspace,
            manifest=value,
        )

    stored = BacktestDatasetManifestRecord.from_manifest(
        workspace_id=WORKSPACE_ID,
        manifest=value,
    )
    stored.canonical_manifest = stored.canonical_manifest + " "
    with pytest.raises(BacktestDatasetManifestConflict, match="canonical"):
        await BacktestDatasetManifestRepository(FakeSession([stored])).append(
            workspace_id=WORKSPACE_ID,
            manifest=value,
        )


@pytest.mark.asyncio
async def test_get_backtest_manifest_hash_scopes_explicit_workspace() -> None:
    value = manifest()
    record = BacktestDatasetManifestRecord.from_manifest(
        workspace_id=WORKSPACE_ID,
        manifest=value,
    )
    session = FakeSession([record])

    loaded = await BacktestDatasetManifestRepository(session).get_by_hash(
        workspace_id=WORKSPACE_ID,
        dataset_hash=value.dataset_hash,
    )

    assert loaded == value
    assert "app.workspace_id" in session.statements[0]
    assert "backtest_dataset_manifest.workspace_id" in session.statements[-1]


def test_backtest_manifest_model_has_workspace_uniqueness_and_immutable_contract() -> None:
    table = BacktestDatasetManifestRecord.__table__
    assert "workspace_id" in table.c
    assert frozenset({"workspace_id", "dataset_hash"}) in {
        frozenset(constraint.columns.keys()) for constraint in table.constraints
    }
    assert "backtest_dataset_manifest" == table.name
