"""Huấn luyện baseline logistic CPU trên batch train/validation đã được cấp quyền."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Literal

from trading_bot.application.learning_training_batch import (
    AuthorizedTrainerBatch,
    AuthorizedTrainerSample,
)
from trading_bot.domain.learning.resources import (
    LearningResourcePlan,
    LearningResourceRequest,
    assess_learning_resource_request,
)

CPU_TRAINER_VERSION = "cpu-logistic-v1"
_CLASS_LABELS = (-1, 0, 1)
_LEARNING_RATE = 0.1
_L2_PENALTY = 0.0001
_PROBABILITY_FLOOR = 1e-15
_MAX_NORMALIZED_FEATURE_MAGNITUDE = 1_000_000.0


@dataclass(frozen=True, slots=True)
class LearningCpuClassModel:
    """Một nhị phân logistic ứng với một lớp PnL so với các lớp còn lại."""

    label: int
    intercept: float
    coefficients: tuple[float, ...]
    training_positive_count: int


@dataclass(frozen=True, slots=True)
class LearningCpuValidationReport:
    """Metric chẩn đoán chỉ trên validation, không chứa sample hay feature payload."""

    sample_count: int
    accuracy: float
    majority_baseline_accuracy: float
    macro_recall: float
    log_loss: float
    majority_class: int
    confusion_matrix: tuple[tuple[int, ...], ...]
    class_support: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class LearningCpuArtifact:
    """Artifact canonical bất biến; chưa có quyền score hoặc đề xuất lệnh."""

    schema_version: str
    trainer_version: str
    dependency_fingerprint: str
    feature_schema_hash: str
    attempt_content_hash: str
    split_policy_version: str
    workspace_id: str
    attempt_id: str
    normalized_dataset_id: str
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    batch_hash: str
    seed: int
    resource_request: LearningResourceRequest
    resource_plan: LearningResourcePlan
    feature_names: tuple[str, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    models: tuple[LearningCpuClassModel, ...]
    training_class_counts: tuple[tuple[int, int], ...]
    optimization_steps: int
    actual_operation_units: int
    validation_report: LearningCpuValidationReport
    calibrated: Literal[False] = False
    scorer_state: Literal["abstain"] = "abstain"

    @property
    def canonical_json(self) -> str:
        """Trả JSON chuẩn để persist/checksum; không serialize sample payload."""
        payload = {
            "actual_operation_units": self.actual_operation_units,
            "approval_hash": self.approval_hash,
            "attempt_id": self.attempt_id,
            "attempt_content_hash": self.attempt_content_hash,
            "batch_hash": self.batch_hash,
            "calibrated": self.calibrated,
            "dependency_fingerprint": self.dependency_fingerprint,
            "feature_means": self.feature_means,
            "feature_names": self.feature_names,
            "feature_scales": self.feature_scales,
            "feature_schema_hash": self.feature_schema_hash,
            "fold_id": self.fold_id,
            "fold_plan_hash": self.fold_plan_hash,
            "manifest_hash": self.manifest_hash,
            "models": [
                {
                    "coefficients": model.coefficients,
                    "intercept": model.intercept,
                    "label": model.label,
                    "training_positive_count": model.training_positive_count,
                }
                for model in self.models
            ],
            "normalized_dataset_id": self.normalized_dataset_id,
            "optimization_steps": self.optimization_steps,
            "schema_version": self.schema_version,
            "scorer_state": self.scorer_state,
            "seed": self.seed,
            "resource_plan": {
                "estimated_memory_bytes": self.resource_plan.estimated_memory_bytes,
                "estimated_operation_units": self.resource_plan.estimated_operation_units,
                "feature_count": self.resource_plan.feature_count,
                "max_runtime_seconds": self.resource_plan.max_runtime_seconds,
                "max_steps": self.resource_plan.max_steps,
                "memory_limit_bytes": self.resource_plan.memory_limit_bytes,
                "sample_count": self.resource_plan.sample_count,
            },
            "resource_request": {
                "feature_count": self.resource_request.feature_count,
                "max_runtime_seconds": self.resource_request.max_runtime_seconds,
                "max_steps": self.resource_request.max_steps,
                "memory_limit_bytes": self.resource_request.memory_limit_bytes,
                "training_samples": self.resource_request.training_samples,
                "validation_samples": self.resource_request.validation_samples,
            },
            "split_policy_version": self.split_policy_version,
            "training_class_counts": self.training_class_counts,
            "trainer_version": self.trainer_version,
            "validation_report": {
                "accuracy": self.validation_report.accuracy,
                "class_support": self.validation_report.class_support,
                "confusion_matrix": self.validation_report.confusion_matrix,
                "log_loss": self.validation_report.log_loss,
                "majority_baseline_accuracy": self.validation_report.majority_baseline_accuracy,
                "majority_class": self.validation_report.majority_class,
                "macro_recall": self.validation_report.macro_recall,
                "sample_count": self.validation_report.sample_count,
            },
            "workspace_id": self.workspace_id,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def artifact_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def train_authorized_batch(batch: AuthorizedTrainerBatch) -> LearningCpuArtifact:
    """Fit ba logistic OVR; chỉ dùng train để fit và validation để đo metric."""
    if type(batch) is not AuthorizedTrainerBatch:
        raise ValueError("Trainer chỉ nhận AuthorizedTrainerBatch từ gateway đã cấp quyền.")
    if batch.trainer_version != CPU_TRAINER_VERSION:
        raise ValueError("trainer_version không được CPU baseline này hỗ trợ.")
    request = batch.resource_request
    if request.validation_samples < 1:
        raise ValueError("CPU trainer yêu cầu validation sample để tạo report chẩn đoán.")
    if batch.resource_plan != assess_learning_resource_request(request):
        raise ValueError("Resource profile của trainer không khớp request đã duyệt.")

    train_samples = tuple(sample for sample in batch.samples if sample.role == "train")
    validation_samples = tuple(sample for sample in batch.samples if sample.role == "validation")
    if (
        len(train_samples) != request.training_samples
        or len(validation_samples) != request.validation_samples
    ):
        raise ValueError("Số sample train/validation không khớp resource request.")
    if not train_samples or not validation_samples:
        raise ValueError("CPU trainer cần train và validation không rỗng.")

    feature_names = tuple(name for name, _ in train_samples[0].features)
    if len(feature_names) != request.feature_count:
        raise ValueError("Feature count không khớp profile của attempt.")
    if any(
        type(sample) is not AuthorizedTrainerSample
        or tuple(name for name, _ in sample.features) != feature_names
        for sample in (*train_samples, *validation_samples)
    ):
        raise ValueError("Trainer nhận feature schema không đồng nhất.")

    started_at = time.monotonic()
    deadline = started_at + request.max_runtime_seconds
    train_matrix = tuple(tuple(value for _, value in row.features) for row in train_samples)
    validation_matrix = tuple(
        tuple(value for _, value in row.features) for row in validation_samples
    )
    feature_means, feature_scales = _fit_standardizer(train_matrix)
    normalized_train = _transform(train_matrix, feature_means, feature_scales)
    normalized_validation = _transform(validation_matrix, feature_means, feature_scales)

    models: list[LearningCpuClassModel] = []
    for label in _CLASS_LABELS:
        models.append(
            _fit_binary_logistic(
                label=label,
                features=normalized_train,
                labels=tuple(sample.label_value for sample in train_samples),
                max_steps=request.max_steps,
                deadline=deadline,
            )
        )

    training_counts = tuple(
        (label, sum(sample.label_value == label for sample in train_samples))
        for label in _CLASS_LABELS
    )
    training_count_by_label = dict(training_counts)
    training_majority_class = min(
        _CLASS_LABELS,
        key=lambda label: (-training_count_by_label[label], label),
    )
    validation_report = _validation_report(
        samples=validation_samples,
        normalized_features=normalized_validation,
        models=tuple(models),
        majority_class=training_majority_class,
    )
    if time.monotonic() > deadline:
        raise TimeoutError("CPU trainer vượt thời gian yêu cầu của attempt.")

    return LearningCpuArtifact(
        schema_version="crypidian.learning.cpu-logistic.v1",
        trainer_version=batch.trainer_version,
        dependency_fingerprint=batch.dependency_fingerprint,
        feature_schema_hash=batch.feature_schema_hash,
        attempt_content_hash=batch.attempt_content_hash,
        split_policy_version=batch.split_policy_version,
        workspace_id=str(batch.workspace_id),
        attempt_id=str(batch.attempt_id),
        normalized_dataset_id=str(batch.normalized_dataset_id),
        manifest_hash=batch.manifest_hash,
        fold_plan_hash=batch.fold_plan_hash,
        fold_id=batch.fold_id,
        approval_hash=batch.approval_hash,
        batch_hash=batch.batch_hash,
        seed=batch.seed,
        resource_request=batch.resource_request,
        resource_plan=batch.resource_plan,
        feature_names=feature_names,
        feature_means=feature_means,
        feature_scales=feature_scales,
        models=tuple(models),
        training_class_counts=training_counts,
        optimization_steps=request.max_steps,
        actual_operation_units=_actual_feature_visit_units(batch),
        validation_report=validation_report,
    )


def _actual_feature_visit_units(batch: AuthorizedTrainerBatch) -> int:
    """Đếm feature-coordinate visit của gradient train và lượt chấm validation."""
    training_count = batch.resource_request.training_samples
    validation_count = batch.resource_request.validation_samples
    feature_count = batch.resource_request.feature_count
    optimization_steps = batch.resource_request.max_steps
    return (
        len(_CLASS_LABELS)
        * feature_count
        * (2 * training_count * optimization_steps + validation_count)
    )


def _fit_standardizer(
    matrix: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not matrix or not matrix[0]:
        raise ValueError("Không thể chuẩn hóa ma trận feature rỗng.")
    feature_count = len(matrix[0])
    if any(len(row) != feature_count for row in matrix):
        raise ValueError("Ma trận train có feature count không đồng nhất.")
    means: list[float] = []
    scales: list[float] = []
    for index in range(feature_count):
        values = tuple(row[index] for row in matrix)
        mean = math.fsum(values) / len(values)
        variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
        scale = math.sqrt(variance) if variance > 0 else 1.0
        if not math.isfinite(mean) or not math.isfinite(scale) or scale <= 0:
            raise ValueError("Feature train vượt miền số có thể chuẩn hóa an toàn.")
        means.append(mean)
        scales.append(scale)
    return tuple(means), tuple(scales)


def _transform(
    matrix: tuple[tuple[float, ...], ...],
    means: tuple[float, ...],
    scales: tuple[float, ...],
) -> tuple[tuple[float, ...], ...]:
    transformed: list[tuple[float, ...]] = []
    for row in matrix:
        if len(row) != len(means):
            raise ValueError("Feature row không khớp scaler đã fit.")
        normalized = tuple(
            (value - means[index]) / scales[index] for index, value in enumerate(row)
        )
        if any(
            not math.isfinite(value) or abs(value) > _MAX_NORMALIZED_FEATURE_MAGNITUDE
            for value in normalized
        ):
            raise ValueError("Feature sau chuẩn hóa vượt miền số được hỗ trợ.")
        transformed.append(normalized)
    return tuple(transformed)


def _fit_binary_logistic(
    *,
    label: int,
    features: tuple[tuple[float, ...], ...],
    labels: tuple[int, ...],
    max_steps: int,
    deadline: float,
) -> LearningCpuClassModel:
    feature_count = len(features[0])
    weights = [0.0] * feature_count
    intercept = 0.0
    sample_count = len(features)
    positive_count = sum(value == label for value in labels)

    for _ in range(max_steps):
        if time.monotonic() > deadline:
            raise TimeoutError("CPU trainer vượt thời gian yêu cầu của attempt.")
        errors: list[float] = []
        for row, actual_label in zip(features, labels, strict=True):
            logit = intercept + math.fsum(
                weight * value for weight, value in zip(weights, row, strict=True)
            )
            error = _sigmoid(logit) - (1.0 if actual_label == label else 0.0)
            errors.append(error)
        intercept_gradient = math.fsum(errors) / sample_count
        gradients = tuple(
            math.fsum(error * row[index] for error, row in zip(errors, features, strict=True))
            / sample_count
            + _L2_PENALTY * weights[index]
            for index in range(feature_count)
        )
        intercept -= _LEARNING_RATE * intercept_gradient
        weights = [
            weight - _LEARNING_RATE * gradient
            for weight, gradient in zip(weights, gradients, strict=True)
        ]
        if not math.isfinite(intercept) or any(not math.isfinite(weight) for weight in weights):
            raise ValueError("Tối ưu logistic tạo tham số không hữu hạn.")

    return LearningCpuClassModel(
        label=label,
        intercept=intercept,
        coefficients=tuple(weights),
        training_positive_count=positive_count,
    )


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _probabilities(
    features: tuple[float, ...], models: tuple[LearningCpuClassModel, ...]
) -> tuple[float, ...]:
    scores = tuple(
        _sigmoid(
            model.intercept
            + math.fsum(
                weight * value for weight, value in zip(model.coefficients, features, strict=True)
            )
        )
        for model in models
    )
    total = math.fsum(scores)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Logistic scores không thể chuẩn hóa thành phân phối lớp.")
    return tuple(score / total for score in scores)


def _validation_report(
    *,
    samples: tuple[AuthorizedTrainerSample, ...],
    normalized_features: tuple[tuple[float, ...], ...],
    models: tuple[LearningCpuClassModel, ...],
    majority_class: int,
) -> LearningCpuValidationReport:
    label_index = {label: index for index, label in enumerate(_CLASS_LABELS)}
    confusion = [[0 for _ in _CLASS_LABELS] for _ in _CLASS_LABELS]
    log_losses: list[float] = []
    correct = 0
    supports = dict.fromkeys(_CLASS_LABELS, 0)
    for sample, feature_vector in zip(samples, normalized_features, strict=True):
        probabilities = _probabilities(feature_vector, models)
        predicted = min(
            _CLASS_LABELS,
            key=lambda label: (-probabilities[label_index[label]], label),
        )
        supports[sample.label_value] += 1
        confusion[label_index[sample.label_value]][label_index[predicted]] += 1
        correct += predicted == sample.label_value
        probability = max(_PROBABILITY_FLOOR, probabilities[label_index[sample.label_value]])
        log_losses.append(-math.log(probability))

    validation_counts = {
        label: sum(sample.label_value == label for sample in samples) for label in _CLASS_LABELS
    }
    majority_correct = sum(sample.label_value == majority_class for sample in samples)
    recalls = tuple(
        confusion[label_index[label]][label_index[label]] / support
        for label, support in supports.items()
        if support > 0
    )
    return LearningCpuValidationReport(
        sample_count=len(samples),
        accuracy=correct / len(samples),
        majority_baseline_accuracy=majority_correct / len(samples),
        macro_recall=math.fsum(recalls) / len(recalls),
        log_loss=math.fsum(log_losses) / len(log_losses),
        majority_class=majority_class,
        confusion_matrix=tuple(tuple(row) for row in confusion),
        class_support=tuple((label, validation_counts[label]) for label in _CLASS_LABELS),
    )


__all__ = [
    "CPU_TRAINER_VERSION",
    "LearningCpuArtifact",
    "LearningCpuClassModel",
    "LearningCpuValidationReport",
    "train_authorized_batch",
]
