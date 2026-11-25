from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from trading_bot.application.learning_scorer_evaluation import (
    ScorerTestOpportunityEntry,
    ScorerTestOpportunityProof,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade


def _database_api():
    spec = importlib.util.find_spec(
        "trading_bot.adapters.database.models.learning_scorer_test_opportunity_entities"
    )
    assert spec is not None, "C.3 proof persistence entity/repository chưa được implement."
    from trading_bot.adapters.database.models.learning_scorer_test_opportunity_entities import (
        LearningScorerTestOpportunityProofRecord,
    )
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        LearningScorerTestOpportunityProofConflict,
        LearningScorerTestOpportunityProofRepository,
    )

    return (
        LearningScorerTestOpportunityProofRecord,
        LearningScorerTestOpportunityProofConflict,
        LearningScorerTestOpportunityProofRepository,
    )


def _proof() -> ScorerTestOpportunityProof:
    event_candidate = hashlib.sha256(b"event:candidate").hexdigest()
    event_no_candidate = hashlib.sha256(b"event:no-candidate").hexdigest()
    entries = (
        ScorerTestOpportunityEntry(
            stable_id_hash=event_candidate,
            status="candidate",
            candidate_id_hash=hashlib.sha256(b"candidate").hexdigest(),
            outcome_id_hash=hashlib.sha256(b"outcome-id").hexdigest(),
            outcome_hash=hashlib.sha256(b"outcome-hash").hexdigest(),
            sample_id_hash=hashlib.sha256(b"sample-id").hexdigest(),
            sample_hash=hashlib.sha256(b"sample-hash").hexdigest(),
            vector_hash=hashlib.sha256(b"vector-hash").hexdigest(),
        ),
        ScorerTestOpportunityEntry(
            stable_id_hash=event_no_candidate,
            status="no_candidate",
            candidate_id_hash=None,
        ),
    )
    return ScorerTestOpportunityProof(
        record_id=uuid4(),
        workspace_id=uuid4(),
        evaluation_run_id=uuid4(),
        attempt_id=uuid4(),
        normalized_dataset_id=uuid4(),
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
        fold_id="3" * 64,
        approval_hash="4" * 64,
        bundle_hash="5" * 64,
        vector_schema_version="crypidian-learning-feature-vector-v1",
        vector_schema_hash="6" * 64,
        test_membership_digest="7" * 64,
        source_scope_hash="8" * 64,
        expected_opportunity_count=2,
        source_gap_count=0,
        membership_mismatch_count=0,
        unlabeled_candidate_count=0,
        entries=tuple(sorted(entries, key=lambda entry: entry.stable_id_hash)),
    )


def test_proof_record_keeps_only_canonical_hashed_inventory_and_exact_scope() -> None:
    proof = _proof()
    record_type, _, repository_type = _database_api()

    record = record_type.from_proof(proof)

    assert record.workspace_id == proof.workspace_id
    assert record.evaluation_run_id == proof.evaluation_run_id
    assert record.attempt_id == proof.attempt_id
    assert record.normalized_dataset_id == proof.normalized_dataset_id
    assert record.manifest_hash == proof.manifest_hash
    assert record.fold_plan_hash == proof.fold_plan_hash
    assert record.fold_id == proof.fold_id
    assert record.approval_hash == proof.approval_hash
    assert record.bundle_hash == proof.bundle_hash
    assert record.vector_schema_hash == proof.vector_schema_hash
    assert record.test_membership_digest == proof.test_membership_digest
    assert record.source_scope_hash == proof.source_scope_hash
    assert record.candidate_count == 1
    assert record.no_candidate_count == 1
    assert record.input_excluded_count == 0
    assert record.unlabeled_candidate_count == 0
    assert record.completeness_verified is True
    assert record.canonical_proof == proof.canonical_json
    assert record.proof_hash == hashlib.sha256(proof.canonical_json.encode()).hexdigest()
    assert "candidate_id_hash" in record.canonical_proof
    assert "test-private" not in record.canonical_proof
    repository_type.validate_record(record)


def test_v2_proof_canonical_hash_remains_unchanged() -> None:
    proof = replace(
        _proof(),
        record_id=UUID(int=1),
        workspace_id=UUID(int=2),
        evaluation_run_id=UUID(int=3),
        attempt_id=UUID(int=4),
        normalized_dataset_id=UUID(int=5),
    )

    payload = json.loads(proof.canonical_json)

    assert proof.content_hash == "6bb91f928de4c21b58627200e141e7fd37bddd5399c35bfd58eacc64c5de3bfe"
    assert payload["schema_version"] == "wp06-scorer-test-opportunity-proof-v2"
    assert "not_run_count" not in payload
    assert all("reason_code" not in entry for entry in payload["entries"])


def test_v2_proof_retains_combined_legacy_input_excluded_count() -> None:
    base = _proof()
    input_excluded = ScorerTestOpportunityEntry(
        stable_id_hash=hashlib.sha256(b"event:legacy-input-excluded").hexdigest(),
        status="input_excluded",
        candidate_id_hash=None,
    )
    proof = replace(
        base,
        expected_opportunity_count=3,
        entries=tuple(
            sorted((*base.entries, input_excluded), key=lambda entry: entry.stable_id_hash)
        ),
    )
    record_type, _, _ = _database_api()

    record = record_type.from_proof(proof)
    payload = json.loads(record.canonical_proof)

    assert payload["schema_version"] == "wp06-scorer-test-opportunity-proof-v2"
    assert proof.input_excluded_count == record.input_excluded_count == 1
    assert record.not_run_count is None
    assert sum(entry["status"] == "input_excluded" for entry in payload["entries"]) == 1
    assert "not_run_count" not in payload


