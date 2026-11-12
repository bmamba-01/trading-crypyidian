from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_drift import (
    LearningDriftCohortMember,
    LearningDriftPersistenceResult,
)
from trading_bot.adapters.database.repositories.learning_shadow import PaperSourceRightsReceipt
from trading_bot.application.learning_drift import (
    LearningDriftApplication,
    LearningDriftIdempotencyConflict,
    LearningDriftRightsConflict,
    LearningDriftTimestampConflict,
    _validate_existing,
)
from trading_bot.domain.learning.drift import (
    DriftObservation,
    DriftPolicy,
    assess_learning_drift,
)
from trading_bot.domain.learning.scoring import ScoreDecision, ScoreReason

WORKSPACE_ID = uuid4()
REGISTRY_ID = uuid4()
REGISTRY_REVISION = 4
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
RIGHTS_FINGERPRINT = "9" * 64


def _members(
    *,
    coverage: float = 0.2,
    count: int = 3,
    rights_eligible: bool = True,
) -> tuple[LearningDriftCohortMember, ...]:
    result: list[LearningDriftCohortMember] = []
    for index in range(count):
        observation = DriftObservation(
            observation_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            challenger_registry_id=REGISTRY_ID,
            challenger_revision=REGISTRY_REVISION,
            observed_at=NOW + timedelta(minutes=index),
            snapshot_hash=f"{index + 1:064x}",
            snapshot_coverage=coverage,
            snapshot_quality_ok=True,
            baseline_available=True,
            schema_compatible=True,
            rights_eligible=rights_eligible,
            challenger_decision=ScoreDecision.ABSTAIN,
            challenger_reason=(
                ScoreReason.UNCALIBRATED_SCORE
                if rights_eligible
                else ScoreReason.SOURCE_RIGHTS_INELIGIBLE
            ),
        )
        result.append(
            LearningDriftCohortMember(
                observation=observation,
                rights_fingerprint=RIGHTS_FINGERPRINT if rights_eligible else None,
            )
        )
    return tuple(result)


class _FakeUnitOfWork:
    def __init__(self, sessions: object, *, workspace_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.session = object()

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.exit_error = exc


class _FakeShadowRepository:
    def __init__(self, session: object, *, calls: list[str], fingerprint: str | None) -> None:
        self.calls = calls
        self.fingerprint = fingerprint

    async def current_paper_rights(
        self, *, workspace_id: UUID, source_id: str = "BINANCE_USDM_REST"
    ) -> PaperSourceRightsReceipt | None:
        self.calls.append("rights")
        assert workspace_id == WORKSPACE_ID
        assert source_id == "BINANCE_USDM_REST"
        if self.fingerprint is None:
            return None
        return PaperSourceRightsReceipt(
            source_id=source_id,
            policy_id="BINANCE_USDM_REST:policy:5",
            policy_sequence=5,
            known_at=NOW - timedelta(seconds=1),
            fingerprint=self.fingerprint,
        )


class _FakeDriftRepository:
    def __init__(
        self,
        session: object,
        *,
        calls: list[str],
        members: tuple[LearningDriftCohortMember, ...],
        stored: LearningDriftPersistenceResult | None = None,
        timestamp: datetime = NOW + timedelta(hours=1),
    ) -> None:
        self.calls = calls
        self.members = members
        self.stored = stored
        self.timestamp = timestamp
        self.persisted: LearningDriftPersistenceResult | None = None
        self.keys: list[str] = []

    async def lock_cohort(self, *, workspace_id: UUID, registry_id: UUID, revision: int) -> None:
        self.calls.append("cohort_lock")
        assert (workspace_id, registry_id, revision) == (
            WORKSPACE_ID,
            REGISTRY_ID,
            REGISTRY_REVISION,
        )

    async def list_cohort(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        revision: int,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LearningDriftCohortMember, ...]:
        self.calls.append("list_cohort")
        assert window_end == self.timestamp
        assert window_start == self.timestamp - timedelta(days=30)
        return self.members

    async def find_assessment(
        self, *, workspace_id: UUID, idempotency_key: str
    ) -> LearningDriftPersistenceResult | None:
        self.calls.append("find_assessment")
        self.keys.append(idempotency_key)
        if self.stored is None:
            return None
        return LearningDriftPersistenceResult(
            assessment=self.stored.assessment,
            proposal=self.stored.proposal,
            is_duplicate=True,
        )

    async def transaction_timestamp(self, *, workspace_id: UUID) -> datetime:
        self.calls.append("transaction_timestamp")
        return self.timestamp

    async def persist(
        self,
        *,
        assessment: object,
        members: tuple[LearningDriftCohortMember, ...],
        proposal: object | None,
        policy: object,
        registry_id: UUID,
        revision: int,
        idempotency_key: str,
    ) -> LearningDriftPersistenceResult:
        self.calls.append("persist")
        result = LearningDriftPersistenceResult(
            assessment=assessment,
            proposal=proposal,
            is_duplicate=False,
        )
        self.persisted = result
        self.stored = result
        self.keys.append(idempotency_key)
        assert len(members) == assessment.member_count
        return result


def _service(
    *,
    members: tuple[LearningDriftCohortMember, ...] | None = None,
    fingerprint: str | None = RIGHTS_FINGERPRINT,
    stored: LearningDriftPersistenceResult | None = None,
    timestamp: datetime = NOW + timedelta(hours=1),
) -> tuple[LearningDriftApplication, list[str], list[_FakeDriftRepository]]:
    calls: list[str] = []
    repos: list[_FakeDriftRepository] = []

    def drift_repository_factory(session: object) -> _FakeDriftRepository:
        repository = _FakeDriftRepository(
            session,
            calls=calls,
            members=members if members is not None else _members(),
            stored=stored,
            timestamp=timestamp,
        )
        repos.append(repository)
        return repository

    service = LearningDriftApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=drift_repository_factory,
        shadow_repository_factory=lambda session: _FakeShadowRepository(
            session, calls=calls, fingerprint=fingerprint
        ),
    )
    return service, calls, repos


