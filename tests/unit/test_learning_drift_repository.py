from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from trading_bot.adapters.database.models import (
    LearningDriftAssessmentMemberRecord,
    LearningDriftAssessmentRecord,
    LearningRetrainProposalRecord,
)
from trading_bot.adapters.database.repositories.learning_drift import (
    LearningDriftCohortMember,
    LearningDriftRepository,
    LearningDriftRepositoryConflict,
    _assessment_record,
    _member_record,
    _proposal_record,
)
from trading_bot.domain.learning.drift import (
    DriftObservation,
    DriftPolicy,
    LearningDriftAssessment,
    LearningRetrainProposal,
    assess_learning_drift,
    create_learning_retrain_proposal,
)
from trading_bot.domain.learning.scoring import ScoreDecision, ScoreReason

WORKSPACE_ID = uuid4()
REGISTRY_ID = uuid4()
REGISTRY_REVISION = 4
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def test_cohort_query_is_workspace_and_revision_scoped_metadata_only() -> None:
    statement = LearningDriftRepository.cohort_statement(
        workspace_id=WORKSPACE_ID,
        registry_id=REGISTRY_ID,
        revision=REGISTRY_REVISION,
        window_start=NOW - timedelta(days=30),
        window_end=NOW,
    )
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).lower()
    select_list = sql.split(" from ", maxsplit=1)[0]

    assert str(WORKSPACE_ID) in sql
    assert str(REGISTRY_ID) in sql
    assert "challenger_revision" in sql
    assert "observed_at >=" in sql
    assert "observed_at <=" in sql
    assert "2026-10-09 12:00:00+00:00" in sql
    assert "2026-11-08 12:00:00+00:00" in sql
    assert "snapshot_coverage" in select_list
    assert "snapshot_quality_ok" in select_list
    assert "rights_fingerprint" in select_list
    assert "baseline_available" in select_list
    assert "source_refs" not in select_list
    assert "features" not in select_list
    assert "warmup" not in select_list
    assert "canonical_json" not in select_list
    assert "payload" not in select_list


@pytest.mark.asyncio
async def test_cohort_lock_calls_database_helper_with_workspace_and_revision() -> None:
    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object]] = []

        async def execute(self, statement: object, params: object = None) -> object:
            self.calls.append((statement, params))
            return object()

    session = Session()
    repository = LearningDriftRepository(session)  # type: ignore[arg-type]

    await repository.lock_cohort(
        workspace_id=WORKSPACE_ID,
        registry_id=REGISTRY_ID,
        revision=REGISTRY_REVISION,
    )

    assert len(session.calls) == 2
    lock_statement, params = session.calls[1]
    assert "wp06_lock_learning_shadow_cohort" in str(lock_statement)
    assert params == {
        "workspace_id": WORKSPACE_ID,
        "registry_id": REGISTRY_ID,
        "revision": REGISTRY_REVISION,
    }


def test_transaction_clock_is_postgres_transaction_timestamp() -> None:
    sql = str(
        LearningDriftRepository.transaction_clock_statement().compile(dialect=postgresql.dialect())
    ).lower()

    assert "transaction_timestamp" in sql
    assert "clock_timestamp" not in sql


def test_server_policy_cannot_be_overridden_by_request_values() -> None:
    policy = LearningDriftRepository.server_policy()

    assert policy.policy_version == "coverage-v2-30d"
    assert policy.observation_window_days == 30
    assert policy.minimum_observations == 3
    assert policy.minimum_snapshot_coverage == 0.8
    assert policy.minimum_quality_rate == 0.9


class _NestedTransaction:
    async def __aenter__(self) -> _NestedTransaction:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


class _WriteSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_count = 0
        self.flush_batches: list[tuple[object, ...] | None] = []
        self.execute_calls: list[tuple[object, object]] = []

    async def execute(self, statement: object, params: object = None) -> object:
        self.execute_calls.append((statement, params))
        return object()

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    def add(self, record: object) -> None:
        self.added.append(record)

    async def flush(self, objects: object = None) -> None:
        self.flush_count += 1
        self.flush_batches.append(None if objects is None else tuple(objects))  # type: ignore[arg-type]


