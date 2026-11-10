from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_shadow import (
    PaperSourceRightsReceipt,
    ShadowObservationAppendResult,
    ShadowObservationSource,
)
from trading_bot.application.learning_shadow import (
    LearningShadowApplication,
    ShadowObservationBatch,
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
HASHES = tuple(f"{value:x}" * 64 for value in range(1, 9))


def _source(*, schema_compatible: bool = True) -> ShadowObservationSource:
    snapshot = ShadowSnapshotReference(
        workspace_id=WORKSPACE_ID,
        snapshot_id=HASHES[0],
        snapshot_hash=HASHES[0],
        schema_hash=HASHES[1],
        as_of=NOW,
    )
    challenger = ShadowModelReference(
        workspace_id=WORKSPACE_ID,
        registry_id=uuid4(),
        artifact_id=uuid4(),
        artifact_hash=HASHES[2],
        manifest_hash=HASHES[3],
        fold_plan_hash=HASHES[4],
        training_approval_hash=HASHES[5],
        registry_approval_event_id=uuid4(),
        registry_approval_event_hash=HASHES[6],
        registry_approval_event_sequence=4,
        revision=5,
        state=LearningModelRegistryState.EVALUATED,
    )
    incumbent = IncumbentDecisionReference(
        decision_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        decision_hash=HASHES[7],
        strategy_release_id="h03-v1",
        strategy_release_hash=HASHES[1],
        status=IncumbentDecisionStatus.CANDIDATE,
        snapshot=snapshot,
        evaluated_at=NOW + timedelta(seconds=1),
    )
    return ShadowObservationSource(
        incumbent_decision=incumbent,
        challenger_model=challenger,
        snapshot=snapshot,
        active_registry_id=uuid4(),
        active_registry_revision=9,
        active_artifact_hash=HASHES[2],
        active_pointer_version=10,
        snapshot_coverage=0.91,
        snapshot_quality_ok=True,
        schema_compatible=schema_compatible,
    )


class _FakeUnitOfWork:
    instances: list[_FakeUnitOfWork] = []

    def __init__(self, sessions: object, *, workspace_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.session = object()
        self.entered = False
        self.exit_error: object | None = None
        self.instances.append(self)

    async def __aenter__(self) -> _FakeUnitOfWork:
        self.entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.exit_error = exc


class _FakeRepository:
    instances: list[_FakeRepository] = []

    def __init__(self, session: object) -> None:
        self.calls: list[str] = []
        self.source = _source(schema_compatible=False)
        self.rights: PaperSourceRightsReceipt | None = None
        self.supported = False
        self.appended: list[ShadowObservation] = []
        self.duplicate = False
        self.clock = NOW + timedelta(seconds=3)
        self.instances.append(self)

    async def lock_active_pointer(self, *, workspace_id: UUID) -> None:
        self.calls.append("pointer_lock")
        assert workspace_id == WORKSPACE_ID

    async def current_paper_rights(
        self, *, workspace_id: UUID, source_id: str = "BINANCE_USDM_REST"
    ) -> PaperSourceRightsReceipt | None:
        self.calls.append("rights")
        assert workspace_id == WORKSPACE_ID
        assert source_id == "BINANCE_USDM_REST"
        return self.rights

    async def list_pending(
        self, *, workspace_id: UUID, limit: int
    ) -> tuple[ShadowObservationSource, ...]:
        self.calls.append("list")
        assert workspace_id == WORKSPACE_ID
        return (self.source,)[:limit]

    async def decision_source_rights_supported(
        self, *, workspace_id: UUID, decision_id: UUID
    ) -> bool:
        self.calls.append("source_support")
        assert workspace_id == WORKSPACE_ID
        assert decision_id == self.source.incumbent_decision.decision_id
        return self.supported

    async def database_clock(self, *, workspace_id: UUID) -> datetime:
        self.calls.append("clock")
        assert workspace_id == WORKSPACE_ID
        return self.clock

    async def append(self, *, observation: ShadowObservation) -> ShadowObservationAppendResult:
        self.calls.append("append")
        self.appended.append(observation)
        return ShadowObservationAppendResult(observation=observation, is_duplicate=self.duplicate)


def _rights() -> PaperSourceRightsReceipt:
    return PaperSourceRightsReceipt(
        source_id="BINANCE_USDM_REST",
        policy_id="BINANCE_USDM_REST:policy:5",
        policy_sequence=5,
        known_at=NOW + timedelta(seconds=2),
        fingerprint="9" * 64,
    )


def _service() -> LearningShadowApplication:
    _FakeRepository.instances.clear()
    _FakeUnitOfWork.instances.clear()
    return LearningShadowApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=_FakeRepository,
    )


@pytest.mark.asyncio
async def test_observer_persists_only_abstain_and_rights_failure_has_precedence() -> None:
    service = _service()
    batch = await service.observe_pending(workspace_id=WORKSPACE_ID)

    assert isinstance(batch, ShadowObservationBatch)
    assert batch.workspace_id == WORKSPACE_ID
    assert batch.scanned_count == 1
    assert batch.created_count == 1
    assert batch.skipped_count == 0
    observation = batch.observations[0]
    assert observation.challenger_reason is ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE
    assert observation.rights_eligible is False
    assert observation.schema_compatible is False
    assert observation.rights_fingerprint is None
    assert observation.rights_policy_id is None
    assert not hasattr(observation, "score")
    assert not hasattr(observation, "features")

    repository = _FakeRepository.instances[-1]
    assert repository.calls == [
        "pointer_lock",
        "rights",
        "list",
        "source_support",
        "clock",
        "append",
    ]
    assert _FakeUnitOfWork.instances[-1].workspace_id == WORKSPACE_ID
    assert _FakeUnitOfWork.instances[-1].exit_error is None


@pytest.mark.asyncio
async def test_observer_pins_current_rights_and_emits_schema_abstain_when_eligible() -> None:
    _service()
    _FakeRepository.instances.clear()

    class EligibleRepository(_FakeRepository):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self.rights = _rights()
            self.supported = True

    application = LearningShadowApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=EligibleRepository,
    )

    batch = await application.observe_pending(workspace_id=WORKSPACE_ID)
    observation = batch.observations[0]

    assert observation.challenger_reason is ShadowAbstainReason.FEATURE_SCHEMA_MISMATCH
    assert observation.rights_eligible is True
    assert observation.rights_fingerprint == "9" * 64
    assert observation.rights_policy_sequence == 5
    assert observation.rights_known_at == NOW + timedelta(seconds=2)
    assert observation.observed_at == NOW + timedelta(seconds=3)


