from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trading_bot.domain.learning.accounting import (
    MAX_TRIALS_PER_PARAMETER_FAMILY,
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptReasonCode,
    LearningAttemptState,
    LearningTrainingAttempt,
    validate_learning_attempt_transition,
)
from trading_bot.domain.learning.resources import (
    LearningResourceLimitCode,
    LearningResourceRejected,
    LearningResourceRequest,
    assess_learning_resource_request,
)


def _event(
    state: LearningAttemptState,
    *,
    attempt_id: UUID | None = None,
    sequence: int = 1,
    reason_code: LearningAttemptReasonCode | None = None,
    elapsed_milliseconds: int | None = None,
    peak_memory_bytes: int | None = None,
) -> LearningAttemptEvent:
    return LearningAttemptEvent(
        attempt_id=attempt_id or uuid4(),
        sequence=sequence,
        state=state,
        occurred_at=datetime.now(UTC),
        actor_principal=LearningAttemptActor.TRAINER_WORKER,
        reason_code=reason_code,
        elapsed_milliseconds=elapsed_milliseconds,
        peak_memory_bytes=peak_memory_bytes,
    )


def _valid_event(
    state: LearningAttemptState, attempt_id: UUID, sequence: int
) -> LearningAttemptEvent:
    reasons = {
        LearningAttemptState.REJECTED: LearningAttemptReasonCode.RESOURCE_LIMIT,
        LearningAttemptState.FAILED: LearningAttemptReasonCode.ADAPTER_ERROR,
        LearningAttemptState.CANCELLED: LearningAttemptReasonCode.CANCELLED_BY_OPERATOR,
    }
    terminal = {
        LearningAttemptState.SUCCEEDED,
        LearningAttemptState.FAILED,
        LearningAttemptState.CANCELLED,
    }
    return _event(
        state,
        attempt_id=attempt_id,
        sequence=sequence,
        reason_code=reasons.get(state),
        elapsed_milliseconds=0 if state in terminal else None,
    )


def test_attempt_lifecycle_accepts_queued_running_terminal_sequence() -> None:
    attempt_id = uuid4()
    queued = _event(LearningAttemptState.QUEUED, attempt_id=attempt_id)
    running = _event(LearningAttemptState.RUNNING, attempt_id=attempt_id, sequence=2)
    succeeded = _event(
        LearningAttemptState.SUCCEEDED,
        attempt_id=attempt_id,
        sequence=3,
        elapsed_milliseconds=250,
        peak_memory_bytes=1024,
    )

    validate_learning_attempt_transition(None, queued)
    validate_learning_attempt_transition(queued, running)
    validate_learning_attempt_transition(running, succeeded)


@pytest.mark.parametrize(
    ("previous", "next_state"),
    [
        (None, LearningAttemptState.RUNNING),
        (None, LearningAttemptState.SUCCEEDED),
        (LearningAttemptState.QUEUED, LearningAttemptState.SUCCEEDED),
        (LearningAttemptState.REJECTED, LearningAttemptState.QUEUED),
        (LearningAttemptState.RUNNING, LearningAttemptState.QUEUED),
        (LearningAttemptState.SUCCEEDED, LearningAttemptState.RUNNING),
        (LearningAttemptState.FAILED, LearningAttemptState.SUCCEEDED),
        (LearningAttemptState.CANCELLED, LearningAttemptState.RUNNING),
    ],
)
def test_attempt_lifecycle_rejects_invalid_transitions(
    previous: LearningAttemptState | None,
    next_state: LearningAttemptState,
) -> None:
    attempt_id = uuid4()
    previous_event = _valid_event(previous, attempt_id, 1) if previous is not None else None
    sequence = previous_event.sequence + 1 if previous_event is not None else 1
    next_event = _valid_event(next_state, attempt_id, sequence)

    with pytest.raises(ValueError, match="Chuyển trạng thái"):
        validate_learning_attempt_transition(previous_event, next_event)