class _ScalarRows:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self.rows = rows

    def all(self) -> tuple[object, ...]:
        return self.rows


class _AssessmentRaceSession(_WriteSession):
    def __init__(
        self,
        *,
        assessment_record: object,
        member_records: tuple[object, ...],
        proposal_record: object,
    ) -> None:
        super().__init__()
        self.scalar_results = [assessment_record, proposal_record]
        self.member_records = member_records

    async def scalar(self, statement: object, params: object = None) -> object:
        return self.scalar_results.pop(0)

    async def scalars(self, statement: object) -> _ScalarRows:
        return _ScalarRows(self.member_records)

    async def flush(self, objects: object = None) -> None:
        original = Exception("unique assessment race")
        original.diag = type(
            "Diagnostic", (), {"constraint_name": "uq_learning_drift_assessment_idempotency"}
        )()
        raise IntegrityError("INSERT", {}, original)


def _assessment_inputs() -> tuple[
    LearningDriftAssessment,
    tuple[LearningDriftCohortMember, ...],
    DriftPolicy,
    str,
    LearningRetrainProposal,
]:
    policy = LearningDriftRepository.server_policy()
    members = tuple(
        sorted(
            (
                LearningDriftCohortMember(
                    observation=DriftObservation(
                        observation_id=uuid4(),
                        workspace_id=WORKSPACE_ID,
                        challenger_registry_id=REGISTRY_ID,
                        challenger_revision=REGISTRY_REVISION,
                        observed_at=NOW + timedelta(minutes=index),
                        snapshot_hash=f"{index + 1:064x}",
                        snapshot_coverage=0.2,
                        snapshot_quality_ok=True,
                        baseline_available=True,
                        schema_compatible=True,
                        rights_eligible=True,
                        challenger_decision=ScoreDecision.ABSTAIN,
                        challenger_reason=ScoreReason.UNCALIBRATED_SCORE,
                    ),
                    rights_fingerprint="9" * 64,
                )
                for index in range(3)
            ),
            key=lambda member: str(member.observation.observation_id),
        )
    )
    assessment = assess_learning_drift(
        workspace_id=WORKSPACE_ID,
        assessment_id=uuid4(),
        policy=policy,
        observations=tuple(member.observation for member in members),
        created_at=NOW + timedelta(hours=1),
    )
    idempotency_key = LearningDriftRepository.assessment_idempotency_key(
        workspace_id=WORKSPACE_ID,
        registry_id=REGISTRY_ID,
        revision=REGISTRY_REVISION,
        policy_version=policy.policy_version,
        membership_digest=assessment.membership_digest,
    )
    proposal = create_learning_retrain_proposal(
        assessment,
        proposal_id=uuid4(),
        created_at=assessment.created_at,
    )
    return assessment, members, policy, idempotency_key, proposal


@pytest.mark.asyncio
async def test_persist_appends_assessment_ordered_members_and_detected_proposal_together() -> None:
    assessment, members, policy, idempotency_key, proposal = _assessment_inputs()
    session = _WriteSession()
    result = await LearningDriftRepository(session).persist(
        assessment=assessment,
        members=members,
        proposal=proposal,
        policy=policy,
        registry_id=REGISTRY_ID,
        revision=REGISTRY_REVISION,
        idempotency_key=idempotency_key,
    )

    assert result.assessment == assessment
    assert result.proposal == proposal
    assert result.is_duplicate is False
    assert isinstance(session.added[0], LearningDriftAssessmentRecord)
    assert session.added[0].observation_window_days == 30
    assert all(isinstance(row, LearningDriftAssessmentMemberRecord) for row in session.added[1:4])
    assert isinstance(session.added[4], LearningRetrainProposalRecord)
    member_rows = session.added[1:4]
    assert [row.ordinal for row in member_rows] == [0, 1, 2]  # type: ignore[attr-defined]
    assert [row.observation_id for row in member_rows] == list(assessment.member_ids)  # type: ignore[attr-defined]
    assert session.flush_count == 3
    assert session.flush_batches == [
        (session.added[0],),
        tuple(member_rows),
        (session.added[4],),
    ]


