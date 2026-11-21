from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
    LearningPaperEntryBlockAppendResult,
    LearningPaperEntryBlockConflict,
)
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode


@pytest.fixture
def operator():
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=1),
        mfa_method=MfaMethod.TOTP,
    )


@pytest.fixture
def scope(operator, monkeypatch):
    from trading_bot.application import learning_paper_entry_block as module

    state = SimpleNamespace(
        decision_id=uuid4(),
        key=uuid4(),
        event=None,
        has_order_intent=False,
        already_blocked=False,
        trace=[],
        candidate=SimpleNamespace(
            bot_id=uuid4(),
            bot_decision_hash="a" * 64,
            candidate_id="b" * 64,
            score_event_id=uuid4(),
            score_event_hash="c" * 64,
            deployment_event_id=uuid4(),
            source_rights_fingerprint="e" * 64,
            deployment_revision=3,
            registry_id=uuid4(),
            registry_revision=4,
            active_pointer_version=5,
            scorer_artifact_id=uuid4(),
            scorer_artifact_hash="d" * 64,
        ),
    )

    state.active = SimpleNamespace(
        mode=ScorerDeploymentMode.PAPER_GATE,
        deployment_event_id=state.candidate.deployment_event_id,
        deployment_revision=3,
        source_rights_fingerprint=state.candidate.source_rights_fingerprint,
        target=SimpleNamespace(
            registry_id=state.candidate.registry_id,
            registry_revision=4,
            active_pointer_version=5,
            scorer_artifact_id=state.candidate.scorer_artifact_id,
            scorer_artifact_hash="d" * 64,
        ),
    )

    class Unit:
        def __init__(
            self,
            sessions,
            *,
            workspace_id,
            actor_membership_id,
            snapshot_read_only=False,
        ):
            assert sessions is scope_sessions
            assert workspace_id == operator.workspace_id
            assert actor_membership_id == operator.membership_id
            self.snapshot_read_only = snapshot_read_only
            self.session = object()

        async def __aenter__(self):
            state.trace.append("transaction")
            return self

        async def __aexit__(self, exc_type, *_):
            state.trace.append("commit" if exc_type is None else "rollback")

    class Settings:
        def __init__(self, _session):
            pass

        async def lock_workspace(self, workspace_id):
            assert workspace_id == operator.workspace_id
            state.trace.append("L1")

    class Ledger:
        def __init__(self, _session):
            pass

        async def lock_decision(self, *, workspace_id, bot_decision_id):
            assert workspace_id == operator.workspace_id
            assert bot_decision_id == state.decision_id
            state.trace.append("decision-lock")

        async def find_by_idempotency_key(self, *, workspace_id, idempotency_key):
            assert workspace_id == operator.workspace_id
            if state.event is not None and state.event.idempotency_key == idempotency_key:
                return state.event
            return None

        async def find_by_decision(self, *, workspace_id, bot_decision_id):
            assert workspace_id == operator.workspace_id
            assert bot_decision_id == state.decision_id
            state.trace.append("existing-block")
            return (state.event or object()) if state.already_blocked else None

        async def load_blockable_evidence(self, *, workspace_id, bot_decision_id):
            assert workspace_id == operator.workspace_id
            assert bot_decision_id == state.decision_id
            state.trace.append("candidate")
            return state.candidate

        async def has_paper_order_intent(self, *, workspace_id, bot_id, bot_decision_id):
            assert workspace_id == operator.workspace_id
            assert bot_id == state.candidate.bot_id
            assert bot_decision_id == state.decision_id
            state.trace.append("intent")
            return state.has_order_intent

        async def append_block(self, *, workspace_id, event):
            assert workspace_id == operator.workspace_id
            state.trace.append("append")
            if state.event is not None:
                raise LearningPaperEntryBlockConflict("Quyết định đã có bản ghi chặn.")
            state.event = event
            return LearningPaperEntryBlockAppendResult(event, False)

    class Target:
        def __init__(self, _session):
            pass

        async def resolve_active(self, *, workspace_id):
            assert workspace_id == operator.workspace_id
            state.trace.append("target")
            return state.active

    scope_sessions = object()
    monkeypatch.setattr(module, "UnitOfWork", Unit)
    monkeypatch.setattr(module, "WorkspaceSettingsRepository", Settings)
    monkeypatch.setattr(module, "LearningPaperEntryBlockRepository", Ledger)
    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", Target)
    state.service = module.LearningPaperEntryBlockService(scope_sessions)
    return state