@pytest.mark.asyncio
async def test_repository_preserves_legacy_evaluation_proof_hash_for_new_builder_operation() -> (
    None
):
    from tests.unit.test_replay_training_rights import (
        BUNDLE_HASH,
        WORKSPACE_ID,
        _row,
        _Session,
    )

    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
        ReplayTrainingOperation,
    )
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        LearningScorerTestOpportunityProofRepository,
    )

    legacy_receipts = await DatabaseReplayTrainingRightsGate(
        _Session((_row("a" * 64),))  # type: ignore[arg-type]
    ).authorize(
        workspace_id=WORKSPACE_ID,
        bundle_hash=BUNDLE_HASH,
        operation=ReplayTrainingOperation.EVALUATION,
    )
    builder_receipts = await DatabaseReplayTrainingRightsGate(
        _Session((_row("a" * 64),))  # type: ignore[arg-type]
    ).authorize(
        workspace_id=WORKSPACE_ID,
        bundle_hash=BUNDLE_HASH,
        operation=ReplayTrainingOperation.CANDIDATE_TEST_PROOF,
    )
    identity_scope_hash = hashlib.sha256(b"identity scope").hexdigest()

    legacy_source_scope_hash = LearningScorerTestOpportunityProofRepository._source_scope_hash(
        identity_scope_hash=identity_scope_hash,
        rights_receipts=legacy_receipts,
    )
    builder_source_scope_hash = LearningScorerTestOpportunityProofRepository._source_scope_hash(
        identity_scope_hash=identity_scope_hash,
        rights_receipts=builder_receipts,
    )
    legacy_proof = replace(_proof(), source_scope_hash=legacy_source_scope_hash)
    builder_retry = replace(legacy_proof, source_scope_hash=builder_source_scope_hash)

    assert builder_receipts[0].operation is ReplayTrainingOperation.CANDIDATE_TEST_PROOF
    assert legacy_source_scope_hash == builder_source_scope_hash
    assert builder_retry.canonical_json == legacy_proof.canonical_json
    assert builder_retry.content_hash == legacy_proof.content_hash


def test_proof_record_rejects_denormalized_scope_tampering() -> None:
    record_type, conflict_type, repository_type = _database_api()
    record = record_type.from_proof(_proof())
    record.bundle_hash = "9" * 64

    with pytest.raises(conflict_type, match="canonical"):
        repository_type.validate_record(record)


def test_proof_record_rejects_canonical_bytes_with_unhashed_or_payload_inventory() -> None:
    record_type, conflict_type, repository_type = _database_api()
    record = record_type.from_proof(_proof())
    document = json.loads(record.canonical_proof)
    document["entries"][0]["stable_id_hash"] = "raw-event-id"
    record.canonical_proof = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )

    with pytest.raises(conflict_type, match="hash"):
        repository_type.validate_record(record)


