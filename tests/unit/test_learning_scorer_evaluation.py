from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest

from trading_bot.application.learning_cpu_trainer import (
    LearningCpuArtifact,
    LearningCpuClassModel,
    LearningCpuValidationReport,
)
from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
from trading_bot.domain.learning.evaluation import EvaluationTrade
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)


def _base_artifact() -> LearningCpuArtifact:
    request = LearningResourceRequest(
        training_samples=3,
        validation_samples=9,
        feature_count=2,
        max_steps=30,
        max_runtime_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    return LearningCpuArtifact(
        schema_version="crypidian.learning.cpu-logistic.v1",
        trainer_version="cpu-logistic-v1",
        dependency_fingerprint="f" * 64,
        feature_schema_hash="3" * 64,
        attempt_content_hash="1" * 64,
        split_policy_version="split-v3",
        workspace_id=str(uuid4()),
        attempt_id=str(uuid4()),
        normalized_dataset_id=str(uuid4()),
        manifest_hash="a" * 64,
        fold_plan_hash="b" * 64,
        fold_id="c" * 64,
        approval_hash="d" * 64,
        batch_hash="e" * 64,
        seed=7,
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
        feature_names=("momentum", "volatility"),
        feature_means=(0.0, 0.0),
        feature_scales=(1.0, 1.0),
        models=(
            LearningCpuClassModel(-1, 1.0, (-1.0, 0.0), 1),
            LearningCpuClassModel(0, 0.0, (0.0, 0.0), 1),
            LearningCpuClassModel(1, -1.0, (1.0, 0.0), 1),
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


def _scorer_artifact(base: LearningCpuArtifact) -> LearningScorerArtifact:
    return LearningScorerArtifact(
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
        strategy_release_content_hash="6" * 64,
        vector_schema_version="crypidian-learning-feature-vector-v1",
        vector_schema_hash="3" * 64,
        training_membership_digest="7" * 64,
        validation_membership_digest="8" * 64,
        training_sample_count=3,
        validation_sample_count=9,
        calibrator_method="temperature_scaling",
        calibrator_version="temperature-scaling-bisection-v1",
        calibration_policy_version="temperature-v1",
        calibration_policy_hash="9" * 64,
        temperature=1.0,
        validation_class_support=((-1, 3), (0, 3), (1, 3)),
        validation_nll_before=0.5,
        validation_nll_after=0.3,
        calibration_iterations=16,
        trainer_version="cpu-logistic-v1",
        trainer_code_version="wp06-scorer-artifact-v2",
        dependency_fingerprint="f" * 64,
    )


def _rows() -> tuple[object, ...]:
    base_time = datetime(2026, 11, 1, tzinfo=UTC)
    rows = []
    for index, label in enumerate((-1, 0, 1)):
        rows.append(
            _evaluation_row(
                sample_id=f"test-private-sample-{index}",
                label=label,
                features=(("momentum", float(label)), ("volatility", 0.5)),
                trade_id=f"trade-private-{index}",
                closed_at=base_time + timedelta(minutes=index),
                pnl=Decimal(label),
            )
        )
    return tuple(rows)


def _payload_commitment(rows) -> str:
    from trading_bot.application.learning_scorer_evaluation import (
        scorer_test_payload_commitment_hash,
    )

    return scorer_test_payload_commitment_hash(rows)


def _evaluation_row(*, sample_id, label, features, trade_id, closed_at, pnl):
    module_spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert module_spec is not None, "C.3 phải có hợp đồng evaluator TEST riêng."
    from trading_bot.application.learning_scorer_evaluation import ScorerTestRow

    trade = EvaluationTrade(
        trade_id=trade_id,
        opened_at=closed_at - timedelta(seconds=30),
        closed_at=closed_at,
        gross_pnl=pnl,
        fees=Decimal("0.01"),
        funding=Decimal("0"),
        slippage=Decimal("0.01"),
        exposure=Decimal("1"),
    )
    return ScorerTestRow(
        sample_id=sample_id,
        outcome_id=uuid5(UUID("00000000-0000-0000-0000-000000000001"), trade_id),
        outcome_hash=hashlib.sha256(f"outcome:{trade_id}".encode()).hexdigest(),
        role="test",
        sample_hash=hashlib.sha256(sample_id.encode()).hexdigest(),
        vector_hash=hashlib.sha256(f"vector:{sample_id}".encode()).hexdigest(),
        candidate_id_hash=hashlib.sha256(f"candidate:{sample_id}".encode()).hexdigest(),
        features=features,
        label_value=label,
        trade=trade,
    )


def _scope(base: LearningCpuArtifact, *, complete: bool = False):
    module_spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert module_spec is not None, "C.3 phải có hợp đồng evaluator TEST riêng."
    from trading_bot.application import learning_scorer_evaluation as scorer_evaluation

    assert hasattr(scorer_evaluation, "ScorerTestOpportunityProof"), (
        "C.3 phải dùng proof inventory có hash, không cờ completeness caller."
    )
    ScorerTestEvaluationScope = scorer_evaluation.ScorerTestEvaluationScope
    ScorerTestOpportunityEntry = scorer_evaluation.ScorerTestOpportunityEntry
    ScorerTestOpportunityProof = scorer_evaluation.ScorerTestOpportunityProof

    rows = _rows()
    members = [
        {
            "sample_id": row.sample_id,
            "sample_hash": row.sample_hash,
            "vector_hash": row.vector_hash,
        }
        for row in sorted(rows, key=lambda item: item.sample_id)
    ]
    membership_digest = hashlib.sha256(
        json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    workspace_id = UUID(base.workspace_id)
    evaluation_run_id = uuid4()
    bundle_hash = "9" * 64
    source_scope_hash = "2" * 64
    proof = ScorerTestOpportunityProof(
        record_id=uuid4(),
        workspace_id=workspace_id,
        evaluation_run_id=evaluation_run_id,
        attempt_id=UUID(base.attempt_id),
        normalized_dataset_id=UUID(base.normalized_dataset_id),
        manifest_hash=base.manifest_hash,
        fold_plan_hash=base.fold_plan_hash,
        fold_id=base.fold_id,
        approval_hash=base.approval_hash,
        bundle_hash=bundle_hash,
        vector_schema_version="crypidian-learning-feature-vector-v1",
        vector_schema_hash="3" * 64,
        test_membership_digest=membership_digest,
        source_scope_hash=source_scope_hash,
        expected_opportunity_count=3,
        source_gap_count=0 if complete else 1,
        membership_mismatch_count=0,
        unlabeled_candidate_count=0,
        entries=tuple(
            sorted(
                (
                    ScorerTestOpportunityEntry(
                        stable_id_hash=hashlib.sha256(row.sample_id.encode()).hexdigest(),
                        status="candidate",
                        candidate_id_hash=row.candidate_id_hash,
                        outcome_id_hash=hashlib.sha256(str(row.outcome_id).encode()).hexdigest(),
                        outcome_hash=row.outcome_hash,
                        sample_id_hash=hashlib.sha256(row.sample_id.encode()).hexdigest(),
                        sample_hash=row.sample_hash,
                        vector_hash=row.vector_hash,
                    )
                    for row in rows
                ),
                key=lambda entry: entry.stable_id_hash,
            )
        ),
    )
    return ScorerTestEvaluationScope(
        workspace_id=workspace_id,
        evaluation_run_id=evaluation_run_id,
        attempt_id=UUID(base.attempt_id),
        normalized_dataset_id=UUID(base.normalized_dataset_id),
        manifest_hash=base.manifest_hash,
        fold_plan_hash=base.fold_plan_hash,
        fold_id=base.fold_id,
        approval_hash=base.approval_hash,
        vector_schema_version="crypidian-learning-feature-vector-v1",
        vector_schema_hash="3" * 64,
        test_membership_digest=membership_digest,
        bundle_hash=bundle_hash,
        source_scope_hash=source_scope_hash,
        opportunity_proof=proof,
    )


def _policy(**overrides):
    module_spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert module_spec is not None, "C.3 phải có hợp đồng evaluator TEST riêng."
    from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationPolicy

    values = {
        "policy_version": "wp06-scorer-oos-v1",
        "minimum_sample_count": 3,
        "minimum_class_support": 1,
        "maximum_calibration_error": 1.0,
        "maximum_log_loss_regression": 100.0,
        "maximum_brier_regression": 100.0,
        "minimum_coverage": 0.0,
        "minimum_candidate_retention": 0.0,
        "maximum_false_veto_rate": 1.0,
        "approve_probability_threshold": 0.0,
        "minimum_confidence": 0.0,
        "reliability_bin_count": 3,
    }
    values.update(overrides)
    return ScorerTestEvaluationPolicy(**values)


def test_scorer_test_report_is_aggregate_only_and_incomplete_proof_stays_shadow_only() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    scorer = _scorer_artifact(base)
    report = build_scorer_test_evaluation_report(
        scope=_scope(base, complete=False),
        scorer_artifact=scorer,
        base_artifact=base,
        policy=_policy(),
        rows=_rows(),
    )

    assert report.status == "ACCEPTED_SHADOW_ONLY"
    assert report.candidate_completeness_verified is False
    assert report.scored_candidate_coverage_verified is True
    assert report.test_sample_count == 3
    assert report.class_support == ((-1, 1), (0, 1), (1, 1))
    assert report.unfiltered_portfolio.trade_count == 3
    assert report.filtered_portfolio.trade_count == report.approved_candidate_count
    assert report.opportunity_proof_id is not None
    assert report.report_hash == hashlib.sha256(report.canonical_json.encode()).hexdigest()
    for private_value in (
        "test-private-sample",
        "trade-private",
        "features",
        "label_value",
        "test_rows",
    ):
        assert private_value not in report.canonical_json


def test_scorer_test_paper_gate_status_requires_a_hash_pinned_complete_opportunity_proof() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    report = build_scorer_test_evaluation_report(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
        rows=_rows(),
    )

    assert report.status == "ACCEPTED_PAPER_GATE_ELIGIBLE"
    assert report.candidate_completeness_verified is True
    assert report.scored_candidate_coverage_verified is True
    assert report.opportunity_proof_id is not None
    assert report.proof_hash is not None
    assert len(report.proof_hash) == 64


def test_complete_inventory_without_a_score_for_every_candidate_cannot_open_paper_gate() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from dataclasses import replace

    from trading_bot.application.learning_scorer_evaluation import (
        ScorerTestOpportunityEntry,
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    scope = _scope(base, complete=True)
    extra_candidate = ScorerTestOpportunityEntry(
        stable_id_hash="0" * 64,
        status="candidate",
        candidate_id_hash="1" * 64,
        outcome_id_hash="2" * 64,
        outcome_hash="3" * 64,
        sample_id_hash="4" * 64,
        sample_hash="5" * 64,
        vector_hash="6" * 64,
    )
    entries = tuple(
        sorted(
            (*scope.opportunity_proof.entries, extra_candidate),
            key=lambda entry: entry.stable_id_hash,
        )
    )
    proof = replace(
        scope.opportunity_proof,
        expected_opportunity_count=4,
        entries=entries,
    )
    scope = replace(scope, opportunity_proof=proof)

    report = build_scorer_test_evaluation_report(
        scope=scope,
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
        rows=_rows(),
    )

    assert report.candidate_completeness_verified is True
    assert report.scored_candidate_coverage_verified is False
    assert report.status == "ACCEPTED_SHADOW_ONLY"
    assert report.candidate_retention is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome_id", uuid4()),
        ("outcome_hash", "a" * 64),
        ("sample_id", "tampered-sample-id"),
        ("sample_hash", "b" * 64),
        ("vector_hash", "c" * 64),
    ],
)
def test_scored_candidate_must_match_exact_opportunity_outcome_identity(
    field: str, value: object
) -> None:
    from dataclasses import replace

    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    scope = _scope(base, complete=True)
    rows = list(_rows())
    rows[0] = replace(rows[0], **{field: value})

    with pytest.raises(ValueError, match="exact opportunity proof linkage"):
        build_scorer_test_evaluation_report(
            scope=scope,
            scorer_artifact=_scorer_artifact(base),
            base_artifact=base,
            policy=_policy(),
            rows=tuple(rows),
        )


def test_scored_candidate_filter_aggregates_only_approved_closed_trades() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    report = build_scorer_test_evaluation_report(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(approve_probability_threshold=0.2),
        rows=_rows(),
    )

    assert report.status == "ACCEPTED_PAPER_GATE_ELIGIBLE"
    assert report.scored_candidate_count == 3
    assert report.approved_candidate_count == 1
    assert report.vetoed_candidate_count == 2
    assert report.candidate_retention == pytest.approx(1 / 3)
    assert report.unfiltered_portfolio.net_pnl == Decimal("-0.06")
    assert report.filtered_portfolio.net_pnl == Decimal("0.98")
    assert report.filtered_portfolio.trade_count == 1


def test_opportunity_proof_counts_no_candidate_and_blocks_input_excluded_scope() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from dataclasses import replace

    from trading_bot.application.learning_scorer_evaluation import ScorerTestOpportunityEntry

    base = _base_artifact()
    proof = _scope(base, complete=True).opportunity_proof
    no_candidate = ScorerTestOpportunityEntry(
        stable_id_hash="0" * 64,
        status="no_candidate",
        candidate_id_hash=None,
    )
    full_entries = tuple(
        sorted((*proof.entries, no_candidate), key=lambda entry: entry.stable_id_hash)
    )
    complete_proof = replace(
        proof,
        expected_opportunity_count=4,
        entries=full_entries,
    )
    assert complete_proof.completeness_verified is True
    assert complete_proof.candidate_count == 3
    assert complete_proof.no_candidate_count == 1
    assert complete_proof.input_excluded_count == 0

    unlabeled_entries = tuple(
        replace(
            entry,
            outcome_id_hash=None,
            outcome_hash=None,
            sample_id_hash=None,
            sample_hash=None,
            vector_hash=None,
        )
        if entry.status == "candidate" and entry.stable_id_hash == proof.entries[0].stable_id_hash
        else entry
        for entry in complete_proof.entries
    )
    unlabeled_candidate_proof = replace(
        complete_proof,
        entries=unlabeled_entries,
        unlabeled_candidate_count=1,
    )
    assert unlabeled_candidate_proof.completeness_verified is False

    excluded = replace(no_candidate, status="input_excluded")
    excluded_entries = tuple(
        sorted((*proof.entries, excluded), key=lambda entry: entry.stable_id_hash)
    )
    incomplete_proof = replace(proof, expected_opportunity_count=4, entries=excluded_entries)
    assert incomplete_proof.completeness_verified is False
    assert incomplete_proof.input_excluded_count == 1


@pytest.mark.asyncio
async def test_scorer_test_service_commits_receipt_before_loading_and_persisting_report() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_test_service")
    assert spec is not None, "C.3 cần orchestrator hai pha riêng cho TEST payload."
    from trading_bot.application.learning_scorer_test_service import (
        ScorerTestAccessReceipt,
        ScorerTestEvaluationRequest,
        ScorerTestEvaluationService,
        ScorerTestPayloadConsumption,
        ScorerTestStoredReport,
    )

    base = _base_artifact()
    request = ScorerTestEvaluationRequest(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
        evaluated_at=datetime(2026, 11, 9, tzinfo=UTC),
    )
    events: list[str] = []

    class Access:
        async def open_committed_receipt(
            self, scope, *, scorer_artifact_hash, policy_hash, request_fingerprint
        ):
            assert scope == request.scope
            assert scorer_artifact_hash == request.scorer_artifact.artifact_hash
            assert policy_hash == request.policy.policy_hash
            events.append("receipt_committed")
            return ScorerTestAccessReceipt(
                receipt_id=uuid4(),
                workspace_id=request.scope.workspace_id,
                receipt_hash="9" * 64,
                request_fingerprint=request_fingerprint,
                scorer_artifact_hash=scorer_artifact_hash,
                policy_hash=policy_hash,
                proof_id=scope.opportunity_proof_id,
                proof_hash=scope.opportunity_proof_hash,
                current_rights_receipt_hash="a" * 64,
                committed_at=datetime(2026, 11, 9, tzinfo=UTC),
            )

        async def evaluate_and_persist(self, receipt, *, request_fingerprint, evaluate_payload):
            assert receipt.committed_at is not None
            events.append("payload_committed")
            rows = _rows()
            consumption = ScorerTestPayloadConsumption(
                consumption_id=uuid4(),
                workspace_id=request.scope.workspace_id,
                receipt_id=receipt.receipt_id,
                request_fingerprint=receipt.request_fingerprint,
                payload_count=len(rows),
                membership_digest=request.scope.test_membership_digest,
                payload_commitment_hash=_payload_commitment(rows),
                consumption_hash="c" * 64,
                current_rights_receipt_hash="b" * 64,
                consumed_at=datetime(2026, 11, 9, 0, 1, tzinfo=UTC),
                rows=rows,
            )
            report = evaluate_payload(consumption)
            stored = await reports.append(
                request_fingerprint=request_fingerprint,
                report=report,
            )
            events.append("payload_and_report_committed")
            return stored

    class Reports:
        stored = None

        async def get_by_run(self, *, workspace_id, evaluation_run_id):
            assert workspace_id == request.scope.workspace_id
            assert evaluation_run_id == request.scope.evaluation_run_id
            return None

        async def append(self, *, request_fingerprint, report):
            assert events == ["receipt_committed", "payload_committed"]
            self.stored = ScorerTestStoredReport(
                record_id=uuid4(),
                request_fingerprint=request_fingerprint,
                report=report,
                is_duplicate=False,
                committed_at=datetime(2026, 11, 9, 0, 2, tzinfo=UTC),
            )
            events.append("report_committed")
            return self.stored

    reports = Reports()
    result = await ScorerTestEvaluationService(Access(), reports).evaluate(request)
    assert result.report.status == "ACCEPTED_PAPER_GATE_ELIGIBLE"
    assert result.report.test_access_receipt_id is not None
    assert result.report.test_payload_consumption_id is not None
    assert result.report.test_current_rights_receipt_hash == "b" * 64
    assert hashlib.sha256(result.report.canonical_json.encode()).hexdigest() == (
        result.report.report_hash
    )
    assert events == [
        "receipt_committed",
        "payload_committed",
        "report_committed",
        "payload_and_report_committed",
    ]


@pytest.mark.asyncio
async def test_scorer_test_report_failure_rolls_back_one_shot_payload_for_retry() -> None:
    from trading_bot.application.learning_scorer_test_service import (
        ScorerTestAccessReceipt,
        ScorerTestEvaluationRequest,
        ScorerTestEvaluationService,
        ScorerTestPayloadConsumption,
        ScorerTestStoredReport,
    )

    base = _base_artifact()
    request = ScorerTestEvaluationRequest(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
        evaluated_at=datetime(2026, 11, 9, tzinfo=UTC),
    )
    receipt: ScorerTestAccessReceipt | None = None
    receipt_opens = 0
    payload_attempts = 0
    committed_consumptions = 0

    class Access:
        async def open_committed_receipt(
            self, scope, *, scorer_artifact_hash, policy_hash, request_fingerprint
        ):
            nonlocal receipt, receipt_opens
            receipt_opens += 1
            if receipt is None:
                receipt = ScorerTestAccessReceipt(
                    receipt_id=uuid4(),
                    workspace_id=scope.workspace_id,
                    receipt_hash="9" * 64,
                    request_fingerprint=request_fingerprint,
                    scorer_artifact_hash=scorer_artifact_hash,
                    policy_hash=policy_hash,
                    proof_id=scope.opportunity_proof_id,
                    proof_hash=scope.opportunity_proof_hash,
                    current_rights_receipt_hash="a" * 64,
                    committed_at=datetime(2026, 11, 9, tzinfo=UTC),
                )
            return receipt

        async def evaluate_and_persist(
            self, active_receipt, *, request_fingerprint, evaluate_payload
        ):
            nonlocal payload_attempts, committed_consumptions
            payload_attempts += 1
            rows = _rows()
            consumption = ScorerTestPayloadConsumption(
                consumption_id=uuid4(),
                workspace_id=request.scope.workspace_id,
                receipt_id=active_receipt.receipt_id,
                request_fingerprint=active_receipt.request_fingerprint,
                payload_count=len(rows),
                membership_digest=request.scope.test_membership_digest,
                payload_commitment_hash=_payload_commitment(rows),
                consumption_hash="f" * 64,
                current_rights_receipt_hash="b" * 64,
                consumed_at=datetime(2026, 11, 9, 0, 1, tzinfo=UTC),
                rows=rows,
            )
            report = evaluate_payload(consumption)
            stored = await reports.append(
                request_fingerprint=request_fingerprint,
                report=report,
            )
            # The fake gateway commits its one-shot marker only after the report
            # append succeeds, matching the adapter's single transaction boundary.
            committed_consumptions += 1
            return stored

    class Reports:
        stored = None
        append_attempts = 0

        async def get_by_run(self, **values):
            return self.stored

        async def append(self, *, request_fingerprint, report):
            self.append_attempts += 1
            if self.append_attempts == 1:
                raise RuntimeError("report insert failed")
            self.stored = ScorerTestStoredReport(
                record_id=uuid4(),
                request_fingerprint=request_fingerprint,
                report=report,
                is_duplicate=False,
                committed_at=datetime(2026, 11, 9, 0, 2, tzinfo=UTC),
            )
            return self.stored

    reports = Reports()
    service = ScorerTestEvaluationService(Access(), reports)
    with pytest.raises(RuntimeError, match="report insert failed"):
        await service.evaluate(request)

    assert committed_consumptions == 0
    assert reports.stored is None
    result = await service.evaluate(request)
    assert result.report.test_payload_consumption_id is not None
    assert committed_consumptions == 1
    assert receipt_opens == 2
    assert payload_attempts == 2


@pytest.mark.asyncio
async def test_scorer_test_service_fails_closed_if_receipt_is_not_committed() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_test_service")
    assert spec is not None, "C.3 cần orchestrator hai pha riêng cho TEST payload."
    from trading_bot.application.learning_scorer_test_service import (
        ScorerTestAccessReceipt,
        ScorerTestEvaluationAccessConflict,
        ScorerTestEvaluationRequest,
        ScorerTestEvaluationService,
    )

    base = _base_artifact()
    request = ScorerTestEvaluationRequest(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
    )
    payload_reads = 0

    class Access:
        async def open_committed_receipt(self, scope, **values):
            return ScorerTestAccessReceipt(
                receipt_id=uuid4(),
                workspace_id=request.scope.workspace_id,
                receipt_hash="8" * 64,
                request_fingerprint=values["request_fingerprint"],
                scorer_artifact_hash=values["scorer_artifact_hash"],
                policy_hash=request.policy.policy_hash,
                proof_id=scope.opportunity_proof_id,
                proof_hash=scope.opportunity_proof_hash,
                current_rights_receipt_hash="c" * 64,
                committed_at=None,
            )

        async def evaluate_and_persist(self, receipt, **values):
            nonlocal payload_reads
            payload_reads += 1
            return _rows()

    class Reports:
        async def get_by_run(self, **values):
            return None

    with pytest.raises(ScorerTestEvaluationAccessConflict, match="commit"):
        await ScorerTestEvaluationService(Access(), Reports()).evaluate(request)
    assert payload_reads == 0


@pytest.mark.asyncio
async def test_scorer_test_service_retry_returns_saved_report_without_reopening_payload() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_test_service")
    assert spec is not None, "C.3 cần orchestrator hai pha riêng cho TEST payload."
    from trading_bot.application.learning_scorer_test_service import (
        ScorerTestAccessReceipt,
        ScorerTestEvaluationRequest,
        ScorerTestEvaluationService,
        ScorerTestPayloadConsumption,
        ScorerTestStoredReport,
    )

    base = _base_artifact()
    request = ScorerTestEvaluationRequest(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
        evaluated_at=datetime(2026, 11, 9, tzinfo=UTC),
    )
    receipt_opens = 0
    payload_reads = 0

    class Access:
        async def open_committed_receipt(
            self, scope, *, scorer_artifact_hash, policy_hash, request_fingerprint
        ):
            nonlocal receipt_opens
            receipt_opens += 1
            return ScorerTestAccessReceipt(
                receipt_id=uuid4(),
                workspace_id=request.scope.workspace_id,
                receipt_hash="7" * 64,
                request_fingerprint=request_fingerprint,
                scorer_artifact_hash=scorer_artifact_hash,
                policy_hash=request.policy.policy_hash,
                proof_id=scope.opportunity_proof_id,
                proof_hash=scope.opportunity_proof_hash,
                current_rights_receipt_hash="d" * 64,
                committed_at=datetime(2026, 11, 9, tzinfo=UTC),
            )

        async def evaluate_and_persist(self, receipt, *, request_fingerprint, evaluate_payload):
            nonlocal payload_reads
            payload_reads += 1
            rows = _rows()
            consumption = ScorerTestPayloadConsumption(
                consumption_id=uuid4(),
                workspace_id=request.scope.workspace_id,
                receipt_id=receipt.receipt_id,
                request_fingerprint=receipt.request_fingerprint,
                payload_count=len(rows),
                membership_digest=request.scope.test_membership_digest,
                payload_commitment_hash=_payload_commitment(rows),
                consumption_hash="f" * 64,
                current_rights_receipt_hash="e" * 64,
                consumed_at=datetime(2026, 11, 9, 0, 1, tzinfo=UTC),
                rows=rows,
            )
            report = evaluate_payload(consumption)
            return await reports.append(request_fingerprint=request_fingerprint, report=report)

    class Reports:
        stored = None

        async def get_by_run(self, **values):
            return self.stored

        async def append(self, *, request_fingerprint, report):
            self.stored = ScorerTestStoredReport(
                record_id=uuid4(),
                request_fingerprint=request_fingerprint,
                report=report,
                is_duplicate=False,
                committed_at=datetime(2026, 11, 9, 0, 2, tzinfo=UTC),
            )
            return self.stored

    reports = Reports()
    service = ScorerTestEvaluationService(Access(), reports)
    first = await service.evaluate(request)
    retry = await service.evaluate(request)

    assert first.is_duplicate is False
    assert retry.is_duplicate is True
    assert retry.report == first.report
    assert receipt_opens == 1
    assert payload_reads == 1


@pytest.mark.asyncio
async def test_scorer_test_service_rejects_saved_report_for_changed_policy() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_test_service")
    assert spec is not None, "C.3 cần orchestrator hai pha riêng cho TEST payload."
    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )
    from trading_bot.application.learning_scorer_test_service import (
        ScorerTestEvaluationReportConflict,
        ScorerTestEvaluationRequest,
        ScorerTestEvaluationService,
        ScorerTestStoredReport,
    )

    base = _base_artifact()
    scope = _scope(base, complete=True)
    original_policy = _policy()
    evaluated_at = datetime(2026, 11, 9, tzinfo=UTC)
    report = build_scorer_test_evaluation_report(
        scope=scope,
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=original_policy,
        rows=_rows(),
        evaluated_at=evaluated_at,
    )
    changed_request = ScorerTestEvaluationRequest(
        scope=scope,
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(policy_version="wp06-scorer-oos-v2"),
        evaluated_at=evaluated_at,
    )
    access_opened = False

    class Access:
        async def open_committed_receipt(self, *args, **kwargs):
            nonlocal access_opened
            access_opened = True
            raise AssertionError("Không được mở TEST khi run đã có report scope khác.")

        async def evaluate_and_persist(self, receipt, **kwargs):
            raise AssertionError("Không được đọc TEST khi fingerprint report không khớp.")

    class Reports:
        async def get_by_run(self, **values):
            return ScorerTestStoredReport(
                record_id=uuid4(),
                request_fingerprint="0" * 64,
                report=report,
                is_duplicate=False,
                committed_at=datetime(2026, 11, 9, 0, 2, tzinfo=UTC),
            )

    with pytest.raises(ScorerTestEvaluationReportConflict, match="fingerprint"):
        await ScorerTestEvaluationService(Access(), Reports()).evaluate(changed_request)
    assert access_opened is False


def test_scorer_test_report_is_inconclusive_when_class_support_policy_fails() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    report = build_scorer_test_evaluation_report(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(minimum_class_support=2),
        rows=_rows(),
    )

    assert report.status == "INCONCLUSIVE"
    assert report.candidate_completeness_verified is True
    assert report.proof_hash is not None


def test_scorer_test_report_rejects_non_test_row_and_base_artifact_mismatch() -> None:
    spec = importlib.util.find_spec("trading_bot.application.learning_scorer_evaluation")
    assert spec is not None, "C.3 evaluator TEST chưa được implement."
    from dataclasses import replace

    from trading_bot.application.learning_scorer_evaluation import (
        build_scorer_test_evaluation_report,
    )

    base = _base_artifact()
    scorer = _scorer_artifact(base)
    with pytest.raises(ValueError, match="artifact v1"):
        build_scorer_test_evaluation_report(
            scope=_scope(base),
            scorer_artifact=replace(scorer, base_artifact_hash="0" * 64),
            base_artifact=base,
            policy=_policy(),
            rows=_rows(),
        )

    with pytest.raises(ValueError, match="TEST"):
        build_scorer_test_evaluation_report(
            scope=_scope(base),
            scorer_artifact=scorer,
            base_artifact=base,
            policy=_policy(),
            rows=(replace(_rows()[0], role="validation"), *_rows()[1:]),
        )
