"""Kiểm tra backfill persistence bằng PostgreSQL thật và provider fixture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

STATE = json.loads(os.environ["WP07B_STATE"])
WORKSPACE_ID = UUID(STATE["workspace_id"])
INSTRUMENT_ID = UUID(STATE["instrument_id"])
OWNER_USER_ID = UUID("00000000-0000-4000-8000-000000000701")
OWNER_MEMBERSHIP_ID = UUID("00000000-0000-4000-8000-000000000702")
OWNER_ROLE_BINDING_ID = UUID("00000000-0000-4000-8000-000000000703")
NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)
START = NOW.replace(second=0, microsecond=0) - timedelta(minutes=2)
END = START + timedelta(minutes=2)
MULTI_RANGE_END = NOW.replace(
    hour=(NOW.hour // 4) * 4,
    minute=0,
    second=0,
    microsecond=0,
)
MULTI_EVALUATION_START = MULTI_RANGE_END - timedelta(hours=4)


def _seed() -> None:
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    with psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    ) as connection:
        connection.execute(
            "INSERT INTO workspace (id,name,slug,mode,status) "
            "VALUES (%s,'WP07 backfill persistence E2E',%s,'PAPER','ACTIVE')",
            (WORKSPACE_ID, "wp07-backfill-" + WORKSPACE_ID.hex[:12]),
        )
        connection.execute(
            "INSERT INTO user_account (id,email_normalized,password_hash,status) "
            "VALUES (%s,'wp07-owner@example.test','unused-password-hash','ACTIVE')",
            (OWNER_USER_ID,),
        )
        connection.execute(
            "INSERT INTO user_membership (id,workspace_id,user_id,status) "
            "VALUES (%s,%s,%s,'ACTIVE')",
            (OWNER_MEMBERSHIP_ID, WORKSPACE_ID, OWNER_USER_ID),
        )
        connection.execute(
            "INSERT INTO role_binding (id,workspace_id,membership_id,role) "
            "VALUES (%s,%s,%s,'owner')",
            (OWNER_ROLE_BINDING_ID, WORKSPACE_ID, OWNER_MEMBERSHIP_ID),
        )
        connection.execute(
            "INSERT INTO market_instrument (id,workspace_id,source,idempotency_key,symbol,"
            "base_asset,quote_asset,tick_size,quantity_step,min_notional,status,revision,checksum,"
            "supersedes_id,event_at,received_at,available_at) VALUES (%s,%s,'BINANCE_USDM',"
            "'wp07-backfill-instrument','BTCUSDT','BTC','USDT',0.1,0.001,5,'TRADING',1,%s,"
            "NULL,%s,%s,%s)",
            (
                INSTRUMENT_ID,
                WORKSPACE_ID,
                "a" * 64,
                NOW - timedelta(days=1),
                NOW - timedelta(days=1),
                NOW - timedelta(days=1),
            ),
        )


class FixtureKlineProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ):
        from trading_bot.domain.market.models import OHLCVBar
        from trading_bot.ports.market_data import MarketDataPage

        del end_time, limit
        expected = START + timedelta(minutes=self.calls)
        if (symbol, interval, start_time) != ("BTCUSDT", "1m", expected):
            raise AssertionError("Provider fixture nhận request ngoài cursor mong đợi.")
        self.calls += 1
        close_time = expected + timedelta(minutes=1) - timedelta(milliseconds=1)
        bar = OHLCVBar(
            symbol=symbol,
            timeframe=interval,
            open_time=expected,
            close_time=close_time,
            event_at=close_time,
            received_at=NOW,
            available_at=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1.5"),
            quote_volume=Decimal("151.5"),
            is_closed=True,
            source="BINANCE_USDM",
            source_cursor=f"fixture:{expected.isoformat()}",
        )
        payload = f"fixture-page:{expected.isoformat()}".encode()
        return MarketDataPage(
            records=(bar,),
            raw_payload=payload,
            checksum=hashlib.sha256(payload).hexdigest(),
            received_at=NOW,
            available_at=NOW,
            is_complete=self.calls == 2,
        )


async def _exercise() -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import SourceLicenseEvidenceRecord
    from trading_bot.adapters.database.models.backtest_entities import (
        BacktestReportRecord,
        BacktestRunRecord,
        BacktestTrialRecord,
    )
    from trading_bot.adapters.database.models.backtest_outcome_entities import (
        BacktestRunDatasetRightsRecord,
        BacktestScenarioRecord,
        BacktestTradeOutcomeRecord,
    )
    from trading_bot.adapters.database.models.market_entities import IngestionCursor as CursorRow
    from trading_bot.adapters.database.models.market_entities import MarketBar
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_outcomes import (
        BacktestOutcomeRepository,
    )
    from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
    from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
    from trading_bot.application.backtest_dataset_manifest import (
        build_backtest_dataset_manifest,
    )
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayLoader
    from trading_bot.application.backtest_dataset_replay_bundle import (
        build_backtest_dataset_replay_bundle,
    )
    from trading_bot.application.backtest_dataset_replay_bundle_loader import (
        BacktestDatasetReplayBundleLoader,
    )
    from trading_bot.application.backtest_dataset_replay_execution import (
        BacktestDatasetReplayExecutionService,
    )
    from trading_bot.application.backtest_sensitivity import SensitivityScenario
    from trading_bot.application.binance_rest_backfill import (
        BackfillRightsPurpose,
        BinanceRestBackfillRunner,
        RestBackfillRequest,
    )
    from trading_bot.application.binance_rest_backfill_ingestion import (
        BinanceRestBackfillIngestionService,
        _make_version,
    )
    from trading_bot.application.binance_rest_backfill_rights import (
        SourceRegistryBackfillRightsGate,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.market.models import OHLCVBar
    from trading_bot.domain.research.license_scope import (
        SourceLicenseScope,
        canonical_scope_json,
        scope_sha256,
    )
    from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
    from trading_bot.domain.research.source_registry import (
        SourceBudget,
        SourceRegistryEntry,
        source_rights_hash,
    )

    class DatabaseRoleUnitOfWork:
        """Áp dụng role ứng dụng trong đúng transaction của kiểm thử Compose."""

        def __init__(self, unit_of_work, role_name: str) -> None:
            if role_name not in {
                "crypidian_admin_web",
                "crypidian_trainer_worker",
                "crypidian_replay_writer",
                "crypidian_backtest_worker",
            }:
                raise ValueError("Role fixture không được phép.")
            self._unit_of_work = unit_of_work
            self._role_name = role_name

        async def __aenter__(self):
            unit_of_work = await self._unit_of_work.__aenter__()
            try:
                await unit_of_work.session.execute(text(f"SET LOCAL ROLE {self._role_name}"))
            except BaseException as error:
                await self._unit_of_work.__aexit__(type(error), error, error.__traceback__)
                raise
            return unit_of_work

        async def __aexit__(self, exc_type, exc, traceback):
            await self._unit_of_work.__aexit__(exc_type, exc, traceback)

    outcome_models = (
        BacktestRunRecord,
        BacktestTrialRecord,
        BacktestReportRecord,
        BacktestScenarioRecord,
        BacktestTradeOutcomeRecord,
        BacktestRunDatasetRightsRecord,
    )

    async def read_outcome_counts(uow) -> tuple[int, ...]:
        counts = []
        for model in outcome_models:
            counts.append(
                await uow.session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == WORKSPACE_ID)
                )
            )
        return tuple(counts)

    url = database_url_from_secret(
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
        host="postgres",
        database="crypidian",
    )
    engine = create_database_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1m",
        start=START,
        end=END,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        page_limit=1,
    )

    def service(
        repository: MarketDataRepository,
        rights_repository: SourceRegistryRepository,
        provider: FixtureKlineProvider,
    ):
        return BinanceRestBackfillIngestionService(
            runner=BinanceRestBackfillRunner(
                market_data=provider,
                rights_gate=SourceRegistryBackfillRightsGate(
                    workspace_id=WORKSPACE_ID,
                    repository=rights_repository,
                ),
            ),
            repository=repository,
            workspace_id=WORKSPACE_ID,
            instrument_id=INSTRUMENT_ID,
        )

    _seed()
    policy_entry = SourceRegistryEntry.discover(
        source_id="BINANCE_USDM_REST",
        publisher="Provider fixture",
        endpoint_url="https://example.invalid/klines",
        access_method="GET",
        auth_scope="public fixture",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=100,
            max_bytes=1024 * 1024,
            max_cost=Decimal("0"),
            cost_unit="USD",
        ),
        actor_id="fixture-operator",
        at=NOW - timedelta(days=3),
        reason="Tạo policy quyền fixture cho E2E.",
    )
    license_scope = SourceLicenseScope(
        source_id="BINANCE_USDM_REST",
        provider="BINANCE",
        product="USDM_KLINES",
        venue="BINANCE_USDM",
        contract="FIXTURE",
        symbols=("BTCUSDT",),
        intervals=("1m", "1h", "4h"),
        data_start=NOW - timedelta(days=365),
        data_end=NOW + timedelta(days=365),
        allowed_purposes=(SourceUsePurpose.BACKTEST_RESEARCH,),
        allowed_outputs=("aggregate_report", "derived_features"),
        derived_use=("aggregate_reporting", "feature_extraction"),
        attribution="Provider fixture",
        max_retention_days=None,
    )
    pending = policy_entry.transition(
        policy_id="BINANCE_USDM_REST:fixture-pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="fixture-operator",
        at=NOW - timedelta(days=2),
        reason="Chờ xác minh policy fixture.",
        evidence_ref="https://example.invalid/test-fixture-terms",
        provider=license_scope.provider,
        product=license_scope.product,
        venue=license_scope.venue,
        contract=license_scope.contract,
        license_scope=license_scope,
        attribution=license_scope.attribution,
    )
    # Policy verified hiện bắt buộc có license evidence digest và scope đã ghim.
    license_evidence_id = uuid4()
    verified = pending.transition(
        policy_id="BINANCE_USDM_REST:fixture-verified",
        state=SourcePolicyState.VERIFIED,
        actor_id="fixture-operator",
        at=NOW - timedelta(hours=1),
        reason="Cho phép fixture backtest research.",
        license_scope=license_scope,
        license_evidence_id=license_evidence_id,
        license_digest="a" * 64,
        license_scope_hash=scope_sha256(license_scope),
        allowed_purposes=frozenset({SourceUsePurpose.BACKTEST_RESEARCH}),
        evidence_ref="https://example.invalid/test-fixture-terms",
        reviewed_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=1),
        approved_by="fixture-reviewer",
    )
    policy_history = SourceRegistryEntry(
        "BINANCE_USDM_REST", (*pending.revisions, verified.current)
    )
    for revision in policy_history.revisions:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            if revision.state is SourcePolicyState.VERIFIED:
                uow.session.add(
                    SourceLicenseEvidenceRecord(
                        id=license_evidence_id,
                        workspace_id=WORKSPACE_ID,
                        source_id="BINANCE_USDM_REST",
                        policy_id=pending.current.policy.policy_id,
                        idempotency_key=uuid4(),
                        evidence_ref="https://example.invalid/test-fixture-terms",
                        digest="a" * 64,
                        byte_count=123,
                        media_type="text/plain",
                        scope_version=license_scope.scope_version,
                        license_scope=json.loads(canonical_scope_json(license_scope)),
                        canonical_scope=canonical_scope_json(license_scope),
                        scope_hash=scope_sha256(license_scope),
                        actor_id="fixture-operator",
                        created_at=NOW - timedelta(days=2),
                    )
                )
                await uow.session.flush()
            await SourceRegistryRepository(uow.session).append_revision(
                workspace_id=WORKSPACE_ID,
                revision=revision,
            )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE_ID,
                source_id="BINANCE_USDM_REST",
                policy_id=revision.policy.policy_id,
            )

    rollback_provider = FixtureKlineProvider()
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await service(
                MarketDataRepository(uow.session),
                SourceRegistryRepository(uow.session),
                rollback_provider,
            ).run_batch(
                request,
                now=NOW,
                max_pages=2,
            )
            raise RuntimeError("rollback fixture")
    except RuntimeError as error:
        if str(error) != "rollback fixture":
            raise

    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        bars_after_rollback = await uow.session.scalar(
            select(func.count())
            .select_from(MarketBar)
            .where(
                MarketBar.workspace_id == WORKSPACE_ID,
                MarketBar.source == "BINANCE_USDM_REST_BACKFILL",
            )
        )
        cursors_after_rollback = await uow.session.scalar(
            select(func.count())
            .select_from(CursorRow)
            .where(
                CursorRow.workspace_id == WORKSPACE_ID,
                CursorRow.source
                == BinanceRestBackfillIngestionService.cursor_stream(request).source,
            )
        )

    committed_provider = FixtureKlineProvider()
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        first = await service(
            MarketDataRepository(uow.session),
            SourceRegistryRepository(uow.session),
            committed_provider,
        ).run_batch(
            request,
            now=NOW,
            max_pages=2,
        )
        first_count = len(first.bars)

    resume_provider = FixtureKlineProvider()
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        resumed = await service(
            MarketDataRepository(uow.session),
            SourceRegistryRepository(uow.session),
            resume_provider,
        ).run_batch(
            request,
            now=NOW,
            max_pages=2,
        )
        cursor = await MarketDataRepository(uow.session).get_cursor(
            workspace_id=WORKSPACE_ID,
            stream=BinanceRestBackfillIngestionService.cursor_stream(request),
            as_of=NOW,
        )
        bars_after_commit = await MarketDataRepository(uow.session).list_bars(
            workspace_id=WORKSPACE_ID,
            symbol="BTCUSDT",
            timeframe="1m",
            start=START,
            end=END,
            as_of=NOW,
            closed_only=True,
        )

    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        rights_repository = SourceRegistryRepository(uow.session)
        rights_gate = SourceRegistryBackfillRightsGate(
            workspace_id=WORKSPACE_ID,
            repository=rights_repository,
        )
        rights_evidence = await rights_gate.authorize(request=request, now=NOW)
        observed_policy = await rights_repository.get_observed_entry(
            workspace_id=WORKSPACE_ID,
            source_id="BINANCE_USDM_REST",
        )
        if observed_policy is None:
            raise AssertionError("Policy fixture không có observer receipts đầy đủ.")
    dataset_manifest = build_backtest_dataset_manifest(
        request,
        evaluation_start=START + timedelta(minutes=1),
        warmup_bars=1,
        bars=tuple(bars_after_commit),
        rights_evidence=rights_evidence,
        prepared_at=NOW,
        schema_version=4,
    )
    dataset_manifest.require_ready()

    multi_timeframe_requests = tuple(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval=interval,
            start=MULTI_EVALUATION_START - duration,
            end=MULTI_RANGE_END,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            page_limit=1,
        )
        for interval, duration in (("1h", timedelta(hours=1)), ("4h", timedelta(hours=4)))
    )
    multi_timeframe_bars: dict[str, tuple[OHLCVBar, ...]] = {}
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        market_repository = MarketDataRepository(uow.session)
        for child_request in multi_timeframe_requests:
            interval = child_request.interval_duration
            count = child_request.expected_bars
            bars = tuple(
                OHLCVBar(
                    symbol="BTCUSDT",
                    timeframe=child_request.interval,
                    open_time=child_request.start + interval * index,
                    close_time=child_request.start
                    + interval * (index + 1)
                    - timedelta(milliseconds=1),
                    event_at=child_request.start
                    + interval * (index + 1)
                    - timedelta(milliseconds=1),
                    # Fixture giả lập nguồn đã phát nến tại thời điểm đóng để replay point-in-time.
                    received_at=child_request.start
                    + interval * (index + 1)
                    - timedelta(milliseconds=1),
                    available_at=child_request.start
                    + interval * (index + 1)
                    - timedelta(milliseconds=1),
                    open=Decimal("100") + Decimal(index),
                    high=Decimal("102") + Decimal(index),
                    low=Decimal("99") + Decimal(index),
                    close=Decimal("101") + Decimal(index),
                    volume=Decimal("1.5"),
                    quote_volume=Decimal("151.5"),
                    is_closed=True,
                    source="BINANCE_USDM_REST_BACKFILL",
                    source_cursor=f"fixture:{child_request.interval}:{index}",
                )
                for index in range(count)
            )
            versioned_bars: list[OHLCVBar] = []
            for bar in bars:
                versioned_bar, version = _make_version(workspace_id=WORKSPACE_ID, bar=bar)
                versioned_bars.append(versioned_bar)
                await market_repository.append_bar(
                    workspace_id=WORKSPACE_ID,
                    instrument_id=INSTRUMENT_ID,
                    bar=versioned_bar,
                    version=version,
                )
            persisted_bars = await market_repository.list_bars(
                workspace_id=WORKSPACE_ID,
                symbol="BTCUSDT",
                timeframe=child_request.interval,
                start=child_request.start,
                end=child_request.end,
                as_of=NOW,
                closed_only=True,
                source="BINANCE_USDM_REST_BACKFILL",
            )
            if len(persisted_bars) != count:
                raise AssertionError("Không đọc lại đủ bars fixture đa timeframe từ PostgreSQL.")
            multi_timeframe_bars[child_request.interval] = tuple(persisted_bars)
    multi_timeframe_manifests = tuple(
        build_backtest_dataset_manifest(
            child_request,
            evaluation_start=MULTI_EVALUATION_START,
            warmup_bars=1,
            bars=multi_timeframe_bars[child_request.interval],
            rights_evidence=rights_evidence,
            prepared_at=NOW,
            schema_version=4,
        )
        for child_request in multi_timeframe_requests
    )
    for child_manifest in multi_timeframe_manifests:
        child_manifest.require_ready()

    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        manifest_repository = BacktestDatasetManifestRepository(uow.session)
        current_rights_gate = SourceRegistryBackfillRightsGate(
            workspace_id=WORKSPACE_ID,
            repository=SourceRegistryRepository(uow.session),
        )
        first_manifest = await manifest_repository.append(
            workspace_id=WORKSPACE_ID,
            manifest=dataset_manifest,
        )
        duplicate_manifest = await manifest_repository.append(
            workspace_id=WORKSPACE_ID,
            manifest=dataset_manifest,
        )
        loaded_manifest = await manifest_repository.get_by_hash(
            workspace_id=WORKSPACE_ID,
            dataset_hash=dataset_manifest.dataset_hash,
        )
        replayable = await BacktestDatasetReplayLoader(
            repository=manifest_repository,
            rights_gate=current_rights_gate,
            market_data=MarketDataRepository(uow.session),
        ).load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=dataset_manifest.dataset_hash,
            now=NOW,
        )
        child_inputs = []
        for child_manifest in multi_timeframe_manifests:
            await manifest_repository.append(
                workspace_id=WORKSPACE_ID,
                manifest=child_manifest,
            )
            child_inputs.append(
                await BacktestDatasetReplayLoader(
                    repository=manifest_repository,
                    rights_gate=current_rights_gate,
                    market_data=MarketDataRepository(uow.session),
                ).load_for_replay(
                    workspace_id=WORKSPACE_ID,
                    dataset_hash=child_manifest.dataset_hash,
                    now=NOW,
                )
            )
        multi_timeframe_bundle = build_backtest_dataset_replay_bundle(tuple(child_inputs))
        bundle_repository = BacktestDatasetReplayBundleRepository(uow.session)
        bundle_first = await bundle_repository.append(
            workspace_id=WORKSPACE_ID,
            bundle=multi_timeframe_bundle,
        )
        bundle_duplicate = await bundle_repository.append(
            workspace_id=WORKSPACE_ID,
            bundle=multi_timeframe_bundle,
        )
        loaded_bundle_reference = await bundle_repository.get_by_hash(
            workspace_id=WORKSPACE_ID,
            bundle_hash=multi_timeframe_bundle.dataset_hash,
        )
        replayed_bundle = await BacktestDatasetReplayBundleLoader(
            repository=bundle_repository,
            replay_loader=BacktestDatasetReplayLoader(
                repository=manifest_repository,
                rights_gate=current_rights_gate,
                market_data=MarketDataRepository(uow.session),
            ),
        ).load_for_replay(
            workspace_id=WORKSPACE_ID,
            bundle_hash=multi_timeframe_bundle.dataset_hash,
            now=NOW,
        )
        other_workspace_manifest = await manifest_repository.get_by_hash(
            workspace_id=UUID(int=WORKSPACE_ID.int + 1),
            dataset_hash=dataset_manifest.dataset_hash,
        )

    if first_manifest.is_duplicate or not duplicate_manifest.is_duplicate:
        raise AssertionError("Retry manifest không trả trạng thái idempotent mong đợi.")
    if bundle_first.is_duplicate or not bundle_duplicate.is_duplicate:
        raise AssertionError("Retry bundle không trả trạng thái idempotent mong đợi.")
    if (
        loaded_bundle_reference is None
        or loaded_bundle_reference.bundle_hash != multi_timeframe_bundle.dataset_hash
    ):
        raise AssertionError("Bundle reference đọc lại không khớp hash đã lưu.")
    if replayed_bundle.dataset_hash != multi_timeframe_bundle.dataset_hash:
        raise AssertionError("Bundle loader chưa trả lại bundle đa timeframe đã kiểm quyền.")

    if os.environ.get("WP07_IDENTITY_ONLY") == "1":
        return {
            "db_backfill_source_identity_hydrated": len(bars_after_commit) == 2
            and all(
                bar.source_identity is not None and bar.source_identity.workspace_id == WORKSPACE_ID
                for bar in bars_after_commit
            ),
            "single_manifest_v4_identity_digest": dataset_manifest.schema_version == 4
            and dataset_manifest.has_pinned_source_identities
            and len(dataset_manifest.bar_identity_hash or "") == 64,
            "single_manifest_replay_loader_verified_identity": (
                replayable.manifest == dataset_manifest
                and replayable.bars == tuple(bars_after_commit)
                and all(bar.source_identity is not None for bar in replayable.bars)
            ),
            "multiframe_manifests_v4": len(multi_timeframe_manifests) == 2
            and all(
                manifest.schema_version == 4 and manifest.has_pinned_source_identities
                for manifest in multi_timeframe_manifests
            ),
            "multiframe_bundle_loader_preserved_identity": (
                replayed_bundle.dataset_hash == multi_timeframe_bundle.dataset_hash
                and all(
                    bar.source_identity is not None
                    for component in replayed_bundle.components
                    for bar in component.bars
                )
            ),
        }

    from wp07_runtime_replay_fixture import build_h01_dataset_runtime_replay

    scenarios = (
        SensitivityScenario("base", "BASE"),
        SensitivityScenario(
            "higher-fees",
            "COST",
            cost_overrides=(
                ("entry_fee_rate", "0.002"),
                ("exit_fee_rate", "0.002"),
            ),
        ),
    )
    _, manifest_comparison = build_h01_dataset_runtime_replay(
        WORKSPACE_ID,
        bundle=replayed_bundle,
        scenarios=scenarios,
    )
    replay_manifest = manifest_comparison.runs[0].sensitivity_run.manifest
    execution_clock = [NOW]
    executor_calls = 0
    fail_second_outcome = [True]

    def bundle_loader_factory(uow):
        manifests = BacktestDatasetManifestRepository(uow.session)
        rights_repository = SourceRegistryRepository(uow.session)
        return BacktestDatasetReplayBundleLoader(
            repository=BacktestDatasetReplayBundleRepository(uow.session),
            replay_loader=BacktestDatasetReplayLoader(
                repository=manifests,
                rights_gate=SourceRegistryBackfillRightsGate(
                    workspace_id=WORKSPACE_ID,
                    repository=rights_repository,
                ),
                market_data=MarketDataRepository(uow.session),
            ),
        )

    def outcome_repository_factory(uow):
        repository = BacktestOutcomeRepository(uow.session)

        class FailSecondChild:
            def __init__(self) -> None:
                self.calls = 0

            async def append_sensitivity_run(self, **kwargs):
                result = await repository.append_sensitivity_run(**kwargs)
                self.calls += 1
                if fail_second_outcome[0] and self.calls == 2:
                    raise RuntimeError("Fault injection sau khi flush child replay thứ hai.")
                return result

        return FailSecondChild()

    def execute_runtime(bundle, manifest, *, progress_callback=None, cancel_requested=None):
        nonlocal executor_calls
        if progress_callback is not None and not callable(progress_callback):
            raise AssertionError("Callback tiến độ replay không hợp lệ.")
        if cancel_requested is not None and not callable(cancel_requested):
            raise AssertionError("Callback hủy replay không hợp lệ.")
        executor_calls += 1
        runtime_bundle, comparison = build_h01_dataset_runtime_replay(
            WORKSPACE_ID,
            bundle=bundle,
            scenarios=scenarios,
        )
        if (
            runtime_bundle.dataset_hash != bundle.dataset_hash
            or comparison.runs[0].sensitivity_run.manifest != manifest
        ):
            raise AssertionError("Runtime H01 không dùng đúng bundle/manifest service đã nạp.")
        return comparison

    replay_service = BacktestDatasetReplayExecutionService(
        load_unit_of_work_factory=lambda workspace_id: DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=workspace_id),
            "crypidian_admin_web",
        ),
        persist_unit_of_work_factory=lambda workspace_id: DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=workspace_id),
            "crypidian_replay_writer",
        ),
        bundle_loader_factory=bundle_loader_factory,
        outcome_repository_factory=outcome_repository_factory,
        executor=execute_runtime,
        clock=lambda: execution_clock[0],
    )

    failed_persist_rolled_back = False
    try:
        await replay_service.execute(
            workspace_id=WORKSPACE_ID,
            bundle_hash=multi_timeframe_bundle.dataset_hash,
            manifest=replay_manifest,
        )
    except RuntimeError as error:
        if "Fault injection" not in str(error):
            raise
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            failed_persist_counts = await read_outcome_counts(uow)
        failed_persist_rolled_back = failed_persist_counts == (0, 0, 0, 0, 0, 0)
    if not failed_persist_rolled_back:
        raise AssertionError("Lỗi child outcome để lại dữ liệu replay chưa commit.")

    fail_second_outcome[0] = False
    replay_first = await replay_service.execute(
        workspace_id=WORKSPACE_ID,
        bundle_hash=multi_timeframe_bundle.dataset_hash,
        manifest=replay_manifest,
    )
    expected_trade_rows = sum(len(run.trade_outcomes) for run in manifest_comparison.runs)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        first_outcome_counts = await read_outcome_counts(uow)
        first_receipts = tuple(
            (
                row.workspace_id,
                row.run_id,
                row.child_dataset_hash,
                row.canonical_evidence,
                row.evidence_hash,
                row.checked_at,
                row.valid_until,
            )
            for row in (
                await uow.session.scalars(
                    select(BacktestRunDatasetRightsRecord)
                    .where(BacktestRunDatasetRightsRecord.workspace_id == WORKSPACE_ID)
                    .order_by(
                        BacktestRunDatasetRightsRecord.run_id,
                        BacktestRunDatasetRightsRecord.child_dataset_hash,
                    )
                )
            ).all()
        )
    if (
        len(replay_first.outcomes) != 2
        or replay_first.scenario_ids != ("base", "higher-fees")
        or first_outcome_counts[0:4] != (2, 2, 2, 2)
        or first_outcome_counts[4] != expected_trade_rows
        or first_outcome_counts[5] != 4
        or len(first_receipts) != 4
        or any(outcome.report_id is None for outcome in replay_first.outcomes)
    ):
        result_summaries = tuple(
            (
                type(run.sensitivity_run.result).__name__,
                getattr(run.sensitivity_run.result, "reason_code", None),
                getattr(run.sensitivity_run.result, "detail_vi", None),
            )
            for run in manifest_comparison.runs
        )
        raise AssertionError(
            "Runtime H01 hoặc receipts child chưa được persist đầy đủ: "
            f"counts={first_outcome_counts}, receipts={len(first_receipts)}, "
            f"scenarios={replay_first.scenario_ids}, reports="
            f"{tuple(outcome.report_id for outcome in replay_first.outcomes)}, "
            f"results={result_summaries}"
        )

    execution_clock[0] = NOW + timedelta(seconds=7)
    replay_retry = await replay_service.execute(
        workspace_id=WORKSPACE_ID,
        bundle_hash=multi_timeframe_bundle.dataset_hash,
        manifest=replay_manifest,
    )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        retry_counts = await read_outcome_counts(uow)
        retry_receipts = tuple(
            (
                row.workspace_id,
                row.run_id,
                row.child_dataset_hash,
                row.canonical_evidence,
                row.evidence_hash,
                row.checked_at,
                row.valid_until,
            )
            for row in (
                await uow.session.scalars(
                    select(BacktestRunDatasetRightsRecord)
                    .where(BacktestRunDatasetRightsRecord.workspace_id == WORKSPACE_ID)
                    .order_by(
                        BacktestRunDatasetRightsRecord.run_id,
                        BacktestRunDatasetRightsRecord.child_dataset_hash,
                    )
                )
            ).all()
        )
    if (
        replay_retry.run_ids != replay_first.run_ids
        or retry_counts != first_outcome_counts
        or retry_receipts != first_receipts
        or not all(outcome.duplicate for outcome in replay_retry.outcomes)
    ):
        raise AssertionError("Retry không idempotent hoặc đã thay đổi receipt gốc.")

    from trading_bot.adapters.database.models.backtest_job_entities import (
        BacktestReplayJobRecord,
    )
    from trading_bot.adapters.database.repositories.backtest_replay_jobs import (
        BacktestReplayJobRepository,
    )
    from trading_bot.application.backtest_replay_jobs import (
        BacktestReplayJobService,
        BacktestReplayJobSubmission,
    )
    from trading_bot.auth.policy import Principal, Role
    from trading_bot.workers.backtest_replay_worker import BacktestReplayJobWorker

    job_request = BacktestReplayJobSubmission(
        bundle_hash=multi_timeframe_bundle.dataset_hash,
        strategy_release_id="h01-v1",
        execution_interval="1h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.0004"),
        slippage_rate=Decimal("0.0002"),
        seed=7,
    )
    admin_engine = create_database_engine(url)

    def set_admin_database_role(connection) -> None:
        connection.exec_driver_sql("SET LOCAL ROLE crypidian_admin_web")

    event.listen(admin_engine.sync_engine, "begin", set_admin_database_role)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    job_service = BacktestReplayJobService(admin_sessions)
    owner_principal = Principal(
        user_id=OWNER_USER_ID,
        membership_id=OWNER_MEMBERSHIP_ID,
        workspace_id=WORKSPACE_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=NOW,
    )
    queued_job = await job_service.submit(owner_principal, job_request)
    replay_job_id = queued_job.id

    def role_uow(role_name: str):
        return lambda workspace_id: DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=workspace_id), role_name
        )

    job_worker = BacktestReplayJobWorker(
        read_sessions=sessions,
        persist_sessions=sessions,
        workspace_id=WORKSPACE_ID,
        worker_id="wp07-replay-e2e",
        tick_seconds=0.01,
        lease_seconds=30,
        read_unit_of_work_factory=role_uow("crypidian_backtest_worker"),
        persist_unit_of_work_factory=role_uow("crypidian_replay_writer"),
    )
    worker_processed_job = await job_worker.run_once()
    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
    ) as uow:
        persisted_job = await uow.session.get(BacktestReplayJobRecord, replay_job_id)

    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        worker_outcome_counts = await read_outcome_counts(uow)

    async def role_read_is_denied(role_name: str, statement: str) -> bool:
        try:
            async with DatabaseRoleUnitOfWork(
                UnitOfWork(sessions, workspace_id=WORKSPACE_ID), role_name
            ) as uow:
                await uow.session.execute(text(statement))
        except DBAPIError:
            return True
        return False

    trainer_bundle_read_denied = await role_read_is_denied(
        "crypidian_trainer_worker",
        "SELECT count(*) FROM public.backtest_dataset_replay_bundle",
    )
    backtest_worker_order_read_denied = await role_read_is_denied(
        "crypidian_backtest_worker", "SELECT count(*) FROM public.order_intent"
    )
    if (
        not worker_processed_job
        or persisted_job is None
        or persisted_job.status != "SUCCEEDED"
        or persisted_job.progress != 100
        or len(persisted_job.result_run_ids) != 1
        or worker_outcome_counts[0] != 3
        or worker_outcome_counts[5] != 6
        or not trainer_bundle_read_denied
        or not backtest_worker_order_read_denied
    ):
        job_state = (
            None
            if persisted_job is None
            else (
                persisted_job.status,
                persisted_job.stage,
                persisted_job.progress,
                persisted_job.attempt_count,
                persisted_job.error_code,
                persisted_job.result_run_ids,
            )
        )
        raise AssertionError(
            "Worker replay không hoàn tất, không ghi receipt đầy đủ hoặc role vượt quyền: "
            f"job={job_state}, "
            f"counts={worker_outcome_counts}, "
            f"trainer_bundle_denied={trainer_bundle_read_denied}, "
            f"worker_order_denied={backtest_worker_order_read_denied}"
        )

    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
    ) as uow:
        cancel_race_job = await BacktestReplayJobRepository(uow.session).enqueue(
            workspace_id=WORKSPACE_ID,
            requested_by=OWNER_MEMBERSHIP_ID,
            request=job_request,
        )
        cancel_race_job_id = cancel_race_job.id
    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_backtest_worker"
    ) as uow:
        cancel_race_claim = await BacktestReplayJobRepository(uow.session).claim_next(
            workspace_id=WORKSPACE_ID,
            worker_id="wp07-cancel-race",
            now=datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
        )
    if cancel_race_claim is None or cancel_race_claim.id != cancel_race_job_id:
        raise AssertionError("Worker không claim được job race hủy fixture.")
    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
    ) as uow:
        await BacktestReplayJobRepository(uow.session).request_cancel(
            workspace_id=WORKSPACE_ID,
            job_id=cancel_race_job_id,
            requested_by=OWNER_MEMBERSHIP_ID,
        )
    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_backtest_worker"
    ) as uow:
        cancel_race_repository = BacktestReplayJobRepository(uow.session)
        persist_transition_allowed = await cancel_race_repository.update_progress(
            workspace_id=WORKSPACE_ID,
            job_id=cancel_race_job_id,
            worker_id="wp07-cancel-race",
            generation=cancel_race_claim.lease_generation,
            stage="persist",
            progress=90,
            now=datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
        )
        cancel_won_before_persist = await cancel_race_repository.cancel_requested(
            workspace_id=WORKSPACE_ID,
            job_id=cancel_race_job_id,
            worker_id="wp07-cancel-race",
            generation=cancel_race_claim.lease_generation,
            now=datetime.now(UTC),
        )
        cancel_race_finished = await cancel_race_repository.finish(
            workspace_id=WORKSPACE_ID,
            job_id=cancel_race_job_id,
            worker_id="wp07-cancel-race",
            generation=cancel_race_claim.lease_generation,
            status="CANCELLED",
            stage="cancelled",
            progress=100,
            now=datetime.now(UTC),
        )
    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
    ) as uow:
        cancelled_job = await uow.session.get(BacktestReplayJobRecord, cancel_race_job_id)
    cancel_race_safe = (
        not persist_transition_allowed
        and cancel_won_before_persist
        and cancel_race_finished
        and cancelled_job is not None
        and cancelled_job.status == "CANCELLED"
        and cancelled_job.progress == 100
    )
    if not cancel_race_safe:
        raise AssertionError("Race hủy ngay trước persist không bị chặn đúng cách.")

    async def enqueue_lifecycle_job():
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
        ) as uow:
            return await BacktestReplayJobRepository(uow.session).enqueue(
                workspace_id=WORKSPACE_ID,
                requested_by=OWNER_MEMBERSHIP_ID,
                request=job_request,
            )

    async def claim_lifecycle_job(worker_id: str, now: datetime):
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_backtest_worker"
        ) as uow:
            return await BacktestReplayJobRepository(uow.session).claim_next(
                workspace_id=WORKSPACE_ID,
                worker_id=worker_id,
                now=now,
                lease_duration=timedelta(seconds=30),
            )

    # Hai worker thật cùng tranh một hàng đợi; chỉ một transaction được nhận job.
    concurrent_job = await enqueue_lifecycle_job()
    concurrent_now = datetime.now(UTC)
    concurrent_claims = await asyncio.gather(
        claim_lifecycle_job("wp07-claim-a", concurrent_now),
        claim_lifecycle_job("wp07-claim-b", concurrent_now),
    )
    concurrent_winners = [claim for claim in concurrent_claims if claim is not None]
    concurrent_claim_single_winner = (
        len(concurrent_winners) == 1 and concurrent_winners[0].id == concurrent_job.id
    )
    if concurrent_claim_single_winner:
        winner = concurrent_winners[0]
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
        ) as uow:
            await BacktestReplayJobRepository(uow.session).request_cancel(
                workspace_id=WORKSPACE_ID,
                job_id=concurrent_job.id,
                requested_by=OWNER_MEMBERSHIP_ID,
            )
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_backtest_worker"
        ) as uow:
            concurrent_claim_cleaned = await BacktestReplayJobRepository(uow.session).finish(
                workspace_id=WORKSPACE_ID,
                job_id=concurrent_job.id,
                worker_id=winner.lease_owner,
                generation=winner.lease_generation,
                status="CANCELLED",
                stage="cancelled",
                progress=100,
                now=datetime.now(UTC),
            )
    else:
        concurrent_claim_cleaned = False

    # Reclaim lease tăng generation và ngăn worker cũ cập nhật tiến độ.
    lease_job = await enqueue_lifecycle_job()
    lease_started = datetime.now(UTC)
    old_claim = await claim_lifecycle_job("wp07-lease-old", lease_started)
    new_claim = await claim_lifecycle_job("wp07-lease-new", lease_started + timedelta(seconds=31))
    stale_worker_fenced = False
    lease_reclaimed = (
        old_claim is not None
        and new_claim is not None
        and old_claim.id == lease_job.id == new_claim.id
        and new_claim.lease_generation > old_claim.lease_generation
    )
    if old_claim is not None and new_claim is not None and lease_reclaimed:
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_backtest_worker"
        ) as uow:
            stale_worker_fenced = not await BacktestReplayJobRepository(
                uow.session
            ).update_progress(
                workspace_id=WORKSPACE_ID,
                job_id=lease_job.id,
                worker_id="wp07-lease-old",
                generation=old_claim.lease_generation,
                stage="replay",
                progress=50,
                now=lease_started + timedelta(seconds=32),
                lease_duration=timedelta(seconds=30),
            )
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
        ) as uow:
            await BacktestReplayJobRepository(uow.session).request_cancel(
                workspace_id=WORKSPACE_ID,
                job_id=lease_job.id,
                requested_by=OWNER_MEMBERSHIP_ID,
            )
        async with DatabaseRoleUnitOfWork(
            UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_backtest_worker"
        ) as uow:
            lease_claim_cleaned = await BacktestReplayJobRepository(uow.session).finish(
                workspace_id=WORKSPACE_ID,
                job_id=lease_job.id,
                worker_id="wp07-lease-new",
                generation=new_claim.lease_generation,
                status="CANCELLED",
                stage="cancelled",
                progress=100,
                now=lease_started + timedelta(seconds=32),
            )
    else:
        lease_claim_cleaned = False

    # Hủy khi queued phải được worker kết thúc mà không chạy replay.
    queued_cancel_job = await enqueue_lifecycle_job()
    async with DatabaseRoleUnitOfWork(
        UnitOfWork(sessions, workspace_id=WORKSPACE_ID), "crypidian_admin_web"
    ) as uow:
        await BacktestReplayJobRepository(uow.session).request_cancel(
            workspace_id=WORKSPACE_ID,
            job_id=queued_cancel_job.id,
            requested_by=OWNER_MEMBERSHIP_ID,
        )
    queued_cancelled = await claim_lifecycle_job("wp07-queued-cancel", datetime.now(UTC))
    queued_cancelled_before_run = (
        queued_cancelled is not None
        and queued_cancelled.id == queued_cancel_job.id
        and queued_cancelled.status == "CANCELLED"
        and queued_cancelled.progress == 100
    )

    # Sau ba lease hết hạn, lượt claim thứ tư phải kết thúc an toàn ở 100%.
    exhausted_job = await enqueue_lifecycle_job()
    exhaustion_start = datetime.now(UTC)
    exhaustion_claims = [
        await claim_lifecycle_job(
            f"wp07-exhaust-{attempt}", exhaustion_start + timedelta(seconds=31 * attempt)
        )
        for attempt in range(3)
    ]
    exhausted = await claim_lifecycle_job(
        "wp07-exhaust-final", exhaustion_start + timedelta(seconds=93)
    )
    attempts_exhausted_cleanly = (
        all(claim is not None and claim.id == exhausted_job.id for claim in exhaustion_claims)
        and exhausted is not None
        and exhausted.id == exhausted_job.id
        and exhausted.status == "FAILED"
        and exhausted.progress == 100
        and exhausted.error_code == "WORKER_ATTEMPTS_EXHAUSTED"
    )
    lifecycle_checks_passed = (
        concurrent_claim_single_winner
        and concurrent_claim_cleaned
        and lease_reclaimed
        and stale_worker_fenced
        and lease_claim_cleaned
        and queued_cancelled_before_run
        and attempts_exhausted_cleanly
    )
    if not lifecycle_checks_passed:
        raise AssertionError(
            "Lifecycle job không đạt: "
            f"claim={concurrent_claim_single_winner}/{concurrent_claim_cleaned}, "
            f"lease={lease_reclaimed}/{stale_worker_fenced}/{lease_claim_cleaned}, "
            f"queued_cancel={queued_cancelled_before_run}, attempts={attempts_exhausted_cleanly}"
        )

    import psycopg

    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    immutable_error = ""
    try:
        with psycopg.connect(
            host="postgres",
            dbname="crypidian",
            user=os.environ["TRADING_BOT_DATABASE_USER"],
            password=password,
        ) as connection:
            connection.execute(
                "UPDATE backtest_dataset_manifest SET status='INCOMPLETE' "
                "WHERE workspace_id=%s AND dataset_hash=%s",
                (WORKSPACE_ID, dataset_manifest.dataset_hash),
            )
    except psycopg.Error as error:
        immutable_error = str(error)

    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        revoked_observation = await SourceRegistryRepository(uow.session).get_observed_entry(
            workspace_id=WORKSPACE_ID,
            source_id="BINANCE_USDM_REST",
        )
        if revoked_observation is None:
            raise AssertionError("Source policy sau revoke không có observer receipt.")
        database_now = await SourceRegistryRepository(uow.session).database_clock(
            workspace_id=WORKSPACE_ID
        )
        revoked_history = revoked_observation.history.transition(
            policy_id="BINANCE_USDM_REST:fixture-revoked",
            state=SourcePolicyState.REVOKED,
            actor_id="fixture-operator",
            at=database_now + timedelta(seconds=1),
            reason="Thu hồi fixture sau khi tạo manifest.",
        )
        await SourceRegistryRepository(uow.session).append_revision(
            workspace_id=WORKSPACE_ID,
            revision=revoked_history.current,
        )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        await SourceRegistryRepository(uow.session).finalize_policy(
            workspace_id=WORKSPACE_ID,
            source_id="BINANCE_USDM_REST",
            policy_id="BINANCE_USDM_REST:fixture-revoked",
        )

    replay_denied_after_revoke = False
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        try:
            await BacktestDatasetReplayLoader(
                repository=BacktestDatasetManifestRepository(uow.session),
                rights_gate=SourceRegistryBackfillRightsGate(
                    workspace_id=WORKSPACE_ID,
                    repository=SourceRegistryRepository(uow.session),
                ),
                market_data=MarketDataRepository(uow.session),
            ).load_for_replay(
                workspace_id=WORKSPACE_ID,
                dataset_hash=dataset_manifest.dataset_hash,
                now=NOW + timedelta(seconds=2),
            )
        except PermissionError:
            replay_denied_after_revoke = True

    bundle_replay_denied_after_revoke = False
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        try:
            await BacktestDatasetReplayBundleLoader(
                repository=BacktestDatasetReplayBundleRepository(uow.session),
                replay_loader=BacktestDatasetReplayLoader(
                    repository=BacktestDatasetManifestRepository(uow.session),
                    rights_gate=SourceRegistryBackfillRightsGate(
                        workspace_id=WORKSPACE_ID,
                        repository=SourceRegistryRepository(uow.session),
                    ),
                    market_data=MarketDataRepository(uow.session),
                ),
            ).load_for_replay(
                workspace_id=WORKSPACE_ID,
                bundle_hash=multi_timeframe_bundle.dataset_hash,
                now=NOW + timedelta(seconds=2),
            )
        except PermissionError:
            bundle_replay_denied_after_revoke = True

    executor_calls_before_revoked_request = executor_calls
    outcome_replay_denied_after_revoke = False
    try:
        await replay_service.execute(
            workspace_id=WORKSPACE_ID,
            bundle_hash=multi_timeframe_bundle.dataset_hash,
            manifest=replay_manifest,
        )
    except PermissionError:
        outcome_replay_denied_after_revoke = True
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        revoked_outcome_counts = await read_outcome_counts(uow)

    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        persisted_after_blocked_update = await BacktestDatasetManifestRepository(
            uow.session
        ).get_by_hash(
            workspace_id=WORKSPACE_ID,
            dataset_hash=dataset_manifest.dataset_hash,
        )

    other_workspace_id = UUID(int=WORKSPACE_ID.int + 1)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        await uow.session.execute(text("SET LOCAL ROLE crypidian_admin_web"))
        await uow.session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(WORKSPACE_ID)},
        )
        visible_in_owner_workspace = await uow.session.scalar(
            text(
                "SELECT count(*) FROM public.backtest_dataset_manifest "
                "WHERE dataset_hash = :dataset_hash"
            ),
            {"dataset_hash": dataset_manifest.dataset_hash},
        )
        await uow.session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(other_workspace_id)},
        )
        visible_in_other_workspace = await uow.session.scalar(
            text(
                "SELECT count(*) FROM public.backtest_dataset_manifest "
                "WHERE dataset_hash = :dataset_hash"
            ),
            {"dataset_hash": dataset_manifest.dataset_hash},
        )
        await uow.session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(WORKSPACE_ID)},
        )
        visible_bundle_in_owner_workspace = await uow.session.scalar(
            text(
                "SELECT count(*) FROM public.backtest_dataset_replay_bundle "
                "WHERE bundle_hash = :bundle_hash"
            ),
            {"bundle_hash": multi_timeframe_bundle.dataset_hash},
        )
        await uow.session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(other_workspace_id)},
        )
        visible_bundle_in_other_workspace = await uow.session.scalar(
            text(
                "SELECT count(*) FROM public.backtest_dataset_replay_bundle "
                "WHERE bundle_hash = :bundle_hash"
            ),
            {"bundle_hash": multi_timeframe_bundle.dataset_hash},
        )
        await uow.session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(WORKSPACE_ID)},
        )
        visible_job_in_owner_workspace = await uow.session.scalar(
            text("SELECT count(*) FROM public.backtest_replay_job WHERE id = :job_id"),
            {"job_id": replay_job_id},
        )
        await uow.session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(other_workspace_id)},
        )
        visible_job_in_other_workspace = await uow.session.scalar(
            text("SELECT count(*) FROM public.backtest_replay_job WHERE id = :job_id"),
            {"job_id": replay_job_id},
        )

    bundle_immutable_errors = []
    for mutation in (
        "UPDATE backtest_dataset_replay_bundle SET schema_version=1 ",
        "DELETE FROM backtest_dataset_replay_bundle ",
    ):
        try:
            with psycopg.connect(
                host="postgres",
                dbname="crypidian",
                user=os.environ["TRADING_BOT_DATABASE_USER"],
                password=password,
            ) as connection:
                connection.execute(
                    mutation + "WHERE workspace_id=%s AND bundle_hash=%s",
                    (WORKSPACE_ID, multi_timeframe_bundle.dataset_hash),
                )
        except psycopg.Error as error:
            bundle_immutable_errors.append(str(error))

    await engine.dispose()
    await admin_engine.dispose()
    return {
        "seeded": True,
        "rollback_left_no_market_rows": bars_after_rollback == 0 and cursors_after_rollback == 0,
        "committed_bars": first_count,
        "committed_cursor": cursor is not None and resumed.checkpoint.finished,
        "resume_added_no_duplicates": not resumed.bars
        and resume_provider.calls == 0
        and len(bars_after_commit) == 2,
        "backtest_manifest_ready": dataset_manifest.is_ready
        and dataset_manifest.bar_count == 2
        and dataset_manifest.warmup_observed_bar_count == 1
        and dataset_manifest.evaluation_observed_bar_count == 1
        and dataset_manifest.coverage_ratio == 1.0,
        "quote_volume_persisted": tuple(bar.quote_volume for bar in bars_after_commit)
        == (Decimal("151.5"), Decimal("151.5")),
        "backtest_manifest_persisted": first_manifest.record.dataset_hash
        == dataset_manifest.dataset_hash
        and loaded_manifest == dataset_manifest,
        "backtest_manifest_duplicate_retry": duplicate_manifest.record.id
        == first_manifest.record.id,
        "backtest_manifest_current_rights_allows_replay": replayable.manifest == dataset_manifest
        and replayable.bars == tuple(bars_after_commit)
        and replayable.current_rights.workspace_id == WORKSPACE_ID,
        "backtest_manifest_workspace_isolated": other_workspace_manifest is None
        and dataset_manifest.rights_workspace_id == WORKSPACE_ID,
        "backtest_manifest_rls_isolated": visible_in_owner_workspace == 1
        and visible_in_other_workspace == 0,
        "backtest_manifest_immutable": "WP07_BACKTEST_DATASET_MANIFEST_IMMUTABLE" in immutable_error
        and persisted_after_blocked_update == dataset_manifest,
        "backtest_manifest_replay_denied_after_revoke": replay_denied_after_revoke,
        "backtest_multitimeframe_bundle_persisted": bundle_first.record.bundle_hash
        == multi_timeframe_bundle.dataset_hash
        and loaded_bundle_reference.bundle_hash == multi_timeframe_bundle.dataset_hash
        and bundle_duplicate.record.id == bundle_first.record.id,
        "backtest_multitimeframe_bundle_intervals": tuple(
            component.manifest.interval for component in replayed_bundle.components
        )
        == ("1h", "4h"),
        "backtest_multitimeframe_bundle_current_rights_replay": replayed_bundle.dataset_hash
        == multi_timeframe_bundle.dataset_hash,
        "backtest_multitimeframe_bundle_workspace_isolated": visible_bundle_in_owner_workspace == 1
        and visible_bundle_in_other_workspace == 0,
        "backtest_multitimeframe_bundle_immutable": len(bundle_immutable_errors) == 2
        and all(
            "WP07C_REPLAY_BUNDLE_OR_RIGHTS_IMMUTABLE" in error for error in bundle_immutable_errors
        ),
        "backtest_multitimeframe_bundle_replay_denied_after_revoke": (
            bundle_replay_denied_after_revoke
        ),
        "backtest_runtime_replay_transaction_rollback": failed_persist_rolled_back,
        "backtest_runtime_replay_persisted": first_outcome_counts[0:4] == (2, 2, 2, 2)
        and first_outcome_counts[4] == expected_trade_rows
        and first_outcome_counts[5] == 4
        and len(first_receipts) == 4
        and len(replay_first.outcomes) == 2,
        "backtest_runtime_replay_retry_kept_rights_receipts": retry_counts == first_outcome_counts
        and retry_receipts == first_receipts
        and replay_retry.run_ids == replay_first.run_ids
        and all(outcome.duplicate for outcome in replay_retry.outcomes),
        "backtest_runtime_replay_denied_after_revoke": outcome_replay_denied_after_revoke
        and executor_calls == executor_calls_before_revoked_request
        and revoked_outcome_counts == worker_outcome_counts,
        "backtest_replay_job_succeeded": worker_processed_job
        and persisted_job is not None
        and persisted_job.status == "SUCCEEDED"
        and persisted_job.progress == 100
        and len(persisted_job.result_run_ids) == 1
        and worker_outcome_counts[0] == 3
        and worker_outcome_counts[5] == 6,
        "backtest_replay_job_role_boundaries": trainer_bundle_read_denied
        and backtest_worker_order_read_denied,
        "backtest_replay_job_workspace_isolation": visible_job_in_owner_workspace == 1
        and visible_job_in_other_workspace == 0,
        "backtest_replay_cancel_before_persist_wins": cancel_race_safe,
        "backtest_replay_concurrent_claim_single_winner": concurrent_claim_single_winner
        and concurrent_claim_cleaned,
        "backtest_replay_lease_reclaim_fences_stale_worker": lease_reclaimed
        and stale_worker_fenced
        and lease_claim_cleaned,
        "backtest_replay_queued_cancel_terminal": queued_cancelled_before_run,
        "backtest_replay_attempt_exhaustion_terminal": attempts_exhausted_cleanly,
        "registry_rights_gate_allowed": rights_evidence.purpose
        is BackfillRightsPurpose.BACKTEST_RESEARCH
        and rights_evidence.checked_at is not None
        and rights_evidence.policy_fingerprint
        == source_rights_hash(observed_policy.history.current),
        "network_requests": 0,
    }


print(json.dumps(asyncio.run(_exercise()), sort_keys=True))
