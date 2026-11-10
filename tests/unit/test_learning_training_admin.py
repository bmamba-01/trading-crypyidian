from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.learning_training_admin import (
    LearningTrainingAdminRepository,
)
from trading_bot.application import learning_training_admin as admin_application
from trading_bot.auth.policy import Permission, Principal, Role, authorize


class _FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def lock_idempotency_scope(self, **kwargs: object) -> None:
        self.calls.append(("lock_idempotency_scope", kwargs))

    async def get_attempt_by_idempotency(self, **_kwargs: object) -> None:
        return None

    async def get_fold_option(self, **kwargs: object) -> object:
        self.calls.append(("get_fold_option", kwargs))
        return admin_application.LearningTrainingFoldOption(
            normalized_dataset_id=kwargs["normalized_dataset_id"],
            manifest_hash="b" * 64,
            split_policy_version="split-v2",
            fold_plan_hash="c" * 64,
            approval_hash="d" * 64,
            fold_id=kwargs["fold_id"],
            training_samples=4,
            validation_samples=2,
            feature_count=3,
            profile_hash="e" * 64,
        )

    async def lock_trial_family(self, **kwargs: object) -> None:
        self.calls.append(("lock_trial_family", kwargs))

    async def next_trial_index(self, **_kwargs: object) -> int:
        return 0

    async def get_job_for_attempt(self, **_kwargs: object) -> None:
        return None


class _FakeAttemptRepository:
    def __init__(self) -> None:
        self.attempts: list[object] = []

    async def append_attempt(self, *, attempt: object, **_kwargs: object) -> object:
        self.attempts.append(attempt)
        return type("WriteResult", (), {"attempt": attempt, "is_duplicate": False})()


class _FakeUnitOfWork:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.session = object()

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _principal(*, role: Role, mfa_verified_at: datetime) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=mfa_verified_at,
    )


def test_only_owner_and_analyst_reviewer_can_manage_learning_training_jobs() -> None:
    now = datetime.now(UTC)
    owner = _principal(role=Role.OWNER, mfa_verified_at=now)
    reviewer = _principal(role=Role.ANALYST_REVIEWER, mfa_verified_at=now)
    operator = _principal(role=Role.OPERATOR, mfa_verified_at=now)

    assert authorize(owner, Permission.LEARNING_TRAINING_MANAGE)
    assert authorize(reviewer, Permission.LEARNING_TRAINING_MANAGE)
    assert not authorize(operator, Permission.LEARNING_TRAINING_MANAGE)


@pytest.mark.asyncio
async def test_enqueue_passes_only_authenticated_scope_and_typed_fold_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    principal = _principal(role=Role.ANALYST_REVIEWER, mfa_verified_at=now)
    repository = _FakeRepository()
    attempt_repository = _FakeAttemptRepository()
    dataset_id = uuid4()
    fold_id = "a" * 64
    idempotency_key = uuid4()
    monkeypatch.setattr(admin_application, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(
        admin_application,
        "LearningTrainingAdminRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        admin_application,
        "LearningAttemptRepository",
        lambda _session: attempt_repository,
    )

    result = await (
        admin_application.LearningTrainingAdminService(object())
        .for_principal(principal)
        .enqueue(
            normalized_dataset_id=dataset_id,
            fold_id=fold_id,
            idempotency_key=idempotency_key,
        )
    )

    assert result.status == "queued"
    assert result.normalized_dataset_id == dataset_id
    assert result.fold_id == fold_id
    assert len(attempt_repository.attempts) == 1
    attempt = attempt_repository.attempts[0]
    assert attempt.workspace_id == principal.workspace_id
    assert attempt.actor_membership_id == principal.membership_id
    assert attempt.idempotency_key == idempotency_key
    assert attempt.resource_request.training_samples == 4
    assert attempt.resource_request.validation_samples == 2
    assert attempt.resource_request.feature_count == 3
    assert not hasattr(attempt, "features")
    assert [call[0] for call in repository.calls] == [
        "lock_idempotency_scope",
        "get_fold_option",
        "lock_trial_family",
    ]


@pytest.mark.asyncio
async def test_enqueue_requires_recent_mfa_before_opening_database_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal(
        role=Role.ANALYST_REVIEWER,
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=16),
    )
    repository = _FakeRepository()
    monkeypatch.setattr(
        admin_application,
        "LearningTrainingAdminRepository",
        lambda _session: repository,
    )

    with pytest.raises(PermissionError, match="MFA"):
        await (
            admin_application.LearningTrainingAdminService(object())
            .for_principal(principal)
            .enqueue(
                normalized_dataset_id=uuid4(),
                fold_id="a" * 64,
                idempotency_key=uuid4(),
            )
        )

    assert repository.calls == []


@pytest.mark.asyncio
async def test_fold_lookup_requires_dataset_header_exact_approval_and_policy_binding() -> None:
    class _Result:
        def all(self) -> list[object]:
            return []

        def one_or_none(self) -> None:
            return None

    class _Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object, *_args: object, **_kwargs: object) -> _Result:
            self.statements.append(statement)
            return _Result()

    session = _Session()
    repository = LearningTrainingAdminRepository(session)  # type: ignore[arg-type]
    workspace_id = uuid4()
    assert (
        await repository.get_fold_option(
            workspace_id=workspace_id,
            normalized_dataset_id=uuid4(),
            fold_id="a" * 64,
        )
        is None
    )

    query = session.statements[-1]
    compiled = str(query.compile(dialect=postgresql.dialect()))
    assert (
        "learning_split_approval.approval_hash = learning_normalized_dataset.approval_hash"
        in compiled
    )
    assert (
        "learning_split_approval.split_policy_version = "
        "learning_normalized_dataset.split_policy_version"
    ) in compiled
    assert (
        "learning_normalized_dataset.id = learning_fold_resource_profile.normalized_dataset_id"
    ) in compiled
    assert "learning_split_approval.canonical_approval" not in compiled
    assert "learning_split_approval.reason" not in compiled
    assert "learning_normalized_sample.features" not in compiled
    assert "learning_fold_assignment" not in compiled
