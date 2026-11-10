from __future__ import annotations

import pytest

from trading_bot.domain.learning.resources import (
    DEFAULT_LEARNING_RESOURCE_LIMITS,
    LearningResourceLimitCode,
    LearningResourceLimits,
    LearningResourceRejected,
    LearningResourceRequest,
    assess_learning_resource_request,
)


def test_default_resource_budget_accepts_bounded_workload() -> None:
    request = LearningResourceRequest(
        training_samples=120,
        validation_samples=30,
        feature_count=12,
        max_steps=250,
        max_runtime_seconds=60,
        memory_limit_bytes=32 * 1024 * 1024,
    )

    plan = assess_learning_resource_request(request)

    assert plan.sample_count == 150
    assert plan.feature_count == 12
    assert plan.max_steps == 250
    assert plan.estimated_operation_units == 2_161_080
    assert plan.estimated_memory_bytes <= request.memory_limit_bytes
    assert plan.max_runtime_seconds == 60


@pytest.mark.parametrize(
    ("workload", "code"),
    [
        (
            LearningResourceRequest(10_001, 0, 1, 1, 1, 1_000_000),
            LearningResourceLimitCode.SAMPLE_COUNT_EXCEEDED,
        ),
        (
            LearningResourceRequest(1, 0, 257, 1, 1, 1_000_000),
            LearningResourceLimitCode.FEATURE_COUNT_EXCEEDED,
        ),
        (
            LearningResourceRequest(1, 0, 1, 2_001, 1, 1_000_000),
            LearningResourceLimitCode.STEP_COUNT_EXCEEDED,
        ),
        (
            LearningResourceRequest(1, 0, 1, 1, 301, 1_000_000),
            LearningResourceLimitCode.RUNTIME_EXCEEDED,
        ),
        (
            LearningResourceRequest(100, 0, 10, 10, 10, 1_000),
            LearningResourceLimitCode.MEMORY_EXCEEDED,
        ),
        (
            LearningResourceRequest(10_000, 0, 256, 2_000, 300, 512 * 1024 * 1024),
            LearningResourceLimitCode.COMPUTE_BUDGET_EXCEEDED,
        ),
    ],
)
def test_resource_policy_rejects_over_budget_before_adapter_call(
    workload: LearningResourceRequest,
    code: LearningResourceLimitCode,
) -> None:
    with pytest.raises(LearningResourceRejected) as error:
        assess_learning_resource_request(workload)

    assert error.value.code is code


@pytest.mark.parametrize(
    "workload",
    [
        LearningResourceRequest(True, 0, 1, 1, 1, 1024),
        LearningResourceRequest(1, False, 1, 1, 1, 1024),
        LearningResourceRequest(1, 0, 0, 1, 1, 1024),
        LearningResourceRequest(1, 0, 1, 0, 1, 1024),
        LearningResourceRequest(1, 0, 1, 1, 0, 1024),
        LearningResourceRequest(1, 0, 1, 1, 1, True),
    ],
)
def test_resource_request_rejects_bool_and_nonpositive_counts(
    workload: LearningResourceRequest,
) -> None:
    with pytest.raises(ValueError, match="phải là số nguyên không âm|lớn hơn 0"):
        assess_learning_resource_request(workload)


def test_server_limits_cannot_exceed_compiled_hard_ceiling() -> None:
    with pytest.raises(ValueError, match="hard ceiling"):
        LearningResourceLimits(max_samples=DEFAULT_LEARNING_RESOURCE_LIMITS.max_samples + 1)


def test_estimated_memory_is_rejected_even_if_requested_memory_is_below_hard_ceiling() -> None:
    request = LearningResourceRequest(
        training_samples=8_000,
        validation_samples=1_000,
        feature_count=200,
        max_steps=1,
        max_runtime_seconds=1,
        memory_limit_bytes=8 * 1024 * 1024,
    )

    with pytest.raises(LearningResourceRejected) as error:
        assess_learning_resource_request(request)

    assert error.value.code is LearningResourceLimitCode.MEMORY_EXCEEDED