@pytest.mark.asyncio
async def test_detected_assessment_revalidates_then_locks_and_persists_proposal() -> None:
    service, calls, repositories = _service()

    result = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert calls == [
        "rights",
        "cohort_lock",
        "transaction_timestamp",
        "list_cohort",
        "find_assessment",
        "persist",
    ]
    assert result.assessment.policy_version == "coverage-v2-30d"
    assert result.assessment.data_quality_status.value == "detected"
    assert result.assessment.performance_status.value == "inconclusive"
    assert result.proposal is not None
    assert result.proposal.assessment_hash == result.assessment.content_hash
    assert result.is_duplicate is False
    assert repositories[0].persisted is not None
    assert repositories[0].persisted.assessment.created_at == NOW + timedelta(hours=1)
    assert not hasattr(result.proposal, "job_id")


@pytest.mark.asyncio
async def test_no_drift_assessment_does_not_create_retrain_proposal() -> None:
    service, _, _ = _service(members=_members(coverage=0.95))

    result = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert result.assessment.data_quality_status.value == "no_drift"
    assert result.assessment.performance_status.value == "inconclusive"
    assert result.assessment.abstention_rate == 1.0
    assert result.proposal is None


@pytest.mark.asyncio
async def test_low_sample_assessment_remains_inconclusive_without_proposal() -> None:
    service, _, _ = _service(members=_members(count=2))

    result = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert result.assessment.data_quality_status.value == "inconclusive"
    assert result.assessment.member_count == 2
    assert result.proposal is None


@pytest.mark.asyncio
async def test_empty_cohort_returns_no_assessment_without_persisting_unpinned_scope() -> None:
    service, calls, repositories = _service(members=())

    result = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert result is None
    assert calls == ["rights", "cohort_lock", "transaction_timestamp", "list_cohort"]
    assert repositories[0].persisted is None


@pytest.mark.asyncio
async def test_transaction_timestamp_before_latest_member_fails_closed() -> None:
    service, calls, repositories = _service(timestamp=NOW - timedelta(seconds=1))

    with pytest.raises(LearningDriftTimestampConflict, match="cũ hơn observation"):
        await service.assess_cohort(
            workspace_id=WORKSPACE_ID,
            challenger_registry_id=REGISTRY_ID,
            challenger_revision=REGISTRY_REVISION,
        )

    assert calls[-1] == "find_assessment"
    assert repositories[0].persisted is None


