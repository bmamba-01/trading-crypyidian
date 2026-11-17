"""AI-5 (CRY-236): candidate dataset gom nhiều trial (mỗi trial một bundle/coin)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.unit.test_learning_split_candidate import NOW, _candidate, _outcomes

from trading_bot.application.candidate_universe_proof import CandidateUniverseProof
from trading_bot.application.learning_split_candidate import (
    LearningCandidateTrial,
    LearningSplitCandidateConfig,
    build_learning_split_candidate,
    build_learning_split_cohort_candidate,
)
from trading_bot.domain.learning.evaluation import (
    TemporalWindow,
    WalkForwardSplit,
    calculate_entry_features_hash,
)
from trading_bot.domain.learning.source_use_lineage import SourceUseLineage


def _config() -> LearningSplitCandidateConfig:
    split = WalkForwardSplit(
        train=TemporalWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=3)),
        validation=TemporalWindow(NOW + timedelta(hours=3), NOW + timedelta(hours=4)),
        test=TemporalWindow(NOW + timedelta(hours=4), NOW + timedelta(hours=5)),
    )
    return LearningSplitCandidateConfig(
        holdout_start=NOW + timedelta(hours=6),
        evaluation_cutoff=NOW + timedelta(hours=5, minutes=30),
        folds=(split,),
    )


def _trial(
    workspace_id,
    *,
    symbol: str,
    bundle: str,
    child: str,
    schema: str = "a" * 64,
    provider: str = "BINANCE_USDM_REST",
):
    trial_id = uuid4()
    outcomes, membership = _outcomes(workspace_id, trial_id)
    lineage = (
        SourceUseLineage(
            source_id=provider,
            provider=provider,
            bundle_hash=bundle,
            child_dataset_hash=child,
            policy_fingerprint="b" * 64,
            symbol=symbol,
            interval="15m",
            data_start=datetime(2026, 1, 1, tzinfo=UTC),
            data_end=datetime(2026, 2, 1, tzinfo=UTC),
            bar_identity_hash="c" * 64,
            market_bar_ids=(f"market-bar-record:{uuid4()}",),
        ),
    )
    outcomes = tuple(
        replace(
            item,
            trade=replace(item.trade, entry_symbol=symbol),
            source_replay_bundle_hash=bundle,
            source_use_lineage=lineage,
        )
        for item in outcomes
    )
    if schema != "a" * 64:
        outcomes = tuple(
            replace(
                item,
                trade=replace(
                    item.trade,
                    entry_feature_schema_hash=schema,
                    entry_features_hash=calculate_entry_features_hash(
                        item.trade.entry_decision_snapshot_id,
                        item.trade.entry_decision_at,
                        schema,
                        item.trade.entry_features,
                    ),
                ),
            )
            for item in outcomes
        )
    proof = CandidateUniverseProof(
        workspace_id=workspace_id,
        run_id=uuid4(),
        trial_record_id=trial_id,
        report_id=uuid4(),
        scenario_id=uuid4(),
        manifest_hash="b" * 64,
        trial_hash="c" * 64,
        report_hash="d" * 64,
        scenario_hash="e" * 64,
        outcomes_hash="f" * 64,
        membership_digest=membership,
        training_rights_digest="1" * 64,
        candidate_count=len(outcomes),
        eligible_count=len(outcomes),
        exclusion_count=0,
        exclusion_reason_counts=(),
    )
    return LearningCandidateTrial(
        trial_record_id=trial_id, proof_record_id=uuid4(), proof=proof, outcomes=outcomes
    )


def _as_of(trials) -> datetime:
    return max(item.trade.closed_at for trial in trials for item in trial.outcomes)


def test_single_trial_wrapper_and_cohort_of_one_give_identical_candidate() -> None:
    candidate, outcomes, proof = _candidate()
    cohort = build_learning_split_cohort_candidate(
        workspace_id=candidate.workspace_id,
        trials=(
            LearningCandidateTrial(
                trial_record_id=candidate.trial_record_id,
                proof_record_id=candidate.proof_record_id,
                proof=proof,
                outcomes=outcomes,
            ),
        ),
        config=candidate.config,
        as_of=candidate.manifest.as_of,
    )
    assert cohort == candidate
    assert len(candidate.sources) == 1
    assert candidate.sources[0].bundle_hash == "9" * 64


def test_two_coin_cohort_builds_one_manifest_with_both_bundles() -> None:
    workspace_id = uuid4()
    trials = (
        _trial(workspace_id, symbol="BTCUSDT", bundle="1" * 64, child="2" * 64),
        _trial(workspace_id, symbol="ETHUSDT", bundle="3" * 64, child="4" * 64),
    )
    candidate = build_learning_split_cohort_candidate(
        workspace_id=workspace_id, trials=trials, config=_config(), as_of=_as_of(trials)
    )
    manifest = candidate.manifest
    assert manifest.symbols == ("BTCUSDT", "ETHUSDT")
    assert len(manifest.samples) == 14
    assert {item.source_replay_bundle_hash for item in manifest.samples} == {"1" * 64, "3" * 64}
    assert len({item.source_use_lineage_set_hash for item in manifest.samples}) == 1
    assert {item.bundle_hash for item in manifest.source_use_lineage} == {"1" * 64, "3" * 64}
    assert [str(item.trial_record_id) for item in candidate.sources] == sorted(
        str(item.trial_record_id) for item in trials
    )
    assert candidate.trial_record_id == candidate.sources[0].trial_record_id
    # Thứ tự trial đầu vào không đổi kết quả.
    reversed_candidate = build_learning_split_cohort_candidate(
        workspace_id=workspace_id,
        trials=tuple(reversed(trials)),
        config=_config(),
        as_of=_as_of(trials),
    )
    assert reversed_candidate == candidate
    # Cohort hai coin có candidate_id khác cohort một coin.
    single = build_learning_split_cohort_candidate(
        workspace_id=workspace_id, trials=trials[:1], config=_config(), as_of=_as_of(trials[:1])
    )
    assert single.candidate_id != candidate.candidate_id


def test_cohort_rejects_mixed_feature_schema_and_shared_bundle() -> None:
    workspace_id = uuid4()
    btc = _trial(workspace_id, symbol="BTCUSDT", bundle="1" * 64, child="2" * 64)
    other_schema = _trial(
        workspace_id, symbol="ETHUSDT", bundle="3" * 64, child="4" * 64, schema="e" * 64
    )
    with pytest.raises(ValueError, match="feature schema"):
        build_learning_split_cohort_candidate(
            workspace_id=workspace_id,
            trials=(btc, other_schema),
            config=_config(),
            as_of=_as_of((btc, other_schema)),
        )
    same_bundle = _trial(workspace_id, symbol="ETHUSDT", bundle="1" * 64, child="4" * 64)
    with pytest.raises(ValueError, match="cùng một replay bundle"):
        build_learning_split_cohort_candidate(
            workspace_id=workspace_id,
            trials=(btc, same_bundle),
            config=_config(),
            as_of=_as_of((btc, same_bundle)),
        )
    with pytest.raises(ValueError, match="Trial bị lặp"):
        build_learning_split_cohort_candidate(
            workspace_id=workspace_id, trials=(btc, btc), config=_config(), as_of=_as_of((btc,))
        )


def test_cohort_checks_membership_per_trial_proof() -> None:
    workspace_id = uuid4()
    btc = _trial(workspace_id, symbol="BTCUSDT", bundle="1" * 64, child="2" * 64)
    eth = _trial(workspace_id, symbol="ETHUSDT", bundle="3" * 64, child="4" * 64)
    tampered = replace(eth, proof=replace(eth.proof, membership_digest="0" * 64))
    with pytest.raises(ValueError, match="Membership digest"):
        build_learning_split_cohort_candidate(
            workspace_id=workspace_id,
            trials=(btc, tampered),
            config=_config(),
            as_of=_as_of((btc, eth)),
        )


def test_wrapper_still_validates_identifiers() -> None:
    candidate, outcomes, proof = _candidate()
    with pytest.raises(ValueError, match="UUID"):
        build_learning_split_candidate(
            workspace_id=candidate.workspace_id,
            trial_record_id="x",  # type: ignore[arg-type]
            proof_record_id=candidate.proof_record_id,
            proof=proof,
            outcomes=outcomes,
            config=candidate.config,
            as_of=candidate.manifest.as_of,
        )


def test_cohort_rejects_mixed_providers_at_stage() -> None:
    workspace_id = uuid4()
    btc = _trial(workspace_id, symbol="BTCUSDT", bundle="1" * 64, child="2" * 64)
    qa = _trial(
        workspace_id,
        symbol="ETHUSDT",
        bundle="3" * 64,
        child="4" * 64,
        provider="CRYPIDIAN_FIRST_PARTY_QA",
    )
    with pytest.raises(ValueError, match="nhiều nguồn"):
        build_learning_split_cohort_candidate(
            workspace_id=workspace_id, trials=(btc, qa), config=_config(), as_of=_as_of((btc, qa))
        )
