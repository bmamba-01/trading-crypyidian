"""Probe race PAPER operator block/runtime với PostgreSQL Compose thật.

Fixture chỉ dựng lineage tối thiểu trong database disposable. Trigger/FK bị tắt cục bộ trong
transaction seed rồi được phục hồi trước khi service/runtime chạy; fixture và target seam không
chứng minh deployment/current-rights eligibility production.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import multibot_runtime
import wp06_scorer_deployment
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.adapters.database.engine import create_database_engine, database_url_from_secret
from trading_bot.adapters.database.models.learning_score_event_entities import (
    LearningScoreEventRecord,
)
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord
from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
    LearningPaperEntryBlockConflict,
    LearningPaperEntryBlockRepository,
)
from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
    ResolvedScorerRuntimeTarget,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.bot_dispatcher import (
    BotDecisionContext,
    BotDecisionJob,
    evaluate_bot_decision,
)
from trading_bot.application.learning_cpu_trainer import (
    LearningCpuArtifact,
    LearningCpuClassModel,
    LearningCpuValidationReport,
)
from trading_bot.application.learning_paper_entry_block import LearningPaperEntryBlockService
from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_runtime_application import (
    PaperBotDecisionCommand,
    PaperEntryEventCommand,
    PaperMonitorCommand,
)
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)
from trading_bot.domain.learning.score_event import LearningScoreDecision
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
    ScorerDeploymentTarget,
)
from trading_bot.domain.market.signal_evidence import SharedSignalEvidence

STATE: dict[str, Any] = json.loads(sys.stdin.read())


def _engine(role: str):
    return create_database_engine(
        database_url_from_secret(
            username=f"crypidian_{role}",
            password_file=Path(f"/run/secrets/database_{role}_password"),
            host="postgres",
            database="crypidian",
        )
    )


def _target_fixture() -> ResolvedScorerRuntimeTarget:
    """Dùng artifact typed hợp lệ; metadata PAPER_GATE chỉ được tiêm trong process probe."""
    request = LearningResourceRequest(
        training_samples=3,
        validation_samples=9,
        feature_count=1,
        max_steps=30,
        max_runtime_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    base = LearningCpuArtifact(
        schema_version="crypidian.learning.cpu-logistic.v1",
        trainer_version="cpu-logistic-v1",
        dependency_fingerprint="1" * 64,
        feature_schema_hash="2" * 64,
        attempt_content_hash="3" * 64,
        split_policy_version="split-v3",
        workspace_id="test-only-target-seam",
        attempt_id=str(uuid4()),
        normalized_dataset_id=str(uuid4()),
        manifest_hash="4" * 64,
        fold_plan_hash="5" * 64,
        fold_id="fold-test",
        approval_hash="6" * 64,
        batch_hash="7" * 64,
        seed=1,
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
        feature_names=("1h:momentum",),
        feature_means=(0.0,),
        feature_scales=(1.0,),
        models=(
            LearningCpuClassModel(-1, 0.0, (-1.0,), 1),
            LearningCpuClassModel(0, 0.0, (0.0,), 1),
            LearningCpuClassModel(1, 0.0, (1.0,), 1),
        ),
        training_class_counts=((-1, 1), (0, 1), (1, 1)),
        optimization_steps=3,
        actual_operation_units=1,
        validation_report=LearningCpuValidationReport(
            sample_count=9,
            accuracy=1.0,
            majority_baseline_accuracy=1 / 3,
            macro_recall=1.0,
            log_loss=0.1,
            majority_class=-1,
            confusion_matrix=((3, 0, 0), (0, 3, 0), (0, 0, 3)),
            class_support=((-1, 3), (0, 3), (1, 3)),
        ),
    )
    scorer = LearningScorerArtifact(
        schema_version="crypidian.learning.scorer-temperature.v2",
        state="ready_for_evaluation",
        reason_code=None,
        workspace_id=base.workspace_id,
        attempt_id=base.attempt_id,
        normalized_dataset_id=base.normalized_dataset_id,
        manifest_hash=base.manifest_hash,
        fold_plan_hash=base.fold_plan_hash,
        fold_id=base.fold_id,
        approval_hash=base.approval_hash,
        base_artifact_hash=base.artifact_hash,
        strategy_release_id="h03-v1",
        strategy_release_content_hash="8" * 64,
        vector_schema_version="scorer-vector-v2",
        vector_schema_hash="2" * 64,
        training_membership_digest="9" * 64,
        validation_membership_digest="a" * 64,
        training_sample_count=3,
        validation_sample_count=9,
        calibrator_method="temperature_scaling",
        calibrator_version="temperature-scaling-bisection-v1",
        calibration_policy_version="temperature-v1",
        calibration_policy_hash="b" * 64,
        temperature=1.0,
        validation_class_support=((-1, 3), (0, 3), (1, 3)),
        validation_nll_before=0.5,
        validation_nll_after=0.3,
        calibration_iterations=16,
        trainer_version="cpu-logistic-v1",
        trainer_code_version="wp06-scorer-artifact-v2",
        dependency_fingerprint="1" * 64,
    )
    registry_id, artifact_id = uuid4(), uuid4()
    target = ScorerDeploymentTarget(
        registry_id=registry_id,
        registry_revision=1,
        active_pointer_version=1,
        scorer_artifact_id=artifact_id,
        scorer_artifact_hash=scorer.artifact_hash,
        evaluation_report_id=uuid4(),
        evaluation_report_hash="a" * 64,
        evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
        proof_id=uuid4(),
        proof_hash="b" * 64,
        candidate_completeness_verified=True,
        report_scope_hash="c" * 64,
        proof_scope_hash="c" * 64,
        policy_id="wp06-paper-entry-block-e2e",
        policy_hash="d" * 64,
        current_rights_receipt_hash="e" * 64,
    )
    return ResolvedScorerRuntimeTarget(
        mode=ScorerDeploymentMode.PAPER_GATE,
        deployment_event_id=uuid4(),
        deployment_revision=1,
        target=target,
        base_artifact_id=uuid4(),
        base_artifact_hash=base.artifact_hash,
        base_artifact=base,
        scorer_artifact=scorer,
        source_rights_fingerprint="f" * 64,
        scored_candidate_coverage_verified=True,
    )


def _score_event(
    *, decision: Any, active_target: ResolvedScorerRuntimeTarget, mode: ScorerDeploymentMode
):
    event = wp06_scorer_deployment._score_event(
        decision.workspace_id,
        decision.bot_id,
        decision.decision_id,
        decision.decision_hash,
        decision.candidate_id,
    )
    target = active_target.target
    return replace(
        event,
        mode=mode,
        deployment_event_id=active_target.deployment_event_id,
        deployment_revision=active_target.deployment_revision,
        source_rights_fingerprint=active_target.source_rights_fingerprint,
        registry_id=target.registry_id,
        registry_revision=target.registry_revision,
        active_pointer_version=target.active_pointer_version,
        scorer_artifact_id=target.scorer_artifact_id,
        scorer_artifact_hash=target.scorer_artifact_hash,
        base_artifact_hash=active_target.base_artifact_hash,
        evaluation_report_id=target.evaluation_report_id,
        evaluation_report_hash=target.evaluation_report_hash,
        evaluation_status=target.evaluation_status,
        report_scope_hash=target.report_scope_hash,
        proof_id=target.proof_id,
        proof_hash=target.proof_hash,
        proof_scope_hash=target.proof_scope_hash,
        candidate_completeness_verified=(
            True if mode is ScorerDeploymentMode.PAPER_GATE else False
        ),
        policy_id=target.policy_id,
        policy_hash=target.policy_hash,
        decision=LearningScoreDecision.APPROVE,
        reason_code=None,
    )


async def _decisions(state: dict[str, Any], active_target: ResolvedScorerRuntimeTarget):

    workspace = state["workspaces"]["main"]
    runtime = multibot_runtime
    runtime.STATE = state
    now = datetime.now(UTC).replace(microsecond=0)
    root_engine = runtime._engine(pool=8)
    sessions = async_sessionmaker(root_engine, expire_on_commit=False)
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT config.id, config.bot_id, config.account_id, config.overlay_hash, "
                    "root.settings_hash FROM public.bot_config_version AS config "
                    "JOIN public.workspace_settings_version AS root "
                    "ON root.workspace_id=config.workspace_id "
                    "AND root.id=config.base_settings_version_id "
                    "WHERE config.workspace_id=:workspace AND config.id=ANY(:ids)"
                ),
                {
                    "workspace": workspace["workspace"],
                    "ids": [UUID(value) for value in state["config_ids"].values()],
                },
            )
        ).all()
    await root_engine.dispose()
    config_by_bot = {row.bot_id: row for row in rows}
    result: dict[str, Any] = {}
    for key in ("a", "b", "a_shadow"):
        account_key = "a" if key.startswith("a") else "b"
        account_id = UUID(workspace["accounts"][account_key])
        bot_id = UUID(workspace["bots"][account_key])
        config = config_by_bot[bot_id]
        release_id = "h01-v1" if account_key == "a" else "h03-v1"
        feature_snapshot_id = workspace["snapshots"][1 if key == "a_shadow" else 0]
        evaluated_at = now + timedelta(seconds=(1 if key == "a_shadow" else 0))
        evidence = SharedSignalEvidence.create(
            workspace_id=UUID(workspace["workspace"]),
            symbol="BTCUSDT",
            setup_type=f"wp06.paper.entry.block.{key}",
            observed_at=evaluated_at - timedelta(seconds=5),
            available_at=evaluated_at - timedelta(seconds=4),
            validity_end=evaluated_at + timedelta(hours=1),
            feature_snapshot_id=feature_snapshot_id,
            source_refs=(f"test:e2e:wp06:{key}",),
            model_release_id=None,
        )
        context = BotDecisionContext(
            workspace_id=UUID(workspace["workspace"]),
            bot_id=bot_id,
            account_id=account_id,
            bot_config_version_id=config.id,
            bot_config_hash=config.overlay_hash,
            root_settings_version_id=UUID(workspace["settings_version_id"]),
            root_settings_hash=config.settings_hash,
            strategy_release_id=release_id,
            symbols=("BTCUSDT",),
            effective_at=now - timedelta(days=1),
        )
        job = BotDecisionJob.create(evidence=evidence, context=context, evaluated_at=evaluated_at)
        decision = await evaluate_bot_decision(
            evidence=evidence,
            job=job,
            strategies=runtime._StubRegistry(),
            snapshots=runtime.SyntheticContextProvider(),
        )
        assert decision.candidate_id is not None, (key, decision.reason_code)
        mode = ScorerDeploymentMode.SHADOW if key == "a_shadow" else ScorerDeploymentMode.PAPER_GATE
        score_event = _score_event(decision=decision, active_target=active_target, mode=mode)
        result[key] = (decision, score_event, account_id)
    return result


async def _seed_rows(engine: Any, records: dict[str, Any]) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL session_replication_role = 'replica'"))
        for decision, event, _account_id in records.values():
            await connection.execute(
                text(
                    "INSERT INTO public.bot_decision (id,workspace_id,bot_id,account_id,job_id,"
                    "evidence_id,evidence_hash,bot_config_version_id,bot_config_hash,"
                    "root_settings_version_id,root_settings_hash,strategy_release_id,"
                    "strategy_release_hash,model_release_id,evaluated_at,status,reason_code,"
                    "candidate_id,idempotency_key,decision_hash,payload,created_at) VALUES "
                    "(:id,:workspace,:bot,:account,:job,:evidence,:evidence_hash,:config_id,"
                    ":config_hash,:root_id,:root_hash,:release,:release_hash,:model_id,:evaluated,"
                    ":status,:reason,:candidate,:key,:decision_hash,"
                    "CAST(:payload AS jsonb),:created)"
                ),
                {
                    "id": decision.decision_id,
                    "workspace": decision.workspace_id,
                    "bot": decision.bot_id,
                    "account": decision.account_id,
                    "job": decision.job_id,
                    "evidence": decision.evidence_id,
                    "evidence_hash": decision.evidence_hash,
                    "config_id": decision.bot_config_version_id,
                    "config_hash": decision.bot_config_hash,
                    "root_id": decision.root_settings_version_id,
                    "root_hash": decision.root_settings_hash,
                    "release": decision.strategy_release_id,
                    "release_hash": decision.strategy_release_hash,
                    "model_id": decision.model_release_id,
                    "evaluated": decision.evaluated_at,
                    "status": decision.status.value,
                    "reason": decision.reason_code,
                    "candidate": decision.candidate_id,
                    "key": decision.idempotency_key,
                    "decision_hash": decision.decision_hash,
                    "payload": json.dumps(decision.canonical_payload()),
                    "created": datetime.now(UTC),
                },
            )
            event_record = LearningScoreEventRecord.from_domain(event)
            await connection.execute(
                text(
                    "INSERT INTO public.learning_score_event (id,workspace_id,bot_id,"
                    "bot_decision_id,bot_decision_hash,incumbent_decision_hash,incumbent_status,"
                    "final_status,candidate_id,candidate_hash,decision_snapshot_id,vector_hash,"
                    "feature_schema_version,feature_schema_hash,timeframe_snapshot_ids,"
                    "source_event_digests,source_rights_fingerprint,"
                    "decision_source_rights_fingerprint,decision_scope_fingerprint,mode,"
                    "deployment_event_id,deployment_revision,registry_id,registry_revision,"
                    "active_pointer_version,scorer_artifact_id,scorer_artifact_hash,"
                    "base_artifact_hash,evaluation_report_id,evaluation_report_hash,"
                    "evaluation_status,report_scope_hash,proof_id,proof_hash,proof_scope_hash,"
                    "candidate_completeness_verified,policy_id,policy_hash,decision,reason_code,"
                    "class_probabilities,probability,confidence,uncertainty,model_latency_ms,"
                    "idempotency_key,event_hash,canonical_event,event_payload,created_at) VALUES "
                    "(:id,:workspace,:bot,:decision,:decision_hash,:incumbent_hash,"
                    ":incumbent_status,:final_status,:candidate,:candidate_hash,:snapshot,:vector,"
                    ":schema_version,:schema_hash,CAST(:timeframes AS jsonb),"
                    "CAST(:digests AS jsonb),:rights,:decision_rights,:scope,:mode,:deployment,"
                    ":deployment_revision,:registry,:registry_revision,:pointer,:artifact,"
                    ":artifact_hash,:base_hash,:report,:report_hash,:evaluation_status,"
                    ":report_scope,:proof,:proof_hash,:proof_scope,:complete,:policy,:policy_hash,"
                    ":result,:reason,CAST(:probabilities AS jsonb),:probability,:confidence,"
                    ":uncertainty,:latency,:key,:event_hash,:canonical,CAST(:payload AS jsonb),"
                    "clock_timestamp())"
                ),
                {
                    "id": event_record.id,
                    "workspace": event_record.workspace_id,
                    "bot": event_record.bot_id,
                    "decision": event_record.bot_decision_id,
                    "decision_hash": event_record.bot_decision_hash,
                    "incumbent_hash": event_record.incumbent_decision_hash,
                    "incumbent_status": event_record.incumbent_status,
                    "final_status": event_record.final_status,
                    "candidate": event_record.candidate_id,
                    "candidate_hash": event_record.candidate_hash,
                    "snapshot": event_record.decision_snapshot_id,
                    "vector": event_record.vector_hash,
                    "schema_version": event_record.feature_schema_version,
                    "schema_hash": event_record.feature_schema_hash,
                    "timeframes": json.dumps(event_record.timeframe_snapshot_ids),
                    "digests": json.dumps(event_record.source_event_digests),
                    "rights": event_record.source_rights_fingerprint,
                    "decision_rights": event_record.decision_source_rights_fingerprint,
                    "scope": event_record.decision_scope_fingerprint,
                    "mode": event_record.mode,
                    "deployment": event_record.deployment_event_id,
                    "deployment_revision": event_record.deployment_revision,
                    "registry": event_record.registry_id,
                    "registry_revision": event_record.registry_revision,
                    "pointer": event_record.active_pointer_version,
                    "artifact": event_record.scorer_artifact_id,
                    "artifact_hash": event_record.scorer_artifact_hash,
                    "base_hash": event_record.base_artifact_hash,
                    "report": event_record.evaluation_report_id,
                    "report_hash": event_record.evaluation_report_hash,
                    "evaluation_status": event_record.evaluation_status,
                    "report_scope": event_record.report_scope_hash,
                    "proof": event_record.proof_id,
                    "proof_hash": event_record.proof_hash,
                    "proof_scope": event_record.proof_scope_hash,
                    "complete": event_record.candidate_completeness_verified,
                    "policy": event_record.policy_id,
                    "policy_hash": event_record.policy_hash,
                    "result": event_record.decision,
                    "reason": event_record.reason_code,
                    "probabilities": json.dumps(event_record.class_probabilities),
                    "probability": event_record.probability,
                    "confidence": event_record.confidence,
                    "uncertainty": event_record.uncertainty,
                    "latency": event_record.model_latency_ms,
                    "key": event_record.idempotency_key,
                    "event_hash": event_record.event_hash,
                    "canonical": event_record.canonical_event,
                    "payload": json.dumps(event_record.event_payload),
                },
            )
        await connection.execute(text("SET LOCAL session_replication_role = 'origin'"))
        restored_role = await connection.scalar(
            text("SELECT current_setting('session_replication_role')")
        )
        assert restored_role == "origin"


def _principal(user_id: UUID, membership_id: UUID, workspace_id: UUID) -> Principal:
    return Principal(
        user_id=user_id,
        membership_id=membership_id,
        workspace_id=workspace_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=2),
        mfa_method=MfaMethod.TOTP,
    )


async def _seed_owner(root_engine: Any, workspace_id: UUID, membership_id: UUID) -> UUID:
    async with root_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT user_id FROM public.user_membership "
                    "WHERE workspace_id=:workspace AND id=:membership"
                ),
                {"workspace": workspace_id, "membership": membership_id},
            )
        ).one()
        return row.user_id


async def _counts(root_engine: Any, workspace_id: UUID, decision_id: UUID) -> dict[str, int]:
    async with root_engine.connect() as connection:
        return {
            "intent": await connection.scalar(
                select(text("count(*)"))
                .select_from(OrderIntentRecord)
                .where(
                    OrderIntentRecord.workspace_id == workspace_id,
                    OrderIntentRecord.bot_decision_id == decision_id,
                )
            ),
            "reservation": await connection.scalar(
                text(
                    "SELECT count(*) FROM public.risk_reservation AS reservation "
                    "JOIN public.order_intent AS intent "
                    "ON intent.workspace_id=reservation.workspace_id "
                    "AND intent.risk_reservation_id=reservation.id "
                    "WHERE intent.workspace_id=:workspace AND intent.bot_decision_id=:decision"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ),
            "trade": await connection.scalar(
                text(
                    "SELECT count(*) FROM public.trade AS trade "
                    "JOIN public.order_intent AS intent "
                    "ON intent.workspace_id=trade.workspace_id AND intent.id=trade.intent_id "
                    "WHERE intent.workspace_id=:workspace AND intent.bot_decision_id=:decision"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ),
        }


async def _race_probe() -> dict[str, bool]:
    import trading_bot.application.learning_paper_entry_block as block_service_module

    active_target = _target_fixture()
    state = {"variants": {"main": "1", "other": "1"}}
    multibot_runtime.STATE = state
    seeded = multibot_runtime.seed()
    state.update(seeded)
    state["config_ids"] = multibot_runtime.seed_candidate_bot_configs()["config_ids"]
    multibot_runtime.STATE = state
    await multibot_runtime.genesis()
    records = await _decisions(state, active_target)
    root_engine = multibot_runtime._engine(pool=8)
    await _seed_rows(root_engine, records)

    async def resolver(_session: Any, workspace_id: UUID):
        assert workspace_id == UUID(state["workspaces"]["main"]["workspace"])
        return active_target

    block_service_module._resolve_current_target = resolver
    runtime_engine = multibot_runtime._engine("crypidian_trading_runtime", pool=8)
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    admin_engine = multibot_runtime._engine("crypidian_admin_web", pool=8)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    try:
        workspace = UUID(state["workspaces"]["main"]["workspace"])
        owner_membership = UUID(state["workspaces"]["main"]["members"]["owner"])
        user_id = await _seed_owner(root_engine, workspace, owner_membership)
        principal = _principal(user_id, owner_membership, workspace)
        block_service = LearningPaperEntryBlockService(admin_sessions).for_principal(principal)
        async with multibot_runtime.runtime_app(
            sessions=runtime_sessions, use_real_strategy=False
        ) as app:
            runtime_application = app.state.paper_application
            active_account_ids = (
                tuple(str(item) for item in runtime_application._active_accounts)
                if runtime_application is not None
                else ()
            )
            expected_account_ids = tuple(
                state["workspaces"]["main"]["accounts"][key] for key in ("a", "b")
            )
            runtime_diagnostics = {
                "paper_runtime_error_code": app.state.paper_runtime_error_code,
                "paper_runtime_ready": getattr(app.state.paper_runtime, "ready", None),
                "paper_runtime_account_states": {
                    str(account_id): {
                        "status": item.status.value,
                        "reason": item.reason_code,
                    }
                    for account_id, item in getattr(
                        app.state.paper_runtime, "account_states", {}
                    ).items()
                },
                "active_account_ids": active_account_ids,
                "expected_account_ids": expected_account_ids,
            }
            assert runtime_application is not None, runtime_diagnostics
            assert runtime_application.is_ready, runtime_diagnostics
            assert set(expected_account_ids).issubset(active_account_ids), runtime_diagnostics
            # Lượt 1: block giữ L1 và decision lock; runtime chờ ở L1 rồi thấy ledger sau commit.
            block_decision, _block_event, block_account = records["a"]
            entered_block_decision = asyncio.Event()
            runtime_waiting_l1 = asyncio.Event()
            release_block = asyncio.Event()
            original_lock_workspace = WorkspaceSettingsRepository.lock_workspace
            original_lock_decision = LearningPaperEntryBlockRepository.lock_decision

            async def lock_workspace(self, workspace_id: UUID):
                task = asyncio.current_task()
                name = task.get_name() if task else ""
                if name == "race-block-first-runtime":
                    runtime_waiting_l1.set()
                return await original_lock_workspace(self, workspace_id)

            async def lock_decision(self, *, workspace_id: UUID, bot_decision_id: UUID):
                await original_lock_decision(
                    self, workspace_id=workspace_id, bot_decision_id=bot_decision_id
                )
                task = asyncio.current_task()
                if task is not None and task.get_name() == "race-block-first-block":
                    entered_block_decision.set()
                    await release_block.wait()

            WorkspaceSettingsRepository.lock_workspace = lock_workspace
            LearningPaperEntryBlockRepository.lock_decision = lock_decision
            try:
                before_block = await _counts(root_engine, workspace, block_decision.decision_id)
                block_task = asyncio.create_task(
                    block_service.block(
                        bot_decision_id=block_decision.decision_id,
                        reason="Block thắng race PAPER.",
                        idempotency_key=uuid4(),
                    ),
                    name="race-block-first-block",
                )
                await asyncio.wait_for(entered_block_decision.wait(), timeout=15)
                runtime_task = asyncio.create_task(
                    app.state.paper_application.evaluate_and_submit(
                        PaperBotDecisionCommand(
                            account_id=block_account,
                            bot_decision_id=block_decision.decision_id,
                        )
                    ),
                    name="race-block-first-runtime",
                )
                await asyncio.wait_for(runtime_waiting_l1.wait(), timeout=15)
                release_block.set()
                block_event = await asyncio.wait_for(block_task, timeout=30)
                runtime_result = await asyncio.wait_for(runtime_task, timeout=30)
                after_block = await _counts(root_engine, workspace, block_decision.decision_id)
                assert runtime_result.submission is None
                assert runtime_result.entry_block_reason == "LEARNING_PAPER_ENTRY_BLOCKED"
                assert before_block == after_block == {"intent": 0, "reservation": 0, "trade": 0}
                assert block_event.bot_decision_id == block_decision.decision_id
                async with UnitOfWork(admin_sessions, workspace_id=workspace) as unit:
                    persisted_block = await LearningPaperEntryBlockRepository(
                        unit.session
                    ).find_by_decision(
                        workspace_id=workspace, bot_decision_id=block_decision.decision_id
                    )
                assert persisted_block is not None
            finally:
                release_block.set()
                WorkspaceSettingsRepository.lock_workspace = original_lock_workspace
                LearningPaperEntryBlockRepository.lock_decision = original_lock_decision

            # Lượt 2: runtime giữ L1 + decision lock; block đợi L1, rồi conflict sau intent commit.
            entry_decision, _entry_event, entry_account = records["b"]
            runtime_has_decision = asyncio.Event()
            block_waiting_l1 = asyncio.Event()
            release_runtime = asyncio.Event()
            original_lock_workspace = WorkspaceSettingsRepository.lock_workspace
            original_lock_decision = LearningPaperEntryBlockRepository.lock_decision

            async def lock_workspace_second(self, workspace_id: UUID):
                task = asyncio.current_task()
                if task is not None and task.get_name() == "race-entry-first-block":
                    block_waiting_l1.set()
                return await original_lock_workspace(self, workspace_id)

            async def lock_decision_second(self, *, workspace_id: UUID, bot_decision_id: UUID):
                await original_lock_decision(
                    self, workspace_id=workspace_id, bot_decision_id=bot_decision_id
                )
                task = asyncio.current_task()
                if task is not None and task.get_name() == "race-entry-first-runtime":
                    runtime_has_decision.set()
                    await release_runtime.wait()

            WorkspaceSettingsRepository.lock_workspace = lock_workspace_second
            LearningPaperEntryBlockRepository.lock_decision = lock_decision_second
            try:
                runtime_task = asyncio.create_task(
                    app.state.paper_application.evaluate_and_submit(
                        PaperBotDecisionCommand(
                            account_id=entry_account,
                            bot_decision_id=entry_decision.decision_id,
                        )
                    ),
                    name="race-entry-first-runtime",
                )
                await asyncio.wait_for(runtime_has_decision.wait(), timeout=15)
                block_task = asyncio.create_task(
                    block_service.block(
                        bot_decision_id=entry_decision.decision_id,
                        reason="Entry đã thắng race.",
                        idempotency_key=uuid4(),
                    ),
                    name="race-entry-first-block",
                )
                await asyncio.wait_for(block_waiting_l1.wait(), timeout=15)
                release_runtime.set()
                entry_result = await asyncio.wait_for(runtime_task, timeout=60)
                try:
                    await asyncio.wait_for(block_task, timeout=30)
                    block_conflicted = False
                except LearningPaperEntryBlockConflict:
                    block_conflicted = True
                assert block_conflicted
                assert entry_result.submission is not None
                assert entry_result.risk_reservation_id is not None
                order = entry_result.submission.order
                filled = await app.state.paper_application.apply_entry_event(
                    PaperEntryEventCommand(
                        account_id=entry_account,
                        ledger_intent_id=entry_result.submission.ledger_intent_id,
                        paper_intent_id=order.intent_id,
                        bar=multibot_runtime._bar(
                            f"wp06-entry-fill-{entry_decision.decision_id.hex[:10]}",
                            order.eligible_at,
                        ),
                    )
                )
                assert filled.trade_id is not None
                assert filled.persisted_fill_count == 1
                entry_counts = await _counts(root_engine, workspace, entry_decision.decision_id)
                assert entry_counts == {"intent": 1, "reservation": 1, "trade": 1}
                async with UnitOfWork(admin_sessions, workspace_id=workspace) as unit:
                    no_new_block = await LearningPaperEntryBlockRepository(
                        unit.session
                    ).find_by_decision(
                        workspace_id=workspace, bot_decision_id=entry_decision.decision_id
                    )
                assert no_new_block is None
            finally:
                release_runtime.set()
                WorkspaceSettingsRepository.lock_workspace = original_lock_workspace
                LearningPaperEntryBlockRepository.lock_decision = original_lock_decision

            # SHADOW không thể tạo ledger PAPER block; một BotDecision khác vẫn có đường entry.
            shadow_decision, shadow_event, shadow_account = records["a_shadow"]
            assert shadow_event.mode is ScorerDeploymentMode.SHADOW
            shadow_block_conflicted = False
            try:
                await block_service.block(
                    bot_decision_id=shadow_decision.decision_id,
                    reason="SHADOW không đủ điều kiện chặn PAPER.",
                    idempotency_key=uuid4(),
                )
            except LearningPaperEntryBlockConflict:
                shadow_block_conflicted = True
            assert shadow_block_conflicted
            shadow_entry = await app.state.paper_application.evaluate_and_submit(
                PaperBotDecisionCommand(
                    account_id=shadow_account,
                    bot_decision_id=shadow_decision.decision_id,
                )
            )
            assert shadow_entry.submission is not None
            shadow_order = shadow_entry.submission.order
            shadow_fill = await app.state.paper_application.apply_entry_event(
                PaperEntryEventCommand(
                    account_id=shadow_account,
                    ledger_intent_id=shadow_entry.submission.ledger_intent_id,
                    paper_intent_id=shadow_order.intent_id,
                    bar=multibot_runtime._bar(
                        f"wp06-shadow-fill-{shadow_decision.decision_id.hex[:10]}",
                        shadow_order.eligible_at,
                    ),
                )
            )
            assert shadow_fill.trade_id is not None
            assert shadow_fill.persisted_fill_count == 1
            shadow_exit = await app.state.paper_application.monitor_position(
                PaperMonitorCommand(
                    account_id=shadow_account,
                    ledger_intent_id=shadow_entry.submission.ledger_intent_id,
                    trade_id=shadow_fill.trade_id,
                    paper_position_id=shadow_order.intent_id,
                    bar=multibot_runtime._bar(
                        f"wp06-shadow-protective-exit-{shadow_decision.decision_id.hex[:10]}",
                        shadow_order.eligible_at + timedelta(minutes=5),
                        low=shadow_order.stop_price - 1,
                    ),
                    feature_snapshot_id=state["workspaces"]["main"]["snapshots"][1],
                )
            )
            assert shadow_exit.trade_closed

            # Scope/RLS và append-only ACL được xác nhận trên login thật sau các đường service.
            other_workspace = UUID(state["workspaces"]["other"]["workspace"])
            async with UnitOfWork(admin_sessions, workspace_id=other_workspace) as unit:
                hidden = await LearningPaperEntryBlockRepository(unit.session).find_by_decision(
                    workspace_id=workspace, bot_decision_id=block_decision.decision_id
                )
            async with root_engine.connect() as connection:
                immutable_acl = await connection.scalar(
                    text(
                        "SELECT NOT has_table_privilege('crypidian_admin_web', "
                        "'public.learning_scorer_paper_entry_block', 'UPDATE') "
                        "AND NOT has_table_privilege('crypidian_admin_web', "
                        "'public.learning_scorer_paper_entry_block', 'DELETE')"
                    )
                )
            assert hidden is None
            assert immutable_acl is True

        return {
            "block_first_prevented_entry_side_effects": True,
            "entry_first_preserved_committed_trade_and_conflicted": True,
            "workspace_reader_scope_and_immutable_audit": True,
            "shadow_entry_and_protective_exit_unaffected": True,
        }
    finally:
        await root_engine.dispose()
        await runtime_engine.dispose()
        await admin_engine.dispose()


print(json.dumps(asyncio.run(_race_probe()), sort_keys=True))
