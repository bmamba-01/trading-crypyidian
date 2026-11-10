from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

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

    event_ids = tuple(f"event-{index}" for index in range(5))
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
        },
        outcome_by_event={event_ids[0]: outcome},
        membership_by_outcome={outcome_id: sample},
    )

    by_stable_id = {entry.stable_id_hash: entry for entry in entries}
    assert missing_events == 1
    assert unlabeled_count == 1
    assert found_candidates == set(candidate_ids.values())
    assert len(entries) == 4
    assert {entry.status for entry in entries} == {"candidate", "no_candidate", "input_excluded"}
    assert by_stable_id[hashlib.sha256(event_ids[1].encode()).hexdigest()].status == (
        "no_candidate"
    )
    assert by_stable_id[hashlib.sha256(event_ids[3].encode()).hexdigest()].status == (
        "input_excluded"
    )
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