def test_test_inventory_includes_unlabeled_candidates_no_candidates_and_incomplete_rows() -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        _build_test_inventory_entries,
        _TestMembershipRow,
    )

    event_ids = tuple(f"event-{index}" for index in range(6))
    candidate_ids = {
        event_ids[0]: hashlib.sha256(b"candidate-labeled").hexdigest(),
        event_ids[2]: hashlib.sha256(b"candidate-unlabeled").hexdigest(),
    }
    vector_hash = hashlib.sha256(b"vector").hexdigest()
    schema_hash = hashlib.sha256(b"schema").hexdigest()
    snapshot_id = uuid4()
    outcome_id = uuid4()
    decision_at = datetime(2026, 11, 9, tzinfo=UTC)

    def opportunity(event_id: str, *, state: str, candidate_id: str | None = None):
        return SimpleNamespace(
            event_id=event_id,
            input_state="INPUT_EXCLUDED" if state == "excluded" else "EVALUATED",
            strategy_state={
                "candidate": "CANDIDATE",
                "no_candidate": "NO_CANDIDATE",
                "excluded": "NOT_RUN",
                "not_run": "NOT_RUN",
            }[state],
            reason_code={
                "candidate": "CANDIDATE_CREATED",
                "no_candidate": "NO_SIGNAL",
                "excluded": "INPUT_NOT_READY",
                "not_run": "SETTINGS_NOT_EFFECTIVE",
            }[state],
            candidate_id=candidate_id,
            vector_hash=vector_hash,
            runtime_snapshot_id=snapshot_id,
            decision_at=decision_at,
            feature_values_hash=hashlib.sha256(b"features").hexdigest(),
            feature_schema_hash=schema_hash,
            symbol="BTCUSDT",
        )

    labeled_row = opportunity(
        event_ids[0], state="candidate", candidate_id=candidate_ids[event_ids[0]]
    )
    no_candidate_row = opportunity(event_ids[1], state="no_candidate")
    unlabeled_row = opportunity(
        event_ids[2], state="candidate", candidate_id=candidate_ids[event_ids[2]]
    )
    excluded_row = opportunity(event_ids[3], state="excluded")
    not_run_row = opportunity(event_ids[4], state="not_run")
    outcome = SimpleNamespace(
        id=outcome_id,
        outcome_hash=hashlib.sha256(b"outcome").hexdigest(),
        entry_candidate_id=labeled_row.candidate_id,
        entry_vector_hash=vector_hash,
        entry_decision_snapshot_id=snapshot_id,
        entry_decision_at=decision_at,
        entry_features_hash=labeled_row.feature_values_hash,
        entry_feature_schema_hash=schema_hash,
        entry_symbol="BTCUSDT",
    )
    sample = _TestMembershipRow(
        sample_id="sample-labeled",
        sample_hash=hashlib.sha256(b"sample").hexdigest(),
        outcome_id=outcome_id,
        outcome_hash=outcome.outcome_hash,
        feature_snapshot_hash=outcome.entry_features_hash,
        feature_schema_hash=schema_hash,
    )

    entries, missing_events, unlabeled_count, found_candidates = _build_test_inventory_entries(
        expected_events=tuple((event_id, index) for index, event_id in enumerate(event_ids)),
        opportunity_by_event={
            event_ids[0]: labeled_row,
            event_ids[1]: no_candidate_row,
            event_ids[2]: unlabeled_row,
            event_ids[3]: excluded_row,
            event_ids[4]: not_run_row,
        },
        outcome_by_event={event_ids[0]: outcome},
        membership_by_outcome={outcome_id: sample},
    )

    by_stable_id = {entry.stable_id_hash: entry for entry in entries}
    assert missing_events == 1
    assert unlabeled_count == 1
    assert found_candidates == set(candidate_ids.values())
    assert len(entries) == 5
    assert {entry.status for entry in entries} == {
        "candidate",
        "no_candidate",
        "input_excluded",
        "not_run",
    }
    assert by_stable_id[hashlib.sha256(event_ids[1].encode()).hexdigest()].status == (
        "no_candidate"
    )
    assert by_stable_id[hashlib.sha256(event_ids[3].encode()).hexdigest()].status == (
        "input_excluded"
    )
    assert (
        by_stable_id[hashlib.sha256(event_ids[3].encode()).hexdigest()].reason_code
        == "INPUT_NOT_READY"
    )
    assert by_stable_id[hashlib.sha256(event_ids[4].encode()).hexdigest()].status == "not_run"
    assert (
        by_stable_id[hashlib.sha256(event_ids[4].encode()).hexdigest()].reason_code
        == "SETTINGS_NOT_EFFECTIVE"
    )
    not_run_entry = by_stable_id[hashlib.sha256(event_ids[4].encode()).hexdigest()]
    assert not_run_entry.vector_hash == vector_hash
    labeled_entry = by_stable_id[hashlib.sha256(event_ids[0].encode()).hexdigest()]
    assert labeled_entry.outcome_id_hash == hashlib.sha256(str(outcome_id).encode()).hexdigest()
    assert labeled_entry.outcome_hash == outcome.outcome_hash
    assert labeled_entry.sample_id_hash == hashlib.sha256(sample.sample_id.encode()).hexdigest()
    assert labeled_entry.sample_hash == sample.sample_hash
    assert labeled_entry.vector_hash == vector_hash
    unlabeled_entry = by_stable_id[hashlib.sha256(event_ids[2].encode()).hexdigest()]
    assert unlabeled_entry.status == "candidate"
    assert unlabeled_entry.outcome_id_hash is None
    assert unlabeled_entry.outcome_hash is None
    assert unlabeled_entry.sample_id_hash is None
    assert unlabeled_entry.sample_hash is None
    assert unlabeled_entry.vector_hash is None


def test_v3_proof_counts_not_run_separately_and_keeps_reason_codes() -> None:
    labeled, no_candidate = _proof().entries
    entries = tuple(
        sorted(
            (
                labeled,
                no_candidate,
                ScorerTestOpportunityEntry(
                    stable_id_hash=hashlib.sha256(b"event:excluded").hexdigest(),
                    status="input_excluded",
                    candidate_id_hash=None,
                    reason_code="INPUT_NOT_READY",
                ),
                ScorerTestOpportunityEntry(
                    stable_id_hash=hashlib.sha256(b"event:not-run").hexdigest(),
                    status="not_run",
                    candidate_id_hash=None,
                    vector_hash=hashlib.sha256(b"not-run-vector").hexdigest(),
                    reason_code="SETTINGS_NOT_EFFECTIVE",
                ),
            ),
            key=lambda entry: entry.stable_id_hash,
        )
    )

    proof = ScorerTestOpportunityProof(
        record_id=uuid4(),
        workspace_id=uuid4(),
        evaluation_run_id=uuid4(),
        attempt_id=uuid4(),
        normalized_dataset_id=uuid4(),
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
        fold_id="3" * 64,
        approval_hash="4" * 64,
        bundle_hash="5" * 64,
        vector_schema_version="crypidian-learning-feature-vector-v1",
        vector_schema_hash="6" * 64,
        test_membership_digest="7" * 64,
        source_scope_hash="8" * 64,
        expected_opportunity_count=4,
        source_gap_count=0,
        membership_mismatch_count=0,
        unlabeled_candidate_count=0,
        proof_schema_version="v3",
        entries=entries,
    )

    payload = json.loads(proof.canonical_json)

    assert proof.input_excluded_count == 1
    assert proof.not_run_count == 1
    assert proof.completeness_verified is False
    assert payload["schema_version"] == "wp06-scorer-test-opportunity-proof-v3"
    assert payload["not_run_count"] == 1
    assert {
        entry["reason_code"]
        for entry in payload["entries"]
        if entry["status"] in {"input_excluded", "not_run"}
    } == {"INPUT_NOT_READY", "SETTINGS_NOT_EFFECTIVE"}
    not_run_entry = next(entry for entry in payload["entries"] if entry["status"] == "not_run")
    assert not_run_entry["vector_hash"] == hashlib.sha256(b"not-run-vector").hexdigest()


