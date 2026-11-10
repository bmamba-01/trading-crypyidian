from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_evaluation_access import (
    LearningEvaluationAccess,
    LearningEvaluationAccessConflict,
)
from trading_bot.application.learning_evaluation import (
    LearningEvaluationReportService,
    LearningEvaluationRequest,
)
from trading_bot.application.learning_evaluation_access import evaluation_access_fingerprint
from trading_bot.application.learning_fold_evaluation import (
    FoldEvaluation,
    FoldRoleMetrics,
    WalkForwardEvaluationReport,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.learning.evaluation import (
    EvaluationMetrics,
    TemporalWindow,
    WalkForwardSplit,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

BASE = datetime(2026, 1, 1, tzinfo=UTC)
WORKSPACE_ID = uuid4()


def at(hours: int) -> datetime:
    return BASE + timedelta(hours=hours)


def manifest(*, source_dependent: bool = False) -> DatasetManifest:
    samples = tuple(
        DatasetSample(
            sample_id=sample_id,
            decision_at=at(decision),
            feature_available_at=at(decision - 1),
            outcome_start=at(decision),
            outcome_end=at(decision + 1),
            feature_snapshot_hash=hash_char * 64,
            outcome_id=f"{sample_id}-outcome",
            outcome_hash=hash_char * 64,
            label_hash=label_char * 64,
            label_value=1,
            label_version="labels-v1",
            split=split,
            source_dependent=False,
            source_rights_hashes=(),
            source_revision_refs=(),
        )
        for sample_id, decision, split, hash_char, label_char in (
            ("train-sample", 1, DatasetSplit.TRAIN, "1", "a"),
            ("validation-sample", 4, DatasetSplit.VALIDATION, "2", "b"),
            ("test-sample", 6, DatasetSplit.TEST, "3", "c"),
        )
    )
    if source_dependent:
        from dataclasses import replace

        reference = DatasetSourceReference(
            source_id="source-1",
            item_id="item-1",
            revision_id="revision-1",
            policy_id="policy-1",
            policy_sequence=1,
            purpose=SourceUsePurpose.TRAINING,
            rights_hash="d" * 64,
        )
        samples = (
            replace(samples[0], source_dependent=True, source_references=(reference,)),
            *samples[1:],
        )
    return DatasetManifest(
        workspace_id=WORKSPACE_ID,
        query_hash="4" * 64,
        symbols=("BTCUSDT",),
        data_start=at(0),
        data_end=at(8),
        as_of=at(8),
        feature_schema_version="features-v1",
        feature_version="feature-v1",
        label_version="labels-v1",
        sample_schema_version="samples-v1",
        split_policy_version="policy-v1",
        samples=samples,
        exclusions=(),
        holdout_sealed_at=None,
        manifest_version="dataset-manifest-v3" if source_dependent else "dataset-manifest-v1",
    )


def folds() -> tuple[WalkForwardSplit, ...]:
    return (
        WalkForwardSplit(
            train=TemporalWindow(at(0), at(3)),
            validation=TemporalWindow(at(3), at(5)),
            test=TemporalWindow(at(5), at(8)),
        ),
    )


def request(**changes: object) -> LearningEvaluationRequest:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "manifest_hash": manifest().content_hash,
        "folds": folds(),
        "evaluation_cutoff": at(8),
        "seed": 19,
        "idempotency_key": uuid4(),
    }
    values.update(changes)
    return LearningEvaluationRequest(**values)  # type: ignore[arg-type]


def _report_for(request_: LearningEvaluationRequest) -> WalkForwardEvaluationReport:
    metrics = EvaluationMetrics(
        observation_count=1,
        usable_observation_count=1,
        trade_count=1,
        coverage=1.0,
        trade_frequency=1.0,
        gross_pnl=__import__("decimal").Decimal("1"),
        net_pnl=__import__("decimal").Decimal("1"),
        fees=__import__("decimal").Decimal("0"),
        funding=__import__("decimal").Decimal("0"),
        slippage=__import__("decimal").Decimal("0"),
        expectancy=__import__("decimal").Decimal("1"),
        max_drawdown=__import__("decimal").Decimal("0"),
        average_exposure=__import__("decimal").Decimal("1"),
        confidence_interval=None,
        caveat_vi="Giới hạn kiểm thử.",
    )
    role_reports = tuple(
        FoldRoleMetrics(
            role=role,
            sample_ids=(f"{role}-sample",),
            trade_lineage_hash="a" * 64,
            metrics=metrics,
        )
        for role in ("train", "validation", "test")
    )
    report = WalkForwardEvaluationReport(
        manifest_hash=request_.manifest_hash,
        fold_plan_hash="b" * 64,
        split_policy_version="policy-v1",
        evaluation_cutoff=request_.evaluation_cutoff,
        seed=request_.seed,
        fold_evaluations=(FoldEvaluation("c" * 64, *role_reports, exclusions=()),),
        candidate_completeness_verified=False,
        report_hash="0" * 64,
        caveat_vi="Giới hạn kiểm thử.",
    )
    return __import__("dataclasses").replace(
        report, report_hash=hashlib.sha256(report.canonical_json.encode()).hexdigest()
    )


@dataclass
class Harness:
    manifest: DatasetManifest
    access_error: Exception | None = None
    evaluator_error: Exception | None = None
    append_error: Exception | None = None
    existing_report: object | None = None
    existing_receipt: object | None = None
    consumed_run: bool = False

    def __post_init__(self) -> None:
        self.units: list[FakeUnitOfWork] = []
        self.access_calls: list[dict[str, object]] = []
        self.evaluator_calls: list[dict[str, object]] = []
        self.write_calls: list[dict[str, object]] = []
        self.append_calls: list[dict[str, object]] = []


class FakeUnitOfWork:
    def __init__(self, harness: Harness, **kwargs: object) -> None:
        self.harness = harness
        self.kwargs = kwargs
        self.session = object()
        self.committed = False

    async def __aenter__(self) -> FakeUnitOfWork:
        self.harness.units.append(self)
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        self.committed = exc_type is None
        if exc_type is not None and self is self.harness.units[0]:
            self.harness.consumed_run = False


def install_harness(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> None:
    import trading_bot.application.learning_evaluation as module

    monkeypatch.setattr(
        module,
        "UnitOfWork",
        lambda _sessions, **kwargs: FakeUnitOfWork(harness, **kwargs),
    )

    class FakeAccessRepository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def open(self, **kwargs):
            harness.access_calls.append({"session": self.session, **kwargs})
            if harness.access_error:
                raise harness.access_error
            if harness.consumed_run:
                raise LearningEvaluationAccessConflict("receipt đã tiêu thụ")
            harness.consumed_run = True
            plan = build_manifest_fold_plan(
                harness.manifest,
                folds=folds(),
                expected_split_policy_version=harness.manifest.split_policy_version,
                evaluation_cutoff=at(8),
            )
            return LearningEvaluationAccess(
                receipt_id=uuid4(),
                manifest=harness.manifest,
                split_policy_version=harness.manifest.split_policy_version,
                fold_plan_hash=plan.content_hash,
                approval_hash="d" * 64,
            )

        async def get_for_run(self, **_kwargs):
            return harness.existing_receipt

    class FakeVerifier:
        def __init__(self, session: object, **kwargs: object) -> None:
            self.session = session
            self.kwargs = kwargs

    class FakeReportRepository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_by_idempotency_key(self, **_kwargs):
            return harness.existing_report

        async def append(self, **kwargs):
            harness.append_calls.append({"session": self.session, **kwargs})
            assert harness.units[0].committed is True
            if harness.append_error:
                raise harness.append_error
            record = SimpleNamespace(id=uuid4())
            return SimpleNamespace(record=record, is_duplicate=False)

    async def evaluator(*args, **kwargs):
        harness.evaluator_calls.append({"args": args, **kwargs})
        if harness.evaluator_error:
            raise harness.evaluator_error
        return _report_for(request())

    monkeypatch.setattr(module, "LearningEvaluationAccessRepository", FakeAccessRepository)
    monkeypatch.setattr(module, "DatabaseFoldOutcomeVerifier", FakeVerifier)
    monkeypatch.setattr(module, "LearningEvaluationReportRepository", FakeReportRepository)
    monkeypatch.setattr(module, "build_walk_forward_evaluation_report", evaluator)


@pytest.mark.asyncio
async def test_service_commits_receipt_snapshot_before_persisting_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(manifest())
    install_harness(monkeypatch, harness)

    result = await LearningEvaluationReportService(object()).evaluate(request())

    assert result.report.report_hash
    assert result.record_id
    assert result.is_duplicate is False
    assert harness.units[0].kwargs["snapshot_repeatable_read"] is False
    assert harness.units[0].kwargs.get("snapshot_read_only", False) is False
    assert harness.units[0].committed is True
    assert len(harness.access_calls) == len(harness.evaluator_calls) == 1
    assert len(harness.append_calls) == 1
    assert harness.append_calls[0]["session"] is harness.units[1].session


@pytest.mark.asyncio
async def test_service_rolls_back_receipt_when_fold_evaluation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(manifest(), evaluator_error=ValueError("bad outcome"))
    install_harness(monkeypatch, harness)

    with pytest.raises(ValueError, match="bad outcome"):
        await LearningEvaluationReportService(object()).evaluate(request())

    assert harness.consumed_run is False
    assert harness.append_calls == []
    assert harness.units[0].committed is False


@pytest.mark.asyncio
async def test_service_rejects_retry_after_access_commit_if_report_persist_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(manifest(), append_error=RuntimeError("report write failed"))
    install_harness(monkeypatch, harness)
    evaluation_request = request()
    service = LearningEvaluationReportService(object())

    with pytest.raises(RuntimeError, match="report write failed"):
        await service.evaluate(evaluation_request)
    assert harness.consumed_run is True

    with pytest.raises(LearningEvaluationAccessConflict, match="tiêu thụ"):
        await service.evaluate(evaluation_request)
    assert len(harness.access_calls) == 2


@pytest.mark.asyncio
async def test_service_rejects_source_dependent_manifest_before_payload_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_manifest = manifest(source_dependent=True)
    harness = Harness(
        source_manifest,
        access_error=LearningEvaluationAccessConflict("source-dependent fail-closed"),
    )
    install_harness(monkeypatch, harness)

    with pytest.raises(LearningEvaluationAccessConflict, match="source-dependent"):
        await LearningEvaluationReportService(object()).evaluate(
            request(manifest_hash=source_manifest.content_hash)
        )

    assert harness.evaluator_calls == []
    assert harness.append_calls == []


@pytest.mark.asyncio
async def test_service_retry_returns_persisted_report_without_reopening_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_request = request()
    report = _report_for(evaluation_request)
    existing = SimpleNamespace(
        id=uuid4(),
        idempotency_key=evaluation_request.idempotency_key,
        canonical_report=report.canonical_json,
        report_hash=report.report_hash,
        fold_plan_hash=report.fold_plan_hash,
        split_policy_version=report.split_policy_version,
    )
    receipt = SimpleNamespace(
        request_fingerprint=evaluation_access_fingerprint(evaluation_request),
        manifest_hash=evaluation_request.manifest_hash,
        evaluation_run_id=evaluation_request.idempotency_key,
        purpose="diagnostic-evaluation",
        actor_principal="crypidian_learning_evaluator",
        fold_plan_hash=report.fold_plan_hash,
        split_policy_version=report.split_policy_version,
    )
    harness = Harness(manifest(), existing_report=existing, existing_receipt=receipt)
    install_harness(monkeypatch, harness)

    result = await LearningEvaluationReportService(object()).evaluate(evaluation_request)

    assert result.report == report
    assert result.record_id == existing.id
    assert result.is_duplicate is True
    assert harness.access_calls == []
    assert harness.evaluator_calls == []
    assert harness.append_calls == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workspace_id": "not-a-uuid"}, "UUID"),
        ({"manifest_hash": "A" * 64}, "SHA-256"),
        ({"folds": ()}, "folds"),
        ({"seed": True}, "seed"),
        ({"seed": -1}, "seed"),
        ({"idempotency_key": "not-a-uuid"}, "UUID"),
        ({"evaluation_cutoff": at(0).replace(tzinfo=None)}, "timezone"),
    ],
)
def test_request_rejects_invalid_scope_lineage_and_parameters(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        request(**changes)
