from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint

from trading_bot.adapters.database.models import Base
from trading_bot.adapters.database.models.learning_attempt_entities import (
    LearningAttemptEventRecord,
    LearningFoldResourceProfileRecord,
    LearningTrainingAttemptRecord,
)
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptReasonCode,
    LearningAttemptState,
    LearningTrainingAttempt,
)
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)


def _attempt() -> LearningTrainingAttempt:
    request = LearningResourceRequest(8, 2, 3, 100, 30, 2 * 1024 * 1024)
    return LearningTrainingAttempt(
        id=uuid4(),
        workspace_id=uuid4(),
        actor_membership_id=uuid4(),
        idempotency_key=uuid4(),
        manifest_hash="a" * 64,
        split_policy_version="split-v2",
        fold_plan_hash="b" * 64,
        approval_hash="c" * 64,
        parameter_family_hash="d" * 64,
        trial_index=0,
        trial_count=1,
        fold_id="e" * 64,
        seed=7,
        trainer_version="cpu-ovr-v1",
        dependency_fingerprint="f" * 64,
        resource_policy_version="budget-v1",
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
    )


def test_attempt_model_round_trips_canonical_request_and_rejection() -> None:
    attempt = _attempt()

    record = LearningTrainingAttemptRecord.from_domain(
        attempt,
        normalized_dataset_id=uuid4(),
    )

    assert record.to_domain() == attempt
    assert record.canonical_request == attempt.canonical_json
    assert record.content_hash == attempt.content_hash
    assert record.initial_state == "queued"


def test_attempt_model_rejects_malformed_persisted_json_before_reconstruction() -> None:
    record = LearningTrainingAttemptRecord.from_domain(_attempt(), normalized_dataset_id=uuid4())
    record.resource_request = []

    with pytest.raises(ValueError, match="resource_request"):
        record.to_domain()


def test_rejected_attempt_resource_plan_uses_sql_null_not_json_null() -> None:
    request = LearningResourceRequest(8, 2, 3, 2_001, 30, 2 * 1024 * 1024)
    attempt = replace(
        _attempt(),
        resource_request=request,
        resource_plan=None,
        rejection_reason_code=LearningAttemptReasonCode.RESOURCE_LIMIT,
    )

    record = LearningTrainingAttemptRecord.from_domain(attempt, normalized_dataset_id=uuid4())

    assert record.resource_plan is None
    assert LearningTrainingAttemptRecord.__table__.c.resource_plan.type.none_as_null is True
    assert record.to_domain() == attempt


def test_event_model_round_trips_redacted_cost_fields() -> None:
    event = LearningAttemptEvent(
        attempt_id=uuid4(),
        sequence=1,
        state=LearningAttemptState.REJECTED,
        occurred_at=datetime.now(UTC),
        actor_principal=LearningAttemptActor.ADMIN_WEB,
        reason_code=LearningAttemptReasonCode.RESOURCE_LIMIT,
        peak_memory_bytes=0,
        actual_operation_units=0,
    )

    assert (
        LearningAttemptEventRecord.from_domain(workspace_id=uuid4(), event=event).to_domain()
        == event
    )


def test_learning_accounting_tables_are_in_schema_with_tenant_constraints() -> None:
    assert {
        "learning_fold_resource_profile",
        "learning_training_attempt",
        "learning_attempt_event",
    } <= set(Base.metadata.tables)
    attempt = LearningTrainingAttemptRecord.__table__
    event = LearningAttemptEventRecord.__table__
    profile = LearningFoldResourceProfileRecord.__table__

    assert attempt.c.workspace_id.foreign_keys
    assert event.c.workspace_id.foreign_keys
    assert profile.c.workspace_id.foreign_keys
    assert attempt.c.canonical_request.type.__class__.__name__ == "Text"
    assert event.c.sequence.type.__class__.__name__ == "Integer"
    assert profile.c.feature_count.type.__class__.__name__ == "Integer"
    attempt_unique_keys = {
        tuple(constraint.columns.keys())
        for constraint in attempt.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("workspace_id", "parameter_family_hash", "trial_index", "fold_id") in (
        attempt_unique_keys
    )