def test_v3_proof_requires_reason_for_not_run_and_input_excluded() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        ScorerTestOpportunityEntry(
            stable_id_hash=hashlib.sha256(b"event:not-run").hexdigest(),
            status="not_run",
            candidate_id_hash=None,
        )


def test_v3_not_run_requires_vector_lineage_without_outcome_linkage() -> None:
    stable_id_hash = hashlib.sha256(b"event:not-run-without-vector").hexdigest()
    with pytest.raises(ValueError, match="vector lineage"):
        ScorerTestOpportunityEntry(
            stable_id_hash=stable_id_hash,
            status="not_run",
            candidate_id_hash=None,
            reason_code="STRATEGY_NOT_RUN",
        )


def test_test_proof_rejects_consistent_sample_and_opportunity_schema_mismatch() -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        LearningScorerTestOpportunityProofRepository,
        _TestMembershipRow,
    )

    outcome_id = uuid4()
    candidate_event = "test-event-candidate"
    no_candidate_event = "test-event-no-candidate"
    source_schema_hash = "a" * 64
    scorer_schema_hash = "b" * 64
    sample = _TestMembershipRow(
        sample_id="sample-1",
        sample_hash="c" * 64,
        outcome_id=outcome_id,
        outcome_hash="d" * 64,
        feature_snapshot_hash="e" * 64,
        feature_schema_hash=source_schema_hash,
    )
    outcome = SimpleNamespace(
        id=outcome_id,
        outcome_hash=sample.outcome_hash,
        entry_features_hash=sample.feature_snapshot_hash,
        entry_feature_schema_hash=source_schema_hash,
        entry_opportunity_event_id=candidate_event,
        entry_candidate_id="f" * 64,
        entry_vector_hash="1" * 64,
    )
    candidate = SimpleNamespace(
        manifest=SimpleNamespace(
            samples=(
                SimpleNamespace(
                    sample_id=sample.sample_id,
                    outcome_id=str(outcome_id),
                    outcome_hash=sample.outcome_hash,
                    feature_snapshot_hash=sample.feature_snapshot_hash,
                ),
            )
        )
    )
    LearningScorerTestOpportunityProofRepository._validate_test_samples(
        candidate, (sample,), (outcome,)
    )
    opportunity_by_event = {
        candidate_event: SimpleNamespace(
            strategy_state="CANDIDATE", feature_schema_hash=source_schema_hash
        ),
        no_candidate_event: SimpleNamespace(
            strategy_state="NO_CANDIDATE", feature_schema_hash=source_schema_hash
        ),
    }

    with pytest.raises(ValueError, match="schema"):
        LearningScorerTestOpportunityProofRepository._validate_test_vector_schemas(
            scorer_vector_schema_hash=scorer_schema_hash,
            samples=(sample,),
            expected_events=((candidate_event, 1), (no_candidate_event, 2)),
            opportunity_by_event=opportunity_by_event,
        )


@pytest.mark.parametrize("strategy_state", ["CANDIDATE", "NO_CANDIDATE"])
def test_test_proof_rejects_opportunity_schema_mismatch_for_each_decision_state(
    strategy_state: str,
) -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        LearningScorerTestOpportunityProofRepository,
        _TestMembershipRow,
    )

    scorer_schema_hash = "a" * 64
    sample = _TestMembershipRow(
        sample_id="sample-matching-scorer-schema",
        sample_hash="b" * 64,
        outcome_id=uuid4(),
        outcome_hash="c" * 64,
        feature_snapshot_hash="d" * 64,
        feature_schema_hash=scorer_schema_hash,
    )
    matching_event = "test-event-schema-mismatch"
    other_event = "test-event-schema-match"
    mismatch = SimpleNamespace(
        strategy_state=strategy_state,
        feature_schema_hash="e" * 64,
    )
    matching = SimpleNamespace(
        strategy_state=strategy_state,
        feature_schema_hash=scorer_schema_hash,
    )

    with pytest.raises(ValueError, match="Decision opportunity TEST"):
        LearningScorerTestOpportunityProofRepository._validate_test_vector_schemas(
            scorer_vector_schema_hash=scorer_schema_hash,
            samples=(sample,),
            expected_events=((matching_event, 1), (other_event, 2)),
            opportunity_by_event={matching_event: mismatch, other_event: matching},
        )


def test_test_proof_rejects_evaluated_not_run_vector_schema_mismatch() -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        LearningScorerTestOpportunityProofRepository,
        _TestMembershipRow,
    )

    scorer_schema_hash = "a" * 64
    sample = _TestMembershipRow(
        sample_id="sample-matching-scorer-schema",
        sample_hash="b" * 64,
        outcome_id=uuid4(),
        outcome_hash="c" * 64,
        feature_snapshot_hash="d" * 64,
        feature_schema_hash=scorer_schema_hash,
    )
    not_run_event = "test-event-evaluated-not-run-schema-mismatch"
    not_run = SimpleNamespace(
        input_state="EVALUATED",
        strategy_state="NOT_RUN",
        feature_schema_hash="e" * 64,
    )

    with pytest.raises(ValueError, match="Decision opportunity TEST"):
        LearningScorerTestOpportunityProofRepository._validate_test_vector_schemas(
            scorer_vector_schema_hash=scorer_schema_hash,
            samples=(sample,),
            expected_events=((not_run_event, 1),),
            opportunity_by_event={not_run_event: not_run},
        )


