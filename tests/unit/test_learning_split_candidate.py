"""Unit tests for trusted split candidate construction and holdout isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.candidate_universe_proof import (
    CandidateUniverseProof,
    canonical,
    digest,
)
from trading_bot.application.learning_split_candidate import (
    LearningCandidateOutcome,
    LearningSplitCandidateConfig,
    build_learning_split_candidate,
    build_normalized_candidate_dataset,
)
from trading_bot.domain.learning.evaluation import (
    EvaluationTrade,
    TemporalWindow,
    WalkForwardSplit,
    calculate_entry_features_hash,
)

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _outcomes(workspace_id, trial_id):
    values = []
    members = []
    for ordinal, hour in enumerate((1, 2.25, 3.25, 4.25, 5.25, 6.25, 7.25)):
        decision = NOW + timedelta(hours=hour)
        record_id = uuid4()
        features = (("momentum", float(ordinal)), ("volatility", 0.25))
        schema_hash = "a" * 64
        snapshot_id = f"runtime-snapshot-{ordinal}"
        trade = EvaluationTrade(
            trade_id=f"trade-{ordinal}",
            opened_at=decision,
            closed_at=decision + timedelta(minutes=10),
            gross_pnl=Decimal("2") if ordinal % 2 == 0 else Decimal("-2"),
            fees=Decimal("0.1"),
            funding=Decimal("0"),
            slippage=Decimal("0.1"),
            exposure=Decimal("1"),
            entry_decision_snapshot_id=snapshot_id,
            entry_decision_at=decision,
            entry_feature_schema_hash=schema_hash,
            entry_features=features,
            entry_features_hash=calculate_entry_features_hash(
                snapshot_id,
                decision,
                schema_hash,
                features,
            ),
            entry_symbol="BTCUSDT",
        )
        outcome_hash = digest(f"outcome-{ordinal}")
        values.append(
            LearningCandidateOutcome(
                workspace_id=workspace_id,
                trial_record_id=trial_id,
                record_id=record_id,
                ordinal=ordinal,
                outcome_hash=outcome_hash,
                trade=trade,
            )
        )
        members.append(
            {
                "ordinal": ordinal,
                "outcome_record_id": str(record_id),
                "outcome_hash": outcome_hash,
                "eligible": True,
                "exclusion_reason": None,
            }
        )
    return tuple(values), digest(canonical(members))


def _candidate():
    workspace_id, trial_id, run_id, report_id, scenario_id, proof_id = (uuid4() for _ in range(6))
    outcomes, membership_digest = _outcomes(workspace_id, trial_id)
    proof = CandidateUniverseProof(
        workspace_id=workspace_id,
        run_id=run_id,
        trial_record_id=trial_id,
        report_id=report_id,
        scenario_id=scenario_id,
        manifest_hash="b" * 64,
        trial_hash="c" * 64,
        report_hash="d" * 64,
        scenario_hash="e" * 64,
        outcomes_hash="f" * 64,
        membership_digest=membership_digest,
        training_rights_digest="1" * 64,
        candidate_count=len(outcomes),
        eligible_count=len(outcomes),
        exclusion_count=0,
        exclusion_reason_counts=(),
    )
    split = WalkForwardSplit(
        train=TemporalWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=3)),
        validation=TemporalWindow(NOW + timedelta(hours=3), NOW + timedelta(hours=4)),
        test=TemporalWindow(NOW + timedelta(hours=4), NOW + timedelta(hours=5)),
    )
    config = LearningSplitCandidateConfig(
        holdout_start=NOW + timedelta(hours=6),
        evaluation_cutoff=NOW + timedelta(hours=5, minutes=30),
        folds=(split,),
    )
    candidate = build_learning_split_candidate(
        workspace_id=workspace_id,
        trial_record_id=trial_id,
        proof_record_id=proof_id,
        proof=proof,
        outcomes=outcomes,
        config=config,
        as_of=max(item.trade.closed_at for item in outcomes),
    )
    return candidate, outcomes, proof


def test_candidate_preview_contains_metadata_but_not_sample_or_label_payload() -> None:
    candidate, outcomes, _ = _candidate()

    assert candidate.manifest.manifest_version == "dataset-manifest-v2"
    assert len(candidate.manifest.samples) == len(outcomes)
    assert candidate.preview_json.count('"holdout_count":2') == 1
    assert "sample_id" not in candidate.preview_json
    assert "outcome_id" not in candidate.preview_json
    assert "label_value" not in candidate.preview_json
    assert "features" not in candidate.preview_json
    assert candidate.fold_plan.folds[0].train_sample_ids
    assert candidate.fold_plan.folds[0].validation_sample_ids
    assert candidate.fold_plan.folds[0].test_sample_ids


def test_normalized_candidate_dataset_never_contains_holdout_samples() -> None:
    candidate, outcomes, _ = _candidate()

    normalized = build_normalized_candidate_dataset(candidate=candidate, outcomes=outcomes)
    manifest_holdout_ids = {
        item.sample_id
        for item in candidate.manifest.samples
        if item.decision_at >= candidate.config.holdout_start
    }

    assert normalized.samples
    assert not manifest_holdout_ids.intersection(item.sample_id for item in normalized.samples)
    assert all(item.role in {"train", "validation", "test"} for item in normalized.assignments)


def test_candidate_rejects_membership_digest_that_does_not_match_db_rows() -> None:
    candidate, outcomes, proof = _candidate()
    changed_proof = CandidateUniverseProof(
        workspace_id=proof.workspace_id,
        run_id=proof.run_id,
        trial_record_id=proof.trial_record_id,
        report_id=proof.report_id,
        scenario_id=proof.scenario_id,
        manifest_hash=proof.manifest_hash,
        trial_hash=proof.trial_hash,
        report_hash=proof.report_hash,
        scenario_hash=proof.scenario_hash,
        outcomes_hash=proof.outcomes_hash,
        membership_digest="2" * 64,
        training_rights_digest=proof.training_rights_digest,
        candidate_count=proof.candidate_count,
        eligible_count=proof.eligible_count,
        exclusion_count=proof.exclusion_count,
        exclusion_reason_counts=proof.exclusion_reason_counts,
    )

    with pytest.raises(ValueError, match="Membership digest"):
        build_learning_split_candidate(
            workspace_id=candidate.workspace_id,
            trial_record_id=candidate.trial_record_id,
            proof_record_id=candidate.proof_record_id,
            proof=changed_proof,
            outcomes=outcomes,
            config=candidate.config,
            as_of=candidate.manifest.as_of,
        )