def test_existing_empty_record_cannot_claim_a_requested_cohort_scope() -> None:
    empty_assessment = assess_learning_drift(
        workspace_id=WORKSPACE_ID,
        assessment_id=uuid4(),
        policy=DriftPolicy(
            policy_version="coverage-v2-30d",
            minimum_observations=3,
            minimum_snapshot_coverage=0.8,
            minimum_quality_rate=0.9,
        ),
        observations=(),
        created_at=NOW,
    )
    persisted = LearningDriftPersistenceResult(
        assessment=empty_assessment,
        proposal=None,
        is_duplicate=True,
    )

    with pytest.raises(LearningDriftIdempotencyConflict, match="cohort"):
        _validate_existing(
            existing=persisted,
            workspace_id=WORKSPACE_ID,
            registry_id=REGISTRY_ID,
            revision=REGISTRY_REVISION,
            policy_version="coverage-v2-30d",
            membership_digest=empty_assessment.membership_digest,
        )


@pytest.mark.asyncio
async def test_rights_receipt_mismatch_aborts_without_cohort_read_or_write() -> None:
    service, calls, repositories = _service(fingerprint="8" * 64)

    with pytest.raises(LearningDriftRightsConflict, match="receipt"):
        await service.assess_cohort(
            workspace_id=WORKSPACE_ID,
            challenger_registry_id=REGISTRY_ID,
            challenger_revision=REGISTRY_REVISION,
        )

    assert calls == ["rights", "cohort_lock", "transaction_timestamp", "list_cohort"]
    assert repositories[0].persisted is None


@pytest.mark.asyncio
async def test_missing_rights_persists_inconclusive_for_all_ineligible_cohort() -> None:
    service, calls, repositories = _service(
        members=_members(rights_eligible=False),
        fingerprint=None,
    )

    result = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert calls == [
        "rights",
        "cohort_lock",
        "transaction_timestamp",
        "list_cohort",
        "find_assessment",
        "persist",
    ]
    assert result.assessment.data_quality_status.value == "inconclusive"
    assert "rights_unverified" in {reason.value for reason in result.assessment.reason_codes}
    assert result.proposal is None
    assert repositories[0].persisted is not None


@pytest.mark.asyncio
async def test_missing_rights_aborts_when_cohort_contains_eligible_observation() -> None:
    ineligible = _members(count=2, rights_eligible=False)
    eligible = _members(count=1, rights_eligible=True)
    service, calls, repositories = _service(
        members=(*ineligible, *eligible),
        fingerprint=None,
    )

    with pytest.raises(LearningDriftRightsConflict, match="receipt"):
        await service.assess_cohort(
            workspace_id=WORKSPACE_ID,
            challenger_registry_id=REGISTRY_ID,
            challenger_revision=REGISTRY_REVISION,
        )

    assert calls == ["rights", "cohort_lock", "transaction_timestamp", "list_cohort"]
    assert repositories[0].persisted is None


@pytest.mark.asyncio
async def test_mismatched_current_rights_is_ignored_for_all_ineligible_cohort() -> None:
    service, _, repositories = _service(
        members=_members(rights_eligible=False),
        fingerprint="8" * 64,
    )

    result = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert result.assessment.data_quality_status.value == "inconclusive"
    assert result.proposal is None
    assert repositories[0].persisted is not None


@pytest.mark.asyncio
async def test_existing_assessment_retry_reads_bounded_cohort_and_returns_original_hash() -> None:
    members = _members()
    service, _, repositories = _service(members=members)
    original = await service.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )
    assert repositories[0].persisted is not None
    calls: list[str] = []

    def retry_repository_factory(session: object) -> _FakeDriftRepository:
        repository = _FakeDriftRepository(
            session,
            calls=calls,
            members=members,
            stored=LearningDriftPersistenceResult(
                assessment=original.assessment,
                proposal=original.proposal,
                is_duplicate=False,
            ),
        )
        return repository

    retry = LearningDriftApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=retry_repository_factory,
        shadow_repository_factory=lambda session: _FakeShadowRepository(
            session, calls=calls, fingerprint=RIGHTS_FINGERPRINT
        ),
    )
    repeated = await retry.assess_cohort(
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=REGISTRY_ID,
        challenger_revision=REGISTRY_REVISION,
    )

    assert repeated.assessment == original.assessment
    assert repeated.proposal == original.proposal
    assert repeated.is_duplicate is True
    assert calls == [
        "rights",
        "cohort_lock",
        "transaction_timestamp",
        "list_cohort",
        "find_assessment",
    ]