@pytest.mark.asyncio
async def test_source_gap_count_sums_missing_manifest_bars(monkeypatch) -> None:
    import trading_bot.adapters.database.repositories.backtest_dataset_manifests as manifests
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        LearningScorerTestOpportunityProofRepository,
    )

    workspace_id = uuid4()
    children = (
        SimpleNamespace(interval="1m", dataset_hash="1" * 64),
        SimpleNamespace(interval="5m", dataset_hash="2" * 64),
    )
    manifest_by_hash = {
        "1" * 64: SimpleNamespace(
            schema_version=4,
            rights_workspace_id=workspace_id,
            dataset_hash="1" * 64,
            expected_bar_count=8,
            is_ready=False,
        ),
        "2" * 64: SimpleNamespace(
            schema_version=4,
            rights_workspace_id=workspace_id,
            dataset_hash="2" * 64,
            expected_bar_count=10,
            is_ready=False,
        ),
    }

    class ManifestRepository:
        def __init__(self, _session):
            pass

        async def get_by_hash(self, *, workspace_id, dataset_hash):
            assert workspace_id == workspace_id_expected
            return manifest_by_hash.get(dataset_hash)

    workspace_id_expected = workspace_id
    monkeypatch.setattr(manifests, "BacktestDatasetManifestRepository", ManifestRepository)
    repository = LearningScorerTestOpportunityProofRepository(SimpleNamespace())

    gaps = await repository._source_gap_count(
        workspace_id=workspace_id,
        reference=SimpleNamespace(components=children),
        inventory={"1m": (object(), object(), object()), "5m": (object(),) * 6},
    )

    assert gaps == 9


def test_test_membership_digest_matches_scorer_payload_digest_exactly() -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
        _membership_digest,
        _TestMembershipRow,
    )
    from trading_bot.application.learning_scorer_evaluation import (
        ScorerTestRow,
        scorer_test_payload_membership_digest,
    )

    samples = tuple(
        _TestMembershipRow(
            sample_id=sample_id,
            sample_hash=hashlib.sha256(f"sample:{sample_id}".encode()).hexdigest(),
            outcome_id=outcome_id,
            outcome_hash=hashlib.sha256(f"outcome:{sample_id}".encode()).hexdigest(),
            feature_snapshot_hash=hashlib.sha256(f"features:{sample_id}".encode()).hexdigest(),
            feature_schema_hash=hashlib.sha256(b"schema").hexdigest(),
        )
        for sample_id, outcome_id in (("sample-z", uuid4()), ("sample-a", uuid4()))
    )
    vectors = {
        sample.outcome_id: hashlib.sha256(f"vector:{sample.sample_id}".encode()).hexdigest()
        for sample in samples
    }
    outcomes = tuple(
        SimpleNamespace(id=outcome_id, entry_vector_hash=vector_hash)
        for outcome_id, vector_hash in vectors.items()
    )
    instant = datetime(2026, 11, 9, tzinfo=UTC)
    rows = tuple(
        ScorerTestRow(
            sample_id=sample.sample_id,
            outcome_id=sample.outcome_id,
            outcome_hash=sample.outcome_hash,
            role="test",
            sample_hash=sample.sample_hash,
            vector_hash=vectors[sample.outcome_id],
            candidate_id_hash=hashlib.sha256(sample.sample_id.encode()).hexdigest(),
            features=(("feature", 0.25),),
            label_value=0,
            trade=EvaluationTrade(
                trade_id=sample.sample_id,
                opened_at=instant,
                closed_at=instant,
                gross_pnl=Decimal("0"),
                fees=Decimal("0"),
                funding=Decimal("0"),
                slippage=Decimal("0"),
                exposure=Decimal("1"),
            ),
        )
        for sample in reversed(samples)
    )

    assert _membership_digest(samples, outcomes) == scorer_test_payload_membership_digest(rows)


def test_test_proof_builder_metadata_migration_adds_only_tenant_scoped_read_access():
    from pathlib import Path

    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/e9a1c3e5f7b9_wp06_scorer_test_builder_metadata.py"
    )
    spec = importlib.util.spec_from_file_location("scorer_test_builder_metadata", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=lambda value: statements.append(str(value)))
    module.upgrade()
    assert module.down_revision == "e8f0a2c4d6b8"
    assert len(statements) == 4
    for table in ("learning_training_attempt", "learning_scorer_artifact"):
        assert f"GRANT SELECT ON TABLE public.{table} TO crypidian_candidate_builder" in statements
        assert any(
            f"ON public.{table} FOR SELECT TO crypidian_candidate_builder "
            "USING (workspace_id = NULLIF" in statement
            for statement in statements
        )
    assert all(
        "GRANT SELECT" in statement or "CREATE POLICY" in statement for statement in statements
    )
    statements.clear()
    module.downgrade()
    assert len(statements) == 4
    for table in ("learning_training_attempt", "learning_scorer_artifact"):
        assert (
            f"REVOKE SELECT ON TABLE public.{table} FROM crypidian_candidate_builder" in statements
        )
        assert f"DROP POLICY wp06_scorer_test_builder_metadata_read ON public.{table}" in statements


