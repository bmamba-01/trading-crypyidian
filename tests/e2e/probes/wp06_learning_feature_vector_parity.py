"""Probe C.1.1: chỉ xuất metadata/hash, giữ vector và nến trong bộ nhớ."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import create_database_engine, database_url_from_secret
from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.models.market_entities import (
    MarketBar,
    MarketInstrument,
    MarketRawReference,
)
from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
    BacktestDatasetReplayBundleRepository,
)
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayLoader
from trading_bot.application.backtest_dataset_replay_bundle import (
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_dataset_replay_bundle_loader import (
    BacktestDatasetReplayBundleLoader,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose, RestBackfillRequest
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.bot_dispatcher import BotDecisionContext
from trading_bot.application.learning_feature_vector import (
    LearningFeatureVectorBuildError,
    build_learning_feature_vector,
)
from trading_bot.application.market_signal_composition import PostgresMarketDecisionSnapshotProvider
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
from trading_bot.domain.strategy.contracts import DecisionSnapshot

STATE = json.loads(os.environ["WP06_PARITY_STATE"])
WORKSPACE_ID = UUID(STATE["workspace_id"])
INSTRUMENT_ID = UUID(STATE["instrument_id"])
RAW_ID = UUID(STATE["raw_id"])
NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)
RANGE_END = NOW.replace(hour=NOW.hour // 4 * 4, minute=0, second=0, microsecond=0)
CUTOFF = RANGE_END - timedelta(milliseconds=1)
EVALUATION_START = RANGE_END - timedelta(hours=4)
RELEASE_HASH = hashlib.sha256(b"h01-v1-approved-content").hexdigest()
SOURCE = "BINANCE_USDM_REST_BACKFILL"
REVISION_1_ID = uuid5(WORKSPACE_ID, "revision-1")
REVISION_2_ID = uuid5(WORKSPACE_ID, "revision-2")
REVISION_2_CHECKSUM = hashlib.sha256(b"revision-2").hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _request(timeframe: str) -> RestBackfillRequest:
    hours, count = {"1h": (1, 224), "4h": (4, 206)}[timeframe]
    return RestBackfillRequest(
        symbol="BTCUSDT",
        interval=timeframe,
        start=RANGE_END - timedelta(hours=hours * count),
        end=RANGE_END,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """Gieo nến có FK raw-reference và policy BACKTEST_RESEARCH hiện hành."""
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        uow.session.add(
            Workspace(
                id=WORKSPACE_ID,
                name="C.1.1 parity",
                slug=f"parity-{WORKSPACE_ID.hex}",
                mode="PAPER",
                status="ACTIVE",
            )
        )
        await uow.session.flush()
        uow.session.add(
            MarketInstrument(
                id=INSTRUMENT_ID,
                workspace_id=WORKSPACE_ID,
                source="BINANCE_USDM",
                idempotency_key="instrument",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                tick_size=Decimal("0.1"),
                quantity_step=Decimal("0.001"),
                min_notional=Decimal("5"),
                status="TRADING",
                revision=1,
                checksum="a" * 64,
                supersedes_id=None,
                event_at=NOW,
                received_at=NOW,
                available_at=NOW,
            )
        )
        uow.session.add(
            MarketRawReference(
                id=RAW_ID,
                workspace_id=WORKSPACE_ID,
                source=SOURCE,
                idempotency_key="raw",
                object_key="fixture/c11/raw-reference",
                checksum="b" * 64,
                byte_length=0,
                content_type="application/json",
            )
        )
        await uow.session.flush()
        for timeframe in ("1h", "4h"):
            request = _request(timeframe)
            for index in range(request.expected_bars):
                opened = request.start + index * request.interval_duration
                closed = opened + request.interval_duration - timedelta(milliseconds=1)
                selected = timeframe == "1h" and index == request.expected_bars - 2
                key = f"{timeframe}:{opened.isoformat()}"
                row = MarketBar(
                    id=REVISION_1_ID if selected else uuid5(WORKSPACE_ID, key),
                    workspace_id=WORKSPACE_ID,
                    instrument_id=INSTRUMENT_ID,
                    symbol="BTCUSDT",
                    timeframe=timeframe,
                    source=SOURCE,
                    logical_key=key,
                    idempotency_key=f"{key}:1",
                    revision=1,
                    checksum=hashlib.sha256(key.encode()).hexdigest(),
                    supersedes_id=None,
                    raw_reference_id=RAW_ID,
                    source_cursor=key,
                    open_time=opened,
                    close_time=closed,
                    event_at=closed,
                    received_at=closed,
                    available_at=closed,
                    open=Decimal(100 + index),
                    high=Decimal(103 + index),
                    low=Decimal(99 + index),
                    close=Decimal(102 + index),
                    volume=Decimal("2"),
                    quote_volume=Decimal(204 + index * 2),
                    is_closed=True,
                )
                uow.session.add(row)
                if selected:
                    await uow.session.flush()
                    uow.session.add(
                        MarketBar(
                            **{
                                column.name: getattr(row, column.name)
                                for column in MarketBar.__table__.columns
                                if column.name
                                not in {
                                    "created_at",
                                    "id",
                                    "revision",
                                    "supersedes_id",
                                    "checksum",
                                    "idempotency_key",
                                    "available_at",
                                    "close",
                                }
                            },
                            id=REVISION_2_ID,
                            revision=2,
                            supersedes_id=REVISION_1_ID,
                            checksum=REVISION_2_CHECKSUM,
                            idempotency_key=f"{key}:2",
                            available_at=closed + timedelta(seconds=1),
                            close=row.close + Decimal("0.25"),
                        )
                    )
    entry = SourceRegistryEntry.discover(
        source_id="BINANCE_USDM_REST",
        publisher="Fixture C.1.1",
        endpoint_url="https://example.invalid/klines",
        access_method="GET",
        auth_scope="public fixture",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=100,
            max_bytes=1048576,
            max_cost=Decimal(0),
            cost_unit="USD",
        ),
        actor_id="fixture-operator",
        at=NOW - timedelta(days=3),
        reason="Khởi tạo policy fixture.",
    )
    pending = entry.transition(
        policy_id="C11:pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="fixture-operator",
        at=NOW - timedelta(days=2),
        reason="Chờ xác minh fixture.",
    )
    verified = pending.transition(
        policy_id="C11:verified",
        state=SourcePolicyState.VERIFIED,
        actor_id="fixture-operator",
        at=NOW - timedelta(hours=1),
        reason="Chỉ cho backtest research.",
        allowed_purposes=frozenset({SourceUsePurpose.BACKTEST_RESEARCH}),
        evidence_ref="https://example.invalid/c11-terms",
        reviewed_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=1),
        approved_by="fixture-reviewer",
    )
    for revision in verified.revisions:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).append_revision(
                workspace_id=WORKSPACE_ID, revision=revision
            )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE_ID,
                source_id="BINANCE_USDM_REST",
                policy_id=revision.policy.policy_id,
            )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        manifests = BacktestDatasetManifestRepository(uow.session)
        rights_gate = SourceRegistryBackfillRightsGate(
            workspace_id=WORKSPACE_ID, repository=SourceRegistryRepository(uow.session)
        )
        market = MarketDataRepository(uow.session)
        loader = BacktestDatasetReplayLoader(
            repository=manifests, rights_gate=rights_gate, market_data=market
        )
        inputs = []
        for timeframe in ("1h", "4h"):
            request = _request(timeframe)
            bars = await market.list_bars(
                workspace_id=WORKSPACE_ID,
                symbol="BTCUSDT",
                timeframe=timeframe,
                start=request.start,
                end=request.end,
                as_of=CUTOFF,
                closed_only=True,
                source=SOURCE,
            )
            rights = await rights_gate.authorize(request=request, now=NOW)
            manifest = build_backtest_dataset_manifest(
                request,
                evaluation_start=EVALUATION_START,
                warmup_bars=int((EVALUATION_START - request.start) / request.interval_duration),
                bars=bars,
                rights_evidence=rights,
                prepared_at=NOW,
                schema_version=4,
            )
            manifest.require_ready()
            await manifests.append(workspace_id=WORKSPACE_ID, manifest=manifest)
            inputs.append(
                await loader.load_for_replay(
                    workspace_id=WORKSPACE_ID, dataset_hash=manifest.dataset_hash, now=NOW
                )
            )
        bundle = build_backtest_dataset_replay_bundle(inputs)
        await BacktestDatasetReplayBundleRepository(uow.session).append(
            workspace_id=WORKSPACE_ID, bundle=bundle
        )
    return {"bundle_hash": bundle.dataset_hash}


def _identity(bar: OHLCVBar) -> MarketBarSourceIdentity:
    assert bar.source_identity is not None
    return bar.source_identity


def _inspect(snapshot: DecisionSnapshot) -> dict[str, Any]:
    """So toàn bộ payload bằng hash; chỉ xuất lineage và metadata an toàn."""
    vector = build_learning_feature_vector(
        snapshot, strategy_release_id="h01-v1", strategy_release_content_hash=RELEASE_HASH
    )
    bars = tuple(bar for series in snapshot.series for bar in series.bars)
    selected = [bar for bar in bars if _identity(bar).record_id == REVISION_2_ID]
    assert len(selected) == 1
    identity = _identity(selected[0])
    assert identity.revision == 2
    assert identity.supersedes_record_id == REVISION_1_ID
    assert identity.checksum == REVISION_2_CHECKSUM
    assert selected[0].checksum == REVISION_2_CHECKSUM
    assert all(_identity(bar).record_id != REVISION_1_ID for bar in bars)
    assert all(bar.raw_reference == str(RAW_ID) for bar in bars)
    assert all(item.source_identity_pinned for item in snapshot.timeframe_lineage)
    assert snapshot.decision_time == snapshot.freshness_as_of == CUTOFF
    first = snapshot.series[0]
    bar = first.bars[-1]
    feature_value = snapshot.features[0].value
    assert feature_value is not None
    candidates = {
        "raw_reference": replace(
            snapshot,
            series=(
                replace(
                    first,
                    bars=(
                        *first.bars[:-1],
                        replace(bar, raw_reference=str(uuid5(WORKSPACE_ID, "tampered"))),
                    ),
                ),
                *snapshot.series[1:],
            ),
        ),
        "source_identity": replace(
            snapshot,
            series=(
                replace(
                    first,
                    bars=(
                        *first.bars[:-1],
                        replace(
                            bar,
                            source_identity=replace(
                                _identity(bar),
                                record_id=uuid5(WORKSPACE_ID, "tampered-record"),
                            ),
                        ),
                    ),
                ),
                *snapshot.series[1:],
            ),
        ),
        "feature_value": replace(
            snapshot,
            features=(
                replace(snapshot.features[0], value=feature_value + 0.25),
                *snapshot.features[1:],
            ),
        ),
    }
    tampering = {}
    for name, changed in candidates.items():
        assert changed.snapshot_id == snapshot.snapshot_id
        if name != "feature_value":
            changed_bar = changed.series[0].bars[-1]
            assert changed_bar.checksum == bar.checksum
            assert _identity(changed_bar).checksum == _identity(bar).checksum
            if name == "raw_reference":
                assert changed_bar.source_identity == bar.source_identity
            else:
                assert _identity(changed_bar).record_id != _identity(bar).record_id
        try:
            build_learning_feature_vector(
                changed, strategy_release_id="h01-v1", strategy_release_content_hash=RELEASE_HASH
            )
        except LearningFeatureVectorBuildError as error:
            tampering[name] = error.reason_code.value
        else:
            raise AssertionError(f"Không từ chối tampering: {name}")
        assert tampering[name] == "SNAPSHOT_CONTENT_MISMATCH"
    payload = vector.canonical_payload()
    lineage_payload = cast(list[dict[str, Any]], payload["timeframe_lineage"])
    return {
        "snapshot_id": vector.snapshot_id,
        "snapshot_hash": vector.snapshot_hash,
        "schema_version": vector.schema_version,
        "feature_schema_hash": vector.feature_schema_hash,
        "vector_hash": vector.vector_hash,
        "feature_keys": list(vector.feature_keys),
        "ordered_features_hash": _digest(payload["features"]),
        "timeframe_lineage_hashes": {item["timeframe"]: _digest(item) for item in lineage_payload},
        "lineage_fields": sorted(lineage_payload[0]),
        "strategy_release_id": vector.strategy_release_id,
        "strategy_release_content_hash": vector.strategy_release_content_hash,
        "bar_counts": {series.timeframe: len(series.bars) for series in snapshot.series},
        "revision_2_exact": True,
        "raw_reference_exact": True,
        "no_vector_rights": not hasattr(vector, "source_rights_fingerprint"),
        "tampering": tampering,
    }


async def _main() -> dict[str, Any]:
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    engine.sync_engine.hide_parameters = True
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if STATE["mode"] == "seed":
            return await _seed(sessions)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            role = await uow.session.scalar(text("SELECT current_user"))
        if STATE["mode"] == "runtime":
            assert os.environ["TRADING_BOT_MARKET_SIGNAL_WORKSPACE_ID"] == str(WORKSPACE_ID)
            snapshot = await PostgresMarketDecisionSnapshotProvider(sessions).snapshot_for(
                evidence=SimpleNamespace(
                    workspace_id=WORKSPACE_ID, symbol="BTCUSDT", observed_at=CUTOFF
                ),
                context=BotDecisionContext(
                    workspace_id=WORKSPACE_ID,
                    bot_id=uuid5(WORKSPACE_ID, "bot"),
                    account_id=uuid5(WORKSPACE_ID, "account"),
                    bot_config_version_id=uuid5(WORKSPACE_ID, "config"),
                    bot_config_hash="c" * 64,
                    root_settings_version_id=uuid5(WORKSPACE_ID, "settings"),
                    root_settings_hash="d" * 64,
                    strategy_release_id="h01-v1",
                    symbols=("BTCUSDT",),
                    effective_at=CUTOFF,
                ),
                evaluated_at=CUTOFF,
            )
        else:
            assert os.environ["TRADING_BOT_BACKTEST_WORKER_WORKSPACE_ID"] == str(WORKSPACE_ID)
            async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
                manifests = BacktestDatasetManifestRepository(uow.session)
                bundle = await BacktestDatasetReplayBundleLoader(
                    repository=BacktestDatasetReplayBundleRepository(uow.session),
                    replay_loader=BacktestDatasetReplayLoader(
                        repository=manifests,
                        rights_gate=SourceRegistryBackfillRightsGate(
                            workspace_id=WORKSPACE_ID,
                            repository=SourceRegistryRepository(uow.session),
                        ),
                        market_data=MarketDataRepository(uow.session),
                    ),
                ).load_for_replay(
                    workspace_id=WORKSPACE_ID, bundle_hash=STATE["bundle_hash"], now=NOW
                )
            assert all(component.manifest.schema_version == 4 for component in bundle.components)
            observations = build_backtest_dataset_replay_observations(
                bundle, strategy_release_id="h01-v1", position_is_flat_at=lambda _: True
            )
            selected = [
                item.snapshot for item in observations if item.snapshot.decision_time == CUTOFF
            ]
            assert len(selected) == 1
            snapshot = selected[0]
        return {"database_role": role, **_inspect(snapshot)}
    finally:
        await engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), sort_keys=True))
