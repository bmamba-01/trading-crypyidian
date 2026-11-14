"""Probe proof BASE với current TRAINING, tenant isolation và concurrent fence."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.models import (
    BacktestDatasetManifestRecord,
    BacktestDatasetReplayBundleRecord,
    MarketInstrument,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
    CandidateUniverseProofError,
)
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.backtest import BacktestRunner, EvaluationReport, ReplayObservation
from trading_bot.application.backtest_sensitivity import SensitivityRun, SensitivityScenario
from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService
from trading_bot.application.first_party_qa_dataset import (
    CRYPIDIAN_FIRST_PARTY_QA_SOURCE,
    FirstPartyQADatasetRequest,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.evaluation import EvaluationTrade, calculate_entry_features_hash
from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.workers.research_rights_worker import ResearchRightsMaintenanceWorker

sys.path.insert(0, "/e2e-tests")
from fixtures.runtime_replay import build_h03_source_pinned_dataset_replay
from fixtures.wp06_qa_license import QA_EVIDENCE_REF, QA_PROVIDER, licensed_qa_source_fixture

WORKSPACE = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
STATE = json.loads(os.environ.get("WP06_STATE", "{}"))


def engine_and_sessions():
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    return engine, create_session_factory(engine)


async def seed():
    import json

    from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
    from trading_bot.application.backtest_dataset_replay_bundle import (
        BacktestDatasetReplayBundleReference,
    )
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
    from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
    from trading_bot.domain.strategy.contracts import DecisionSnapshot

    engine, sessions = engine_and_sessions()
    membership_id = uuid4()
    account_id = uuid4()
    job_ids = tuple(uuid4() for _ in range(7))
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO workspace (id,name,slug,mode,status) VALUES (:id,'Proof "
                "E2E',:slug,'PAPER','ACTIVE')"
            ),
            {"id": WORKSPACE, "slug": f"proof-{WORKSPACE}"},
        )
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(WORKSPACE)},
        )
        await session.execute(
            text(
                "INSERT INTO user_account(id,email_normalized,password_hash,status) "
                "VALUES (:id,:email,'fixture','ACTIVE')"
            ),
            {"id": account_id, "email": f"{account_id}@example.test"},
        )
        await session.execute(
            text(
                "INSERT INTO user_membership(id,workspace_id,user_id,status) "
                "VALUES (:id,:workspace,:user_id,'ACTIVE')"
            ),
            {"id": membership_id, "workspace": WORKSPACE, "user_id": account_id},
        )
    dataset_start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=206
    )
    range_end = dataset_start + timedelta(hours=206)
    qa_request = FirstPartyQADatasetRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=dataset_start,
        end=range_end,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    policy_now = datetime.now(UTC)
    history = (
        SourceRegistryEntry.discover(
            source_id=QA_PROVIDER,
            publisher="Crypidian Repository QA Fixture",
            endpoint_url=QA_EVIDENCE_REF,
            access_method="GET",
            auth_scope="repo-owned synthetic QA data; no exchange account",
            budget=SourceBudget(
                window=timedelta(hours=1),
                max_requests=100,
                max_bytes=1024,
                max_cost=Decimal("0"),
                cost_unit="USD",
            ),
            actor_id="fixture-operator",
            at=policy_now - timedelta(minutes=3),
            reason="Đăng ký nguồn synthetic first-party trong workspace Compose tạm.",
        )
        .transition(
            policy_id=f"{QA_PROVIDER}:proof-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=policy_now - timedelta(minutes=2),
            reason="Ghi nhận nguồn QA chờ review trong workspace Compose.",
        )
        .transition(
            policy_id=f"{QA_PROVIDER}:proof-research",
            state=SourcePolicyState.VERIFIED,
            actor_id="fixture-operator",
            at=policy_now - timedelta(minutes=1),
            reason="Chỉ cấp BACKTEST_RESEARCH cho fixture synthetic QA.",
            allowed_purposes=frozenset({SourceUsePurpose.BACKTEST_RESEARCH}),
            evidence_ref=QA_EVIDENCE_REF,
            reviewed_at=policy_now - timedelta(minutes=1),
            valid_until=policy_now + timedelta(days=1),
            approved_by="fixture-reviewer",
        )
    )
    for revision in history.revisions:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await SourceRegistryRepository(uow.session).append_revision(
                workspace_id=WORKSPACE, revision=revision
            )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE,
                source_id=QA_PROVIDER,
                policy_id=revision.policy.policy_id,
            )
    rights_tick = await ResearchRightsMaintenanceWorker(
        session_factory=sessions,
        workspace_id=WORKSPACE,
        worker_id="wp06-candidate-qa-seed",
        tick_seconds=1,
    ).run_once()
    assert rights_tick.rights_batch.failed == 0
    assert rights_tick.rights_batch.dead_lettered == 0
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        qa_rights = await SourceRegistryBackfillRightsGate(
            workspace_id=WORKSPACE,
            repository=SourceRegistryRepository(uow.session),
            source_id=QA_PROVIDER,
        ).authorize(request=qa_request, now=datetime.now(UTC))
    bundle, comparison = build_h03_source_pinned_dataset_replay(
        WORKSPACE,
        trade_patterns=("loss", "loss"),
        pattern_stride=3,
        rights_evidence=qa_rights,
        dataset_start=dataset_start,
    )
    manifest = comparison.runs[0].sensitivity_run.manifest
    decision = bundle.evaluation_start + timedelta(minutes=1)
    snapshot = DecisionSnapshot(
        symbol=bundle.symbol,
        decision_time=decision,
        snapshot_id="proof:runtime:entry",
        series=(),
        features=(),
        position_is_flat=True,
    )
    observation = ReplayObservation(
        event_id="entry", event_at=decision, available_at=decision, snapshot=snapshot
    )
    feature_values = (("1h:close", 101.0),)
    trade = EvaluationTrade(
        trade_id="proof-closed-trade",
        opened_at=decision + timedelta(seconds=1),
        closed_at=decision + timedelta(minutes=1),
        gross_pnl=Decimal("4"),
        fees=Decimal("1"),
        funding=Decimal("0"),
        slippage=Decimal("0.25"),
        exposure=Decimal("100"),
        entry_decision_snapshot_id=snapshot.snapshot_id,
        entry_decision_at=decision,
        entry_feature_schema_hash=manifest.feature_schema_hash,
        entry_features=feature_values,
        entry_features_hash=calculate_entry_features_hash(
            snapshot.snapshot_id, decision, manifest.feature_schema_hash, feature_values
        ),
        entry_symbol=snapshot.symbol,
    )
    trial_ids = []
    instrument_id = uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(WORKSPACE)},
        )
        session.add(
            MarketInstrument(
                id=instrument_id,
                workspace_id=WORKSPACE,
                source=CRYPIDIAN_FIRST_PARTY_QA_SOURCE,
                idempotency_key="wp06-candidate-proof-qa-instrument",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                tick_size="0.1",
                quantity_step="0.001",
                min_notional="5",
                status="TRADING",
                revision=1,
                checksum="a" * 64,
                supersedes_id=None,
                event_at=dataset_start,
                received_at=dataset_start,
                available_at=dataset_start,
            )
        )
        await session.flush()
        market_repository = MarketDataRepository(session)
        for component in bundle.components:
            for bar in component.bars:
                identity = bar.source_identity
                if identity is None:
                    raise AssertionError("Mỗi bar QA phải ghim MarketBar identity.")
                version = MarketRecordVersion(
                    record_id=identity.record_id,
                    stream=MarketStream(bar.source, bar.symbol, bar.timeframe),
                    logical_key=identity.logical_key,
                    idempotency_key=identity.logical_key,
                    revision=identity.revision,
                    content_checksum=identity.checksum,
                    event_at=bar.event_at,
                    received_at=bar.received_at,
                    available_at=bar.available_at,
                    supersedes_record_id=identity.supersedes_record_id,
                )
                await market_repository.append_bar(
                    workspace_id=WORKSPACE,
                    instrument_id=instrument_id,
                    bar=bar,
                    version=version,
                )
        for component in bundle.components:
            session.add(
                BacktestDatasetManifestRecord.from_manifest(
                    workspace_id=WORKSPACE, manifest=component.manifest
                )
            )
        session.add(
            BacktestDatasetReplayBundleRecord.from_reference(
                workspace_id=WORKSPACE,
                reference=BacktestDatasetReplayBundleReference.from_bundle(bundle),
            )
        )
        await session.flush()
        for index in range(7):
            learning_job_id = job_ids[index]
            trial_manifest = replace(manifest, seed=index, learning_job_id=str(learning_job_id))
            request = BacktestReplayJobSubmission(
                bundle_hash=bundle.dataset_hash,
                strategy_release_id=manifest.strategy_release_id,
                execution_interval="1h",
                starting_equity=Decimal("10000"),
                per_trade_risk_fraction=Decimal("0.01"),
                fee_rate=Decimal("0.001"),
                slippage_rate=Decimal("0"),
                seed=index,
                learning_intent="TRAINING_BASE",
            )
            request_payload = request.to_payload()
            job = BacktestReplayJobRecord(
                id=learning_job_id,
                workspace_id=WORKSPACE,
                requested_by=membership_id,
                bundle_hash=request.bundle_hash,
                strategy_release_id=request.strategy_release_id,
                execution_interval=request.execution_interval,
                request_payload=request_payload,
                canonical_request=json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                status="RUNNING",
                stage="persist",
                progress=80,
                cancel_requested=False,
                attempt_count=1,
                lease_owner="fixture-worker",
                lease_generation=1,
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
                result_run_ids=[],
            )
            session.add(job)
            await session.flush()
            closed_trade = replace(trade, trade_id=f"proof-trade-{index}")
            observations = (observation,)
            trades = (closed_trade,)
            if index == 6:
                observations_list = []
                trades_list = []
                for ordinal, minutes in enumerate((0, 70, 130, 190)):
                    at = bundle.evaluation_start + timedelta(minutes=minutes)
                    entry = replace(snapshot, decision_time=at, snapshot_id=f"split:{ordinal}")
                    observations_list.append(ReplayObservation(str(ordinal), at, at, entry))
                    trades_list.append(
                        replace(
                            trade,
                            trade_id=f"split-trade-{ordinal}",
                            opened_at=at + timedelta(seconds=1),
                            closed_at=at + timedelta(minutes=1),
                            entry_decision_at=at,
                            entry_decision_snapshot_id=entry.snapshot_id,
                            entry_features_hash=calculate_entry_features_hash(
                                entry.snapshot_id, at, manifest.feature_schema_hash, feature_values
                            ),
                        )
                    )
                observations = tuple(observations_list)
                trades = tuple(trades_list)
            by_event = dict(
                zip((item.snapshot.snapshot_id for item in observations), trades, strict=True)
            )
            report = BacktestRunner().run(
                observations,
                manifest=trial_manifest,
                evaluator=lambda item, values=by_event: values[item.snapshot_id],
            )
            assert isinstance(report, EvaluationReport), report
            receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
                workspace_id=WORKSPACE,
                sensitivity_run=SensitivityRun(
                    SensitivityScenario("base", "BASE"), trial_manifest, report
                ),
                trade_outcomes=trades,
                dataset_bundle=bundle,
            )
            job.status = "SUCCEEDED"
            job.stage = "completed"
            job.progress = 100
            job.lease_owner = None
            job.lease_until = None
            job.result_run_ids = [str(receipt.run_id)]
            trial_ids.append(str(receipt.trial_record_id))
    await engine.dispose()
    return {
        "trial_ids": trial_ids,
        "membership_id": str(membership_id),
        "account_id": str(account_id),
        "bundle_hash": bundle.dataset_hash,
        "rights_receipts": len(bundle.components),
        "evaluation_start": bundle.evaluation_start.isoformat(),
    }


async def grant_training():
    engine, sessions = engine_and_sessions()
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        registry = SourceRegistryRepository(uow.session)
        history = await registry.get_entry(workspace_id=WORKSPACE, source_id=QA_PROVIDER)
        now = await registry.database_clock(workspace_id=WORKSPACE)
        pending = history.transition(
            policy_id=f"{QA_PROVIDER}:proof-training-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=now,
            reason="Xét lại purpose TRAINING riêng.",
        )
        await registry.append_revision(workspace_id=WORKSPACE, revision=pending.current)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        await SourceRegistryRepository(uow.session).finalize_policy(
            workspace_id=WORKSPACE,
            source_id=QA_PROVIDER,
            policy_id=pending.current.policy.policy_id,
        )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        registry = SourceRegistryRepository(uow.session)
        history = await registry.get_entry(workspace_id=WORKSPACE, source_id=QA_PROVIDER)
        now = await registry.database_clock(workspace_id=WORKSPACE)
        updated = history.transition(
            policy_id=f"{QA_PROVIDER}:proof-training",
            state=SourcePolicyState.VERIFIED,
            actor_id="fixture-operator",
            at=now,
            reason="Cấp TRAINING riêng cho fixture synthetic.",
            allowed_purposes=frozenset({SourceUsePurpose.TRAINING}),
            evidence_ref=QA_EVIDENCE_REF,
            reviewed_at=now,
            valid_until=now + timedelta(days=1),
            approved_by="fixture-reviewer",
        )
        await registry.append_revision(workspace_id=WORKSPACE, revision=updated.current)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        await SourceRegistryRepository(uow.session).finalize_policy(
            workspace_id=WORKSPACE,
            source_id=QA_PROVIDER,
            policy_id=updated.current.policy.policy_id,
        )
    rights_tick = await ResearchRightsMaintenanceWorker(
        session_factory=sessions,
        workspace_id=WORKSPACE,
        worker_id="wp06-candidate-qa-training",
        tick_seconds=1,
    ).run_once()
    assert rights_tick.rights_batch.failed == 0
    assert rights_tick.rights_batch.dead_lettered == 0
    await engine.dispose()
    return {"training_granted": True}


async def denied():
    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
        ReplayTrainingOperation,
    )

    engine, sessions = engine_and_sessions()
    statements = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, executemany: statements.append(
            statement
        ),
    )
    try:
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
                await DatabaseReplayTrainingRightsGate(uow.session).authorize(
                    workspace_id=WORKSPACE,
                    bundle_hash=STATE["bundle_hash"],
                    operation=ReplayTrainingOperation.TRAINING,
                )
        except DBAPIError as error:
            if "WP06_TRAINING_REPLAY_CURRENT_POLICY_DENIED" not in str(error):
                raise AssertionError(
                    "Verifier phải từ chối policy chỉ có BACKTEST_RESEARCH."
                ) from error
        else:
            raise AssertionError("Verifier đã mở rộng BACKTEST_RESEARCH thành TRAINING.")
        await CandidateUniverseProofService(sessions).build(
            workspace_id=WORKSPACE, trial_record_id=UUID(STATE["trial_ids"][0])
        )
    except CandidateUniverseProofError as error:
        if "Current TRAINING rights" not in str(error):
            raise AssertionError("Candidate proof không giữ fail-closed denial.") from error
    else:
        raise AssertionError("BACKTEST_RESEARCH đã được suy rộng thành TRAINING.")
    assert not any(
        "FROM backtest_trade_outcome" in sql or "FROM backtest_report" in sql for sql in statements
    ), statements
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        count = await uow.session.scalar(text("SELECT count(*) FROM candidate_universe_proof"))
        assert count == 0
    await engine.dispose()
    return {"denied_before_payload": True, "no_proof": True}


async def verify_authorized():
    """Xác minh build/retry dưới đúng principal mà verifier cho phép."""
    engine, sessions = engine_and_sessions()
    service = CandidateUniverseProofService(sessions)
    trial = UUID(STATE["trial_ids"][0])
    first = await service.build(workspace_id=WORKSPACE, trial_record_id=trial)
    retry = await service.build(workspace_id=WORKSPACE, trial_record_id=trial)
    assert retry.duplicate
    assert retry.record_id == first.record_id
    assert retry.proof == first.proof
    assert (
        first.proof.candidate_count,
        first.proof.eligible_count,
        first.proof.exclusion_count,
    ) == (1, 1, 0)
    assert first.proof.training_authorized is False
    assert not any(
        key in json.loads(first.proof.canonical_json) for key in ("features", "labels", "samples")
    )
    await engine.dispose()
    return {
        "record_id": str(first.record_id),
        "proof_hash": first.proof.content_hash,
        "retry": True,
        "training_authorized": False,
    }


async def race_proof_before_append():
    """Builder giữ khóa proof đến khi migrator thực sự chờ INSERT."""
    engine, sessions = engine_and_sessions()
    service = CandidateUniverseProofService(sessions)
    trial = UUID(STATE["trial_ids"][1])
    original = BacktestOutcomeRepository.load_trade_outcome_rows

    async def held_load(self, **kwargs):
        result = await original(self, **kwargs)
        if kwargs["trial_record_id"] == trial:
            blocker_pid = await self._session.scalar(text("SELECT pg_backend_pid()"))
            print(f"WP06_PROOF_LOCK_HELD {trial} {blocker_pid}", flush=True)
            await wait_for_lock_waiting_on(sessions, blocker_pid)
        return result

    BacktestOutcomeRepository.load_trade_outcome_rows = held_load
    try:
        result = await service.build(workspace_id=WORKSPACE, trial_record_id=trial)
    finally:
        BacktestOutcomeRepository.load_trade_outcome_rows = original
        await engine.dispose()
    return {"proof_id": str(result.record_id), "proof_created": True}


async def race_append_after_proof():
    """Migrator INSERT chờ builder commit rồi phải bị trigger fence từ chối."""
    engine, sessions = engine_and_sessions()
    trial = UUID(STATE["trial_ids"][1])

    async def append_after():
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await insert_extra(uow.session, trial)

    insert_task = await start_and_confirm_lock_wait(
        engine, sessions, "INSERT INTO backtest_trade_outcome", append_after
    )
    try:
        await insert_task
    except DBAPIError as error:
        if "WP06_OUTCOME_TRIAL_SEALED_BY_PROOF" not in str(error):
            raise AssertionError("INSERT chờ proof không bị chặn bởi seal marker.") from error
    else:
        raise AssertionError("Concurrent INSERT đã vượt proof seal.")
    await engine.dispose()
    return {"append_waited_and_denied": True}


async def race_hold_append_before_proof():
    """Migrator giữ INSERT chưa commit đến khi builder chờ khóa trial."""
    engine, sessions = engine_and_sessions()
    trial = UUID(STATE["trial_ids"][2])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        await insert_extra(uow.session, trial)
        blocker_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        print(f"WP06_APPEND_LOCK_HELD {trial} {blocker_pid}", flush=True)
        await wait_for_lock_waiting_on(sessions, blocker_pid)
    await engine.dispose()
    return {"append_lock_was_contended": True}


async def race_proof_after_append():
    """Builder chờ append commit rồi phải phát hiện outcome bổ sung và từ chối."""
    from trading_bot.adapters.database.models import CandidateUniverseProofRecord

    engine, sessions = engine_and_sessions()
    trial = UUID(STATE["trial_ids"][2])
    try:
        await CandidateUniverseProofService(sessions).build(
            workspace_id=WORKSPACE, trial_record_id=trial
        )
    except CandidateUniverseProofError:
        pass
    else:
        raise AssertionError("Proof đã bỏ qua outcome commit trước scan.")
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        proof_id = await uow.session.scalar(
            select(CandidateUniverseProofRecord.id).where(
                CandidateUniverseProofRecord.trial_record_id == trial
            )
        )
        assert proof_id is None
    await engine.dispose()
    return {"append_before_proof_denied": True, "no_proof": True}


async def insert_extra(session, trial_id):
    # Bản sao hợp lệ về CHECK; trigger fence phải chạy trước unique/hash application checks.
    await session.execute(
        text("""INSERT INTO backtest_trade_outcome
        SELECT (jsonb_populate_record(NULL::backtest_trade_outcome,
          to_jsonb(r) ||
          jsonb_build_object('id',gen_random_uuid(),'ordinal',99,'trade_id','extra-'
          || gen_random_uuid()::text))).*
        FROM backtest_trade_outcome r WHERE workspace_id=:workspace
        AND trial_record_id=:trial LIMIT 1
        """),
        {"workspace": WORKSPACE, "trial": trial_id},
    )


async def start_and_confirm_lock_wait(engine, sessions, sql_fragment, operation):
    """Đợi PostgreSQL xác nhận contention thật, không suy từ task chưa được chạy."""
    observed_pid = asyncio.get_running_loop().create_future()

    def on_sql(connection, cursor, statement, parameters, context, executemany):
        if sql_fragment in statement and not observed_pid.done():
            observed_pid.set_result(connection.connection.driver_connection.info.backend_pid)

    event.listen(engine.sync_engine, "before_cursor_execute", on_sql)
    task = asyncio.create_task(operation())
    try:
        pid = await asyncio.wait_for(observed_pid, 10)
        async with sessions() as monitor:
            for _ in range(250):
                await monitor.execute(text("SELECT pg_stat_clear_snapshot()"))
                status = (
                    await monitor.execute(
                        text(
                            "SELECT wait_event_type, pg_blocking_pids(pid) "
                            "FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": pid},
                    )
                ).one()
                if status[0] == "Lock" and status[1]:
                    return task
                if task.done():
                    await task
                    raise AssertionError("Thao tác kết thúc trước khi chờ lock PostgreSQL.")
                await asyncio.sleep(0.02)
        raise AssertionError("Không quan sát được row/advisory lock contention trong timeout.")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", on_sql)
        if not observed_pid.done():
            observed_pid.cancel()


async def wait_for_lock_waiting_on(sessions, blocker_pid: int) -> None:
    """Đợi một phiên khác thực sự bị PostgreSQL chặn bởi PID giữ khóa."""
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        async with sessions() as monitor:
            blocked = await monitor.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity activity "
                    "WHERE activity.pid <> :blocker_pid "
                    "AND :blocker_pid = ANY(pg_blocking_pids(activity.pid)))"
                ),
                {"blocker_pid": blocker_pid},
            )
        if blocked:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Không thấy phiên khác chờ khóa do PID {blocker_pid} giữ.")


async def assert_product_mutation_acl(sessions):
    """Thực thi UPDATE/DELETE dưới đúng login; lỗi phải là thiếu privilege."""
    for table in ("candidate_universe_proof", "candidate_training_rights"):
        for sql in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}"):
            try:
                async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
                    await uow.session.execute(text(sql))
            except DBAPIError as error:
                if error.orig.sqlstate != "42501":
                    raise AssertionError("Product mutation không bị ACL chặn.") from error
            else:
                raise AssertionError("Product role đã sửa/xóa proof hoặc receipt.")


async def verify():
    from trading_bot.adapters.database.models import CandidateUniverseProofRecord

    engine, sessions = engine_and_sessions()
    trial = UUID(STATE["trial_ids"][0])
    proof_id = UUID(STATE["authorized_proof"]["record_id"])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        first = await uow.session.get(CandidateUniverseProofRecord, proof_id)
        assert first is not None
        assert first.workspace_id == WORKSPACE
        assert first.trial_record_id == trial
        first_canonical_proof = first.canonical_proof
        first_proof = json.loads(first_canonical_proof)
    # Probe privileged điều phối writers; actual builder login được kiểm riêng.
    # Cùng khóa trial/query với bytes khác bị unique constraint từ chối, không ghi đè.
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.session.execute(
                text("""INSERT INTO candidate_universe_proof
                SELECT (jsonb_populate_record(NULL::candidate_universe_proof,
                  to_jsonb(r) || jsonb_build_object('id',gen_random_uuid(),
                    'canonical_proof','{}','proof_hash',repeat('0',64)))).*
                FROM candidate_universe_proof r WHERE id=:id"""),
                {"id": proof_id},
            )
    except DBAPIError as error:
        if error.orig.sqlstate != "23505":
            raise AssertionError("Duplicate changed-payload không bị unique key chặn.") from error
    else:
        raise AssertionError("Đã ghi đè proof cùng trial/query bằng payload khác.")
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        unchanged = await uow.session.get(CandidateUniverseProofRecord, proof_id)
        assert unchanged is not None
        assert unchanged.canonical_proof == first_canonical_proof
    assert (
        first_proof["candidate_count"],
        first_proof["eligible_count"],
        first_proof["exclusion_count"],
    ) == (1, 1, 0)
    assert first_proof["training_authorized"] is False
    assert not any(key in first_proof for key in ("features", "labels", "samples"))
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        count = await uow.session.scalar(
            text("SELECT count(*) FROM candidate_training_rights WHERE proof_id=:id"),
            {"id": proof_id},
        )
        expected_receipts = STATE["rights_receipts"]
        assert count == expected_receipts
        await uow.session.execute(text("SET LOCAL ROLE crypidian_candidate_builder"))
        privileges = (
            await uow.session.execute(
                text(
                    "SELECT "
                    "has_table_privilege(current_user,'candidate_universe_proof','SELECT'),has_table_privilege(current_user,'candidate_universe_proof','INSERT'),has_table_privilege(current_user,'candidate_universe_proof','UPDATE'),has_table_privilege(current_user,'candidate_universe_proof','DELETE')"
                )
            )
        ).one()
        assert privileges == (True, True, False, False)
    async with UnitOfWork(sessions, workspace_id=UUID(int=WORKSPACE.int + 1)) as uow:
        await uow.session.execute(text("SET LOCAL ROLE crypidian_candidate_builder"))
        assert (
            await uow.session.scalar(
                text("SELECT count(*) FROM candidate_universe_proof WHERE workspace_id=:id"),
                {"id": WORKSPACE},
            )
            == 0
        )
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await insert_extra(uow.session, trial)
    except DBAPIError as error:
        if "WP06_OUTCOME_TRIAL_SEALED_BY_PROOF" not in str(error):
            raise AssertionError(
                "Lỗi DB không khớp marker WP06_OUTCOME_TRIAL_SEALED_BY_PROOF."
            ) from error
    else:
        raise AssertionError("Đã append outcome sau khi proof seal.")
    # Kiểm lại hai phía của race trong phiên migrator; việc tạo proof chạy ở builder riêng.
    trial_two = UUID(STATE["trial_ids"][1])
    race_proof_id = UUID(STATE["proof_before_append_race"]["proof_id"])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        race_proof = await uow.session.get(CandidateUniverseProofRecord, race_proof_id)
        assert race_proof is not None
        assert race_proof.trial_record_id == trial_two
        assert STATE["append_waited_and_denied"] is True
    # Race đối diện đã append khi giữ khóa; builder phải chờ rồi từ chối sau khi quét.
    trial_three = UUID(STATE["trial_ids"][2])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        assert (
            await uow.session.scalar(
                text("SELECT count(*) FROM candidate_universe_proof WHERE trial_record_id=:id"),
                {"id": trial_three},
            )
            == 0
        )
        assert STATE["append_before_proof_race"] == {
            "append_before_proof_denied": True,
            "no_proof": True,
        }
    await engine.dispose()
    return {
        "counts": [1, 1, 0],
        "retry": True,
        "rights_receipts": STATE["rights_receipts"],
        "no_payload": True,
        "workspace_isolation": True,
        "append_after_proof_denied": True,
        "proof_before_append_race": True,
        "append_before_proof_race": True,
        "least_privilege": True,
    }


async def admin():
    engine, sessions = engine_and_sessions()
    await assert_product_mutation_acl(sessions)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        assert await uow.session.scalar(text("SELECT count(*) FROM candidate_universe_proof")) == 2
        for table in ("candidate_universe_proof", "candidate_training_rights"):
            rights = (
                await uow.session.execute(
                    text(
                        "SELECT "
                        "has_table_privilege(current_user,:table,'SELECT'),has_table_privilege(current_user,:table,'INSERT'),has_table_privilege(current_user,:table,'UPDATE'),has_table_privilege(current_user,:table,'DELETE')"
                    ),
                    {"table": table},
                )
            ).one()
            assert rights == (True, False, False, False)
    async with UnitOfWork(sessions, workspace_id=UUID(int=WORKSPACE.int + 1)) as uow:
        assert await uow.session.scalar(text("SELECT count(*) FROM candidate_universe_proof")) == 0
    await engine.dispose()
    return {"select_only": True, "workspace_isolation": True}


async def security():
    engine, sessions = engine_and_sessions()
    roles = (
        "crypidian_app",
        "crypidian_trading_runtime",
        "crypidian_ingestion_worker",
        "crypidian_backtest_worker",
        "crypidian_execution_gateway",
        "crypidian_signal_worker",
        "crypidian_market_signal_worker",
    )
    async with sessions() as session, session.begin():
        for table in ("candidate_universe_proof", "candidate_training_rights"):
            rls = (
                await session.execute(
                    text(
                        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE "
                        "oid=CAST(:table AS regclass)"
                    ),
                    {"table": table},
                )
            ).scalar_one()
            assert rls is True
            for role in roles:
                assert not await session.scalar(
                    text("SELECT has_table_privilege(:role,:table,'SELECT,INSERT,UPDATE,DELETE')"),
                    {"role": role, "table": table},
                )
        catalog = (
            await session.execute(
                text(
                    "SELECT prosecdef,proconfig,pg_get_userbyid(proowner) FROM pg_proc WHERE "
                    "oid='public.wp06_lock_candidate_trial(uuid,uuid)'::regprocedure"
                )
            )
        ).one()
        assert catalog == (True, ["search_path=pg_catalog, pg_temp"], "crypidian_candidate_locker")
        assert not await session.scalar(
            text(
                "SELECT "
                "has_function_privilege('crypidian_admin_web','public.wp06_lock_candidate_trial(uuid,uuid)','EXECUTE')"
            )
        )
        job_acl = (
            await session.execute(
                text(
                    "SELECT has_table_privilege('crypidian_candidate_builder',"
                    "'backtest_replay_job','SELECT'), "
                    "has_table_privilege('crypidian_candidate_builder',"
                    "'backtest_replay_job','INSERT,UPDATE,DELETE'), "
                    "EXISTS (SELECT 1 FROM pg_policies WHERE tablename='backtest_replay_job' "
                    "AND policyname='wp06_candidate_learning_job_select')"
                )
            )
        ).one()
        assert job_acl == (True, False, True)
    for table in ("candidate_universe_proof", "candidate_training_rights"):
        for mutation in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}"):
            try:
                async with sessions() as session, session.begin():
                    await session.execute(text(mutation))
            except DBAPIError as error:
                if "WP06_CANDIDATE_PROOF_IMMUTABLE" not in str(error):
                    raise AssertionError(
                        "Lỗi DB không khớp marker WP06_CANDIDATE_PROOF_IMMUTABLE."
                    ) from error
            else:
                raise AssertionError("Trigger cho phép mutation proof/receipt.")
    for table in ("candidate_universe_proof", "candidate_training_rights"):
        try:
            async with sessions() as session, session.begin():
                await session.execute(text("SET LOCAL ROLE crypidian_candidate_builder"))
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(WORKSPACE)},
                )
                await session.execute(
                    text(f"""INSERT INTO {table}
                    SELECT (jsonb_populate_record(NULL::{table},to_jsonb(r)
                      || jsonb_build_object('id',gen_random_uuid(),'workspace_id',
                        CAST(:other AS uuid)))).* FROM {table} r LIMIT 1"""),
                    {"other": str(UUID(int=WORKSPACE.int + 1))},
                )
        except DBAPIError as error:
            if "row-level security policy" not in str(error):
                raise AssertionError("INSERT khác workspace không chạm RLS WITH CHECK.") from error
        else:
            raise AssertionError("RLS WITH CHECK cho phép tenant khác.")
    # Quyền nguồn bị serialize theo workspace/source tới khi proof commit.
    trial_four = UUID(STATE["trial_ids"][3])
    service = CandidateUniverseProofService(sessions)
    gate_passed = asyncio.Event()
    release_scan = asyncio.Event()
    original = BacktestOutcomeRepository.load_trade_outcome_rows

    async def held_load(self, **kwargs):
        result = await original(self, **kwargs)
        if kwargs["trial_record_id"] == trial_four:
            gate_passed.set()
            await release_scan.wait()
        return result

    async def revoke():
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            registry = SourceRegistryRepository(uow.session)
            history = await registry.get_entry(workspace_id=WORKSPACE, source_id=QA_PROVIDER)
            now = await registry.database_clock(workspace_id=WORKSPACE)
            revoked = history.transition(
                policy_id=f"{QA_PROVIDER}:proof-revoked",
                state=SourcePolicyState.REVOKED,
                actor_id="fixture-operator",
                at=now,
                reason="Thu hồi quyền fixture để kiểm race.",
            )
            await registry.append_revision(workspace_id=WORKSPACE, revision=revoked.current)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE,
                source_id=QA_PROVIDER,
                policy_id=revoked.current.policy.policy_id,
            )

    BacktestOutcomeRepository.load_trade_outcome_rows = held_load
    proof_task = asyncio.create_task(
        service.build(workspace_id=WORKSPACE, trial_record_id=trial_four)
    )
    try:
        await asyncio.wait_for(gate_passed.wait(), 10)
        writer_task = await start_and_confirm_lock_wait(
            engine, sessions, "pg_advisory_xact_lock", revoke
        )
        release_scan.set()
        await proof_task
        await writer_task
    finally:
        BacktestOutcomeRepository.load_trade_outcome_rows = original
    # Writer có source lock trước scanner: scanner phải chờ rồi đọc policy mới.
    trial_five = UUID(STATE["trial_ids"][4])
    statements = []

    def trace(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", trace)
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as writer:
            registry = SourceRegistryRepository(writer.session)
            history = await registry.get_entry(workspace_id=WORKSPACE, source_id=QA_PROVIDER)
            now = await registry.database_clock(workspace_id=WORKSPACE)
            pending = history.transition(
                policy_id=f"{QA_PROVIDER}:proof-reconsider",
                state=SourcePolicyState.RIGHTS_PENDING,
                actor_id="fixture-operator",
                at=now,
                reason="Xét lại quyền trong writer transaction.",
            )
            await registry.append_revision(workspace_id=WORKSPACE, revision=pending.current)
            waiting_proof = await start_and_confirm_lock_wait(
                engine,
                sessions,
                "pg_advisory_xact_lock",
                lambda: service.build(workspace_id=WORKSPACE, trial_record_id=trial_five),
            )
        try:
            await waiting_proof
        except CandidateUniverseProofError:
            pass
        else:
            raise AssertionError("Scanner đã dùng policy stale trước writer commit.")
        if any(
            "FROM backtest_trade_outcome" in sql or "FROM backtest_report" in sql
            for sql in statements
        ):
            raise AssertionError("Đã đọc payload sau khi policy mất TRAINING.")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", trace)
    async with sessions() as session, session.begin():
        if await session.scalar(
            text("SELECT count(*) FROM candidate_universe_proof WHERE trial_record_id=:trial"),
            {"trial": trial_five},
        ):
            raise AssertionError("Đã append proof dù current policy không cho TRAINING.")
    await engine.dispose()
    return {
        "policy_write_waited_for_proof": True,
        "policy_before_proof_denied_without_payload": True,
        "role_matrix": True,
        "rls_with_check": True,
        "immutable_triggers": True,
        "function_acl": True,
        "learning_job_acl": True,
    }


async def builder():
    engine, sessions = engine_and_sessions()
    async with sessions() as session:
        assert await session.scalar(text("SELECT current_user")) == "crypidian_candidate_builder"
        for table in (
            "candidate_universe_proof",
            "candidate_training_rights",
            "backtest_run",
            "backtest_trial",
            "backtest_report",
            "backtest_scenario",
            "backtest_trade_outcome",
            "backtest_replay_job",
            "backtest_dataset_manifest",
            "backtest_dataset_replay_bundle",
            "source_policy_revision",
            "source_policy_observation",
        ):
            privileges = (
                await session.execute(
                    text(
                        "SELECT has_table_privilege(current_user,:table,'SELECT'), "
                        "has_table_privilege(current_user,:table,'INSERT'), "
                        "has_table_privilege(current_user,:table,'UPDATE'), "
                        "has_table_privilege(current_user,:table,'DELETE')"
                    ),
                    {"table": table},
                )
            ).one()
            expected = (
                (True, True, False, False)
                if table.startswith("candidate_")
                else (True, False, False, False)
            )
            assert privileges == expected, (table, privileges)
        assert await session.scalar(
            text(
                "SELECT has_function_privilege(current_user,"
                "'public.wp06_lock_candidate_trial(uuid,uuid)','EXECUTE')"
            )
        )
        assert not await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r "
                "ON m.member=r.oid OR m.roleid=r.oid WHERE r.rolname=current_user)"
            )
        )
    service = CandidateUniverseProofService(sessions)
    trial = UUID(STATE["trial_ids"][0])
    first = await service.build(workspace_id=WORKSPACE, trial_record_id=trial)
    second = await service.build(workspace_id=WORKSPACE, trial_record_id=trial)
    assert first.record_id == second.record_id
    assert second.duplicate
    await assert_product_mutation_acl(sessions)
    await engine.dispose()
    return {"trusted_builder": True}


async def trainer():
    engine, sessions = engine_and_sessions()
    assert not Path("/run/secrets/database_candidate_builder_password").exists()
    await assert_product_mutation_acl(sessions)
    for table in ("candidate_universe_proof", "candidate_training_rights"):
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
                await uow.session.execute(
                    text(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
                )
        except DBAPIError as error:
            if error.orig.sqlstate != "42501":
                raise AssertionError(
                    "Trainer INSERT không bị ACL deny trước unique check."
                ) from error
        else:
            raise AssertionError("Trainer đã forge được proof/receipt.")
    try:
        async with sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE crypidian_candidate_builder"))
    except DBAPIError as error:
        if error.orig.sqlstate != "42501":
            raise AssertionError("Trainer SET ROLE không bị ACL chặn.") from error
    else:
        raise AssertionError("Trainer đã escalate thành trusted writer.")
    async with sessions() as session:
        assert not await session.scalar(
            text(
                "SELECT has_function_privilege(current_user,"
                "'public.wp06_lock_candidate_trial(uuid,uuid)','EXECUTE')"
            )
        )
    await engine.dispose()
    return {"cannot_forge": True, "no_escalation": True, "no_builder_secret": True}


async def lease_boundary():
    from sqlalchemy.ext.asyncio import AsyncSession

    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
    )
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofRepository,
    )

    engine, sessions = engine_and_sessions()
    original_gate = DatabaseReplayTrainingRightsGate.authorize
    original_flush = AsyncSession.flush
    original_read = CandidateUniverseProofRepository._validate_receipts
    after_flush = False

    async def short_gate(self, **kwargs):
        receipts = await original_gate(self, **kwargs)
        return tuple(
            replace(receipt, valid_until=receipt.checked_at + timedelta(seconds=2))
            for receipt in receipts
        )

    async def delayed_flush(self, *args, **kwargs):
        nonlocal after_flush
        pending_receipt = any(
            type(row).__name__ == "CandidateTrainingRightsRecord" for row in self.new
        )
        await original_flush(self, *args, **kwargs)
        if pending_receipt:
            after_flush = True
            await self.execute(text("SELECT pg_sleep(2.1)"))

    async def delayed_retry(self, *args, **kwargs):
        await original_read(self, *args, **kwargs)
        await self._session.execute(text("SELECT pg_sleep(2.1)"))

    DatabaseReplayTrainingRightsGate.authorize = short_gate
    AsyncSession.flush = delayed_flush
    service = CandidateUniverseProofService(sessions)
    trial = UUID(STATE["trial_ids"][5])
    try:
        try:
            await service.build(workspace_id=WORKSPACE, trial_record_id=trial)
        except CandidateUniverseProofError:
            pass
        else:
            raise AssertionError("Đã commit proof dù lease hết trong receipt flush.")
        assert after_flush
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            assert (
                await uow.session.scalar(
                    text("SELECT count(*) FROM candidate_universe_proof WHERE trial_record_id=:id"),
                    {"id": trial},
                )
                == 0
            )
            assert (
                await uow.session.scalar(text("SELECT count(*) FROM candidate_training_rights"))
                == 6
            )
        AsyncSession.flush = original_flush
        CandidateUniverseProofRepository._validate_receipts = delayed_retry
        try:
            await service.build(workspace_id=WORKSPACE, trial_record_id=UUID(STATE["trial_ids"][0]))
        except CandidateUniverseProofError:
            pass
        else:
            raise AssertionError("Retry đã trả proof sau khi lease hết trong receipt read.")
    finally:
        AsyncSession.flush = original_flush
        DatabaseReplayTrainingRightsGate.authorize = original_gate
        CandidateUniverseProofRepository._validate_receipts = original_read
    await engine.dispose()
    return {"post_flush_rollback": True, "retry_expiry_rejected": True}


async def empty_downgrade():
    engine, sessions = engine_and_sessions()
    async with sessions() as session:
        assert not await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='crypidian_candidate_locker')"
            )
        )
    await engine.dispose()
    return {"locker_role_removed": True}


async def assert_acl_denied(sessions, statement):
    """Xác nhận DB từ chối truy vấn thật bằng SQLSTATE quyền truy cập."""
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.session.execute(text(statement))
    except DBAPIError as error:
        if error.orig.sqlstate != "42501":
            raise AssertionError("SQLSTATE không khớp ACL denial.") from error
    else:
        raise AssertionError("Truy vấn payload đã vượt quyền DB.")


async def split_unapproved():
    from trading_bot.application.learning_split_candidate import (
        LearningSplitCandidateBuilderService,
    )

    engine, sessions = engine_and_sessions()
    try:
        await LearningSplitCandidateBuilderService(sessions).seal(
            workspace_id=WORKSPACE, candidate_id=UUID(STATE["candidate"]["candidate_id"])
        )
    except PermissionError as error:
        if "exact-match" not in str(error):
            raise AssertionError("Không chạm exact approval guard.") from error
    else:
        raise AssertionError("Seal đã bỏ qua approval.")
    await engine.dispose()
    return {"unapproved_denied": True}


async def split_review():
    from dataclasses import asdict

    from trading_bot.application.learning_split_admin import LearningSplitAdminService
    from trading_bot.auth.policy import Principal, Role

    engine, sessions = engine_and_sessions()
    principal = Principal(
        user_id=UUID(STATE["account_id"]),
        membership_id=UUID(STATE["membership_id"]),
        workspace_id=WORKSPACE,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    service = LearningSplitAdminService(sessions).for_principal(principal)
    candidates = await service.list_candidates()
    assert len(candidates) == 1
    candidate = candidates[0]
    staged = STATE["candidate"]
    assert str(candidate.candidate_id) == staged["candidate_id"]
    assert candidate.manifest_hash == staged["manifest_hash"]
    assert candidate.fold_plan_hash == staged["fold_plan_hash"]
    assert candidate.preview == staged["preview"]
    assert candidate.preview["development_count"] == 3
    assert candidate.preview["holdout_count"] == 1
    assert all(
        candidate.preview["folds"][0][role]["count"] == 1
        for role in ("train", "validation", "test")
    )
    serialized = json.dumps(asdict(candidate), default=str)
    for private in (
        "canonical_manifest",
        "canonical_configuration",
        "label_value",
        "label_hash",
        "features",
        "sample_id",
        "outcome_id",
    ):
        assert private not in serialized
    for column in ("canonical_manifest", "canonical_configuration"):
        await assert_acl_denied(sessions, f"SELECT {column} FROM learning_split_candidate")
    other = LearningSplitAdminService(sessions).for_principal(
        replace(principal, workspace_id=UUID(int=WORKSPACE.int + 1))
    )
    assert await other.list_candidates() == ()
    try:
        await other.approve(
            candidate_id=candidate.candidate_id,
            reason="Duyệt split fixture hợp lệ.",
            idempotency_key=uuid4(),
        )
    except LookupError:
        pass
    else:
        raise AssertionError("Reviewer đã duyệt candidate khác workspace.")
    stale = LearningSplitAdminService(sessions).for_principal(
        replace(principal, mfa_verified_at=datetime.now(UTC) - timedelta(days=1))
    )
    try:
        await stale.approve(
            candidate_id=candidate.candidate_id,
            reason="Duyệt split fixture hợp lệ.",
            idempotency_key=uuid4(),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("Reviewer đã bỏ qua MFA gần đây.")
    key = uuid4()
    arguments = {
        "candidate_id": candidate.candidate_id,
        "reason": "Duyệt split fixture hợp lệ.",
        "idempotency_key": key,
    }
    approval = await service.approve(**arguments)
    assert await service.approve(**arguments) == approval
    assert approval.manifest_hash == candidate.manifest_hash
    assert approval.fold_plan_hash == candidate.fold_plan_hash
    assert approval.actor_membership_id == principal.membership_id
    listed = await service.list_candidates()
    assert listed[0].approved_at == approval.approved_at
    await engine.dispose()
    return {"approval_hash": approval.content_hash}


async def split_atomic():
    from trading_bot.adapters.database.repositories.learning_fold_samples import (
        LearningFoldSampleRepository,
    )
    from trading_bot.application.learning_split_candidate import (
        LearningSplitCandidateBuilderService,
    )

    engine, sessions = engine_and_sessions()
    original = LearningFoldSampleRepository.append_normalized_dataset
    reached = False

    async def fail_after_payload(self, **kwargs):
        nonlocal reached
        await original(self, **kwargs)
        reached = True
        raise RuntimeError("WP06_TEST_ATOMIC_ROLLBACK")

    LearningFoldSampleRepository.append_normalized_dataset = fail_after_payload
    try:
        try:
            await LearningSplitCandidateBuilderService(sessions).seal(
                workspace_id=WORKSPACE, candidate_id=UUID(STATE["candidate"]["candidate_id"])
            )
        except RuntimeError as error:
            if str(error) != "WP06_TEST_ATOMIC_ROLLBACK":
                raise AssertionError("Không chạm điểm inject atomic rollback.") from error
        else:
            raise AssertionError("Probe không inject lỗi sau append payload.")
    finally:
        LearningFoldSampleRepository.append_normalized_dataset = original
    assert reached
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        for table in (
            "dataset_manifest",
            "learning_normalized_dataset",
            "learning_normalized_sample",
            "learning_fold_assignment",
            "learning_fold_resource_profile",
        ):
            assert await uow.session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    await engine.dispose()
    return {"atomic_rollback": True}


async def split_verify():
    from trading_bot.adapters.database.repositories.learning_split_candidates import (
        LearningSplitCandidateRepository,
    )
    from trading_bot.domain.learning.dataset import DatasetSplit

    engine, sessions = engine_and_sessions()
    staged = STATE["candidate"]
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        candidate = await LearningSplitCandidateRepository(uow.session).get_for_builder(
            workspace_id=WORKSPACE, candidate_id=UUID(staged["candidate_id"])
        )
        assert candidate is not None
        holdout_ids = {
            sample.sample_id
            for sample in candidate.manifest.samples
            if sample.split is DatasetSplit.HOLDOUT
        }
        assert len(holdout_ids) == 1
        sample_ids = set(
            (
                await uow.session.execute(text("SELECT sample_id FROM learning_normalized_sample"))
            ).scalars()
        )
        assert len(sample_ids) == 3
        assert not holdout_ids & sample_ids
        assert sample_ids == {
            sample.sample_id
            for sample in candidate.manifest.samples
            if sample.split is DatasetSplit.DEVELOPMENT
        }
        header = (
            await uow.session.execute(
                text(
                    "SELECT manifest_hash, fold_plan_hash, approval_hash, sample_count, "
                    "assignment_count FROM learning_normalized_dataset"
                )
            )
        ).one()
        assert tuple(header) == (
            staged["manifest_hash"],
            staged["fold_plan_hash"],
            STATE["approval_hash"],
            3,
            3,
        )
        assert (
            await uow.session.scalar(
                text("SELECT count(*) FROM dataset_manifest WHERE manifest_hash=:hash"),
                {"hash": staged["manifest_hash"]},
            )
            == 1
        )
        roles = (
            await uow.session.execute(
                text("SELECT role, count(*) FROM learning_fold_assignment GROUP BY role")
            )
        ).all()
        assert dict(roles) == {"train": 1, "validation": 1, "test": 1}
        assert (
            await uow.session.scalar(text("SELECT count(*) FROM learning_fold_resource_profile"))
            == 1
        )
    for table, marker in (
        ("learning_split_candidate", "WP06_LEARNING_SPLIT_CANDIDATE_IMMUTABLE"),
        ("learning_split_approval", "WP06_LEARNING_SPLIT_APPROVAL_IMMUTABLE"),
    ):
        for mutation in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}"):
            try:
                async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
                    await uow.session.execute(text(mutation))
            except DBAPIError as error:
                if marker not in str(error):
                    raise AssertionError("Không chạm immutable trigger.") from error
            else:
                raise AssertionError("DB cho phép sửa candidate/approval bất biến.")
    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE crypidian_candidate_builder"))
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(UUID(int=WORKSPACE.int + 1))},
        )
        assert await session.scalar(text("SELECT count(*) FROM learning_split_candidate")) == 0
    await engine.dispose()
    return {"persisted": True, "holdout_excluded": True, "immutable": True}


async def split_private():
    engine, sessions = engine_and_sessions()
    await assert_acl_denied(sessions, "SELECT canonical_manifest FROM learning_split_candidate")
    await assert_acl_denied(
        sessions, "SELECT canonical_configuration FROM learning_split_candidate"
    )
    await assert_acl_denied(sessions, "SELECT id FROM learning_split_candidate")
    await engine.dispose()
    return {"payload_denied": True}


async def split_revoked():
    from trading_bot.application.learning_split_candidate import (
        LearningSplitCandidateBuilderService,
    )

    engine, sessions = engine_and_sessions()
    try:
        await LearningSplitCandidateBuilderService(sessions).seal(
            workspace_id=WORKSPACE, candidate_id=UUID(STATE["candidate"]["candidate_id"])
        )
    except CandidateUniverseProofError:
        pass
    else:
        raise AssertionError("Seal retry đã bỏ qua current TRAINING rights.")
    await engine.dispose()
    return {"revoked_denied": True}


async def main():
    modes = {
        "split-unapproved": split_unapproved,
        "split-review": split_review,
        "split-atomic": split_atomic,
        "split-verify": split_verify,
        "split-private": split_private,
        "split-revoked": split_revoked,
        "seed": seed,
        "builder": builder,
        "trainer": trainer,
        "lease-boundary": lease_boundary,
        "empty-downgrade": empty_downgrade,
        "grant-training": grant_training,
        "denied": denied,
        "verify": verify,
        "verify-authorized": verify_authorized,
        "race-proof-before-append": race_proof_before_append,
        "race-append-after-proof": race_append_after_proof,
        "race-hold-append-before-proof": race_hold_append_before_proof,
        "race-proof-after-append": race_proof_after_append,
        "admin": admin,
        "security": security,
    }
    with licensed_qa_source_fixture():
        print(json.dumps(await modes[sys.argv[1]]()))


asyncio.run(main())
