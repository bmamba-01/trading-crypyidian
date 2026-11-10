"""Giới hạn tài nguyên trước khi gọi trainer/evaluator adapter."""

from dataclasses import dataclass
from enum import StrEnum

_HARD_MAX_SAMPLES = 10_000
_HARD_MAX_FEATURES = 256
_HARD_MAX_STEPS = 2_000
_HARD_MAX_RUNTIME_SECONDS = 300
_HARD_MAX_MEMORY_BYTES = 512 * 1024 * 1024
_HARD_MAX_COMPUTE_UNITS = 2_000_000_000
_ONE_VS_REST_CLASS_COUNT = 3
_ESTIMATED_BYTES_PER_FEATURE_VALUE = 64
_ESTIMATED_BYTES_PER_SAMPLE = 128
_ESTIMATED_BYTES_PER_FEATURE = 1_024
_ESTIMATED_FIXED_BYTES = 64 * 1024


class LearningResourceLimitCode(StrEnum):
    """Mã ổn định để ledger ghi nhận lý do từ chối đã được làm sạch."""

    SAMPLE_COUNT_EXCEEDED = "sample_count_exceeded"
    FEATURE_COUNT_EXCEEDED = "feature_count_exceeded"
    STEP_COUNT_EXCEEDED = "step_count_exceeded"
    RUNTIME_EXCEEDED = "runtime_exceeded"
    MEMORY_EXCEEDED = "memory_exceeded"
    COMPUTE_BUDGET_EXCEEDED = "compute_budget_exceeded"


class LearningResourceRejected(ValueError):
    """Yêu cầu học vượt giới hạn máy chủ, bị chặn trước khi gọi adapter."""

    def __init__(self, code: LearningResourceLimitCode) -> None:
        self.code = code
        super().__init__(f"Yêu cầu vượt giới hạn tài nguyên learning: {code.value}.")


@dataclass(frozen=True, slots=True)
class LearningResourceLimits:
    """Ngân sách máy chủ; mỗi giới hạn chỉ có thể thấp hơn hard ceiling."""

    max_samples: int = _HARD_MAX_SAMPLES
    max_features: int = _HARD_MAX_FEATURES
    max_steps: int = _HARD_MAX_STEPS
    max_runtime_seconds: int = _HARD_MAX_RUNTIME_SECONDS
    max_memory_bytes: int = _HARD_MAX_MEMORY_BYTES
    max_compute_units: int = _HARD_MAX_COMPUTE_UNITS

    def __post_init__(self) -> None:
        ceilings = (
            ("max_samples", self.max_samples, _HARD_MAX_SAMPLES),
            ("max_features", self.max_features, _HARD_MAX_FEATURES),
            ("max_steps", self.max_steps, _HARD_MAX_STEPS),
            ("max_runtime_seconds", self.max_runtime_seconds, _HARD_MAX_RUNTIME_SECONDS),
            ("max_memory_bytes", self.max_memory_bytes, _HARD_MAX_MEMORY_BYTES),
            ("max_compute_units", self.max_compute_units, _HARD_MAX_COMPUTE_UNITS),
        )
        for name, value, ceiling in ceilings:
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError(f"{name} phải là số nguyên dương không vượt hard ceiling.")


DEFAULT_LEARNING_RESOURCE_LIMITS = LearningResourceLimits()


@dataclass(frozen=True, slots=True)
class LearningResourceRequest:
    """Kích thước dữ liệu và trần thực thi được yêu cầu cho một lần chạy."""

    training_samples: int
    validation_samples: int
    feature_count: int
    max_steps: int
    max_runtime_seconds: int
    memory_limit_bytes: int


@dataclass(frozen=True, slots=True)
class LearningResourcePlan:
    """Ước lượng workload đã qua giới hạn cứng của máy chủ."""

    sample_count: int
    feature_count: int
    max_steps: int
    max_runtime_seconds: int
    memory_limit_bytes: int
    estimated_operation_units: int
    estimated_memory_bytes: int


def assess_learning_resource_request(
    request: LearningResourceRequest,
    *,
    limits: LearningResourceLimits = DEFAULT_LEARNING_RESOURCE_LIMITS,
) -> LearningResourcePlan:
    """Từ chối kích thước vượt policy trước khi gọi trainer hoặc evaluator."""
    if type(request) is not LearningResourceRequest:
        raise ValueError("request phải là LearningResourceRequest.")
    if type(limits) is not LearningResourceLimits:
        raise ValueError("limits phải là LearningResourceLimits do máy chủ chọn.")

    for name in (
        "training_samples",
        "validation_samples",
        "feature_count",
        "max_steps",
        "max_runtime_seconds",
        "memory_limit_bytes",
    ):
        value = getattr(request, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} phải là số nguyên không âm; không chấp nhận bool.")
    if request.training_samples == 0 or request.feature_count == 0:
        raise ValueError("Tập train và số đặc trưng phải lớn hơn 0.")
    if (
        request.max_steps == 0
        or request.max_runtime_seconds == 0
        or request.memory_limit_bytes == 0
    ):
        raise ValueError("Steps, thời gian và bộ nhớ phải lớn hơn 0.")

    sample_count = request.training_samples + request.validation_samples
    if sample_count > limits.max_samples:
        raise LearningResourceRejected(LearningResourceLimitCode.SAMPLE_COUNT_EXCEEDED)
    if request.feature_count > limits.max_features:
        raise LearningResourceRejected(LearningResourceLimitCode.FEATURE_COUNT_EXCEEDED)
    if request.max_steps > limits.max_steps:
        raise LearningResourceRejected(LearningResourceLimitCode.STEP_COUNT_EXCEEDED)
    if request.max_runtime_seconds > limits.max_runtime_seconds:
        raise LearningResourceRejected(LearningResourceLimitCode.RUNTIME_EXCEEDED)
    if request.memory_limit_bytes > limits.max_memory_bytes:
        raise LearningResourceRejected(LearningResourceLimitCode.MEMORY_EXCEEDED)

    estimated_operation_units = (
        _ONE_VS_REST_CLASS_COUNT
        * request.feature_count
        * (2 * request.training_samples * request.max_steps + request.validation_samples)
    )
    if estimated_operation_units > limits.max_compute_units:
        raise LearningResourceRejected(LearningResourceLimitCode.COMPUTE_BUDGET_EXCEEDED)

    estimated_memory_bytes = (
        sample_count * request.feature_count * _ESTIMATED_BYTES_PER_FEATURE_VALUE
        + sample_count * _ESTIMATED_BYTES_PER_SAMPLE
        + request.feature_count * _ESTIMATED_BYTES_PER_FEATURE
        + _ESTIMATED_FIXED_BYTES
    )
    if estimated_memory_bytes > request.memory_limit_bytes:
        raise LearningResourceRejected(LearningResourceLimitCode.MEMORY_EXCEEDED)

    return LearningResourcePlan(
        sample_count=sample_count,
        feature_count=request.feature_count,
        max_steps=request.max_steps,
        max_runtime_seconds=request.max_runtime_seconds,
        memory_limit_bytes=request.memory_limit_bytes,
        estimated_operation_units=estimated_operation_units,
        estimated_memory_bytes=estimated_memory_bytes,
    )


__all__ = [
    "DEFAULT_LEARNING_RESOURCE_LIMITS",
    "LearningResourceLimitCode",
    "LearningResourceLimits",
    "LearningResourcePlan",
    "LearningResourceRejected",
    "LearningResourceRequest",
    "assess_learning_resource_request",
]
