"""Kiểm tra service chặn entry gắn với Principal, MFA, CAS và ledger."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.domain.learning.drift import (
    DriftObservation,
    DriftPolicy,
    assess_learning_drift,
    create_learning_retrain_proposal,
)
from trading_bot.domain.learning.scoring import ScoreDecision

WORKSPACE_ID = UUID(int=1)
ACTOR_ID = UUID(int=2)


def _principal(
    *,
    role: Role = Role.OWNER,
    mfa_method: MfaMethod | None = MfaMethod.TOTP,
    mfa_verified_at: datetime | None = None,
) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=ACTOR_ID,
        workspace_id=WORKSPACE_ID,
        roles=frozenset({role}),
        mfa_verified_at=mfa_verified_at or datetime.now(UTC),
        mfa_method=mfa_method,
    )


def _service_module():
    name = "trading_bot.application.learning_incident_entry_hold"
    assert importlib.util.find_spec(name) is not None, "Chưa có application service incident hold"
    return importlib.import_module(name)


def _eligible():
    from tests.unit.test_learning_incident_entry_hold import _target

    now = datetime.now(UTC)
    observation = DriftObservation(
        observation_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=uuid4(),
        challenger_revision=2,
        observed_at=now - timedelta(minutes=5),
        snapshot_hash="a" * 64,
        snapshot_coverage=0.4,
        snapshot_quality_ok=True,
        baseline_available=True,
        schema_compatible=True,
        rights_eligible=True,
        challenger_decision=ScoreDecision.APPROVE,
        challenger_reason=None,
    )
    assessment = assess_learning_drift(
        workspace_id=WORKSPACE_ID,
        assessment_id=uuid4(),
        policy=DriftPolicy("coverage-v2-30d", 1, 0.8, 0.9),
        observations=(observation,),
        created_at=now - timedelta(minutes=4),
    )
    proposal = create_learning_retrain_proposal(
        assessment,
        proposal_id=uuid4(),
        created_at=now - timedelta(minutes=3),
    )
    target = _target()
    repository_module = importlib.import_module(
        "trading_bot.adapters.database.repositories.learning_incident_entry_holds"
    )
    return repository_module.EligibleLearningIncidentTarget(
        proposal=proposal,
        assessment=assessment,
        target=target,
        incumbent_artifact_hash="8" * 64,
    )


class _UnitOfWork:
    def __init__(self) -> None:
        self.session = object()
        self.request_id = uuid4()
        self.audits: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def record_audit(self, **values: object) -> None:
        self.audits.append(values)


class _Repository:
    def __init__(self, eligible=None, history=(), existing=None) -> None:
        self.eligible = eligible
        self.history = tuple(history)
        self.existing = existing
        self.proposal_ids = () if eligible is None else (eligible.proposal.proposal_id,)
        self.lock_calls: list[dict[str, object]] = []
        self.lookup_calls: list[tuple[object, object]] = []
        self.eligible_calls = 0
        self.appended = []

    async def lock_runtime_entry_scope(self, **kwargs) -> None:
        self.lock_calls.append(kwargs)

    async def find_by_idempotency_key(self, **kwargs):
        self.lookup_calls.append((kwargs["workspace_id"], kwargs["idempotency_key"]))
        if self.existing is not None and self.existing.idempotency_key == kwargs["idempotency_key"]:
            return self.existing
        return None

    async def eligible_incident_target(self, **_kwargs):
        self.eligible_calls += 1
        if self.eligible is None:
            raise LookupError("Không có proposal eligible")
        return self.eligible

    async def list_history(self, *, proposal_id=None, **_kwargs):
        if proposal_id is None:
            return self.history
        return tuple(item for item in self.history if item.proposal_id == proposal_id)

    async def list_open_holds(self, **_kwargs):
        return tuple(item for item in self.history if item.action.value == "HOLD")

    async def list_recent_proposal_ids(self, **_kwargs):
        return self.proposal_ids

    async def append_event(self, *, workspace_id, event):
        assert workspace_id == event.workspace_id
        self.appended.append(event)
        return importlib.import_module(
            "trading_bot.adapters.database.repositories.learning_incident_entry_holds"
        ).LearningIncidentHoldAppendResult(event=event, is_duplicate=False)


def _build_service(repository, *, principal=None, unit=None):
    service_module = _service_module()
    unit = unit or _UnitOfWork()
    service = service_module.LearningIncidentEntryHoldService(
        object(),
        unit_of_work_factory=lambda *_args, **_kwargs: unit,
        repository_factory=lambda _session: repository,
    )
    principal = principal or _principal()
    return service.for_principal(principal), principal, unit


def _hold_request(
    eligible, *, expected_revision=0, idempotency_key=None, reason="Xác nhận thiếu coverage"
):
    return {
        "proposal_id": eligible.proposal.proposal_id,
        "assessment_id": eligible.assessment.assessment_id,
        "assessment_hash": eligible.assessment.content_hash,
        "expected_revision": expected_revision,
        "expected_deployment_revision": eligible.target.deployment_revision,
        "expected_active_pointer_version": eligible.target.active_pointer_version,
        "idempotency_key": idempotency_key or uuid4(),
        "reason": reason,
    }


def test_learning_incident_service_accepts_owner_and_risk_admin_only() -> None:
    repository = _Repository(_eligible())
    for role in (Role.OWNER, Role.RISK_ADMIN):
        facade, principal, _unit = _build_service(repository, principal=_principal(role=role))
        assert facade._principal is principal
    with pytest.raises(PermissionError, match="incident"):
        _build_service(repository, principal=_principal(role=Role.ANALYST_REVIEWER))
    with pytest.raises(PermissionError, match="incident"):
        _build_service(repository, principal=_principal(role=Role.OPERATOR))


@pytest.mark.asyncio
async def test_actionable_proposals_are_principal_scoped_current_and_unheld() -> None:
    eligible = _eligible()
    repository = _Repository(eligible)
    facade, principal, unit = _build_service(repository, principal=_principal(role=Role.RISK_ADMIN))

    candidates = await facade.list_actionable_proposals()

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposal_id == eligible.proposal.proposal_id
    assert candidate.assessment_id == eligible.assessment.assessment_id
    assert candidate.assessment_hash == eligible.assessment.content_hash
    assert candidate.expected_revision == 0
    assert candidate.expected_deployment_revision == eligible.target.deployment_revision
    assert candidate.expected_active_pointer_version == eligible.target.active_pointer_version
    assert repository.proposal_ids == (candidate.proposal_id,)
    assert principal.workspace_id == WORKSPACE_ID
    assert unit is not None


@pytest.mark.asyncio
async def test_actionable_proposals_skip_existing_ledger_and_stale_current_target() -> None:
    from tests.unit.test_learning_incident_entry_hold import _hold

    from trading_bot.adapters.database.repositories.learning_incident_entry_holds import (
        LearningIncidentHoldConflict,
    )

    existing = _hold()
    held_repository = _Repository(history=(existing,))
    held_repository.proposal_ids = (existing.proposal_id,)
    held_service, _principal, _unit = _build_service(held_repository)
    assert await held_service.list_actionable_proposals() == ()
    assert held_repository.eligible_calls == 0

    stale_repository = _Repository()
    stale_repository.proposal_ids = (uuid4(),)

    async def stale_target(**_kwargs):
        raise LearningIncidentHoldConflict("scorer target đã đổi")

    stale_repository.eligible_incident_target = stale_target
    stale_service, _principal, _unit = _build_service(stale_repository)
    assert await stale_service.list_actionable_proposals() == ()


@pytest.mark.asyncio
async def test_hold_uses_principal_scope_and_writes_ledger_and_audit_together() -> None:
    eligible = _eligible()
    repository = _Repository(eligible)
    facade, principal, unit = _build_service(repository, principal=_principal(role=Role.RISK_ADMIN))
    request = _hold_request(eligible)

    event = await facade.hold(**request)

    assert event.proposal_id == eligible.proposal.proposal_id
    assert event.workspace_id == principal.workspace_id
    assert event.actor_membership_id == principal.membership_id
    assert event.assessment_hash == eligible.assessment.content_hash
    assert event.incumbent_artifact_hash == eligible.incumbent_artifact_hash
    assert event.incumbent_artifact_hash != eligible.target.target.scorer_artifact_hash
    assert repository.lock_calls == [{"workspace_id": principal.workspace_id}]
    assert repository.eligible_calls == 1
    assert repository.appended == [event]
    assert len(unit.audits) == 1
    assert unit.audits[0]["action"] == "learning.incident_entry_hold.hold"
    assert unit.audits[0]["target_id"] == event.proposal_id


@pytest.mark.asyncio
async def test_hold_exact_retry_returns_original_before_target_and_cas_checks() -> None:
    eligible = _eligible()
    request = _hold_request(eligible)
    from trading_bot.domain.learning.incident_entry_hold import create_hold_event

    principal = _principal()
    now = datetime.now(UTC)
    original = create_hold_event(
        event_id=uuid4(),
        workspace_id=principal.workspace_id,
        proposal=eligible.proposal,
        assessment=eligible.assessment,
        target=eligible.target,
        incumbent_artifact_hash=eligible.incumbent_artifact_hash,
        revision=1,
        previous_event=None,
        actor_membership_id=principal.membership_id,
        mfa_verified_at=principal.mfa_verified_at,
        mfa_method=principal.mfa_method,
        reason=request["reason"],
        idempotency_key=request["idempotency_key"],
        request_id=str(uuid4()),
        created_at=now,
    )
    repository = _Repository(existing=original)
    facade, ignored_principal, unit = _build_service(repository, principal=principal)

    event = await facade.hold(**request)

    assert event is original
    assert ignored_principal is principal
    assert repository.eligible_calls == 0
    assert repository.appended == []
    assert unit.audits == []


@pytest.mark.asyncio
async def test_reused_hold_key_with_changed_reason_conflicts_before_eligibility() -> None:
    eligible = _eligible()
    request = _hold_request(eligible)
    from trading_bot.domain.learning.incident_entry_hold import create_hold_event

    principal = _principal()
    original = create_hold_event(
        event_id=uuid4(),
        workspace_id=principal.workspace_id,
        proposal=eligible.proposal,
        assessment=eligible.assessment,
        target=eligible.target,
        incumbent_artifact_hash=eligible.incumbent_artifact_hash,
        revision=1,
        previous_event=None,
        actor_membership_id=principal.membership_id,
        mfa_verified_at=principal.mfa_verified_at,
        mfa_method=principal.mfa_method,
        reason=request["reason"],
        idempotency_key=request["idempotency_key"],
        request_id=str(uuid4()),
        created_at=datetime.now(UTC),
    )
    repository = _Repository(existing=original)
    facade, ignored_principal, _unit = _build_service(repository, principal=principal)

    with pytest.raises(RuntimeError, match="Idempotency"):
        await facade.hold(**(request | {"reason": "Lý do đã bị thay đổi"}))
    assert repository.eligible_calls == 0
    assert repository.appended == []
    assert ignored_principal is principal


@pytest.mark.asyncio
async def test_hold_rejects_assessment_or_active_target_cas_mismatch_without_append() -> None:
    eligible = _eligible()
    repository = _Repository(eligible)
    facade, ignored_principal, unit = _build_service(repository)
    request = _hold_request(eligible)

    with pytest.raises(RuntimeError, match="Assessment|assessment"):
        await facade.hold(**(request | {"assessment_hash": "0" * 64}))
    with pytest.raises(RuntimeError, match="deployment"):
        await facade.hold(**(request | {"expected_deployment_revision": 999}))
    assert repository.appended == []
    assert unit.audits == []
    assert ignored_principal.workspace_id == WORKSPACE_ID


@pytest.mark.asyncio
async def test_hold_rejects_existing_proposal_event_and_invalid_expected_revision() -> None:
    eligible = _eligible()
    from trading_bot.domain.learning.incident_entry_hold import create_hold_event

    principal = _principal()
    existing = create_hold_event(
        event_id=uuid4(),
        workspace_id=principal.workspace_id,
        proposal=eligible.proposal,
        assessment=eligible.assessment,
        target=eligible.target,
        incumbent_artifact_hash=eligible.incumbent_artifact_hash,
        revision=1,
        previous_event=None,
        actor_membership_id=principal.membership_id,
        mfa_verified_at=principal.mfa_verified_at,
        mfa_method=principal.mfa_method,
        reason="Đã có hold trước đó",
        idempotency_key=uuid4(),
        request_id=str(uuid4()),
        created_at=datetime.now(UTC),
    )
    repository = _Repository(eligible, history=(existing,))
    facade, ignored_principal, _unit = _build_service(repository, principal=principal)
    request = _hold_request(eligible, expected_revision=1)

    with pytest.raises(RuntimeError, match="Revision|proposal"):
        await facade.hold(**request)
    assert repository.appended == []
    assert ignored_principal is principal


@pytest.mark.asyncio
async def test_release_uses_ledger_without_rechecking_changed_active_target() -> None:
    eligible = _eligible()
    principal = _principal(role=Role.RISK_ADMIN)
    from trading_bot.domain.learning.incident_entry_hold import create_hold_event

    hold = create_hold_event(
        event_id=uuid4(),
        workspace_id=principal.workspace_id,
        proposal=eligible.proposal,
        assessment=eligible.assessment,
        target=eligible.target,
        incumbent_artifact_hash=eligible.incumbent_artifact_hash,
        revision=1,
        previous_event=None,
        actor_membership_id=principal.membership_id,
        mfa_verified_at=principal.mfa_verified_at,
        mfa_method=principal.mfa_method,
        reason="Sự cố cần rà soát",
        idempotency_key=uuid4(),
        request_id=str(uuid4()),
        created_at=datetime.now(UTC),
    )
    repository = _Repository(history=(hold,))
    facade, ignored_principal, unit = _build_service(repository, principal=principal)

    release = await facade.release(
        proposal_id=hold.proposal_id,
        expected_revision=1,
        idempotency_key=uuid4(),
        reason="Đã xử lý incident",
    )

    assert release.action.value == "RELEASE"
    assert release.previous_event_hash == hold.event_hash
    assert repository.eligible_calls == 0
    assert repository.appended == [release]
    assert len(unit.audits) == 1
    assert ignored_principal is principal


@pytest.mark.asyncio
async def test_exact_release_retry_returns_original_without_duplicate_audit() -> None:
    eligible = _eligible()
    from trading_bot.domain.learning.incident_entry_hold import (
        create_hold_event,
        create_release_event,
    )

    principal = _principal()
    hold = create_hold_event(
        event_id=uuid4(),
        workspace_id=principal.workspace_id,
        proposal=eligible.proposal,
        assessment=eligible.assessment,
        target=eligible.target,
        incumbent_artifact_hash=eligible.incumbent_artifact_hash,
        revision=1,
        previous_event=None,
        actor_membership_id=principal.membership_id,
        mfa_verified_at=principal.mfa_verified_at,
        mfa_method=principal.mfa_method,
        reason="Sự cố cần rà soát",
        idempotency_key=uuid4(),
        request_id=str(uuid4()),
        created_at=datetime.now(UTC),
    )
    release = create_release_event(
        event_id=uuid4(),
        previous_event=hold,
        actor_membership_id=principal.membership_id,
        mfa_verified_at=principal.mfa_verified_at,
        mfa_method=principal.mfa_method,
        reason="Đã xử lý incident",
        idempotency_key=uuid4(),
        request_id=str(uuid4()),
        created_at=datetime.now(UTC),
    )
    repository = _Repository(existing=release)
    facade, ignored_principal, unit = _build_service(repository, principal=principal)

    event = await facade.release(
        proposal_id=hold.proposal_id,
        expected_revision=1,
        idempotency_key=release.idempotency_key,
        reason=release.reason,
    )

    assert event is release
    assert repository.appended == []
    assert unit.audits == []
    assert ignored_principal is principal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mfa_method", "offset"),
    [
        (None, timedelta(0)),
        (MfaMethod.TOTP, -timedelta(minutes=16)),
        (MfaMethod.TOTP, timedelta(seconds=1)),
    ],
)
async def test_mutations_reject_missing_stale_or_future_mfa_before_database(
    mfa_method: MfaMethod | None, offset: timedelta
) -> None:
    repository = _Repository(_eligible())
    principal = _principal(
        mfa_method=mfa_method,
        mfa_verified_at=datetime.now(UTC) + offset,
    )
    facade, ignored_principal, _unit = _build_service(repository, principal=principal)

    with pytest.raises(PermissionError, match="TOTP gần đây"):
        await facade.hold(**_hold_request(repository.eligible))
    assert repository.lock_calls == []
    assert repository.eligible_calls == 0
    assert ignored_principal is principal


@pytest.mark.asyncio
async def test_history_is_metadata_only_and_workspace_scoped() -> None:
    principal = _principal(role=Role.RISK_ADMIN)
    repository = _Repository(history=())
    facade, ignored_principal, _unit = _build_service(repository, principal=principal)

    assert await facade.list_history(limit=50) == ()
    assert ignored_principal is principal
