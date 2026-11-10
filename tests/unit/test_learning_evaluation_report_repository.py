from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.learning_evaluation_entities import (
    LearningEvaluationReportRecord,
)
from trading_bot.adapters.database.repositories.learning_evaluation_reports import (
    LearningEvaluationReportConflict,
    LearningEvaluationReportRepository,
)
from trading_bot.application.learning_fold_evaluation import (
    FOLD_ROLES,
    FoldEvaluation,
    FoldRoleMetrics,
    WalkForwardEvaluationReport,
)
from trading_bot.domain.learning.evaluation import build_evaluation_metrics


def _report(*, seed: int = 17) -> WalkForwardEvaluationReport:
    roles = {
        role: FoldRoleMetrics(
            role=role,
            sample_ids=(),
            trade_lineage_hash="1" * 64,
            metrics=build_evaluation_metrics((), observation_count=0, seed=seed),
        )
        for role in FOLD_ROLES
    }
    fold = FoldEvaluation(
        fold_id="fold-1",
        train=roles["train"],
        validation=roles["validation"],
        test=roles["test"],
        exclusions=(),
    )
    report = WalkForwardEvaluationReport(
        manifest_hash="2" * 64,
        fold_plan_hash="3" * 64,
        split_policy_version="split-v1",
        evaluation_cutoff=datetime(2026, 11, 1, tzinfo=UTC),
        seed=seed,
        fold_evaluations=(fold,),
        candidate_completeness_verified=False,
        report_hash="",
        caveat_vi="Kết quả chỉ dùng để chẩn đoán.",
    )
    return replace(report, report_hash=hashlib.sha256(report.canonical_json.encode()).hexdigest())


def _record(
    report: WalkForwardEvaluationReport,
    *,
    workspace_id=None,
    idempotency_key=None,
    canonical_report: str | None = None,
) -> LearningEvaluationReportRecord:
    canonical = canonical_report or report.canonical_json
    return LearningEvaluationReportRecord(
        id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        idempotency_key=idempotency_key or uuid4(),
        report_hash=report.report_hash,
        manifest_hash=report.manifest_hash,
        fold_plan_hash=report.fold_plan_hash,
        split_policy_version=report.split_policy_version,
        evaluator_version="wp06-fold-evaluator-v1",
        evaluation_mode="diagnostic",
        candidate_completeness_verified=False,
        evaluation_cutoff=report.evaluation_cutoff,
        seed=report.seed,
        report=json.loads(canonical),
        canonical_report=canonical,
    )


def test_repository_accepts_same_workspace_key_and_canonical_report() -> None:
    workspace_id = uuid4()
    idempotency_key = uuid4()
    report = _report()
    record = _record(report, workspace_id=workspace_id, idempotency_key=idempotency_key)

    LearningEvaluationReportRepository.assert_same_request(
        record,
        workspace_id=workspace_id,
        idempotency_key=idempotency_key,
        report=report,
    )


@pytest.mark.parametrize(
    ("workspace_id", "idempotency_key", "report", "canonical_report"),
    [
        (uuid4(), None, None, None),
        (None, uuid4(), None, None),
        (None, None, _report(seed=18), None),
        (None, None, None, '{"evaluation_mode":"diagnostic"}'),
    ],
)
def test_repository_rejects_workspace_key_payload_or_canonical_mismatch(
    workspace_id,
    idempotency_key,
    report,
    canonical_report,
) -> None:
    requested_workspace = uuid4()
    requested_key = uuid4()
    stored_report = _report()
    record = _record(
        stored_report,
        workspace_id=workspace_id or requested_workspace,
        idempotency_key=idempotency_key or requested_key,
        canonical_report=canonical_report,
    )

    with pytest.raises(LearningEvaluationReportConflict):
        LearningEvaluationReportRepository.assert_same_request(
            record,
            workspace_id=requested_workspace,
            idempotency_key=requested_key,
            report=report or stored_report,
        )


def test_repository_rejects_report_with_non_diagnostic_or_complete_claim() -> None:
    report = _report()
    complete_claim = replace(report, candidate_completeness_verified=True)
    repository = LearningEvaluationReportRepository(None)

    with pytest.raises(LearningEvaluationReportConflict, match="chẩn đoán"):
        repository.validate_report(complete_claim)

    wrong_mode_record = _record(report)
    wrong_mode_record.evaluation_mode = "promotion"
    with pytest.raises(LearningEvaluationReportConflict, match="chẩn đoán"):
        repository.validate_record(wrong_mode_record)


def test_repository_rejects_non_object_canonical_json_as_integrity_conflict() -> None:
    report = _report()
    record = _record(report, canonical_report="[]")
    record.report = []
    record.report_hash = hashlib.sha256(b"[]").hexdigest()

    with pytest.raises(LearningEvaluationReportConflict, match="JSON/hash/lineage"):
        LearningEvaluationReportRepository.validate_record(record)