async def _block(scope, principal, *, reason="  Kiểm tra lại tín hiệu  ", key=None):
    return await scope.service.for_principal(principal).block(
        bot_decision_id=scope.decision_id,
        reason=reason,
        idempotency_key=scope.key if key is None else key,
    )


async def _eligible_decisions(service, values):
    method = getattr(service, "eligible_decision_ids", None)
    assert callable(method), "Service cần có phép đọc eligibility chỉ phục vụ giao diện."
    return await method(values)


@pytest.mark.parametrize("role", [Role.OWNER, Role.RISK_ADMIN])
async def test_block_persists_operator_and_authoritative_evidence_under_l1(scope, operator, role):
    event = await _block(scope, replace(operator, roles=frozenset({role})))

    assert scope.trace == [
        "transaction",
        "L1",
        "decision-lock",
        "candidate",
        "intent",
        "target",
        "append",
        "commit",
    ]
    assert event.workspace_id == operator.workspace_id
    assert event.actor_user_id == operator.user_id
    assert event.mfa_verified_at == operator.mfa_verified_at
    assert event.reason == "Kiểm tra lại tín hiệu"
    assert event.bot_decision_id == scope.decision_id
    assert event.idempotency_key == scope.key
    assert event.bot_id == scope.candidate.bot_id
    assert event.bot_decision_hash == scope.candidate.bot_decision_hash
    assert event.candidate_id == scope.candidate.candidate_id
    assert event.score_event_id == scope.candidate.score_event_id
    assert event.score_event_hash == scope.candidate.score_event_hash
    assert event.deployment_event_id == scope.active.deployment_event_id
    assert event.deployment_revision == 3
    assert event.registry_id == scope.active.target.registry_id
    assert event.registry_revision == 4
    assert event.active_pointer_version == 5
    assert event.scorer_artifact_id == scope.active.target.scorer_artifact_id
    assert event.scorer_artifact_hash == "d" * 64


async def test_read_only_eligibility_uses_shared_active_target_predicate(scope, operator):
    service = scope.service.for_principal(operator)

    eligible = await _eligible_decisions(service, [scope.decision_id])

    assert eligible == frozenset({scope.decision_id})
    assert scope.trace == [
        "transaction",
        "target",
        "existing-block",
        "candidate",
        "intent",
        "commit",
    ]

    scope.trace.clear()
    scope.active.target.scorer_artifact_hash = "f" * 64
    assert await _eligible_decisions(service, [scope.decision_id]) == frozenset()
    assert "target" in scope.trace
    assert scope.trace[-1] == "commit"


@pytest.mark.parametrize("ineligible", ["existing_block", "order_intent", "missing_evidence"])
async def test_read_only_eligibility_hides_blocked_or_unavailable_decision(
    scope, operator, ineligible
):
    if ineligible == "existing_block":
        scope.already_blocked = True
    elif ineligible == "order_intent":
        scope.has_order_intent = True
    else:
        scope.candidate = None

    eligible = await _eligible_decisions(scope.service.for_principal(operator), [scope.decision_id])

    assert eligible == frozenset()
    assert scope.event is None
    assert scope.trace[-1] == "commit"


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.STRATEGY_ADMIN, Role.ANALYST_REVIEWER])
async def test_unprivileged_operator_cannot_acquire_locks_or_write(scope, operator, role):
    with pytest.raises(PermissionError):
        await _block(scope, replace(operator, roles=frozenset({role})))

    assert scope.trace == []