def test_trainer_lineage_migration_joins_approved_source_trial_across_distinct_manifests():
    from pathlib import Path

    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/f9c1e3a5b7d9_wp06_trainer_source_trial_lineage.py"
    )
    spec = importlib.util.spec_from_file_location("trainer_source_trial_lineage", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=lambda value: statements.append(str(value)))
    module.upgrade()
    assert module.down_revision == "e9a1c3e5f7b9"
    rendered = "\n".join(statements)
    assert "FOR SELECT TO crypidian_learning_batch_reader USING (workspace_id = NULLIF" in rendered
    assert (
        "GRANT SELECT (workspace_id, manifest_hash, fold_plan_hash, trial_record_id) "
        "ON TABLE public.learning_split_candidate TO crypidian_learning_batch_reader"
    ) in statements
    assert "source_candidate.workspace_id = sample.workspace_id" in module.SOURCE_TRIAL_LINK
    assert "source_candidate.manifest_hash = sample.manifest_hash" in module.SOURCE_TRIAL_LINK
    assert "source_candidate.fold_plan_hash = sample.fold_plan_hash" in module.SOURCE_TRIAL_LINK
    assert "source_candidate.trial_record_id = outcome.trial_record_id" in module.SOURCE_TRIAL_LINK
    assert "outcome.manifest_hash = sample.manifest_hash" not in module.SOURCE_TRIAL_LINK
    assert "pg_get_functiondef" in rendered
    assert "WP06_TRAINER_LINEAGE_DEFINITION_UNEXPECTED" in rendered
    assert "length(v_definition)" in rendered
    statements.clear()
    module.downgrade()
    assert any("REVOKE SELECT" in value for value in statements)
    assert any("DROP POLICY" in value for value in statements)


def test_trainer_lineage_gateway_casts_varchar_metadata_to_declared_text_result():
    from pathlib import Path

    path = Path(__file__).parents[2] / (
        "migrations/versions/f9c1e3a5b7d9_wp06_trainer_source_trial_lineage.py"
    )
    spec = importlib.util.spec_from_file_location("trainer_lineage_result_types", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=lambda value: statements.append(str(value)))
    module.upgrade()
    rendered = "\n".join(statements)
    for column in (
        "strategy_release_id",
        "strategy_content_hash",
        "feature_schema_hash",
        "vector_hash",
    ):
        assert f"opportunity.{column}::text" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", [None, "proof_record_id", "proof_hash"])
async def test_test_builder_revalidates_approved_closed_trade_proof_separately_from_opportunities(
    monkeypatch,
    changed_field,
):
    from trading_bot.adapters.database.repositories import (
        learning_scorer_test_opportunity_proofs as module,
    )

    workspace_id, trial_id, approved_id = uuid4(), uuid4(), uuid4()
    candidate = SimpleNamespace(proof_record_id=approved_id, proof_hash="a" * 64)
    if changed_field is not None:
        setattr(
            candidate, changed_field, uuid4() if changed_field == "proof_record_id" else "b" * 64
        )
    calls = []

    class ProofRepository:
        def __init__(self, session):
            pass

        async def build_and_append(self, **scope):
            assert scope == {"workspace_id": workspace_id, "trial_record_id": trial_id}
            calls.append("approved-closed-trades")
            return SimpleNamespace(
                record_id=approved_id, proof=SimpleNamespace(content_hash="a" * 64)
            )

        async def build_decision_opportunity(self, **scope):
            assert scope == {"workspace_id": workspace_id, "trial_record_id": trial_id}
            calls.append("source-opportunities")
            return SimpleNamespace(record_id=uuid4(), proof=SimpleNamespace(content_hash="c" * 64))

    monkeypatch.setattr(module, "CandidateUniverseProofRepository", ProofRepository)
    repository = module.LearningScorerTestOpportunityProofRepository(SimpleNamespace())
    if changed_field is None:
        await repository._verify_approved_base_proofs(
            workspace_id=workspace_id,
            trial_record_id=trial_id,
            candidate=candidate,
        )
        assert calls == ["approved-closed-trades", "source-opportunities"]
    else:
        with pytest.raises(ValueError, match="split candidate"):
            await repository._verify_approved_base_proofs(
                workspace_id=workspace_id,
                trial_record_id=trial_id,
                candidate=candidate,
            )
        assert calls == ["approved-closed-trades"]