@pytest.mark.asyncio
async def test_compatible_artifact_still_abstains_without_calibration() -> None:
    _service()
    _FakeRepository.instances.clear()

    class CompatibleRepository(_FakeRepository):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self.source = _source(schema_compatible=True)
            self.rights = _rights()
            self.supported = True

    application = LearningShadowApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=CompatibleRepository,
    )

    batch = await application.observe_pending(workspace_id=WORKSPACE_ID)
    observation = batch.observations[0]

    assert observation.schema_compatible is True
    assert observation.rights_eligible is True
    assert observation.challenger_reason is ShadowAbstainReason.UNCALIBRATED_SCORE
    assert not hasattr(observation, "probability")
    assert not hasattr(observation, "confidence")


@pytest.mark.asyncio
async def test_current_policy_does_not_cover_unsupported_decision_sources() -> None:
    _service()
    _FakeRepository.instances.clear()

    class UnsupportedSourceRepository(_FakeRepository):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self.source = _source(schema_compatible=True)
            self.rights = _rights()
            self.supported = False

    application = LearningShadowApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=UnsupportedSourceRepository,
    )

    batch = await application.observe_pending(workspace_id=WORKSPACE_ID)
    observation = batch.observations[0]

    assert observation.challenger_reason is ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE
    assert observation.rights_eligible is False
    assert observation.rights_fingerprint is None
    assert observation.rights_policy_id is None


@pytest.mark.asyncio
async def test_observer_validates_workspace_and_batch_limit_before_opening_uow() -> None:
    service = _service()

    with pytest.raises(ValueError, match="workspace_id"):
        await service.observe_pending(workspace_id="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        await service.observe_pending(workspace_id=WORKSPACE_ID, limit=0)

    assert not _FakeUnitOfWork.instances


@pytest.mark.asyncio
async def test_observer_uses_database_time_and_returns_duplicate_record() -> None:
    _service()
    _FakeRepository.instances.clear()
    saved_observation_id = uuid4()

    class DuplicateRepository(_FakeRepository):
        async def append(self, *, observation: ShadowObservation) -> ShadowObservationAppendResult:
            del observation
            self.calls.append("append")
            # A duplicate result returns the previously persisted domain record.
            result = ShadowObservationAppendResult(
                observation=_observation_for_duplicate(saved_observation_id), is_duplicate=True
            )
            return result

    application = LearningShadowApplication(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=DuplicateRepository,
    )

    batch = await application.observe_pending(workspace_id=WORKSPACE_ID)

    assert batch.created_count == 0
    assert batch.observations[0].observation_id == saved_observation_id


def _observation_for_duplicate(observation_id: UUID) -> ShadowObservation:
    source = _source()
    return ShadowObservation(
        observation_id=observation_id,
        workspace_id=WORKSPACE_ID,
        snapshot=source.snapshot,
        challenger_model=source.challenger_model,
        incumbent_decision=source.incumbent_decision,
        active_registry_id=source.active_registry_id,
        active_registry_revision=source.active_registry_revision,
        active_artifact_hash=source.active_artifact_hash,
        active_pointer_version=source.active_pointer_version,
        snapshot_coverage=source.snapshot_coverage,
        snapshot_quality_ok=source.snapshot_quality_ok,
        schema_compatible=source.schema_compatible,
        rights_eligible=False,
        rights_fingerprint=None,
        rights_source_id=None,
        rights_policy_id=None,
        rights_policy_sequence=None,
        rights_known_at=None,
        challenger_reason=ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE,
        observed_at=NOW + timedelta(seconds=3),
    )
