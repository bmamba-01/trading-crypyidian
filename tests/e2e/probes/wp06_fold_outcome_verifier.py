"""Probe Compose kiểm tra bộ xác minh outcome fold WP06.4."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.models import (
    LearningEvaluationAccessFoldRecord,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.adapters.database.repositories.learning_evaluation_access import (
    LearningEvaluationAccessConflict,
    LearningEvaluationAccessRepository,
)
from trading_bot.adapters.database.repositories.learning_evaluation_reports import (
    LearningEvaluationReportConflict,
    LearningEvaluationReportRepository,
)
from trading_bot.adapters.database.repositories.learning_fold_samples import (
    LearningFoldSampleRepository,
)
from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalRepository,
)
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.learning_evaluation import (
    LearningEvaluationReportService,
    LearningEvaluationRequest,
)
from trading_bot.application.learning_evaluation_access import evaluation_access_fingerprint
from trading_bot.application.learning_fold_evaluation import (
    FoldOutcomeVerificationBatch,
    FoldOutcomeVerificationRequest,
    PersistedFoldTrade,
    persisted_outcome_row_hash,
    persisted_trade_row_hash,
    verification_batch_hash,
)
from trading_bot.application.learning_fold_plan import ManifestFoldPlan, build_manifest_fold_plan
from trading_bot.application.learning_fold_samples import (
    NormalizedFoldAssignment,
    NormalizedLearningDataset,
    NormalizedLearningSample,
    build_normalized_learning_dataset,
    normalized_learning_dataset_hash,
)
from trading_bot.application.learning_split_approval import (
    LearningSplitApprovalService,
    SplitApprovalRequest,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

STATE = json.loads(os.environ.get("WP06_STATE", "{}"))
WORKSPACE_ID = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])


def _engine():
    url = database_url_from_secret(
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return create_database_engine(url)


async def _seed() -> dict[str, object]:
    engine = _engine()
    sessions = create_session_factory(engine)
    async with sessions() as session, session.begin():
        user_id, membership_id = uuid4(), uuid4()
        session.add_all(
            [
                Workspace(
                    id=WORKSPACE_ID,
                    name="WP06 verifier E2E",
                    slug=f"wp06-fold-{str(WORKSPACE_ID)[:8]}",
                ),
                UserAccount(
                    id=user_id,
                    email_normalized=f"wp06-fold-{user_id.hex}@example.invalid",
                    password_hash="fixture-only-not-a-credential",
                ),
            ]
        )
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(WORKSPACE_ID)},
        )
        session.add(UserMembership(id=membership_id, workspace_id=WORKSPACE_ID, user_id=user_id))
        sys.path.insert(0, "/e2e-tests")
        from fixtures.runtime_replay import build_h03_runtime_sensitivity

        persisted_rows = []
        for index in range(4):
            start_at = datetime(2026, 10, 20, tzinfo=UTC) + timedelta(days=2 * index)
            replay = build_h03_runtime_sensitivity(
                WORKSPACE_ID,
                (SensitivityScenario(scenario_id=f"base-{index}", kind="BASE"),),
                start_at=start_at,
            )
            if not replay.is_complete or len(replay.runs) != 1:
                raise AssertionError("PAPER replay fixture chưa hoàn tất đúng một trial.")
            run = replay.runs[0]
            receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
                workspace_id=WORKSPACE_ID,
                sensitivity_run=run.sensitivity_run,
                trade_outcomes=tuple(
                    replace(trade, trade_id=f"fold-fixture-{index}-{trade.trade_id}")
                    for trade in run.trade_outcomes
                ),
            )
            persisted = await BacktestOutcomeRepository(session).load_trade_outcome_rows(
                workspace_id=WORKSPACE_ID,
                trial_record_id=receipt.trial_record_id,
            )
            if len(persisted.rows) != 1:
                raise AssertionError("Mỗi PAPER trial fixture phải có đúng một trade.")
            persisted_rows.append(persisted.rows[0])

        samples = []
        roles = (
            ("train-sample", DatasetSplit.TRAIN),
            ("validation-sample", DatasetSplit.VALIDATION),
            ("test-sample", DatasetSplit.TEST),
            ("sealed-holdout-sample", DatasetSplit.HOLDOUT),
        )
        for index, (sample_id, split) in enumerate(roles):
            outcome = persisted_rows[index]
            if not outcome.trade.has_entry_provenance:
                raise AssertionError("Fixture trade cần lineage feature tại entry để chuẩn hóa.")
            decision = outcome.trade.opened_at - timedelta(hours=1)
            samples.append(
                DatasetSample(
                    sample_id=sample_id,
                    decision_at=decision,
                    feature_available_at=decision - timedelta(minutes=1),
                    outcome_start=outcome.trade.opened_at,
                    outcome_end=outcome.trade.closed_at,
                    feature_snapshot_hash=outcome.trade.entry_features_hash,
                    outcome_id=str(outcome.record_id),
                    outcome_hash=outcome.outcome_hash,
                    label_hash=hex(index + 6)[2:] * 64,
                    label_value=(outcome.trade.net_pnl > 0) - (outcome.trade.net_pnl < 0),
                    label_version="e2e-label-v1",
                    split=split,
                    source_dependent=False,
                    source_rights_hashes=(),
                    source_revision_refs=(),
                )
            )
        data_start = samples[0].decision_at - timedelta(hours=1)
        data_end = persisted_rows[-1].trade.closed_at + timedelta(hours=1)
        manifest = DatasetManifest(
            workspace_id=WORKSPACE_ID,
            query_hash="1" * 64,
            symbols=("BTCUSDT",),
            data_start=data_start,
            data_end=data_end,
            as_of=data_end,
            feature_schema_version="e2e-feature-schema-v1",
            feature_version="e2e-feature-v1",
            label_version="e2e-label-v1",
            sample_schema_version="e2e-sample-v1",
            split_policy_version="wp06-walk-forward-v1",
            samples=tuple(samples),
            exclusions=(),
            holdout_sealed_at=data_end,
            retention_policy_ref="e2e-retention-v1",
        )
        folds = _plan(manifest).folds
        from e2e.probes.wp06_split_approval_anchor import append_split_approval_anchor

        await append_split_approval_anchor(
            session,
            manifest=manifest,
            folds=tuple(item.split for item in folds),
            evaluation_cutoff=manifest.as_of,
            trial_record_id=persisted_rows[0].trial_record_id,
        )
        identity = Principal(
            user_id=user_id,
            membership_id=membership_id,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=datetime.now(UTC),
        )
        approval = (
            await LearningSplitApprovalService(LearningSplitApprovalRepository(session))
            .for_principal(identity)
            .approve(
                SplitApprovalRequest(
                    manifest=manifest,
                    folds=tuple(item.split for item in folds),
                    evaluation_cutoff=manifest.as_of,
                    reason="Duyệt fixture kiểm thử receipt evaluator WP06.",
                    idempotency_key=uuid4(),
                )
            )
        )
        await DatasetManifestRepository(session).append_for_training(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
            folds=tuple(item.split for item in folds),
            evaluation_cutoff=manifest.as_of,
        )
        assignments = tuple(
            NormalizedFoldAssignment(fold.fold_id, sample_id, role)
            for fold in folds
            for role, sample_ids in (
                ("train", fold.train_sample_ids),
                ("validation", fold.validation_sample_ids),
                ("test", fold.test_sample_ids),
            )
            for sample_id in sample_ids
        )
        selected_sample_ids = {assignment.sample_id for assignment in assignments}
        samples_by_id = {sample.sample_id: sample for sample in manifest.samples}
        outcomes_by_id = {str(row.record_id): row for row in persisted_rows}
        normalized_samples = tuple(
            NormalizedLearningSample(
                sample_id=sample_id,
                outcome_id=samples_by_id[sample_id].outcome_id,
                outcome_hash=samples_by_id[sample_id].outcome_hash,
                feature_snapshot_hash=samples_by_id[sample_id].feature_snapshot_hash,
                feature_schema_hash=outcomes_by_id[
                    samples_by_id[sample_id].outcome_id
                ].trade.entry_feature_schema_hash,
                label_hash=samples_by_id[sample_id].label_hash,
                label_value=samples_by_id[sample_id].label_value,
                features=outcomes_by_id[samples_by_id[sample_id].outcome_id].trade.entry_features,
            )
            for sample_id in sorted(selected_sample_ids)
        )
        normalized = NormalizedLearningDataset(
            workspace_id=WORKSPACE_ID,
            manifest_hash=manifest.content_hash,
            split_policy_version=manifest.split_policy_version,
            fold_plan_hash=_plan(manifest).content_hash,
            samples=normalized_samples,
            assignments=assignments,
            content_hash="",
        )
        from dataclasses import replace as replace_dataclass

        normalized = replace_dataclass(
            normalized, content_hash=normalized_learning_dataset_hash(normalized)
        )
        await LearningFoldSampleRepository(session).append_normalized_dataset(
            manifest=manifest,
            fold_plan=_plan(manifest),
            approval=approval,
            dataset=normalized,
        )
    await engine.dispose()
    plan = _plan(manifest)
    fold = plan.folds[0]
    return {
        "manifest_hash": manifest.content_hash,
        "workspace_id": str(WORKSPACE_ID),
        "idempotency_key": str(uuid4()),
        "rollback_run_id": str(uuid4()),
        "crash_run_id": str(uuid4()),
        "race_run_id": str(uuid4()),
        "evaluation_cutoff": manifest.as_of.isoformat(),
        "train_start": fold.split.train.start.isoformat(),
        "train_end": fold.split.train.end.isoformat(),
        "validation_start": fold.split.validation.start.isoformat(),
        "validation_end": fold.split.validation.end.isoformat(),
        "test_start": fold.split.test.start.isoformat(),
        "test_end": fold.split.test.end.isoformat(),
    }


def _plan(manifest: DatasetManifest):
    train = next(item for item in manifest.samples if item.sample_id == "train-sample")
    validation = next(item for item in manifest.samples if item.sample_id == "validation-sample")
    test = next(item for item in manifest.samples if item.sample_id == "test-sample")
    split = WalkForwardSplit(
        train=TemporalWindow(
            train.decision_at - timedelta(hours=1), train.decision_at + timedelta(hours=1)
        ),
        validation=TemporalWindow(
            validation.decision_at - timedelta(hours=1),
            validation.decision_at + timedelta(hours=1),
        ),
        test=TemporalWindow(
            test.decision_at - timedelta(hours=1), test.decision_at + timedelta(hours=1)
        ),
    )
    return build_manifest_fold_plan(
        manifest,
        folds=(split,),
        expected_split_policy_version=manifest.split_policy_version,
        evaluation_cutoff=manifest.as_of,
    )


async def _verify() -> dict[str, object]:
    from trading_bot.application import learning_evaluation as evaluation_module

    engine = _engine()
    sessions = create_session_factory(engine)
    workspace_id = UUID(STATE["workspace_id"])
    evaluation_request = LearningEvaluationRequest(
        workspace_id=workspace_id,
        manifest_hash=STATE["manifest_hash"],
        folds=_folds_for_seed(),
        evaluation_cutoff=datetime.fromisoformat(STATE["evaluation_cutoff"]),
        seed=19,
        idempotency_key=UUID(STATE["idempotency_key"]),
    )
    request_fingerprint = evaluation_access_fingerprint(evaluation_request)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
        fingerprint_valid = await uow.session.scalar(
            text("SELECT CAST(:value AS text) ~ '^[0-9a-f]{64}$'"),
            {"value": request_fingerprint},
        )
    if not fingerprint_valid:
        raise AssertionError("Fingerprint SHA-256 không khớp regex PostgreSQL.")
    evaluation_service = LearningEvaluationReportService(sessions)
    captured_batches = []
    verifier_type = evaluation_module.DatabaseFoldOutcomeVerifier

    class CapturingDatabaseFoldOutcomeVerifier(verifier_type):
        async def load_verified_fold_outcomes(
            self, request: FoldOutcomeVerificationRequest
        ) -> FoldOutcomeVerificationBatch:
            batch = await super().load_verified_fold_outcomes(request)
            captured_batches.append((self._manifest, request, batch))
            return batch

    evaluation_module.DatabaseFoldOutcomeVerifier = CapturingDatabaseFoldOutcomeVerifier
    try:
        saved = await evaluation_service.evaluate(evaluation_request)
    finally:
        evaluation_module.DatabaseFoldOutcomeVerifier = verifier_type

    normalizer_accepts_database_verified_batch = False
    normalizer_rejects_no_trade_sample = False
    normalizer_rejects_multi_trade_sample = False
    if len(captured_batches) == 1:
        verified_manifest, verifier_request, verified_batch = captured_batches[0]
        normalized = build_normalized_learning_dataset(
            manifest=verified_manifest,
            fold_plan=verifier_request.fold_plan,
            verified_batches=(verified_batch,),
        )
        normalizer_accepts_database_verified_batch = {
            sample.sample_id for sample in normalized.samples
        } == {"train-sample", "validation-sample", "test-sample"}
        no_trade_batch = _batch_with_trade_count(
            verifier_request, verified_batch, sample_id="train-sample", trade_count=0
        )
        multi_trade_batch = _batch_with_trade_count(
            verifier_request, verified_batch, sample_id="train-sample", trade_count=2
        )
        normalizer_rejects_no_trade_sample = _normalizer_rejects_batch(
            verified_manifest,
            verifier_request.fold_plan,
            no_trade_batch,
            expected_message="Outcome không có đúng một trade đã đóng",
        )
        normalizer_rejects_multi_trade_sample = _normalizer_rejects_batch(
            verified_manifest,
            verifier_request.fold_plan,
            multi_trade_batch,
            expected_message="Outcome có nhiều trade trên một sample chưa có quy tắc gộp",
        )

    retried = await evaluation_service.evaluate(evaluation_request)
    changed_payload_rejected = False
    try:
        await evaluation_service.evaluate(replace(evaluation_request, seed=20))
    except LearningEvaluationReportConflict, LearningEvaluationAccessConflict:
        changed_payload_rejected = True

    mismatched_workspace_rejected = False
    try:
        await evaluation_service.evaluate(replace(evaluation_request, workspace_id=uuid4()))
    except LearningEvaluationAccessConflict:
        mismatched_workspace_rejected = True

    # Rollback trước commit không tiêu thụ run; retry cùng khóa phải chạy được.
    rollback_request = replace(evaluation_request, idempotency_key=UUID(STATE["rollback_run_id"]))
    try:
        async with UnitOfWork(
            sessions,
            workspace_id=workspace_id,
            snapshot_repeatable_read=True,
        ) as uow:
            await LearningEvaluationAccessRepository(uow.session).open(
                workspace_id=workspace_id,
                manifest_hash=rollback_request.manifest_hash,
                evaluation_run_id=rollback_request.idempotency_key,
                request_fingerprint=evaluation_access_fingerprint(rollback_request),
            )
            raise RuntimeError("rollback-before-disclosure")
    except RuntimeError:
        pass
    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
        rollback_receipt = await LearningEvaluationAccessRepository(uow.session).get_for_run(
            workspace_id=workspace_id,
            evaluation_run_id=rollback_request.idempotency_key,
        )
    rollback_succeeded = rollback_receipt is None
    rollback_retry = await evaluation_service.evaluate(rollback_request)
    rollback_succeeded = rollback_succeeded and bool(rollback_retry.record_id)

    # Mô phỏng process chết sau commit receipt nhưng trước khi persist report.
    crash_request = replace(evaluation_request, idempotency_key=UUID(STATE["crash_run_id"]))
    async with UnitOfWork(
        sessions,
        workspace_id=workspace_id,
        snapshot_repeatable_read=True,
    ) as uow:
        await LearningEvaluationAccessRepository(uow.session).open(
            workspace_id=workspace_id,
            manifest_hash=crash_request.manifest_hash,
            evaluation_run_id=crash_request.idempotency_key,
            request_fingerprint=evaluation_access_fingerprint(crash_request),
        )
    crash_retry_denied = False
    try:
        await evaluation_service.evaluate(crash_request)
    except LearningEvaluationAccessConflict:
        crash_retry_denied = True

    # Hai transaction đồng thời cùng khóa: unique receipt chỉ cho phép một bên.
    race_request = replace(evaluation_request, idempotency_key=UUID(STATE["race_run_id"]))

    async def open_race_receipt() -> bool:
        try:
            async with UnitOfWork(
                sessions,
                workspace_id=workspace_id,
                snapshot_repeatable_read=True,
            ) as uow:
                await LearningEvaluationAccessRepository(uow.session).open(
                    workspace_id=workspace_id,
                    manifest_hash=race_request.manifest_hash,
                    evaluation_run_id=race_request.idempotency_key,
                    request_fingerprint=evaluation_access_fingerprint(race_request),
                )
            return True
        except LearningEvaluationAccessConflict:
            return False

    race_outcomes = await asyncio.gather(open_race_receipt(), open_race_receipt())
    concurrent_single_winner = sum(race_outcomes) == 1

    async with UnitOfWork(
        sessions,
        workspace_id=workspace_id,
        snapshot_repeatable_read=True,
    ) as uow:
        isolation = str(
            (await uow.session.execute(text("SHOW transaction_isolation"))).scalar_one()
        )
        read_only = str(
            (await uow.session.execute(text("SHOW transaction_read_only"))).scalar_one()
        )
    repeatable_read_write_required = isolation.lower() == "repeatable read" and (
        read_only.lower() in ("off", "false", "0")
    )

    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
        saved_record = await LearningEvaluationReportRepository(uow.session).get_by_idempotency_key(
            workspace_id=workspace_id,
            idempotency_key=evaluation_request.idempotency_key,
        )
        if saved_record is None:
            raise AssertionError("Report diagnostic không đọc lại được trong workspace.")
        saved_receipt = await LearningEvaluationAccessRepository(uow.session).get_for_run(
            workspace_id=workspace_id,
            evaluation_run_id=evaluation_request.idempotency_key,
        )
        if saved_receipt is None:
            raise AssertionError("Receipt đã commit trước khi report được persist.")
        access_fold_count = await uow.session.scalar(
            select(func.count())
            .select_from(LearningEvaluationAccessFoldRecord)
            .where(
                LearningEvaluationAccessFoldRecord.workspace_id == workspace_id,
                LearningEvaluationAccessFoldRecord.receipt_id == saved_receipt.id,
            )
        )
        evaluator_grants = await uow.session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'learning_evaluation_report', 'SELECT'), "
                "has_table_privilege(current_user, 'learning_evaluation_report', 'INSERT'), "
                "has_table_privilege(current_user, 'learning_evaluation_report', 'UPDATE'), "
                "has_table_privilege(current_user, 'learning_evaluation_report', 'DELETE')"
            )
        )
        evaluator_select, evaluator_insert, evaluator_update, evaluator_delete = (
            evaluator_grants.one()
        )
        update_denied = await _execute_mutation_probe(
            uow.session,
            "UPDATE learning_evaluation_report SET seed = seed WHERE id = :record_id",
            saved.record_id,
        )
        delete_denied = await _execute_mutation_probe(
            uow.session,
            "DELETE FROM learning_evaluation_report WHERE id = :record_id",
            saved.record_id,
        )
    other_workspace_id = uuid4()
    async with UnitOfWork(sessions, workspace_id=other_workspace_id) as uow:
        cross_workspace_record = await LearningEvaluationReportRepository(
            uow.session
        ).get_by_idempotency_key(
            workspace_id=other_workspace_id,
            idempotency_key=evaluation_request.idempotency_key,
        )
    report = saved.report
    evaluated_fold = report.fold_evaluations[0]
    evaluator_direct_payload_denied = True
    for column, table in (
        ("manifest", "dataset_manifest"),
        ("canonical_manifest", "dataset_manifest"),
        ("outcome", "backtest_trade_outcome"),
    ):
        async with engine.connect() as connection:
            try:
                await connection.execute(text(f"SELECT {column} FROM public.{table} LIMIT 1"))
            except SQLAlchemyError as error:
                evaluator_direct_payload_denied = evaluator_direct_payload_denied and (
                    "permission denied" in str(error).lower()
                )
            else:
                evaluator_direct_payload_denied = False
    await engine.dispose()
    return {
        "manifest_hash": report.manifest_hash,
        "report_hash": report.report_hash,
        "train_sample_ids": list(evaluated_fold.train.sample_ids),
        "validation_sample_ids": list(evaluated_fold.validation.sample_ids),
        "test_sample_ids": list(evaluated_fold.test.sample_ids),
        "train_trade_count": evaluated_fold.train.metrics.trade_count,
        "train_net_pnl": str(evaluated_fold.train.metrics.net_pnl),
        "validation_trade_count": evaluated_fold.validation.metrics.trade_count,
        "test_trade_count": evaluated_fold.test.metrics.trade_count,
        "normalizer_accepts_database_verified_batch": normalizer_accepts_database_verified_batch,
        "normalizer_rejects_no_trade_sample": normalizer_rejects_no_trade_sample,
        "normalizer_rejects_multi_trade_sample": normalizer_rejects_multi_trade_sample,
        "candidate_completeness_verified": report.candidate_completeness_verified,
        "holdout_payload_in_report": "sealed-holdout-sample" in str(report.fold_evaluations),
        "mismatched_workspace_rejected": mismatched_workspace_rejected,
        "repeatable_read_write_required": repeatable_read_write_required,
        "rollback_before_commit_allows_retry": rollback_succeeded,
        "crash_after_commit_retry_denied": crash_retry_denied,
        "concurrent_single_receipt_winner": concurrent_single_winner,
        "receipt_persisted_before_report": saved_receipt is not None,
        "receipt_fold_consumed_once": access_fold_count == 1,
        "evaluator_direct_payload_select_denied": evaluator_direct_payload_denied,
        "persisted_report_id": str(saved.record_id),
        "persisted_report_hash": saved.report.report_hash,
        "persisted_report_canonical_hash_matches": saved_record.report_hash
        == saved.report.report_hash,
        "persisted_report_duplicate_retry": retried.is_duplicate,
        "retry_kept_same_record": retried.record_id == saved.record_id,
        "changed_payload_rejected": changed_payload_rejected,
        "holdout_payload_in_canonical_report": "sealed-holdout-sample"
        in saved.report.canonical_json,
        "cross_workspace_hidden": cross_workspace_record is None,
        "evaluator_select_insert_only": evaluator_select
        and evaluator_insert
        and not evaluator_update
        and not evaluator_delete,
        "evaluator_update_denied": update_denied,
        "evaluator_delete_denied": delete_denied,
    }


def _batch_with_trade_count(
    request: FoldOutcomeVerificationRequest,
    batch: FoldOutcomeVerificationBatch,
    *,
    sample_id: str,
    trade_count: int,
) -> FoldOutcomeVerificationBatch:
    outcomes = []
    for outcome in batch.outcomes:
        if outcome.sample_id != sample_id:
            outcomes.append(outcome)
            continue
        trades = outcome.trades
        changed = replace(outcome, trade_count=trade_count, trades=(), row_hash="0" * 64)
        if trade_count == 2:
            if len(trades) != 1:
                raise AssertionError(
                    "PostgreSQL verifier phải trả đúng một trade chuẩn cho fixture."
                )
            first = trades[0]
            second = PersistedFoldTrade(
                ordinal=1,
                trade=replace(first.trade, trade_id=f"{first.trade.trade_id}-extra"),
                row_hash="0" * 64,
            )
            changed = replace(changed, trades=(first, second))
            first = replace(
                first,
                row_hash=persisted_trade_row_hash(request, changed, first),
            )
            second = replace(
                second,
                row_hash=persisted_trade_row_hash(request, changed, second),
            )
            changed = replace(changed, trades=(first, second))
        changed = replace(
            changed,
            row_hash=persisted_outcome_row_hash(request, changed, changed.trades),
        )
        outcomes.append(changed)
    ordered = tuple(sorted(outcomes, key=lambda item: item.sample_id))
    return replace(
        batch,
        outcomes=ordered,
        batch_hash=verification_batch_hash(request, ordered),
    )


def _normalizer_rejects_batch(
    manifest: DatasetManifest,
    fold_plan: ManifestFoldPlan,
    batch: FoldOutcomeVerificationBatch,
    *,
    expected_message: str,
) -> bool:
    try:
        build_normalized_learning_dataset(
            manifest=manifest,
            fold_plan=fold_plan,
            verified_batches=(batch,),
        )
    except ValueError as error:
        return expected_message in str(error)
    return False


def _folds_for_seed() -> tuple[WalkForwardSplit, ...]:
    return (
        WalkForwardSplit(
            train=TemporalWindow(
                datetime.fromisoformat(STATE["train_start"]),
                datetime.fromisoformat(STATE["train_end"]),
            ),
            validation=TemporalWindow(
                datetime.fromisoformat(STATE["validation_start"]),
                datetime.fromisoformat(STATE["validation_end"]),
            ),
            test=TemporalWindow(
                datetime.fromisoformat(STATE["test_start"]),
                datetime.fromisoformat(STATE["test_end"]),
            ),
        ),
    )


async def _execute_mutation_probe(session, statement: str, record_id: UUID) -> bool:
    try:
        async with session.begin_nested():
            await session.execute(text(statement), {"record_id": record_id})
    except SQLAlchemyError:
        return True
    return False


async def _verify_admin() -> dict[str, object]:
    engine = _engine()
    sessions = create_session_factory(engine)
    workspace_id = UUID(STATE["workspace_id"])
    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
        record = await LearningEvaluationReportRepository(uow.session).get_by_idempotency_key(
            workspace_id=workspace_id,
            idempotency_key=UUID(STATE["idempotency_key"]),
        )
        grants = await uow.session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'learning_evaluation_report', 'SELECT'), "
                "has_table_privilege(current_user, 'learning_evaluation_report', 'INSERT'), "
                "has_table_privilege(current_user, 'learning_evaluation_report', 'UPDATE'), "
                "has_table_privilege(current_user, 'learning_evaluation_report', 'DELETE')"
            )
        )
        can_select, can_insert, can_update, can_delete = grants.one()
        if record is None:
            raise AssertionError("Admin không đọc được report diagnostic trong workspace.")
        update_denied = await _execute_mutation_probe(
            uow.session,
            "UPDATE learning_evaluation_report SET seed = seed WHERE id = :record_id",
            record.id,
        )
        delete_denied = await _execute_mutation_probe(
            uow.session,
            "DELETE FROM learning_evaluation_report WHERE id = :record_id",
            record.id,
        )
    other_workspace_id = uuid4()
    async with UnitOfWork(sessions, workspace_id=other_workspace_id) as other_uow:
        hidden = await LearningEvaluationReportRepository(other_uow.session).get_by_idempotency_key(
            workspace_id=other_workspace_id,
            idempotency_key=UUID(STATE["idempotency_key"]),
        )
    await engine.dispose()
    return {
        "admin_can_read": record is not None and can_select,
        "admin_select_only": can_select and not can_insert and not can_update and not can_delete,
        "admin_update_denied": update_denied,
        "admin_delete_denied": delete_denied,
        "admin_cross_workspace_hidden": hidden is None,
    }


async def _verify_legacy_payload_denied() -> dict[str, object]:
    engine = _engine()
    sessions = create_session_factory(engine)
    workspace_id = UUID(STATE["workspace_id"])
    denied: dict[str, bool] = {}
    for table, column in (
        ("dataset_manifest", "manifest"),
        ("dataset_manifest", "canonical_manifest"),
        ("backtest_trade_outcome", "outcome"),
    ):
        async with engine.connect() as connection:
            try:
                await connection.execute(text(f"SELECT {column} FROM public.{table} LIMIT 1"))
            except SQLAlchemyError as error:
                denied[f"{table}.{column}"] = "permission denied" in str(error).lower()
            else:
                denied[f"{table}.{column}"] = False

    async with engine.connect() as connection:
        metadata_read = await connection.scalar(
            text(
                "SELECT has_column_privilege(current_user, 'public.dataset_manifest', "
                "'manifest_hash', 'SELECT')"
            )
        )
        app_payload_read = await connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_app', 'public.dataset_manifest', "
                "'manifest', 'SELECT') OR has_column_privilege('crypidian_app', "
                "'public.backtest_trade_outcome', 'outcome', 'SELECT')"
            )
        )

    legacy_verifier_denied = False
    try:
        async with UnitOfWork(
            sessions,
            workspace_id=workspace_id,
        ) as uow:
            await BacktestOutcomeRepository(uow.session).load_trade_outcome_rows_by_id(
                workspace_id=workspace_id,
                trial_record_id=uuid4(),
                outcome_record_ids={uuid4()},
            )
    except SQLAlchemyError as error:
        legacy_verifier_denied = "permission denied" in str(error).lower()
    await engine.dispose()
    return {
        "direct_payload_select_denied": all(denied.values()),
        "metadata_column_allowed": bool(metadata_read),
        "app_payload_select_denied": not bool(app_payload_read),
        "legacy_verifier_denied": legacy_verifier_denied,
    }


async def main() -> None:
    phase = sys.argv[1]
    if phase == "seed":
        result = await _seed()
    elif phase == "verify-admin":
        result = await _verify_admin()
    elif phase == "verify-payload-denied":
        result = await _verify_legacy_payload_denied()
    else:
        result = await _verify()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