def test_test_proof_hash_migration_preserves_64_hexadecimal_character_regex():
    import re
    from pathlib import Path

    path = Path(__file__).parents[2] / (
        "migrations/versions/a4c6e8f0b2d4_wp06_scorer_test_entry_hashes.py"
    )
    spec = importlib.util.spec_from_file_location("scorer_test_entry_hashes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert re.fullmatch(module.HASH_PATTERN, "a" * 64)
    assert re.fullmatch(module.LEGACY_PATTERN, "a" * 64) is None
    assert re.fullmatch(module.HASH_PATTERN, "a" * 63) is None
    assert re.fullmatch(module.HASH_PATTERN, "g" * 64) is None
    statements = []
    module.op = SimpleNamespace(execute=lambda value: statements.append(str(value)))
    module.upgrade()
    assert module.down_revision == "f9c1e3a5b7d9"
    assert "^[0-9a-f]{64}$" in statements[0]
    assert "<> 7" in statements[0]
    assert "pg_get_functiondef" in statements[0]


def test_scorer_test_gateway_return_types_cast_only_declared_text_metadata():
    from pathlib import Path

    path = Path(__file__).parents[2] / (
        "migrations/versions/b3d5f7a9c1e3_wp06_scorer_test_gateway_return_types.py"
    )
    spec = importlib.util.spec_from_file_location("scorer_test_gateway_return_types", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=lambda value: statements.append(str(value)))
    module.upgrade()
    assert module.down_revision == "a4c6e8f0b2d4"
    rendered = "\n".join(statements)
    assert "WP06_SCORER_TEST_RETURN_DEFINITION_UNEXPECTED" in rendered
    for prefix, columns in (
        (
            "receipt",
            (
                "receipt_hash",
                "request_fingerprint",
                "scorer_artifact_hash",
                "policy_hash",
                "proof_hash",
                "current_rights_receipt_hash",
            ),
        ),
        (
            "consumption",
            (
                "request_fingerprint",
                "membership_digest",
                "payload_commitment_hash",
                "consumption_hash",
                "current_rights_receipt_hash",
            ),
        ),
        (
            "sample",
            (
                "outcome_hash",
                "sample_hash",
                "feature_snapshot_hash",
                "feature_schema_hash",
                "label_hash",
            ),
        ),
        ("outcome", ("outcome_hash", "trade_id", "entry_vector_hash", "entry_candidate_id")),
    ):
        for column in columns:
            assert f"{prefix}.{column}::text" in rendered
    assert "consumption.payload_count::text" not in rendered
    assert "sample.features::text" not in rendered
    assert "sample.label_value::text" not in rendered
    assert all(
        "GRANT" not in statement and "ALTER FUNCTION" not in statement for statement in statements
    )


def test_scorer_gateway_type_patches_match_exact_projections_in_existing_e8_definition():
    from pathlib import Path

    root = Path(__file__).parents[2] / "migrations/versions"

    def load(name):
        spec = importlib.util.spec_from_file_location(name, root / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    class Operations:
        def __init__(self):
            self.statements = []

        def execute(self, value):
            self.statements.append(str(value))

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    legacy = load("e8f0a2c4d6b8_wp06_scorer_test_receipts_reports.py")
    operations = Operations()
    legacy.op = operations
    legacy.upgrade()
    migration = load("b3d5f7a9c1e3_wp06_scorer_test_gateway_return_types.py")
    definitions = {}
    for signature, previous, replacement, count in migration.PATCHES:
        name = signature.split("(")[0]
        definition = definitions.get(name)
        if definition is None:
            definition = next(
                value for value in operations.statements if f"CREATE FUNCTION {name}(" in value
            )
        assert definition.count(previous) == count, previous
        definitions[name] = definition.replace(previous, replacement)
    assert (
        "'sample_hash', sample.sample_hash,"
        in definitions[migration.CONSUME_SIGNATURE.split("(")[0]]
    )
    for signature, previous, replacement, count in reversed(migration.PATCHES):
        name = signature.split("(")[0]
        assert definitions[name].count(replacement) == count
        definitions[name] = definitions[name].replace(replacement, previous)


def test_c3_migrations_pin_full_function_definition_hashes_for_both_directions():
    from pathlib import Path

    migration_root = Path(__file__).parents[2] / "migrations/versions"
    migrations = (
        ("trainer_source_trial_lineage", "f9c1e3a5b7d9_wp06_trainer_source_trial_lineage.py"),
        ("scorer_test_entry_hashes", "a4c6e8f0b2d4_wp06_scorer_test_entry_hashes.py"),
        (
            "scorer_test_gateway_return_types",
            "b3d5f7a9c1e3_wp06_scorer_test_gateway_return_types.py",
        ),
    )
    for name, filename in migrations:
        spec = importlib.util.spec_from_file_location(name, migration_root / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        digest_values = tuple(
            digest
            for function_hashes in module.FUNCTION_DEFINITION_HASHES.values()
            for digest in function_hashes.values()
        )
        assert digest_values
        assert all(
            len(value) == 64 and set(value) <= set("0123456789abcdef") for value in digest_values
        )
        statements = []
        module.op = SimpleNamespace(
            execute=lambda value, statements=statements: statements.append(str(value))
        )
        module.upgrade()
        assert "pg_catalog.sha256" in "\n".join(statements)
        assert all(value in "\n".join(statements) for value in digest_values)
        statements.clear()
        module.downgrade()
        assert "pg_catalog.sha256" in "\n".join(statements)
        assert all(value in "\n".join(statements) for value in digest_values)


def test_v3_not_run_blocks_paper_gate_eligibility_even_when_metrics_pass() -> None:
    """Proof v3 có NOT_RUN phải giữ completeness=False và chặn PAPER_GATE dù metric đạt."""
    from datetime import timedelta
    from uuid import uuid5

    from trading_bot.application.learning_cpu_trainer import (
        LearningCpuArtifact,
        LearningCpuClassModel,
        LearningCpuValidationReport,
    )
    from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
    from trading_bot.application.learning_scorer_evaluation import (
        ScorerTestEvaluationPolicy,
        ScorerTestEvaluationScope,
        ScorerTestRow,
        build_scorer_test_evaluation_report,
    )
    from trading_bot.domain.learning.resources import (
        LearningResourceRequest,
        assess_learning_resource_request,
    )
    from trading_bot.domain.learning.scoring_deployment import (
        ScorerDeploymentMode,
        ScorerDeploymentTarget,
    )

    request = LearningResourceRequest(
        training_samples=3,
        validation_samples=9,
        feature_count=2,
        max_steps=30,
        max_runtime_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    base = LearningCpuArtifact(
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
    base_time = datetime(2026, 11, 1, tzinfo=UTC)
    rows = tuple(
        ScorerTestRow(
            sample_id=f"test-v3-sample-{index}",
            outcome_id=uuid5(UUID("00000000-0000-0000-0000-000000000001"), f"trade-v3-{index}"),
            outcome_hash=hashlib.sha256(f"outcome:trade-v3-{index}".encode()).hexdigest(),
            role="test",
            sample_hash=hashlib.sha256(f"test-v3-sample-{index}".encode()).hexdigest(),
            vector_hash=hashlib.sha256(f"vector:test-v3-sample-{index}".encode()).hexdigest(),
            candidate_id_hash=hashlib.sha256(
                f"candidate:test-v3-sample-{index}".encode()
            ).hexdigest(),
            features=(("momentum", float(label)), ("volatility", 0.5)),
            label_value=label,
            trade=EvaluationTrade(
                trade_id=f"trade-v3-{index}",
                opened_at=base_time + timedelta(minutes=index) - timedelta(seconds=30),
                closed_at=base_time + timedelta(minutes=index),
                gross_pnl=Decimal(label),
                fees=Decimal("0.01"),
                funding=Decimal("0"),
                slippage=Decimal("0.01"),
                exposure=Decimal("1"),
            ),
        )
        for index, label in enumerate((-1, 0, 1))
    )
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
    evaluation_run_id = uuid4()
    bundle_hash = "9" * 64
    source_scope_hash = "2" * 64
    candidate_entries = tuple(
        ScorerTestOpportunityEntry(
            stable_id_hash=hashlib.sha256(f"event:{row.sample_id}".encode()).hexdigest(),
            status="candidate",
            candidate_id_hash=row.candidate_id_hash,
            outcome_id_hash=hashlib.sha256(str(row.outcome_id).encode()).hexdigest(),
            outcome_hash=row.outcome_hash,
            sample_id_hash=hashlib.sha256(row.sample_id.encode()).hexdigest(),
            sample_hash=row.sample_hash,
            vector_hash=row.vector_hash,
        )
        for row in rows
    )
    not_run_entry = ScorerTestOpportunityEntry(
        stable_id_hash=hashlib.sha256(b"event:not-run").hexdigest(),
        status="not_run",
        candidate_id_hash=None,
        vector_hash=hashlib.sha256(b"vector:not-run").hexdigest(),
        reason_code="SETTINGS_NOT_EFFECTIVE",
    )
    proof = ScorerTestOpportunityProof(
        record_id=uuid4(),
        workspace_id=UUID(base.workspace_id),
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
        expected_opportunity_count=4,
        source_gap_count=0,
        membership_mismatch_count=0,
        unlabeled_candidate_count=0,
        entries=tuple(
            sorted((*candidate_entries, not_run_entry), key=lambda entry: entry.stable_id_hash)
        ),
        proof_schema_version="v3",
    )
    assert proof.not_run_count == 1
    assert proof.completeness_verified is False

    scope = ScorerTestEvaluationScope(
        workspace_id=UUID(base.workspace_id),
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
    policy = ScorerTestEvaluationPolicy(
        policy_version="wp06-scorer-oos-v1",
        minimum_sample_count=3,
        minimum_class_support=1,
        maximum_calibration_error=1.0,
        maximum_log_loss_regression=100.0,
        maximum_brier_regression=100.0,
        minimum_coverage=0.0,
        minimum_candidate_retention=0.0,
        maximum_false_veto_rate=1.0,
        approve_probability_threshold=0.0,
        minimum_confidence=0.0,
        reliability_bin_count=3,
        maximum_standardized_feature_deviation=6.0,
        runtime_inference_timeout_ms=100,
    )
    report = build_scorer_test_evaluation_report(
        scope=scope,
        scorer_artifact=scorer,
        base_artifact=base,
        policy=policy,
        rows=rows,
    )

    assert report.status == "ACCEPTED_SHADOW_ONLY"
    assert report.candidate_completeness_verified is False
    assert report.scored_candidate_coverage_verified is True

    target = ScorerDeploymentTarget(
        registry_id=uuid4(),
        registry_revision=1,
        active_pointer_version=0,
        scorer_artifact_id=uuid4(),
        scorer_artifact_hash=scorer.artifact_hash,
        evaluation_report_id=uuid4(),
        evaluation_report_hash=report.report_hash,
        evaluation_status=report.status,
        proof_id=proof.record_id,
        proof_hash=proof.content_hash,
        candidate_completeness_verified=report.candidate_completeness_verified,
        report_scope_hash=report.source_scope_hash,
        proof_scope_hash=proof.source_scope_hash,
        policy_id=policy.policy_version,
        policy_hash=policy.policy_hash,
        current_rights_receipt_hash="a" * 64,
    )
    target.require_mode(ScorerDeploymentMode.SHADOW)
    with pytest.raises(ValueError, match="PAPER_GATE"):
        target.require_mode(ScorerDeploymentMode.PAPER_GATE)
