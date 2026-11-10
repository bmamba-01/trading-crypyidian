from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from trading_bot.adapters.database.models import LearningShadowObservationRecord
from trading_bot.adapters.database.repositories.learning_shadow import (
    LearningShadowConflict,
    LearningShadowCorruptRecord,
    LearningShadowRepository,
    PaperSourceRightsReceipt,
    _observation_from_record,
    _record_from_observation,
    _source_from_mapping,
)
from trading_bot.domain.learning.model_registry import LearningModelRegistryState
from trading_bot.domain.learning.shadow import (
    IncumbentDecisionReference,
    IncumbentDecisionStatus,
    ShadowAbstainReason,
    ShadowModelReference,
    ShadowObservation,
    ShadowSnapshotReference,
)

WORKSPACE_ID = uuid4()
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def _observation() -> ShadowObservation:
    snapshot = ShadowSnapshotReference(
        workspace_id=WORKSPACE_ID,
        snapshot_id="a" * 64,
        snapshot_hash="a" * 64,
        schema_hash="b" * 64,
        as_of=NOW,
    )
    model = ShadowModelReference(
        workspace_id=WORKSPACE_ID,
        registry_id=uuid4(),
        artifact_id=uuid4(),
        artifact_hash="c" * 64,
        manifest_hash="d" * 64,
        fold_plan_hash="e" * 64,
        training_approval_hash="f" * 64,
        registry_approval_event_id=uuid4(),
        registry_approval_event_hash="1" * 64,
        registry_approval_event_sequence=2,
        revision=3,
        state=LearningModelRegistryState.EVALUATED,
    )
    incumbent = IncumbentDecisionReference(
        decision_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        decision_hash="2" * 64,
        strategy_release_id="h03-v1",
        strategy_release_hash="3" * 64,
        status=IncumbentDecisionStatus.CANDIDATE,
        snapshot=snapshot,
        evaluated_at=NOW + timedelta(seconds=1),
    )
    return ShadowObservation(
        observation_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        snapshot=snapshot,
        challenger_model=model,
        incumbent_decision=incumbent,
        active_registry_id=uuid4(),
        active_registry_revision=4,
        active_artifact_hash="4" * 64,
        active_pointer_version=5,
        snapshot_coverage=0.9,
        snapshot_quality_ok=True,
        schema_compatible=True,
        rights_eligible=False,
        rights_fingerprint=None,
        rights_source_id=None,
        rights_policy_id=None,
        rights_policy_sequence=None,
        rights_known_at=None,
        challenger_reason=ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE,
        observed_at=NOW + timedelta(seconds=2),
    )


def test_pending_query_is_workspace_scoped_and_projects_metadata_only() -> None:
    workspace_id = uuid4()
    statement = LearningShadowRepository.pending_statement(workspace_id=workspace_id, limit=10)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    sql = str(compiled).lower()

    select_list = sql.split(" from ", maxsplit=1)[0]
    assert str(workspace_id) in sql
    assert "workspace_id" in sql
    assert "features" not in select_list
    assert "warmup" not in select_list
    assert "canonical_json" not in select_list
    assert "source_refs" not in select_list
    assert "payload" not in select_list
    assert "canonical_artifact" not in select_list
    assert "feature_schema_hash" in select_list
    assert "active_registry" in sql
    assert "wp06_learning_registry_approval_event_hash" in select_list
    assert "learning_shadow_observation" in sql


def test_shadow_observation_has_logical_lineage_uniqueness() -> None:
    table = LearningShadowObservationRecord.__table__
    expected = {
        "workspace_id",
        "incumbent_decision_id",
        "challenger_registry_id",
        "challenger_revision",
        "snapshot_hash",
    }

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_learning_shadow_observation_logical_identity"
        and {column.name for column in constraint.columns} == expected
        for constraint in table.constraints
    )


