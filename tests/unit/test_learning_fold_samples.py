import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    NormalizedLearningDatasetRecord,
    NormalizedLearningSampleRecord,
)
from trading_bot.adapters.database.repositories.learning_fold_samples import _validate_input
from trading_bot.application.learning_fold_evaluation import (
    FoldOutcomeExpectation,
    FoldOutcomeVerificationBatch,
    FoldOutcomeVerificationRequest,
    PersistedFoldOutcome,
    PersistedFoldTrade,
    persisted_outcome_row_hash,
    persisted_trade_row_hash,
    verification_batch_hash,
)
from trading_bot.application.learning_fold_plan import ManifestFoldPlan, build_manifest_fold_plan
from trading_bot.application.learning_fold_samples import (
    build_normalized_learning_dataset,
    build_trainer_fold_batch,
    normalized_learning_dataset_hash,
)
from trading_bot.domain.learning.approval import LearningSplitApproval
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import (
    EvaluationTrade,
    TemporalWindow,
    WalkForwardSplit,
    calculate_entry_features_hash,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
WORKSPACE = uuid4()
SCHEMA_HASH = "a" * 64


def test_normalized_sample_downgrade_guard_precedes_destructive_ddl() -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/f2a4c6e8b0d3_wp06_normalized_fold_samples.py"
    )
    spec = importlib.util.spec_from_file_location(
        "wp06_normalized_sample_migration", migration_path
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.downgrade()

    sql = output.getvalue()
    guard_end = sql.index("END $$")
    guard_sql = sql[:guard_end]
    assert "WP06_NORMALIZED_SAMPLE_DOWNGRADE_REQUIRES_EMPTY_TABLES" in guard_sql
    for table in (
        "learning_fold_assignment",
        "learning_normalized_sample",
        "learning_normalized_dataset",
    ):
        assert f"SELECT 1 FROM public.{table}" in guard_sql
    first_drop = min(sql.index("DROP TRIGGER"), sql.index("DROP TABLE"))
    assert guard_end < first_drop


def at(hour: int) -> datetime:
    return BASE + timedelta(hours=hour)


def manifest() -> DatasetManifest:
    values = (
        ("dev-1", 1, DatasetSplit.DEVELOPMENT, 1),
        ("dev-2", 4, DatasetSplit.DEVELOPMENT, -1),
        ("dev-3", 6, DatasetSplit.DEVELOPMENT, 1),
        ("dev-4", 9, DatasetSplit.DEVELOPMENT, -1),
        ("dev-5", 11, DatasetSplit.DEVELOPMENT, 1),
        ("holdout", 14, DatasetSplit.HOLDOUT, 1),
    )
    samples = []
    for sample_id, decision_hour, split_role, label_value in values:
        decision = at(decision_hour)
        features = (("momentum", float(decision_hour)), ("volatility", 0.25))
        feature_hash = calculate_entry_features_hash(
            f"snapshot:{sample_id}", decision, SCHEMA_HASH, features
        )
        samples.append(
            DatasetSample(
                sample_id=sample_id,
                decision_at=decision,
                feature_available_at=decision - timedelta(minutes=1),
                outcome_start=decision,
                outcome_end=decision + timedelta(minutes=30),
                feature_snapshot_hash=feature_hash,
                outcome_id=str(uuid4()),
                outcome_hash=(str(decision_hour % 10) or "1") * 64,
                label_hash="b" * 64,
                label_value=label_value,
                label_version="labels-v1",
                split=split_role,
                source_dependent=False,
                source_rights_hashes=(),
                source_revision_refs=(),
            )
        )
    return DatasetManifest(
        workspace_id=WORKSPACE,
        query_hash="c" * 64,
        symbols=("BTCUSDT",),
        data_start=at(0),
        data_end=at(16),
        as_of=at(16),
        feature_schema_version="features-v1",
        feature_version="entry-v1",
        label_version="labels-v1",
        sample_schema_version="samples-v1",
        split_policy_version="policy-v2",
        samples=tuple(samples),
        exclusions=(),
        holdout_sealed_at=at(16),
        manifest_version="dataset-manifest-v2",
    )


def fold_plan(dataset: DatasetManifest) -> ManifestFoldPlan:
    folds = (
        WalkForwardSplit(
            train=TemporalWindow(at(0), at(3)),
            validation=TemporalWindow(at(3), at(5)),
            test=TemporalWindow(at(5), at(8)),
        ),
        WalkForwardSplit(
            train=TemporalWindow(at(0), at(8)),
            validation=TemporalWindow(at(8), at(10)),
            test=TemporalWindow(at(10), at(13)),
        ),
    )
    return build_manifest_fold_plan(
        dataset,
        folds=folds,
        expected_split_policy_version="policy-v2",
        evaluation_cutoff=at(16),
    )


def verified_batches(
    dataset: DatasetManifest, plan: ManifestFoldPlan
) -> tuple[FoldOutcomeVerificationBatch, ...]:
    samples_by_id = {sample.sample_id: sample for sample in dataset.samples}
    batches = []
    for selection in plan.folds:
        role_ids = {
            **dict.fromkeys(selection.train_sample_ids, "train"),
            **dict.fromkeys(selection.validation_sample_ids, "validation"),
            **dict.fromkeys(selection.test_sample_ids, "test"),
        }
        expectations = tuple(
            FoldOutcomeExpectation(
                sample_id=sample_id,
                role=role,
                outcome_id=samples_by_id[sample_id].outcome_id,
                outcome_hash=samples_by_id[sample_id].outcome_hash,
                outcome_start=samples_by_id[sample_id].outcome_start,
                outcome_end=samples_by_id[sample_id].outcome_end,
            )
            for sample_id, role in sorted(role_ids.items())
        )
        request = FoldOutcomeVerificationRequest(
            workspace_id=dataset.workspace_id,
            manifest_hash=dataset.content_hash,
            split_policy_version=dataset.split_policy_version,
            fold_plan=plan,
            fold_plan_hash=plan.content_hash,
            fold_id=selection.fold_id,
            expected_outcomes=expectations,
        )
        outcome_rows = []
        for expected in expectations:
            sample = samples_by_id[expected.sample_id]
            decision_hour = int((sample.decision_at - BASE).total_seconds() // 3600)
            features = (("momentum", float(decision_hour)), ("volatility", 0.25))
            trade = EvaluationTrade(
                trade_id=f"trade:{expected.sample_id}",
                opened_at=sample.outcome_start,
                closed_at=sample.outcome_end,
                gross_pnl=Decimal(str(sample.label_value)),
                fees=Decimal(0),
                funding=Decimal(0),
                slippage=Decimal(0),
                exposure=Decimal(1),
                entry_decision_snapshot_id=f"snapshot:{expected.sample_id}",
                entry_decision_at=sample.decision_at,
                entry_feature_schema_hash=SCHEMA_HASH,
                entry_features=features,
                entry_features_hash=sample.feature_snapshot_hash,
                entry_symbol="BTCUSDT",
            )
            outcome = PersistedFoldOutcome(
                sample_id=expected.sample_id,
                role=expected.role,
                outcome_id=expected.outcome_id,
                outcome_hash=expected.outcome_hash,
                status="COMPLETED",
                trade_count=1,
                row_hash="0" * 64,
                trades=(),
            )
            row = PersistedFoldTrade(ordinal=0, trade=trade, row_hash="0" * 64)
            row = replace(row, row_hash=persisted_trade_row_hash(request, outcome, row))
            outcome = replace(outcome, trades=(row,))
            outcome = replace(
                outcome,
                row_hash=persisted_outcome_row_hash(request, outcome, (row,)),
            )
            outcome_rows.append(outcome)
        ordered = tuple(sorted(outcome_rows, key=lambda item: item.sample_id))
        batches.append(
            FoldOutcomeVerificationBatch(
                workspace_id=dataset.workspace_id,
                manifest_hash=dataset.content_hash,
                split_policy_version=dataset.split_policy_version,
                fold_plan_hash=plan.content_hash,
                fold_id=selection.fold_id,
                outcomes=ordered,
                batch_hash=verification_batch_hash(request, ordered),
            )
        )
    return tuple(batches)


def approval_for(dataset: DatasetManifest, plan: ManifestFoldPlan) -> LearningSplitApproval:
    now = datetime.now(UTC)
    return LearningSplitApproval(
        workspace_id=dataset.workspace_id,
        manifest_hash=dataset.content_hash,
        split_policy_version=dataset.split_policy_version,
        fold_plan_hash=plan.content_hash,
        actor_membership_id=uuid4(),
        idempotency_key=uuid4(),
        reason="Đã kiểm tra candidate và kế hoạch chia theo thời gian.",
        approved_at=now,
        mfa_verified_at=now,
    )


def test_normalized_fold_dataset_keeps_fold_roles_and_excludes_final_holdout() -> None:
    dataset = manifest()
    plan = fold_plan(dataset)
    normalized = build_normalized_learning_dataset(
        manifest=dataset,
        fold_plan=plan,
        verified_batches=verified_batches(dataset, plan),
    )

    assert normalized.manifest_hash == dataset.content_hash
    assert {item.sample_id for item in normalized.samples} == {
        "dev-1",
        "dev-2",
        "dev-3",
        "dev-4",
        "dev-5",
    }
    assert "holdout" not in {item.sample_id for item in normalized.samples}
    assert len(normalized.assignments) == 8
    assert (
        next(
            row.role
            for row in normalized.assignments
            if row.sample_id == "dev-3" and row.fold_id == plan.folds[0].fold_id
        )
        == "test"
    )
    assert (
        next(
            row.role
            for row in normalized.assignments
            if row.sample_id == "dev-3" and row.fold_id == plan.folds[1].fold_id
        )
        == "train"
    )


def test_trainer_fold_batch_contains_exact_train_and_validation_only() -> None:
    dataset = manifest()
    plan = fold_plan(dataset)
    normalized = build_normalized_learning_dataset(
        manifest=dataset,
        fold_plan=plan,
        verified_batches=verified_batches(dataset, plan),
    )
    batch = build_trainer_fold_batch(
        normalized,
        approval=approval_for(dataset, plan),
        fold_id=plan.folds[0].fold_id,
    )

    assert batch.fold_id == plan.folds[0].fold_id
    assert tuple(item.role for item in batch.samples) == ("train", "validation")
    assert {item.sample_id for item in batch.samples} == {"dev-1", "dev-2"}
    assert all(item.role in {"train", "validation"} for item in batch.samples)


def test_trainer_fold_batch_rejects_wrong_approval_or_unknown_fold() -> None:
    dataset = manifest()
    plan = fold_plan(dataset)
    normalized = build_normalized_learning_dataset(
        manifest=dataset,
        fold_plan=plan,
        verified_batches=verified_batches(dataset, plan),
    )
    approval = replace(approval_for(dataset, plan), fold_plan_hash="d" * 64)

    with pytest.raises(ValueError, match="[Aa]pproval"):
        build_trainer_fold_batch(normalized, approval=approval, fold_id=plan.folds[0].fold_id)
    with pytest.raises(LookupError, match="[Ff]old"):
        build_trainer_fold_batch(normalized, approval=approval_for(dataset, plan), fold_id="f" * 64)


def test_normalized_dataset_hash_is_recomputed_from_samples_and_assignments() -> None:
    dataset = manifest()
    plan = fold_plan(dataset)
    normalized = build_normalized_learning_dataset(
        manifest=dataset,
        fold_plan=plan,
        verified_batches=verified_batches(dataset, plan),
    )

    assert normalized_learning_dataset_hash(normalized) == normalized.content_hash
    tampered = replace(normalized, samples=normalized.samples[:-1])
    assert normalized_learning_dataset_hash(tampered) != normalized.content_hash


def test_normalized_dataset_builder_rejects_duplicated_fold_assignment() -> None:
    dataset = manifest()
    plan = fold_plan(dataset)
    normalized = build_normalized_learning_dataset(
        manifest=dataset,
        fold_plan=plan,
        verified_batches=verified_batches(dataset, plan),
    )
    tampered = replace(
        normalized,
        assignments=normalized.assignments + (normalized.assignments[0],),
        content_hash="0" * 64,
    )

    assert normalized_learning_dataset_hash(tampered) != tampered.content_hash


def test_normalized_dataset_database_contract_checks_approval_manifest_and_fold_rows() -> None:
    dataset = manifest()
    plan = fold_plan(dataset)
    normalized = build_normalized_learning_dataset(
        manifest=dataset,
        fold_plan=plan,
        verified_batches=verified_batches(dataset, plan),
    )
    approval = approval_for(dataset, plan)

    _validate_input(dataset, plan, approval, normalized)
    record = NormalizedLearningDatasetRecord.from_domain(
        normalized, approval_hash=approval.content_hash
    )
    sample_record = NormalizedLearningSampleRecord.from_domain(normalized, normalized.samples[0])
    assert record.content_hash == normalized.content_hash
    assert sample_record.to_domain() == normalized.samples[0]

    missing_test_assignment = replace(
        normalized,
        assignments=normalized.assignments[:-1],
        content_hash="",
    )
    missing_test_assignment = replace(
        missing_test_assignment,
        content_hash=normalized_learning_dataset_hash(missing_test_assignment),
    )
    with pytest.raises(ValueError, match="membership canonical"):
        _validate_input(dataset, plan, approval, missing_test_assignment)