@pytest.mark.asyncio
async def test_repository_rejects_empty_assessment_without_a_pinned_cohort() -> None:
    policy = LearningDriftRepository.server_policy()
    assessment = assess_learning_drift(
        workspace_id=WORKSPACE_ID,
        assessment_id=uuid4(),
        policy=policy,
        observations=(),
        created_at=NOW,
    )
    idempotency_key = LearningDriftRepository.assessment_idempotency_key(
        workspace_id=WORKSPACE_ID,
        registry_id=REGISTRY_ID,
        revision=REGISTRY_REVISION,
        policy_version=policy.policy_version,
        membership_digest=assessment.membership_digest,
    )

    with pytest.raises(ValueError, match="assessment rỗng"):
        await LearningDriftRepository(_WriteSession()).persist(
            assessment=assessment,
            members=(),
            proposal=None,
            policy=policy,
            registry_id=REGISTRY_ID,
            revision=REGISTRY_REVISION,
            idempotency_key=idempotency_key,
        )


@pytest.mark.asyncio
async def test_assessment_unique_race_returns_original_timestamped_result() -> None:
    assessment, members, policy, idempotency_key, proposal = _assessment_inputs()
    original_assessment = assess_learning_drift(
        workspace_id=assessment.workspace_id,
        assessment_id=uuid4(),
        policy=policy,
        observations=tuple(member.observation for member in members),
        created_at=assessment.created_at + timedelta(minutes=1),
    )
    original_proposal = create_learning_retrain_proposal(
        original_assessment,
        proposal_id=uuid4(),
        created_at=original_assessment.created_at,
    )
    session = _AssessmentRaceSession(
        assessment_record=_assessment_record(
            original_assessment,
            policy=policy,
            idempotency_key=idempotency_key,
        ),
        member_records=tuple(
            _member_record(original_assessment, ordinal, member)
            for ordinal, member in enumerate(members)
        ),
        proposal_record=_proposal_record(original_proposal),
    )

    result = await LearningDriftRepository(session).persist(
        assessment=assessment,
        members=members,
        proposal=proposal,
        policy=policy,
        registry_id=REGISTRY_ID,
        revision=REGISTRY_REVISION,
        idempotency_key=idempotency_key,
    )

    assert result.is_duplicate is True
    assert result.assessment == original_assessment
    assert result.proposal == original_proposal
    assert result.assessment.content_hash != assessment.content_hash


@pytest.mark.asyncio
async def test_assessment_unique_race_conflicts_when_membership_payload_changed() -> None:
    assessment, members, policy, idempotency_key, proposal = _assessment_inputs()
    original_assessment = assess_learning_drift(
        workspace_id=assessment.workspace_id,
        assessment_id=uuid4(),
        policy=policy,
        observations=tuple(member.observation for member in members),
        created_at=assessment.created_at + timedelta(minutes=1),
    )
    original_proposal = create_learning_retrain_proposal(
        original_assessment,
        proposal_id=uuid4(),
        created_at=original_assessment.created_at,
    )
    session = _AssessmentRaceSession(
        assessment_record=_assessment_record(
            original_assessment,
            policy=policy,
            idempotency_key=idempotency_key,
        ),
        member_records=tuple(
            _member_record(original_assessment, ordinal, member)
            for ordinal, member in enumerate(members)
        ),
        proposal_record=_proposal_record(original_proposal),
    )
    changed_assessment = replace(
        assessment,
        quality_rate=0.0,
        data_quality_status=assessment.data_quality_status,
    )
    changed_proposal = create_learning_retrain_proposal(
        changed_assessment,
        proposal_id=uuid4(),
        created_at=changed_assessment.created_at,
    )

    with pytest.raises(LearningDriftRepositoryConflict, match="cohort"):
        await LearningDriftRepository(session).persist(
            assessment=changed_assessment,
            members=members,
            proposal=changed_proposal,
            policy=policy,
            registry_id=REGISTRY_ID,
            revision=REGISTRY_REVISION,
            idempotency_key=idempotency_key,
        )