def test_rights_helpers_bind_workspace_and_decision_without_payload_projection() -> None:
    statement = LearningShadowRepository.rights_supported_statement(
        workspace_id=uuid4(), decision_id=uuid4()
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()

    assert "wp06_shadow_decision_source_rights_supported" in sql
    assert "workspace_id" in compiled.params
    assert "decision_id" in compiled.params


def test_database_clock_statement_uses_transaction_timestamp() -> None:
    statement = LearningShadowRepository.database_clock_statement()

    assert "transaction_timestamp" in str(statement.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_active_pointer_lock_uses_registry_writer_advisory_key() -> None:
    session = _FakeSession()

    await LearningShadowRepository(session).lock_active_pointer(workspace_id=WORKSPACE_ID)

    assert len(session.execute_calls) == 2
    lock_statement, lock_params = session.execute_calls[1]
    assert "pg_advisory_xact_lock" in str(lock_statement)
    assert lock_params == {"lock_key": f"learning-model-registry:{WORKSPACE_ID}:active-pointer"}


def test_candidate_mapping_uses_snapshot_quality_and_artifact_schema_metadata() -> None:
    row = {
        "workspace_id": WORKSPACE_ID,
        "snapshot_id": "a" * 64,
        "snapshot_hash": "a" * 64,
        "snapshot_schema_hash": "b" * 64,
        "snapshot_as_of": NOW,
        "decision_id": uuid4(),
        "decision_hash": "2" * 64,
        "strategy_release_id": "h03-v1",
        "strategy_release_hash": "3" * 64,
        "decision_status": "CANDIDATE",
        "decision_evaluated_at": NOW + timedelta(seconds=1),
        "challenger_registry_id": uuid4(),
        "challenger_artifact_id": uuid4(),
        "challenger_artifact_hash": "c" * 64,
        "challenger_manifest_hash": "d" * 64,
        "challenger_fold_plan_hash": "e" * 64,
        "training_approval_hash": "f" * 64,
        "challenger_revision": 3,
        "challenger_state": LearningModelRegistryState.EVALUATED.value,
        "registry_approval_event_id": uuid4(),
        "registry_approval_event_hash": "1" * 64,
        "registry_approval_event_sequence": 2,
        "active_registry_id": uuid4(),
        "active_registry_revision": 4,
        "active_artifact_hash": "4" * 64,
        "active_pointer_version": 5,
        "snapshot_coverage": 0.9,
        "snapshot_quality_flags": ["warmup_incomplete"],
        "snapshot_data_freshness": "FRESH",
        "snapshot_is_usable": True,
        "artifact_schema_hash": "b" * 64,
    }

    source = _source_from_mapping(row)

    assert source.snapshot_coverage == 0.9
    assert source.snapshot_quality_ok is False
    assert source.schema_compatible is True
    assert not hasattr(source, "features")


class _FakeSession:
    def __init__(self, *, scalar_values: tuple[object, ...] = ()) -> None:
        self.scalar_values = list(scalar_values)
        self.execute_calls: list[tuple[object, object]] = []
        self.scalar_calls: list[tuple[object, object]] = []

    async def execute(self, statement: object, params: object = None) -> object:
        self.execute_calls.append((statement, params))
        return object()

    async def scalar(self, statement: object, params: object = None) -> object:
        self.scalar_calls.append((statement, params))
        return self.scalar_values.pop(0) if self.scalar_values else None


@pytest.mark.asyncio
async def test_source_rights_are_pinned_from_current_policy_receipt_and_db_fingerprint() -> None:
    known_at = NOW + timedelta(seconds=1)
    session = _FakeSession(
        scalar_values=(
            {
                "source_id": "BINANCE_USDM_REST",
                "policy_id": "BINANCE_USDM_REST:policy:2",
                "policy_sequence": 2,
                "known_at": known_at.isoformat(),
                "fingerprint": "9" * 64,
            },
        )
    )
    receipt = await LearningShadowRepository(session).current_paper_rights(
        workspace_id=WORKSPACE_ID
    )

    assert receipt == PaperSourceRightsReceipt(
        source_id="BINANCE_USDM_REST",
        policy_id="BINANCE_USDM_REST:policy:2",
        policy_sequence=2,
        known_at=known_at,
        fingerprint="9" * 64,
    )
    assert "wp06_shadow_current_paper_rights_receipt" in str(session.scalar_calls[0][0])
    assert session.scalar_calls[0][1] == {"workspace_id": WORKSPACE_ID}


@pytest.mark.asyncio
async def test_source_rights_helper_null_or_policy_denial_fails_closed() -> None:
    session = _FakeSession(scalar_values=(None,))
    assert (
        await LearningShadowRepository(session).decision_source_rights_supported(
            workspace_id=WORKSPACE_ID,
            decision_id=uuid4(),
        )
        is False
    )
    supported_session = _FakeSession(scalar_values=(True,))
    assert await LearningShadowRepository(supported_session).decision_source_rights_supported(
        workspace_id=WORKSPACE_ID,
        decision_id=uuid4(),
    )

    malformed_session = _FakeSession(
        scalar_values=(
            {
                "source_id": "BINANCE_USDM_REST",
                "policy_id": "policy:bad",
                "policy_sequence": 3,
                "known_at": "2026-11-08T12:00:00",
                "fingerprint": "9" * 64,
            },
        )
    )
    assert (
        await LearningShadowRepository(malformed_session).current_paper_rights(
            workspace_id=WORKSPACE_ID
        )
        is None
    )
    assert not session.scalar_calls[1:]


@pytest.mark.asyncio
async def test_observation_mapping_round_trip_checks_canonical_hashes() -> None:
    observation = _observation()
    record = _record_from_observation(observation)

    assert _observation_from_record(record) == observation
    record.content_hash = "0" * 64
    with pytest.raises(LearningShadowCorruptRecord):
        _observation_from_record(record)


@pytest.mark.asyncio
async def test_append_retry_returns_original_and_tampered_idempotency_conflicts() -> None:
    stored_observation = _observation()
    stored_record = _record_from_observation(stored_observation)
    session = _FakeSession()
    session.scalar = lambda statement, params=None: _async_value(stored_record)  # type: ignore[method-assign]
    repository = LearningShadowRepository(session)
    retry = replace(
        stored_observation,
        observation_id=uuid4(),
        observed_at=stored_observation.observed_at + timedelta(seconds=5),
    )

    result = await repository.append(observation=retry)

    assert result.is_duplicate is True
    assert result.observation == stored_observation

    stored_record.snapshot_coverage = 0.5
    with pytest.raises(LearningShadowConflict):
        await repository.append(observation=retry)


class _FailingNestedTransaction:
    async def __aenter__(self) -> _FailingNestedTransaction:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


class _LogicalRaceSession(_FakeSession):
    def __init__(self, *, existing_record: object, constraint_name: str) -> None:
        super().__init__()
        self.scalar_results: list[object] = [None, None, existing_record]
        self.constraint_name = constraint_name
        self.added: list[object] = []

    async def scalar(self, statement: object, params: object = None) -> object:
        self.scalar_calls.append((statement, params))
        return self.scalar_results.pop(0) if self.scalar_results else None

    def begin_nested(self) -> _FailingNestedTransaction:
        return _FailingNestedTransaction()

    def add(self, record: object) -> None:
        self.added.append(record)

    async def flush(self) -> None:
        original = Exception("unique race")
        original.diag = SimpleNamespace(constraint_name=self.constraint_name)  # type: ignore[attr-defined]
        raise IntegrityError("INSERT", {}, original)


@pytest.mark.asyncio
async def test_logical_unique_race_returns_matching_retry_and_rejects_changed_identity() -> None:
    stored = _observation()
    existing_record = _record_from_observation(stored)
    same_identity_retry = replace(
        stored,
        observation_id=uuid4(),
        observed_at=stored.observed_at + timedelta(seconds=5),
    )
    same_identity_session = _LogicalRaceSession(
        existing_record=existing_record,
        constraint_name="uq_learning_shadow_observation_logical_identity",
    )
    same_identity_result = await LearningShadowRepository(same_identity_session).append(
        observation=same_identity_retry
    )

    assert same_identity_result.is_duplicate is True
    assert same_identity_result.observation == stored
    assert len(same_identity_session.added) == 1

    changed_identity_session = _LogicalRaceSession(
        existing_record=existing_record,
        constraint_name="uq_learning_shadow_observation_logical_identity",
    )
    changed_pointer_retry = replace(
        stored,
        observation_id=uuid4(),
        active_pointer_version=stored.active_pointer_version + 1,
    )
    with pytest.raises(LearningShadowConflict, match="lineage"):
        await LearningShadowRepository(changed_identity_session).append(
            observation=changed_pointer_retry
        )


async def _async_value(value: object) -> object:
    return value