def test_rejected_and_failed_events_require_redacted_reason_code() -> None:
    for state in (LearningAttemptState.REJECTED, LearningAttemptState.FAILED):
        with pytest.raises(ValueError, match="mã lý do"):
            _event(state)

    failed = _event(
        LearningAttemptState.FAILED,
        reason_code=LearningAttemptReasonCode.ADAPTER_ERROR,
        elapsed_milliseconds=1,
    )
    assert failed.reason_code is LearningAttemptReasonCode.ADAPTER_ERROR


@pytest.mark.parametrize(
    ("state", "reason_code", "elapsed_milliseconds", "peak_memory_bytes"),
    [
        (LearningAttemptState.QUEUED, None, 0, None),
        (LearningAttemptState.SUCCEEDED, None, -1, None),
        (LearningAttemptState.SUCCEEDED, None, 1, -1),
    ],
)
def test_attempt_events_reject_invalid_measurements(
    state: LearningAttemptState,
    reason_code: LearningAttemptReasonCode | None,
    elapsed_milliseconds: int | None,
    peak_memory_bytes: int | None,
) -> None:
    with pytest.raises(
        ValueError, match="Chỉ event terminal|elapsed_milliseconds|peak_memory_bytes"
    ):
        _event(
            state,
            reason_code=reason_code,
            elapsed_milliseconds=elapsed_milliseconds,
            peak_memory_bytes=peak_memory_bytes,
        )


def test_attempt_event_rejects_free_form_reason_text() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        _event(
            LearningAttemptState.FAILED,
            reason_code="database password leaked",  # type: ignore[arg-type]
            elapsed_milliseconds=1,
        )


def test_attempt_event_requires_sequence_and_utc_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="sequence"):
        LearningAttemptEvent(
            attempt_id=uuid4(),
            sequence=True,
            state=LearningAttemptState.QUEUED,
            occurred_at=datetime.now(UTC),
            actor_principal=LearningAttemptActor.TRAINER_WORKER,
        )


def _attempt(*, trial_count: int = 3) -> LearningTrainingAttempt:
    request = LearningResourceRequest(10, 2, 4, 100, 30, 4 * 1024 * 1024)
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
        trial_count=trial_count,
        fold_id="e" * 64,
        seed=42,
        trainer_version="cpu-ovr-v1",
        dependency_fingerprint="f" * 64,
        resource_policy_version="resource-v1",
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
    )


def test_training_attempt_binds_dataset_family_fold_versions_and_resource_estimates() -> None:
    attempt = _attempt()
    roundtrip_hash = LearningTrainingAttempt(
        **{field: getattr(attempt, field) for field in attempt.__dataclass_fields__}
    )

    assert attempt.initial_state is LearningAttemptState.QUEUED
    assert attempt.canonical_json == roundtrip_hash.canonical_json
    assert attempt.content_hash == roundtrip_hash.content_hash
    assert attempt.trial_count == 3


def test_resource_rejection_can_be_recorded_without_running_an_adapter() -> None:
    request = LearningResourceRequest(10_001, 0, 1, 1, 1, 1024 * 1024)
    with pytest.raises(LearningResourceRejected) as rejected:
        assess_learning_resource_request(request)
    assert rejected.value.code is LearningResourceLimitCode.SAMPLE_COUNT_EXCEEDED

    attempt = _attempt()
    rejected_attempt = LearningTrainingAttempt(
        **{
            **{
                field: getattr(attempt, field)
                for field in attempt.__dataclass_fields__
                if field not in {"resource_request", "resource_plan", "rejection_reason_code"}
            },
            "resource_request": request,
            "resource_plan": None,
            "rejection_reason_code": LearningAttemptReasonCode.RESOURCE_LIMIT,
        }
    )
    assert rejected_attempt.initial_state is LearningAttemptState.REJECTED
    assert "resource_limit" in rejected_attempt.canonical_json


def test_parameter_family_trial_count_has_a_hard_ceiling() -> None:
    with pytest.raises(ValueError, match="trial_index/count"):
        _attempt(trial_count=MAX_TRIALS_PER_PARAMETER_FAMILY + 1)
    with pytest.raises(ValueError, match="múi giờ"):
        LearningAttemptEvent(
            attempt_id=uuid4(),
            sequence=1,
            state=LearningAttemptState.QUEUED,
            occurred_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
            actor_principal=LearningAttemptActor.TRAINER_WORKER,
        )