@pytest.mark.parametrize(
    ("method", "age"),
    [(None, 1), (MfaMethod.TOTP, 16), (MfaMethod.TOTP, -1)],
)
async def test_block_requires_current_totp(scope, operator, method, age):
    principal = replace(
        operator,
        mfa_method=method,
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=age),
    )
    with pytest.raises(PermissionError, match="TOTP"):
        await _block(scope, principal)

    assert scope.trace == []


@pytest.mark.parametrize("reason", ["   ", "x" * 501, "Kiểm tra\x00"])
async def test_invalid_block_reason_cannot_write(scope, operator, reason):
    with pytest.raises(ValueError, match="reason"):
        await _block(scope, operator, reason=reason)

    assert scope.trace == []


async def test_zero_uuid_key_cannot_write(scope, operator):
    with pytest.raises(ValueError, match="UUID khác zero"):
        await _block(scope, operator, key=UUID(int=0))

    assert scope.trace == []


async def test_matching_retry_returns_immutable_audit_after_target_rotates(scope, operator):
    original = await _block(scope, operator)
    scope.active = None
    scope.trace.clear()

    retry = await _block(scope, operator, reason="Kiểm tra lại tín hiệu")

    assert retry == original
    assert scope.trace == ["transaction", "L1", "decision-lock", "commit"]


async def test_retry_cannot_reuse_key_for_other_intent_or_actor(scope, operator):
    original = await _block(scope, operator)

    with pytest.raises(LearningPaperEntryBlockConflict):
        await _block(scope, operator, reason="Lý do khác")
    with pytest.raises(LearningPaperEntryBlockConflict):
        await _block(scope, replace(operator, user_id=uuid4()))
    with pytest.raises(LearningPaperEntryBlockConflict):
        await _block(scope, operator, key=uuid4())

    assert scope.event == original


async def test_existing_order_intent_rejects_block_without_appending(scope, operator):
    scope.has_order_intent = True

    with pytest.raises(LearningPaperEntryBlockConflict, match="đã gửi"):
        await _block(scope, operator)

    assert scope.trace == ["transaction", "L1", "decision-lock", "candidate", "intent", "rollback"]
    assert scope.event is None


async def test_missing_eligible_score_in_principal_scope_rejects_block(scope, operator):
    scope.candidate = None

    with pytest.raises(LearningPaperEntryBlockConflict):
        await _block(scope, operator)

    assert scope.event is None
    assert scope.trace[-1] == "rollback"


@pytest.mark.parametrize(
    "drift",
    [
        "disabled",
        "shadow",
        "deployment",
        "deployment-revision",
        "source-rights",
        "registry",
        "registry-revision",
        "pointer",
        "artifact",
        "artifact-hash",
    ],
)
async def test_current_target_must_match_score_lineage(scope, operator, drift):
    if drift == "disabled":
        scope.active = None
    elif drift == "shadow":
        scope.active.mode = ScorerDeploymentMode.SHADOW
    elif drift == "deployment":
        scope.active.deployment_event_id = uuid4()
    elif drift == "deployment-revision":
        scope.active.deployment_revision += 1
    elif drift == "source-rights":
        scope.active.source_rights_fingerprint = "f" * 64
    elif drift == "registry":
        scope.active.target.registry_id = uuid4()
    elif drift == "registry-revision":
        scope.active.target.registry_revision += 1
    elif drift == "pointer":
        scope.active.target.active_pointer_version += 1
    elif drift == "artifact":
        scope.active.target.scorer_artifact_id = uuid4()
    else:
        scope.active.target.scorer_artifact_hash = "f" * 64

    with pytest.raises(LearningPaperEntryBlockConflict):
        await _block(scope, operator)

    assert scope.event is None
    assert scope.trace[-2:] == ["target", "rollback"]
