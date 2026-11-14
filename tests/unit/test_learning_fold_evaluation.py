from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.learning_fold_evaluation import (
    FoldOutcomeVerificationBatch,
    FoldOutcomeVerificationRequest,
    PersistedFoldOutcome,
    PersistedFoldTrade,
    build_walk_forward_evaluation_report,
    persisted_outcome_row_hash,
    persisted_trade_row_hash,
    verification_batch_hash,
    walk_forward_evaluation_report_from_canonical_json,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.learning.evaluation import (
    EvaluationTrade,
    TemporalWindow,
    WalkForwardSplit,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

BASE = datetime(2026, 1, 1, tzinfo=UTC)
WORKSPACE_ID = uuid4()


def at(hours: int) -> datetime:
    return BASE + timedelta(hours=hours)


def manifest() -> DatasetManifest:
    samples = tuple(
        DatasetSample(
            sample_id=sample_id,
            decision_at=at(decision),
            feature_available_at=at(decision - 1),
            outcome_start=at(decision),
            outcome_end=at(outcome_end),
            feature_snapshot_hash=("a" if index == 0 else "b" if index == 1 else "c") * 64,
            outcome_id=str(uuid4()),
            outcome_hash=("d" if index == 0 else "e" if index == 1 else "f") * 64,
            label_hash=("1" if index == 0 else "2" if index == 1 else "3") * 64,
            label_value=1,
            label_version="labels-v1",
            split=split,
            source_dependent=False,
            source_rights_hashes=(),
            source_revision_refs=(),
        )
        for index, (sample_id, decision, outcome_end, split) in enumerate(
            (
                ("train-sample", 1, 2, DatasetSplit.TRAIN),
                ("validation-sample", 4, 5, DatasetSplit.VALIDATION),
                ("test-sample", 6, 7, DatasetSplit.TEST),
            )
        )
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
    )


def fold_plan(dataset: DatasetManifest):
    split = WalkForwardSplit(
        train=TemporalWindow(at(0), at(3)),
        validation=TemporalWindow(at(3), at(5)),
        test=TemporalWindow(at(5), at(8)),
    )
    return build_manifest_fold_plan(
        dataset,
        folds=(split,),
        expected_split_policy_version=dataset.split_policy_version,
        evaluation_cutoff=at(8),
    )


def trade_for(sample: DatasetSample, *, trade_id: str | None = None) -> EvaluationTrade:
    return EvaluationTrade(
        trade_id=trade_id or f"trade-{sample.sample_id}",
        opened_at=sample.outcome_start,
        closed_at=sample.outcome_end,
        gross_pnl=Decimal("10"),
        fees=Decimal("1"),
        funding=Decimal("0.5"),
        slippage=Decimal("0.5"),
        exposure=Decimal("100"),
    )


class FixtureVerifier:
    def __init__(self, *, corrupt_payload: bool = False) -> None:
        self.requests: list[FoldOutcomeVerificationRequest] = []
        self.corrupt_payload = corrupt_payload

    async def load_verified_fold_outcomes(
        self, request: FoldOutcomeVerificationRequest
    ) -> FoldOutcomeVerificationBatch:
        self.requests.append(request)
        outcomes: list[PersistedFoldOutcome] = []
        samples = {item.sample_id: item for item in dataset_fixture.samples}
        for expectation in request.expected_outcomes:
            sample = samples[expectation.sample_id]
            trade = trade_for(sample)
            if self.corrupt_payload and expectation.sample_id == "train-sample":
                trade = replace(trade, gross_pnl=Decimal("999"))
            outcome = PersistedFoldOutcome(
                sample_id=expectation.sample_id,
                role=expectation.role,
                outcome_id=expectation.outcome_id,
                outcome_hash=expectation.outcome_hash,
                status="COMPLETED",
                trade_count=1,
                row_hash="0" * 64,
                trades=(),
            )
            row = PersistedFoldTrade(ordinal=0, trade=trade, row_hash="0" * 64)
            row = replace(
                row,
                row_hash=persisted_trade_row_hash(request, outcome, row),
            )
            if self.corrupt_payload and expectation.sample_id == "train-sample":
                row = replace(row, trade=replace(row.trade, gross_pnl=Decimal("1000")))
            outcome = replace(outcome, trades=(row,))
            outcome = replace(
                outcome,
                row_hash=persisted_outcome_row_hash(request, outcome, (row,)),
            )
            outcomes.append(outcome)
        ordered = tuple(sorted(outcomes, key=lambda item: item.sample_id))
        return FoldOutcomeVerificationBatch(
            workspace_id=request.workspace_id,
            manifest_hash=request.manifest_hash,
            split_policy_version=request.split_policy_version,
            fold_plan_hash=request.fold_plan_hash,
            fold_id=request.fold_id,
            outcomes=ordered,
            batch_hash=verification_batch_hash(request, ordered),
        )


dataset_fixture = manifest()


@pytest.mark.asyncio
async def test_evaluator_uses_exact_fold_membership_and_builds_stable_report() -> None:
    dataset = dataset_fixture
    plan = fold_plan(dataset)
    verifier = FixtureVerifier()

    report = await build_walk_forward_evaluation_report(
        dataset,
        plan,
        verifier=verifier,
        seed=17,
    )
    repeated = await build_walk_forward_evaluation_report(
        dataset,
        plan,
        verifier=FixtureVerifier(),
        seed=17,
    )

    assert len(verifier.requests) == 1
    assert {item.sample_id for item in verifier.requests[0].expected_outcomes} == {
        "train-sample",
        "validation-sample",
        "test-sample",
    }
    fold = report.fold_evaluations[0]
    assert fold.train.metrics.net_pnl == Decimal("8")
    assert fold.validation.metrics.trade_count == 1
    assert fold.test.sample_ids == ("test-sample",)
    assert report.candidate_completeness_verified is False
    assert len(report.report_hash) == 64
    restored = walk_forward_evaluation_report_from_canonical_json(
        report.canonical_json,
        expected_report_hash=report.report_hash,
    )
    assert restored == report
    assert report.report_hash == repeated.report_hash


@pytest.mark.asyncio
async def test_evaluator_rejects_multi_trade_sample_without_aggregation_contract() -> None:
    class MultiTradeVerifier(FixtureVerifier):
        async def load_verified_fold_outcomes(
            self, request: FoldOutcomeVerificationRequest
        ) -> FoldOutcomeVerificationBatch:
            batch = await super().load_verified_fold_outcomes(request)
            outcomes = tuple(
                replace(item, trade_count=2) if item.sample_id == "train-sample" else item
                for item in batch.outcomes
            )
            return replace(
                batch,
                outcomes=outcomes,
                batch_hash=verification_batch_hash(request, outcomes),
            )

    with pytest.raises(
        ValueError,
        match="Outcome có nhiều trade trên một sample chưa có quy tắc gộp; từ chối đánh giá",
    ):
        await build_walk_forward_evaluation_report(
            dataset_fixture,
            fold_plan(dataset_fixture),
            verifier=MultiTradeVerifier(),
            seed=17,
        )


@pytest.mark.asyncio
async def test_evaluator_exposes_canonical_diagnostic_payload_without_holdout() -> None:
    holdout = DatasetSample(
        sample_id="sealed-holdout-sample",
        decision_at=at(9),
        feature_available_at=at(8),
        outcome_start=at(9),
        outcome_end=at(10),
        feature_snapshot_hash="9" * 64,
        outcome_id=str(uuid4()),
        outcome_hash="a" * 64,
        label_hash="b" * 64,
        label_value=-1,
        label_version="labels-v1",
        split=DatasetSplit.HOLDOUT,
        source_dependent=False,
        source_rights_hashes=(),
        source_revision_refs=(),
    )
    dataset = replace(
        dataset_fixture,
        data_end=at(12),
        as_of=at(12),
        samples=(*dataset_fixture.samples, holdout),
        holdout_sealed_at=at(11),
    )
    verifier = FixtureVerifier()
    report = await build_walk_forward_evaluation_report(
        dataset,
        fold_plan(dataset),
        verifier=verifier,
        seed=17,
    )

    canonical = report.canonical_json
    payload = json.loads(canonical)
    assert canonical == json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == report.report_hash
    assert payload["evaluation_mode"] == "diagnostic"
    assert payload["candidate_completeness_verified"] is False
    assert "sealed-holdout-sample" not in canonical
    assert all(
        expectation.sample_id != "sealed-holdout-sample"
        for request in verifier.requests
        for expectation in request.expected_outcomes
    )


@pytest.mark.asyncio
async def test_evaluator_rejects_trade_payload_that_does_not_match_persisted_hash() -> None:
    with pytest.raises(ValueError, match="trade row hash"):
        await build_walk_forward_evaluation_report(
            dataset_fixture,
            fold_plan(dataset_fixture),
            verifier=FixtureVerifier(corrupt_payload=True),
            seed=17,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opened_at", "closed_at", "allowed"),
    [
        (at(1), at(2), True),
        (at(1), at(3), False),
        (at(2), at(2), False),
    ],
)
async def test_evaluator_enforces_half_open_trade_open_interval_and_closed_end_boundary(
    opened_at: datetime,
    closed_at: datetime,
    allowed: bool,
) -> None:
    class BoundaryVerifier(FixtureVerifier):
        async def load_verified_fold_outcomes(
            self, request: FoldOutcomeVerificationRequest
        ) -> FoldOutcomeVerificationBatch:
            batch = await super().load_verified_fold_outcomes(request)
            outcome = next(item for item in batch.outcomes if item.sample_id == "train-sample")
            row = outcome.trades[0]
            changed = replace(row.trade, opened_at=opened_at, closed_at=closed_at)
            changed_row = replace(row, trade=changed)
            # Tính lại mã băm để chỉ kiểm tra quy tắc khoảng thời gian.
            changed_row = replace(
                changed_row,
                row_hash=persisted_trade_row_hash(request, outcome, changed_row),
            )
            changed_outcome = replace(outcome, trades=(changed_row,), row_hash="0" * 64)
            changed_outcome = replace(
                changed_outcome,
                row_hash=persisted_outcome_row_hash(request, changed_outcome, (changed_row,)),
            )
            outcomes = tuple(
                changed_outcome if item.sample_id == "train-sample" else item
                for item in batch.outcomes
            )
            outcomes = tuple(sorted(outcomes, key=lambda item: item.sample_id))
            return replace(
                batch,
                outcomes=outcomes,
                batch_hash=verification_batch_hash(request, outcomes),
            )

    operation = build_walk_forward_evaluation_report(
        dataset_fixture,
        fold_plan(dataset_fixture),
        verifier=BoundaryVerifier(),
        seed=17,
    )
    if allowed:
        report = await operation
        assert report.fold_evaluations[0].train.metrics.trade_count == 1
    else:
        with pytest.raises(ValueError, match="outcome interval"):
            await operation


@pytest.mark.asyncio
async def test_evaluator_fails_closed_when_verifier_fails() -> None:
    class BrokenVerifier:
        async def load_verified_fold_outcomes(
            self, request: FoldOutcomeVerificationRequest
        ) -> FoldOutcomeVerificationBatch:
            raise RuntimeError("database unavailable")

    with pytest.raises(ValueError, match="Fold outcome verifier thất bại"):
        await build_walk_forward_evaluation_report(
            dataset_fixture,
            fold_plan(dataset_fixture),
            verifier=BrokenVerifier(),
            seed=17,
        )


@pytest.mark.asyncio
async def test_evaluator_requires_live_rights_gate_for_source_dependent_manifest() -> None:
    reference = DatasetSourceReference(
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-1",
        policy_id="policy-1",
        policy_sequence=3,
        purpose=SourceUsePurpose.TRAINING,
        rights_hash="a" * 64,
    )
    first = replace(
        dataset_fixture.samples[0],
        source_dependent=True,
        source_references=(reference,),
    )
    source_dataset = replace(
        dataset_fixture,
        manifest_version="dataset-manifest-v3",
        samples=(first, *dataset_fixture.samples[1:]),
    )
    verifier = FixtureVerifier()

    with pytest.raises(ValueError, match="source-rights eligibility gate"):
        await build_walk_forward_evaluation_report(
            source_dataset,
            fold_plan(source_dataset),
            verifier=verifier,
            seed=17,
        )

    assert verifier.requests == []


@pytest.mark.asyncio
async def test_evaluator_checks_live_rights_gate_before_loading_outcomes() -> None:
    reference = DatasetSourceReference(
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-1",
        policy_id="policy-1",
        policy_sequence=3,
        purpose=SourceUsePurpose.TRAINING,
        rights_hash="a" * 64,
    )
    first = replace(
        dataset_fixture.samples[0],
        source_dependent=True,
        source_references=(reference,),
    )
    source_dataset = replace(
        dataset_fixture,
        manifest_version="dataset-manifest-v3",
        samples=(first, *dataset_fixture.samples[1:]),
    )

    class DeniedGate:
        async def assert_eligible(self, *, workspace_id, manifest) -> None:
            raise RuntimeError("rights changed")

    verifier = FixtureVerifier()
    with pytest.raises(ValueError, match="eligibility gate failed"):
        await build_walk_forward_evaluation_report(
            source_dataset,
            fold_plan(source_dataset),
            verifier=verifier,
            seed=17,
            source_eligibility_gate=DeniedGate(),
        )

    assert verifier.requests == []
