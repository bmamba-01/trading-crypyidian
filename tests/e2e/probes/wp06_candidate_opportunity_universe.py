"""Probe Task4 dùng PostgreSQL thật; chỉ xuất metadata, không đưa vector ra host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.adapters.database.engine import create_database_engine, database_url_from_secret
from trading_bot.adapters.database.models import Workspace
from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
from trading_bot.adapters.database.models.backtest_opportunity_entities import (
    BacktestDecisionOpportunityRecord,
)
from trading_bot.adapters.database.models.backtest_outcome_entities import (
    BacktestScenarioRecord,
    BacktestTradeOutcomeRecord,
)
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
from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeRepository,
    _canonical,
    _sha256,
    _trade_payload,
)
from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
    SOURCE_METADATA_COLUMNS,
    CandidateUniverseProofError,
    CandidateUniverseProofRepository,
)
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.backtest import BacktestRunner, EvaluationReport, ReplayManifest
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayLoader
from trading_bot.application.backtest_dataset_replay_bundle import (
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.backtest_decision_opportunities import (
    ReplayDecisionOpportunity,
    metadata_hash,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.backtest_sensitivity import SensitivityRun, SensitivityScenario
from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose, RestBackfillRequest
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService
from trading_bot.application.learning_feature_vector import build_learning_feature_vector
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.evaluation import EvaluationTrade, calculate_entry_features_hash
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
from trading_bot.domain.strategy.registry import StrategyRegistry

PHASE = {"name": "initialization"}
STATE = json.loads(os.environ["WP06_UNIVERSE_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
OTHER = uuid5(WORKSPACE, "other-workspace")
NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)
END = NOW.replace(hour=NOW.hour // 4 * 4, minute=0, second=0, microsecond=0) - timedelta(hours=4)
SOURCE = "BINANCE_USDM_REST_BACKFILL"
MEMBERSHIP = uuid5(WORKSPACE, "membership")


def pool(role):
    password = "postgres_password" if role == "admin" else f"database_{role}_password"
    engine = create_database_engine(
        database_url_from_secret(
            username=f"crypidian_{role}",
            password_file=Path("/run/secrets") / password,
            host="postgres",
            database="crypidian",
        )
    )
    engine.sync_engine.hide_parameters = True
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def must_deny(session, operation, marker=None, sqlstate=None, exact_marker=False):
    """Savepoint giữ nguyên fixture sau mỗi câu lệnh DB bị chặn."""
    failure = None
    try:
        async with session.begin_nested():
            await operation()
            await session.flush()
    except DBAPIError as error:
        failure = error
    if failure is None:
        raise AssertionError("PostgreSQL đã chấp nhận thao tác phải bị chặn.")
    if marker is not None:
        assert marker in str(failure.orig), (
            marker,
            failure.orig.sqlstate,
            str(failure.orig).splitlines()[0],
        )
    if exact_marker:
        assert marker is not None
        assert str(failure.orig).splitlines()[0] == marker
    if sqlstate is not None:
        assert failure.orig.sqlstate == sqlstate, (sqlstate, failure.orig.sqlstate)


async def finalize(sessions, revision):
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        await SourceRegistryRepository(uow.session).append_revision(
            workspace_id=WORKSPACE, revision=revision
        )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        await SourceRegistryRepository(uow.session).finalize_policy(
            workspace_id=WORKSPACE,
            source_id="BINANCE_USDM_REST",
            policy_id=revision.policy.policy_id,
        )


async def transition(sessions, state, suffix, purposes=None, ttl=timedelta(days=1)):
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        registry = SourceRegistryRepository(uow.session)
        history = await registry.get_entry(workspace_id=WORKSPACE, source_id="BINANCE_USDM_REST")
        now = await registry.database_clock(workspace_id=WORKSPACE)
    arguments = {
        "policy_id": f"Task4:{suffix}",
        "state": state,
        "actor_id": "fixture-operator",
        "at": now,
        "reason": "Kiểm quyền nguồn cho fixture Task4.",
    }
    if state is SourcePolicyState.VERIFIED:
        arguments.update(
            allowed_purposes=purposes,
            evidence_ref="https://example.invalid/task4-training",
            reviewed_at=now,
            valid_until=now + ttl,
            approved_by="fixture-reviewer",
        )
    updated = history.transition(**arguments)
    await finalize(sessions, updated.current)
    return updated.current.policy.valid_until


async def seed_source(sessions):
    instrument = uuid5(WORKSPACE, "instrument")
    raw = uuid5(WORKSPACE, "raw-reference")
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        for workspace in (WORKSPACE, OTHER):
            uow.session.add(
                Workspace(
                    id=workspace,
                    name="Task4 synthetic",
                    slug=f"universe-{workspace.hex}",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
        await uow.session.flush()
        await uow.session.execute(
            text(
                "INSERT INTO user_account(id,email_normalized,password_hash,status) VALUES "
                "(:id,:email,'fixture','ACTIVE')"
            ),
            {"id": uuid5(WORKSPACE, "user"), "email": f"{WORKSPACE}@example.test"},
        )
        await uow.session.execute(
            text(
                "INSERT INTO user_membership(id,workspace_id,user_id,status) VALUES "
                "(:id,:ws,:user,'ACTIVE')"
            ),
            {"id": MEMBERSHIP, "ws": WORKSPACE, "user": uuid5(WORKSPACE, "user")},
        )
        uow.session.add(
            MarketInstrument(
                id=instrument,
                workspace_id=WORKSPACE,
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
                event_at=END,
                received_at=END,
                available_at=END,
            )
        )
        uow.session.add(
            MarketRawReference(
                id=raw,
                workspace_id=WORKSPACE,
                source=SOURCE,
                idempotency_key="raw",
                object_key="fixture/task4",
                checksum="b" * 64,
                byte_length=0,
                content_type="application/json",
            )
        )
        await uow.session.flush()
        for timeframe, hours, count in (("1h", 1, 228), ("4h", 4, 207)):
            for index in range(count):
                opened = END - timedelta(hours=hours * (count - index))
                closed = opened + timedelta(hours=hours) - timedelta(milliseconds=1)
                key = f"{timeframe}:{opened.isoformat()}"
                uow.session.add(
                    MarketBar(
                        id=uuid5(WORKSPACE, key),
                        workspace_id=WORKSPACE,
                        instrument_id=instrument,
                        symbol="BTCUSDT",
                        timeframe=timeframe,
                        source=SOURCE,
                        logical_key=key,
                        idempotency_key=key,
                        revision=1,
                        checksum=hashlib.sha256(key.encode()).hexdigest(),
                        raw_reference_id=raw,
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
                )
    history = SourceRegistryEntry.discover(
        source_id="BINANCE_USDM_REST",
        publisher="Task4 fixture",
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
        reason="Nguồn synthetic Task4.",
    )
    history = history.transition(
        policy_id="Task4:pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="fixture-operator",
        at=NOW - timedelta(days=2),
        reason="Chờ duyệt fixture.",
    )
    history = history.transition(
        policy_id="Task4:research",
        state=SourcePolicyState.VERIFIED,
        actor_id="fixture-operator",
        at=NOW - timedelta(hours=1),
        reason="Chỉ BACKTEST_RESEARCH.",
        allowed_purposes=frozenset({SourceUsePurpose.BACKTEST_RESEARCH}),
        evidence_ref="https://example.invalid/task4-research",
        reviewed_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=1),
        approved_by="fixture-reviewer",
    )
    for revision in history.revisions:
        await finalize(sessions, revision)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        manifests = BacktestDatasetManifestRepository(uow.session)
        gate = SourceRegistryBackfillRightsGate(
            workspace_id=WORKSPACE, repository=SourceRegistryRepository(uow.session)
        )
        market = MarketDataRepository(uow.session)
        inputs = []
        for timeframe, hours, count in (("1h", 1, 228), ("4h", 4, 207)):
            request = RestBackfillRequest(
                symbol="BTCUSDT",
                interval=timeframe,
                start=END - timedelta(hours=hours * count),
                end=END,
                purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            )
            bars = await market.list_bars(
                workspace_id=WORKSPACE,
                symbol="BTCUSDT",
                timeframe=timeframe,
                start=request.start,
                end=END,
                as_of=NOW,
                closed_only=True,
                source=SOURCE,
            )
            rights = await gate.authorize(request=request, now=NOW)
            manifest = build_backtest_dataset_manifest(
                request,
                evaluation_start=END - timedelta(hours=4),
                warmup_bars=count - 4 // hours,
                bars=bars,
                rights_evidence=rights,
                prepared_at=NOW,
                schema_version=4,
            )
            await manifests.append(workspace_id=WORKSPACE, manifest=manifest)
            inputs.append(
                await BacktestDatasetReplayLoader(
                    repository=manifests, rights_gate=gate, market_data=market
                ).load_for_replay(
                    workspace_id=WORKSPACE, dataset_hash=manifest.dataset_hash, now=NOW
                )
            )
        bundle = build_backtest_dataset_replay_bundle(inputs)
        await BacktestDatasetReplayBundleRepository(uow.session).append(
            workspace_id=WORKSPACE, bundle=bundle
        )
    return bundle


def case(bundle, label, index):
    """Strategy fixture xác định bốn trạng thái từ snapshot/vector nguồn thật."""
    release = StrategyRegistry().get_release("h01-v1")
    observations = build_backtest_dataset_replay_observations(
        bundle, strategy_release_id="h01-v1", position_is_flat_at=lambda _: True
    )
    assert len(observations) == 4
    vectors = tuple(
        build_learning_feature_vector(
            item.snapshot,
            strategy_release_id="h01-v1",
            strategy_release_content_hash=release.content_hash,
        )
        for item in observations
    )
    job_id = uuid5(WORKSPACE, "job:" + label)
    manifest = ReplayManifest(
        dataset_hash=bundle.dataset_hash,
        range_start=bundle.evaluation_start,
        range_end=bundle.range_end,
        settings_version_id="task4-settings",
        risk_profile_version_id="task4-risk",
        strategy_release_id="h01-v1",
        strategy_content_hash=release.content_hash,
        feature_schema_hash=vectors[0].feature_schema_hash,
        fill_model_version="task4-fill",
        pipeline_content_hash="d" * 64,
        cost_assumptions=(("fee_rate", "0.001"),),
        parameter_family_hash="e" * 64,
        trial_count=1,
        trial_index=0,
        seed=index,
        learning_intent="TRAINING_BASE",
        learning_job_id=str(job_id),
    )
    opportunities = []
    for ordinal, (observation, vector) in enumerate(zip(observations, vectors, strict=True)):
        snapshot = observation.snapshot
        values = tuple((feature.key, float(feature.value)) for feature in vector.features)
        candidate = ordinal in (0, 2, 3)
        excluded = label == "excluded" and ordinal == 3
        digests = tuple(
            (part.timeframe, part.source_event_digest) for part in vector.timeframe_lineage
        )
        opportunities.append(
            ReplayDecisionOpportunity(
                workspace_id=WORKSPACE,
                manifest_hash=manifest.content_hash,
                bundle_hash=bundle.dataset_hash,
                scenario_id="base",
                symbol=bundle.symbol,
                strategy_release_id="h01-v1",
                strategy_content_hash=release.content_hash,
                decision_timeframe="1h",
                evaluation_start=bundle.evaluation_start,
                range_end=bundle.range_end,
                event_id=observation.event_id,
                decision_ordinal=ordinal,
                event_at=observation.event_at,
                available_at=observation.available_at,
                decision_at=snapshot.decision_time,
                snapshot_id=snapshot.snapshot_id,
                runtime_snapshot_id=None if excluded else snapshot.snapshot_id,
                feature_snapshot_ids=tuple(
                    (part.timeframe, part.feature_snapshot_id) for part in vector.timeframe_lineage
                ),
                source_event_digests=digests,
                source_refs_digest=metadata_hash(
                    [{"timeframe": tf, "source_event_digest": value} for tf, value in digests]
                ),
                feature_schema_hash=None if excluded else vector.feature_schema_hash,
                vector_hash=None if excluded else vector.vector_hash,
                feature_values_hash=None
                if excluded
                else calculate_entry_features_hash(
                    snapshot.snapshot_id, snapshot.decision_time, vector.feature_schema_hash, values
                ),
                input_state="INPUT_EXCLUDED" if excluded else "EVALUATED",
                strategy_state="NOT_RUN"
                if excluded
                else "CANDIDATE"
                if candidate
                else "NO_CANDIDATE",
                decision_outcome="NO_TRADE"
                if excluded or not candidate
                else "PAPER_INTENT_CREATED"
                if ordinal in (0, 3)
                else "RISK_REJECTED",
                reason_code="INPUT_NOT_READY" if excluded else "TASK4_SYNTHETIC_STATE",
                candidate_id=None
                if excluded or not candidate
                else metadata_hash([label, ordinal, "candidate"]),
            )
        )
    entry = opportunities[0]
    values = tuple((feature.key, float(feature.value)) for feature in vectors[0].features)
    trade = EvaluationTrade(
        trade_id="task4:" + label,
        opened_at=entry.decision_at + timedelta(microseconds=1),
        closed_at=observations[1].available_at,
        gross_pnl=Decimal("4"),
        fees=Decimal("1"),
        funding=Decimal(0),
        slippage=Decimal(0),
        exposure=Decimal("100"),
        entry_decision_snapshot_id=entry.runtime_snapshot_id,
        entry_decision_at=entry.decision_at,
        entry_feature_schema_hash=entry.feature_schema_hash,
        entry_features=values,
        entry_features_hash=entry.feature_values_hash,
        entry_symbol=bundle.symbol,
        entry_opportunity_event_id=entry.event_id,
        entry_candidate_id=entry.candidate_id,
        entry_vector_hash=entry.vector_hash,
    )

    class ClosedTradeFixture:
        """Phát outcome ở event đóng; giữ entry event trước đó như runtime."""

        emits_trade_on_close = True

        def __call__(self, snapshot):
            return (
                trade if snapshot.decision_time == observations[1].snapshot.decision_time else None
            )

    report = BacktestRunner().run(observations, manifest=manifest, evaluator=ClosedTradeFixture())
    assert isinstance(report, EvaluationReport), report
    return SimpleNamespace(
        run=SensitivityRun(SensitivityScenario("base", "BASE"), manifest, report),
        trades=(trade,),
        opportunities=tuple(opportunities),
        job_id=job_id,
        label=label,
    )


async def persist_case(admin, writer, bundle, item, partial=False):
    request = BacktestReplayJobSubmission(
        bundle_hash=bundle.dataset_hash,
        strategy_release_id="h01-v1",
        execution_interval="1h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal(0),
        seed=item.run.manifest.seed,
        learning_intent="TRAINING_BASE",
    )
    payload = request.to_payload()
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        uow.session.add(
            BacktestReplayJobRecord(
                id=item.job_id,
                workspace_id=WORKSPACE,
                requested_by=MEMBERSHIP,
                bundle_hash=bundle.dataset_hash,
                strategy_release_id="h01-v1",
                execution_interval="1h",
                request_payload=payload,
                canonical_request=_canonical(payload),
                status="RUNNING",
                stage="persist",
                progress=80,
                cancel_requested=False,
                attempt_count=1,
                lease_generation=1,
                lease_owner="task4-writer",
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
                result_run_ids=[],
            )
        )
    async with UnitOfWork(writer, workspace_id=WORKSPACE) as uow:
        assert await uow.session.scalar(text("SELECT session_user")) == "crypidian_replay_writer"
        await uow.session.execute(
            text("SELECT begin_backtest_job_persist(:ws,:job,'task4-writer',1)"),
            {"ws": WORKSPACE, "job": item.job_id},
        )
        repo = BacktestOutcomeRepository(uow.session)
        # Partial case mô phỏng writer crash trước nến cuối; không sửa row đã ghi.
        method = repo._append_sensitivity_run if partial else repo.append_sensitivity_run
        item.receipt = await method(
            workspace_id=WORKSPACE,
            sensitivity_run=item.run,
            trade_outcomes=item.trades,
            dataset_bundle=bundle,
            opportunities=item.opportunities[:-1] if partial else item.opportunities,
        )
        await uow.session.execute(
            text(
                "SELECT complete_backtest_job_persist(:ws,:job,'task4-writer',1,CAST(:runs AS"
                " uuid[]))"
            ),
            {"ws": WORKSPACE, "job": item.job_id, "runs": [item.receipt.run_id]},
        )
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        item.scenario_id = await uow.session.scalar(
            select(BacktestScenarioRecord.id).where(
                BacktestScenarioRecord.trial_record_id == item.receipt.trial_record_id
            )
        )
    return item


async def append_opportunity(session, item, opportunity):
    return await BacktestOutcomeRepository(session)._append_opportunity(
        appended=item.receipt, scenario_id=item.scenario_id, opportunity=opportunity
    )


async def expect_proof_denied(builder, item):
    try:
        await CandidateUniverseProofService(builder).build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=item.receipt.trial_record_id
        )
    except CandidateUniverseProofError:
        return
    raise AssertionError("Proof đã vượt boundary phải từ chối.")


async def expect_rights_denied(engine, builder, item):
    """Không được xem conflict proof sau đọc payload là rights denial hợp lệ."""
    statements = []

    def trace(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", trace)
    try:
        await expect_proof_denied(builder, item)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", trace)
    assert not any(
        "FROM backtest_trade_outcome" in sql
        or "FROM backtest_report" in sql
        or "FROM market_bar" in sql
        for sql in statements
    )
    return True


async def acl(builder, writer, admin, item):
    async with UnitOfWork(builder, workspace_id=WORKSPACE) as uow:
        session = uow.session
        assert await session.scalar(text("SELECT session_user")) == "crypidian_candidate_builder"
        privileges = dict(
            (
                await session.execute(
                    text(
                        "SELECT "
                        "column_name,has_column_privilege(current_user,'market_bar',column_name,'SELECT')"
                        " FROM information_schema.columns WHERE table_schema='public' AND "
                        "table_name='market_bar'"
                    )
                )
            ).all()
        )
        # information_schema chỉ hiện cột được cấp; đối chiếu catalog toàn bảng riêng.
        catalog = (
            await session.execute(
                text(
                    "SELECT "
                    "attname,has_column_privilege(current_user,'market_bar',attname,'SELECT') "
                    "FROM pg_attribute WHERE attrelid='market_bar'::regclass AND attnum>0 AND NOT"
                    " attisdropped"
                )
            )
        ).all()
        assert {name for name, allowed in catalog if allowed} == set(SOURCE_METADATA_COLUMNS)
        assert not await session.scalar(
            text("SELECT has_table_privilege(current_user,'market_bar','SELECT')")
        )
        assert (
            len(
                (
                    await session.execute(
                        select(*(getattr(MarketBar, key) for key in SOURCE_METADATA_COLUMNS))
                    )
                ).all()
            )
            == 435
        )
        for column in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "source_cursor",
            "received_at",
            "created_at",
        ):
            await must_deny(
                session,
                lambda col=column: session.execute(text(f"SELECT {col} FROM market_bar LIMIT 0")),
                sqlstate="42501",
            )
        await must_deny(
            session,
            lambda: session.execute(text("SELECT object_key FROM market_raw_reference LIMIT 0")),
            sqlstate="42501",
        )
        flags = (
            await session.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE "
                    "oid='market_bar'::regclass"
                )
            )
        ).one()
        assert tuple(flags) == (True, True)
        for workspace in (str(OTHER), ""):
            await session.execute(
                text("SELECT set_config('app.workspace_id',:ws,true)"), {"ws": workspace}
            )
            assert await session.scalar(text("SELECT count(id) FROM market_bar")) == 0
            assert (
                await session.scalar(text("SELECT count(*) FROM backtest_decision_opportunity"))
                == 0
            )
        await session.execute(
            text("SELECT set_config('app.workspace_id',:ws,true)"), {"ws": str(WORKSPACE)}
        )
        changed = replace(item.opportunities[-1], event_id="a" * 64)
        await must_deny(
            session, lambda: append_opportunity(session, item, changed), sqlstate="42501"
        )
    async with UnitOfWork(writer, workspace_id=WORKSPACE) as uow:
        await must_deny(
            uow.session,
            lambda: uow.session.execute(
                text(
                    "INSERT INTO candidate_universe_proof SELECT * FROM candidate_universe_proof "
                    "LIMIT 0"
                )
            ),
            sqlstate="42501",
        )
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        assert (
            await uow.session.execute(
                text(
                    "SELECT rolbypassrls,rolsuper FROM pg_roles WHERE "
                    "rolname='crypidian_candidate_builder'"
                )
            )
        ).one() == (False, False)
    return len(privileges)


async def negative_rows(writer, item):
    """Canonical/scope/link sai phải bị PostgreSQL hoặc scanner từ chối."""
    async with UnitOfWork(writer, workspace_id=WORKSPACE) as uow:
        for field, value in (
            ("strategy_content_hash", "0" * 64),
            ("bundle_hash", "0" * 64),
            ("strategy_release_id", "forged"),
            ("decision_ordinal", 99),
        ):
            changed = replace(item.opportunities[-1], **{field: value})
            await must_deny(
                uow.session,
                lambda op=changed: append_opportunity(uow.session, item, op),
                marker="WP06_OPPORTUNITY_SCOPE_MISMATCH",
            )
        # Hai natural keys bị lặp, vẫn buộc DB kiểm unique chứ không nhận claimed count.
        original = item.opportunities[-1]
        for changed in (
            replace(original, decision_ordinal=0),
            replace(original, event_id=item.opportunities[0].event_id),
        ):
            payload = changed.canonical_payload()
            values = {
                key: getattr(changed, key)
                for key in payload
                if key not in {"scenario_id", "feature_snapshot_ids", "source_event_digests"}
            }
            record = BacktestDecisionOpportunityRecord(
                id=uuid4(),
                run_id=item.receipt.run_id,
                trial_record_id=item.receipt.trial_record_id,
                scenario_id=item.scenario_id,
                scenario_key="base",
                **values,
                feature_snapshot_ids=[list(pair) for pair in changed.feature_snapshot_ids],
                source_event_digests=[list(pair) for pair in changed.source_event_digests],
                canonical_opportunity=changed.canonical_json(),
                content_hash=changed.content_hash,
                idempotency_key=changed.idempotency_key,
            )

            async def duplicate(row=record):
                uow.session.add(row)
                await uow.session.flush()

            await must_deny(uow.session, duplicate, sqlstate="23505")
        for field, value in (
            ("workspace_id", OTHER),
            ("run_id", uuid4()),
            ("candidate_id", None),
            ("strategy_state", "NO_CANDIDATE"),
        ):
            changed = item.opportunities[-1]
            payload = changed.canonical_payload()
            values = {
                key: getattr(changed, key)
                for key in payload
                if key not in {"scenario_id", "feature_snapshot_ids", "source_event_digests"}
            }
            values.update(
                run_id=item.receipt.run_id,
                trial_record_id=item.receipt.trial_record_id,
                scenario_id=item.scenario_id,
                scenario_key="base",
            )
            values[field] = value
            record = BacktestDecisionOpportunityRecord(
                id=uuid4(),
                **values,
                feature_snapshot_ids=[list(pair) for pair in changed.feature_snapshot_ids],
                source_event_digests=[list(pair) for pair in changed.source_event_digests],
                canonical_opportunity=changed.canonical_json(),
                content_hash=changed.content_hash,
                idempotency_key=changed.idempotency_key,
            )

            if field == "strategy_state":
                document = json.loads(changed.canonical_json())
                document["strategy_state"] = value
                record.canonical_opportunity = _canonical(document)
                record.content_hash = _sha256(record.canonical_opportunity)

            async def invalid_row(row=record):
                uow.session.add(row)
                await uow.session.flush()

            if field == "strategy_state":
                await must_deny(uow.session, invalid_row, sqlstate="23514")
            else:
                await must_deny(
                    uow.session,
                    invalid_row,
                    marker={
                        "workspace_id": "WP06_OPPORTUNITY_WORKSPACE_MISMATCH",
                        "run_id": "WP06_OPPORTUNITY_SCOPE_MISMATCH",
                        "candidate_id": "WP06_OPPORTUNITY_CANONICAL_MISMATCH",
                    }[field],
                )
        for field, value in (
            ("entry_candidate_id", "0" * 64),
            ("entry_vector_hash", "0" * 64),
            ("entry_decision_snapshot_id", "0" * 64),
            ("entry_feature_schema_hash", "0" * 64),
            ("entry_opportunity_event_id", "not-an-entry"),
            ("entry_symbol", "ETHUSDT"),
        ):
            changes = {field: value}
            original_trade = item.trades[0]
            if field in {"entry_decision_snapshot_id", "entry_feature_schema_hash"}:
                changes["entry_features_hash"] = calculate_entry_features_hash(
                    changes.get(
                        "entry_decision_snapshot_id", original_trade.entry_decision_snapshot_id
                    ),
                    original_trade.entry_decision_at,
                    changes.get(
                        "entry_feature_schema_hash", original_trade.entry_feature_schema_hash
                    ),
                    original_trade.entry_features,
                )
            trade = replace(original_trade, **changes)
            payload = _trade_payload(trade)
            record = BacktestTradeOutcomeRecord(
                id=uuid4(),
                workspace_id=WORKSPACE,
                run_id=item.receipt.run_id,
                trial_record_id=item.receipt.trial_record_id,
                manifest_hash=item.run.manifest.content_hash,
                ordinal=1,
                trade_id=trade.trade_id,
                outcome_hash=_sha256(_canonical(payload)),
                outcome=payload,
                canonical_json=_canonical(payload),
                **{
                    key: getattr(trade, key)
                    for key in payload
                    if key.startswith("entry_") and key != "entry_features"
                },
                entry_features=[list(pair) for pair in trade.entry_features],
            )

            async def wrong_entry(row=record):
                uow.session.add(row)
                await uow.session.flush()

            await must_deny(uow.session, wrong_entry, marker="WP06_OUTCOME_OPPORTUNITY_MISMATCH")
        record.workspace_id = OTHER
        record.entry_symbol = item.trades[0].entry_symbol

        async def cross_tenant_outcome():
            uow.session.add(record)
            await uow.session.flush()

        await must_deny(
            uow.session, cross_tenant_outcome, marker="WP06_OUTCOME_OPPORTUNITY_WORKSPACE_MISMATCH"
        )


@asynccontextmanager
async def damaged_fixture(admin, table, record_id, field, value):
    """Fault injection chỉ trong DB test; luôn phục hồi và bật trigger trước yield."""
    assert table in {"market_bar", "backtest_scenario", "backtest_decision_opportunity"}
    assert field in {"id", "revision", "timeframe", "available_at", "kind", "decision_ordinal"}
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        original = await uow.session.scalar(
            text(f"SELECT {field} FROM {table} WHERE id=:id"), {"id": record_id}
        )
        await uow.session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        await uow.session.execute(
            text(f"UPDATE {table} SET {field}=:value WHERE id=:id"),
            {"value": value, "id": record_id},
        )
        await uow.session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    try:
        yield
    finally:
        current_id = value if field == "id" else record_id
        async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
            await uow.session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
            await uow.session.execute(
                text(f"UPDATE {table} SET {field}=:value WHERE id=:id"),
                {"value": original, "id": current_id},
            )
            await uow.session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))


async def scanner_negatives(admin, writer, builder, item, source_gap_case):
    """Gieo corruption có kiểm soát rồi gọi service dưới login builder thật."""
    checks = []
    original = item.opportunities[-1]
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        one_hour = await uow.session.scalar(
            select(MarketBar.id)
            .where(MarketBar.workspace_id == WORKSPACE, MarketBar.timeframe == "1h")
            .order_by(MarketBar.open_time.desc())
            .limit(1)
        )
        four_hour = await uow.session.scalar(
            select(MarketBar.id)
            .where(MarketBar.workspace_id == WORKSPACE, MarketBar.timeframe == "4h")
            .order_by(MarketBar.open_time.desc())
            .limit(1)
        )
    for label, changed in (
        ("extra_event", replace(original, event_id="9" * 64)),
        ("feature_schema", replace(original, feature_schema_hash="0" * 64)),
    ):
        PHASE["name"] = "scanner:" + label
        async with UnitOfWork(writer, workspace_id=WORKSPACE) as uow:
            await append_opportunity(uow.session, item, changed)
        try:
            if label == "extra_event":
                async with damaged_fixture(
                    admin, "market_bar", one_hour, "available_at", NOW + timedelta(days=1)
                ):
                    await expect_proof_denied(builder, item)
            else:
                await expect_proof_denied(builder, item)
            checks.append(label)
        finally:
            async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
                await uow.session.execute(
                    text("ALTER TABLE backtest_decision_opportunity DISABLE TRIGGER USER")
                )
                await uow.session.execute(
                    text(
                        "DELETE FROM backtest_decision_opportunity "
                        "WHERE trial_record_id=:trial AND event_id=:event"
                    ),
                    {"trial": item.receipt.trial_record_id, "event": changed.event_id},
                )
                await uow.session.execute(
                    text("ALTER TABLE backtest_decision_opportunity ENABLE TRIGGER USER")
                )
    for label, record_id, field, value in (
        ("source_identity", one_hour, "id", uuid4()),
        ("source_revision", one_hour, "revision", 2),
        ("source_timeframe", one_hour, "timeframe", "4h"),
    ):
        PHASE["name"] = "scanner:" + label
        async with damaged_fixture(admin, "market_bar", record_id, field, value):
            await expect_proof_denied(builder, item)
        checks.append(label)
    async with damaged_fixture(admin, "backtest_scenario", item.scenario_id, "kind", "COST"):
        await expect_proof_denied(builder, item)
    checks.append("not_base")
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        opportunity_id = await uow.session.scalar(
            select(BacktestDecisionOpportunityRecord.id).where(
                BacktestDecisionOpportunityRecord.trial_record_id == item.receipt.trial_record_id,
                BacktestDecisionOpportunityRecord.decision_ordinal == 1,
            )
        )
    async with (
        damaged_fixture(admin, "market_bar", four_hour, "available_at", NOW + timedelta(days=1)),
        damaged_fixture(
            admin, "backtest_decision_opportunity", opportunity_id, "decision_ordinal", 99
        ),
    ):
        await expect_proof_denied(builder, item)
    checks.append("ordinal_with_other_child_gap")
    async with damaged_fixture(
        admin, "market_bar", one_hour, "available_at", NOW + timedelta(days=1)
    ):
        result = await CandidateUniverseProofService(builder).build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=source_gap_case.receipt.trial_record_id
        )
        assert result.proof.completeness_verified is False
        assert result.proof.opportunity_count == 4
    checks.append("missing_source_diagnostic")
    return checks


async def observed_wait(monitor, engine, fragment, operation, blocker):
    """PID và pg_blocking_pids là bằng chứng; polling không suy lock từ sleep."""
    seen = asyncio.get_running_loop().create_future()

    def on_sql(connection, cursor, statement, parameters, context, executemany):
        if fragment in statement and not seen.done():
            seen.set_result(connection.connection.driver_connection.info.backend_pid)

    event.listen(engine.sync_engine, "before_cursor_execute", on_sql)
    task = asyncio.create_task(operation())
    try:
        pid = await asyncio.wait_for(seen, 10)
        async with monitor() as session:
            for _ in range(300):
                await session.execute(text("SELECT pg_stat_clear_snapshot()"))
                blockers = await session.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pid})
                if blocker in blockers:
                    return task, {"waiter_pid": pid, "blocker_pid": blocker}
                if task.done():
                    await task
                    raise AssertionError(
                        "Thao tác kết thúc trước khi PostgreSQL xác nhận lock wait."
                    )
                await asyncio.sleep(0.02)
        raise AssertionError("Không thấy đúng blocker PID trong pg_blocking_pids.")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", on_sql)
        if not seen.done():
            seen.cancel()


async def races(admin, writer, builder, writer_engine, builder_engine, first, second):
    async with UnitOfWork(writer, workspace_id=WORKSPACE) as uow:
        await append_opportunity(uow.session, first, first.opportunities[-1])
        pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        task, first_pids = await observed_wait(
            admin,
            builder_engine,
            "wp06_lock_candidate_trial",
            lambda: CandidateUniverseProofService(builder).build_decision_opportunity(
                workspace_id=WORKSPACE, trial_record_id=first.receipt.trial_record_id
            ),
            pid,
        )
    result = await asyncio.wait_for(task, 20)
    assert result.proof.completeness_verified
    assert result.proof.opportunity_count == 4
    async with UnitOfWork(builder, workspace_id=WORKSPACE) as uow:
        proof = await CandidateUniverseProofRepository(uow.session).build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=second.receipt.trial_record_id
        )
        assert proof.proof.completeness_verified is False
        pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))

        async def append_late():
            async with UnitOfWork(writer, workspace_id=WORKSPACE) as write:
                await append_opportunity(write.session, second, second.opportunities[-1])

        task, second_pids = await observed_wait(
            admin, writer_engine, "INSERT INTO backtest_decision_opportunity", append_late, pid
        )
    try:
        await asyncio.wait_for(task, 20)
    except DBAPIError as error:
        if "WP06_OUTCOME_TRIAL_SEALED_BY_PROOF" not in str(error.orig):
            raise AssertionError("Writer bị chặn bởi lỗi khác seal fence.") from error
    else:
        raise AssertionError("Writer đã vượt proof seal sau khi được đánh thức.")
    return first_pids, second_pids


async def source_lock_race(admin, admin_engine, builder, item):
    """Policy writer phải đợi source lock đang được proof giữ tới commit."""
    async with UnitOfWork(builder, workspace_id=WORKSPACE) as uow:
        await CandidateUniverseProofRepository(uow.session).build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=item.receipt.trial_record_id
        )
        holder = await uow.session.scalar(text("SELECT pg_backend_pid()"))

        async def append_policy():
            async with UnitOfWork(admin, workspace_id=WORKSPACE) as policy:
                registry = SourceRegistryRepository(policy.session)
                history = await registry.get_entry(
                    workspace_id=WORKSPACE, source_id="BINANCE_USDM_REST"
                )
                now = await registry.database_clock(workspace_id=WORKSPACE)
                pending = history.transition(
                    policy_id="Task4:lock-pending",
                    state=SourcePolicyState.RIGHTS_PENDING,
                    actor_id="fixture-operator",
                    at=now,
                    reason="Kiểm source lock tới proof commit.",
                )
                await registry.append_revision(workspace_id=WORKSPACE, revision=pending.current)

        task, pids = await observed_wait(
            admin, admin_engine, "pg_advisory_xact_lock", append_policy, holder
        )
    await asyncio.wait_for(task, 20)
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        await SourceRegistryRepository(uow.session).finalize_policy(
            workspace_id=WORKSPACE, source_id="BINANCE_USDM_REST", policy_id="Task4:lock-pending"
        )
    await transition(
        admin, SourcePolicyState.VERIFIED, "after-lock", frozenset({SourceUsePurpose.TRAINING})
    )
    return pids


async def delete_immutability(admin, builder, writer):
    """Tách ACL role thật và trigger bất biến; DELETE phải giữ nguyên mọi row."""
    markers = {
        "backtest_decision_opportunity": "WP06_OPPORTUNITY_IMMUTABLE",
        "candidate_universe_proof": "WP06_CANDIDATE_PROOF_IMMUTABLE",
        "candidate_training_rights": "WP06_CANDIDATE_PROOF_IMMUTABLE",
    }
    checks = {}
    async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
        for table, marker in markers.items():
            before = await uow.session.scalar(text(f"SELECT count(*) FROM {table}"))
            assert before > 0
            await must_deny(
                uow.session,
                lambda target=table: uow.session.execute(text(f"DELETE FROM {target}")),
                marker=marker,
                sqlstate="P0001",
                exact_marker=True,
            )
            assert await uow.session.scalar(text(f"SELECT count(*) FROM {table}")) == before
            checks[table] = {
                "trigger_marker": marker,
                "trigger_sqlstate": "P0001",
                "rows_preserved": True,
            }
    for role, sessions in (("candidate_builder", builder), ("replay_writer", writer)):
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            assert await uow.session.scalar(text("SELECT session_user")) == f"crypidian_{role}"
            for table in markers:
                await must_deny(
                    uow.session,
                    lambda target=table: uow.session.execute(
                        text(f"DELETE FROM {target} WHERE workspace_id=:ws"), {"ws": WORKSPACE}
                    ),
                    sqlstate="42501",
                )
                checks[table][f"{role}_sqlstate"] = "42501"
    return checks


async def verify():
    admin_engine, admin = pool("admin")
    writer_engine, writer = pool("replay_writer")
    builder_engine, builder = pool("candidate_builder")
    try:
        bundle = await seed_source(admin)
        cases = {}
        for index, label in enumerate(
            (
                "mixed",
                "excluded",
                "missing",
                "writer-first",
                "proof-first",
                "negative",
                "revoked",
                "source-gap",
            ),
            1,
        ):
            cases[label] = await persist_case(
                admin,
                writer,
                bundle,
                case(bundle, label, index),
                partial=label in {"missing", "writer-first", "proof-first", "negative"},
            )
        metadata_columns = await acl(builder, writer, admin, cases["negative"])
        rights_cases = {
            "research_only": await expect_rights_denied(builder_engine, builder, cases["mixed"])
        }
        async with UnitOfWork(builder, workspace_id=WORKSPACE) as uow:
            assert (
                await uow.session.scalar(text("SELECT count(*) FROM candidate_universe_proof")) == 0
            )
        await transition(admin, SourcePolicyState.RIGHTS_PENDING, "training-pending")
        await transition(
            admin, SourcePolicyState.VERIFIED, "training", frozenset({SourceUsePurpose.TRAINING})
        )
        service = CandidateUniverseProofService(builder)
        result = await service.build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=cases["mixed"].receipt.trial_record_id
        )
        retry = await service.build_decision_opportunity(
            workspace_id=WORKSPACE, trial_record_id=cases["mixed"].receipt.trial_record_id
        )
        assert retry.record_id == result.record_id
        assert retry.duplicate
        assert retry.proof.canonical_json == result.proof.canonical_json
        positive = {
            key: getattr(result.proof, key)
            for key in (
                "opportunity_count",
                "candidate_count",
                "no_candidate_count",
                "eligible_count",
                "unlabeled_candidate_count",
                "completeness_verified",
            )
        }
        assert positive == {
            "opportunity_count": 4,
            "candidate_count": 3,
            "no_candidate_count": 1,
            "eligible_count": 1,
            "unlabeled_candidate_count": 2,
            "completeness_verified": True,
        }
        for label in ("excluded", "missing"):
            diagnostic = await service.build_decision_opportunity(
                workspace_id=WORKSPACE, trial_record_id=cases[label].receipt.trial_record_id
            )
            assert diagnostic.proof.completeness_verified is False
            assert diagnostic.proof.opportunity_count == (4 if label == "excluded" else 3)
        async with UnitOfWork(builder, workspace_id=WORKSPACE) as uow:
            receipts = (
                await uow.session.execute(
                    text(
                        "SELECT purpose,count(*) FROM candidate_training_rights WHERE proof_id=:id "
                        "GROUP BY purpose"
                    ),
                    {"id": result.record_id},
                )
            ).all()
            assert receipts == [("training", 2)]
            rights_cases["training_child_receipts"] = True
            assert (
                await uow.session.scalar(
                    text("SELECT count(*) FROM candidate_universe_proof WHERE id=:id"),
                    {"id": result.record_id},
                )
                == 1
            )
            for tenant in (str(OTHER), ""):
                await uow.session.execute(
                    text("SELECT set_config('app.workspace_id',:tenant,true)"), {"tenant": tenant}
                )
                assert (
                    await uow.session.scalar(text("SELECT count(*) FROM candidate_universe_proof"))
                    == 0
                )
                assert (
                    await uow.session.scalar(text("SELECT count(*) FROM candidate_training_rights"))
                    == 0
                )
        await negative_rows(writer, cases["negative"])
        scanner_checks = await scanner_negatives(
            admin, writer, builder, cases["negative"], cases["source-gap"]
        )
        first_pids, second_pids = await races(
            admin,
            writer,
            builder,
            writer_engine,
            builder_engine,
            cases["writer-first"],
            cases["proof-first"],
        )
        async with UnitOfWork(writer, workspace_id=WORKSPACE) as uow:
            item = cases["mixed"]
            await must_deny(
                uow.session,
                lambda: append_opportunity(
                    uow.session, item, replace(item.opportunities[-1], event_id="f" * 64)
                ),
                marker="WP06_OUTCOME_TRIAL_SEALED_BY_PROOF",
            )
            await must_deny(
                uow.session,
                lambda: uow.session.execute(
                    text(
                        "INSERT INTO backtest_trade_outcome SELECT * FROM backtest_trade_outcome "
                        "WHERE trial_record_id=:id"
                    ),
                    {"id": item.receipt.trial_record_id},
                ),
                marker="WP06_OUTCOME_TRIAL_SEALED_BY_PROOF",
            )
        async with UnitOfWork(admin, workspace_id=WORKSPACE) as uow:
            for table, marker in (
                ("backtest_decision_opportunity", "WP06_OPPORTUNITY_IMMUTABLE"),
                ("candidate_universe_proof", "WP06_CANDIDATE_PROOF_IMMUTABLE"),
                ("candidate_training_rights", "WP06_CANDIDATE_PROOF_IMMUTABLE"),
            ):
                await must_deny(
                    uow.session,
                    lambda target=table: uow.session.execute(text(f"UPDATE {target} SET id=id")),
                    marker=marker,
                )
        delete_checks = await delete_immutability(admin, builder, writer)
        policy_pids = await source_lock_race(admin, admin_engine, builder, cases["mixed"])
        await transition(admin, SourcePolicyState.REVOKED, "revoked")
        rights_cases["source_policy_lock"] = True
        rights_cases["revoked_new"] = await expect_rights_denied(
            builder_engine, builder, cases["revoked"]
        )
        rights_cases["revoked_retry"] = await expect_rights_denied(
            builder_engine, builder, cases["mixed"]
        )
        await transition(admin, SourcePolicyState.RIGHTS_PENDING, "expired-pending")
        expiry = await transition(
            admin,
            SourcePolicyState.VERIFIED,
            "before-expired",
            frozenset({SourceUsePurpose.TRAINING}),
            ttl=timedelta(milliseconds=200),
        )
        async with admin() as session:
            await session.execute(text("SELECT pg_sleep(0.25)"))
            assert await session.scalar(
                text("SELECT clock_timestamp() >= :expiry"), {"expiry": expiry}
            )
        rights_cases["expired_verified_policy"] = await expect_rights_denied(
            builder_engine, builder, cases["revoked"]
        )
        await transition(admin, SourcePolicyState.EXPIRED, "expired")
        rights_cases["expired_state"] = await expect_rights_denied(
            builder_engine, builder, cases["revoked"]
        )
        return {
            "metadata_columns": metadata_columns,
            "positive": positive,
            "payload_denied": True,
            "rls": True,
            "rights": True,
            "rights_cases": rights_cases,
            "incomplete": True,
            "negative": True,
            "writer_before_proof": True,
            "proof_before_writer": True,
            "sealed": True,
            "immutable": True,
            "delete_checks": delete_checks,
            "race_pids": {
                "writer_first": first_pids,
                "proof_first": second_pids,
                "source_policy": policy_pids,
            },
            "scanner_negative_checks": scanner_checks,
        }
    finally:
        await builder_engine.dispose()
        await writer_engine.dispose()
        await admin_engine.dispose()


async def post_lifecycle():
    """Failed downgrade phải rollback cả revoke grants của revision phía trên."""
    engine, builder = pool("candidate_builder")
    try:
        async with UnitOfWork(builder, workspace_id=WORKSPACE) as uow:
            assert (
                await uow.session.scalar(text("SELECT session_user"))
                == "crypidian_candidate_builder"
            )
            rows = (
                (
                    await uow.session.execute(
                        text(
                            "SELECT attname FROM pg_attribute "
                            "WHERE attrelid='market_bar'::regclass AND attnum>0 "
                            "AND NOT attisdropped AND "
                            "has_column_privilege(current_user,'market_bar',attname,'SELECT')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(rows) == set(SOURCE_METADATA_COLUMNS)
            assert await uow.session.scalar(text("SELECT count(id) FROM market_bar")) == 435
        return {"grant_retained_after_populated_downgrade": True, "metadata_columns": len(rows)}
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        verified = asyncio.run(post_lifecycle() if STATE["mode"] == "post-lifecycle" else verify())
    except DBAPIError as failure:
        # Chỉ primary message, không SQL parameters/DETAIL có thể chứa feature values.
        raise AssertionError(
            {
                "phase": PHASE["name"],
                "sqlstate": failure.orig.sqlstate,
                "primary": str(failure.orig).splitlines()[0],
            }
        ) from None
    print(json.dumps(verified, sort_keys=True))
