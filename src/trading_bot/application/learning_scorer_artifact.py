"""Tạo scorer artifact v2 từ artifact v1 và batch TRAIN/VALIDATION đã cấp quyền."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from trading_bot.application.learning_cpu_trainer import (
    CPU_TRAINER_VERSION,
    LearningCpuArtifact,
)
from trading_bot.application.learning_training_batch import (
    AuthorizedTrainerBatch,
    AuthorizedTrainerSample,
)
from trading_bot.domain.learning.calibration import (
    CLASS_LABELS,
    TemperatureCalibrationError,
    TemperatureCalibrationFailureCode,
    TemperatureCalibrationPolicy,
    ValidationCalibrationExample,
    fit_validation_temperature,
)

if TYPE_CHECKING:
    from trading_bot.application.learning_training_process import LearningCpuTrainingProcessResult

SCORER_ARTIFACT_SCHEMA_VERSION = "crypidian.learning.scorer-temperature.v2"
SCORER_CALIBRATOR_VERSION = "temperature-scaling-bisection-v1"
SCORER_CODE_VERSION = "wp06-scorer-artifact-v2"
DEFAULT_CALIBRATION_POLICY = TemperatureCalibrationPolicy(
    policy_version="wp06-temperature-calibration-v1",
    minimum_temperature=0.05,
    maximum_temperature=20.0,
    minimum_samples=9,
    minimum_class_support=2,
    relative_tolerance=1e-10,
    maximum_iterations=256,
)


@dataclass(frozen=True, slots=True)
class LearningScorerArtifact:
    """Scorer v2 chỉ giữ phép biến đổi và lineage; hệ số tiếp tục nằm ở artifact v1."""

    schema_version: str
    state: Literal["ready_for_evaluation", "abstain"]
    reason_code: str | None
    workspace_id: str
    attempt_id: str
    normalized_dataset_id: str
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    base_artifact_hash: str
    strategy_release_id: str | None
    strategy_release_content_hash: str | None
    vector_schema_version: str | None
    vector_schema_hash: str
    training_membership_digest: str
    validation_membership_digest: str
    training_sample_count: int
    validation_sample_count: int
    calibrator_method: str
    calibrator_version: str
    calibration_policy_version: str | None
    calibration_policy_hash: str | None
    temperature: float | None
    validation_class_support: tuple[tuple[int, int], ...]
    validation_nll_before: float | None
    validation_nll_after: float | None
    calibration_iterations: int | None
    trainer_version: str
    trainer_code_version: str
    dependency_fingerprint: str

    @property
    def canonical_json(self) -> str:
        """Serialize lineage, phép biến đổi và thống kê validation tổng hợp."""
        payload = asdict(self)
        payload["validation_class_support"] = [list(item) for item in self.validation_class_support]
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def artifact_hash(self) -> str:
        """Hash SHA-256 của nội dung canonical."""
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedLearningScorerArtifact:
    """Scorer v2 đã qua canonical/hash/lineage checks tại ranh giới process."""

    workspace_id: UUID
    attempt_id: UUID
    normalized_dataset_id: UUID
    artifact_hash: str
    base_artifact_hash: str
    canonical_artifact: str
    artifact: LearningScorerArtifact

    @classmethod
    def from_process_result(
        cls,
        batch: AuthorizedTrainerBatch,
        result: LearningCpuTrainingProcessResult,
    ) -> VerifiedLearningScorerArtifact:
        """Từ chối v2 sai hash, mang payload hoặc khác batch TRAIN/VALIDATION."""
        from trading_bot.application.learning_training_process import (
            LearningCpuTrainingProcessResult,
        )

        if type(batch) is not AuthorizedTrainerBatch:
            raise ValueError("Scorer artifact cần batch trainer đã cấp quyền.")
        if type(result) is not LearningCpuTrainingProcessResult:
            raise ValueError("Scorer artifact thiếu process result đã kiểm.")
        raw = result.scorer_artifact_json
        supplied_hash = result.scorer_artifact_hash
        if not isinstance(raw, str) or not isinstance(supplied_hash, str):
            raise ValueError("Process thiếu scorer artifact v2.")
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != supplied_hash:
            raise ValueError("Scorer artifact v2 checksum không khớp.")
        try:
            payload = json.loads(raw)
        except TypeError, ValueError, json.JSONDecodeError:
            raise ValueError("Scorer artifact v2 JSON không hợp lệ.") from None
        if type(payload) is not dict:
            raise ValueError("Scorer artifact v2 phải là object.")
        if any(key in payload for key in ("samples", "labels", "features", "test_payload")):
            raise ValueError("Scorer artifact v2 không được chứa payload mẫu.")
        expected_keys = {field.name for field in fields(LearningScorerArtifact)}
        if set(payload) != expected_keys:
            raise ValueError("Scorer artifact v2 có trường ngoài contract.")
        try:
            support = tuple(
                (int(label), int(count)) for label, count in payload.pop("validation_class_support")
            )
            artifact = LearningScorerArtifact(
                **payload,
                validation_class_support=support,
            )
        except TypeError, ValueError:
            raise ValueError("Scorer artifact v2 chứa metadata không hợp lệ.") from None
        if artifact.canonical_json != raw:
            raise ValueError("Scorer artifact v2 không canonical.")
        if artifact.schema_version != SCORER_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("Scorer artifact v2 schema không được hỗ trợ.")

        training = tuple(sample for sample in batch.samples if sample.role == "train")
        validation = tuple(sample for sample in batch.samples if sample.role == "validation")
        release_id = batch.samples[0].strategy_release_id
        release_hash = batch.samples[0].strategy_release_content_hash
        vector_version = batch.samples[0].vector_schema_version
        lineage_ready = release_id is not None
        expected_policy_hash = _canonical_hash(asdict(DEFAULT_CALIBRATION_POLICY))
        expected_metadata = {
            "workspace_id": str(batch.workspace_id),
            "attempt_id": str(batch.attempt_id),
            "normalized_dataset_id": str(batch.normalized_dataset_id),
            "manifest_hash": batch.manifest_hash,
            "fold_plan_hash": batch.fold_plan_hash,
            "fold_id": batch.fold_id,
            "approval_hash": batch.approval_hash,
            "base_artifact_hash": result.artifact_hash,
            "strategy_release_id": release_id,
            "strategy_release_content_hash": release_hash,
            "vector_schema_version": vector_version,
            "vector_schema_hash": batch.feature_schema_hash,
            "training_membership_digest": _membership_digest(training),
            "validation_membership_digest": _membership_digest(validation),
            "training_sample_count": len(training),
            "validation_sample_count": len(validation),
            "validation_class_support": tuple(
                (label, sum(sample.label_value == label for sample in validation))
                for label in CLASS_LABELS
            ),
            "calibrator_method": "temperature_scaling",
            "calibrator_version": SCORER_CALIBRATOR_VERSION,
            "calibration_policy_version": DEFAULT_CALIBRATION_POLICY.policy_version,
            "calibration_policy_hash": expected_policy_hash,
            "trainer_version": batch.trainer_version,
            "trainer_code_version": SCORER_CODE_VERSION,
            "dependency_fingerprint": batch.dependency_fingerprint,
        }
        if any(getattr(artifact, key) != value for key, value in expected_metadata.items()):
            raise ValueError("Scorer artifact v2 lineage khác batch được cấp.")
        if artifact.state == "ready_for_evaluation":
            if (
                not lineage_ready
                or artifact.reason_code is not None
                or artifact.temperature is None
                or not DEFAULT_CALIBRATION_POLICY.minimum_temperature
                < artifact.temperature
                < DEFAULT_CALIBRATION_POLICY.maximum_temperature
                or artifact.validation_nll_before is None
                or artifact.validation_nll_after is None
                or artifact.calibration_iterations is None
                or artifact.calibration_iterations < 1
                or any(
                    count < DEFAULT_CALIBRATION_POLICY.minimum_class_support
                    for _, count in artifact.validation_class_support
                )
                or artifact.validation_sample_count < DEFAULT_CALIBRATION_POLICY.minimum_samples
            ):
                raise ValueError("Scorer artifact v2 ready state không đạt policy.")
        elif artifact.state == "abstain":
            allowed_reasons = {code.value for code in TemperatureCalibrationFailureCode} | {
                "authoritative_lineage_missing",
                "calibration_policy_missing",
            }
            if artifact.reason_code not in allowed_reasons or artifact.temperature is not None:
                raise ValueError("Scorer artifact v2 ABSTAIN state không hợp lệ.")
            if not lineage_ready and artifact.reason_code != "authoritative_lineage_missing":
                raise ValueError(
                    "Scorer thiếu lineage nhưng không ở trạng thái ABSTAIN fail-closed."
                )
        else:
            raise ValueError("Scorer artifact v2 state không được hỗ trợ.")

        return cls(
            workspace_id=batch.workspace_id,
            attempt_id=batch.attempt_id,
            normalized_dataset_id=batch.normalized_dataset_id,
            artifact_hash=supplied_hash,
            base_artifact_hash=result.artifact_hash,
            canonical_artifact=raw,
            artifact=artifact,
        )


def build_calibrated_scorer_artifact(
    batch: AuthorizedTrainerBatch,
    base_artifact: LearningCpuArtifact,
    *,
    policy: TemperatureCalibrationPolicy | None = DEFAULT_CALIBRATION_POLICY,
) -> LearningScorerArtifact:
    """Fit nhiệt độ chỉ từ VALIDATION; thiếu policy hoặc calibration đạt yêu cầu thì ABSTAIN."""
    if type(batch) is not AuthorizedTrainerBatch:
        raise ValueError("Scorer artifact cần AuthorizedTrainerBatch đã được cấp quyền.")
    if type(base_artifact) is not LearningCpuArtifact:
        raise ValueError("Scorer artifact cần artifact v1 đã được trainer tạo.")
    if (
        base_artifact.schema_version != "crypidian.learning.cpu-logistic.v1"
        or base_artifact.trainer_version != CPU_TRAINER_VERSION
        or base_artifact.calibrated is not False
        or base_artifact.scorer_state != "abstain"
        or base_artifact.workspace_id != str(batch.workspace_id)
        or base_artifact.attempt_id != str(batch.attempt_id)
        or base_artifact.batch_hash != batch.batch_hash
        or base_artifact.feature_schema_hash != batch.feature_schema_hash
    ):
        raise ValueError("Artifact v1 không khớp batch đã cấp hoặc không còn diagnostic-only.")

    train_samples = tuple(sample for sample in batch.samples if sample.role == "train")
    validation_samples = tuple(sample for sample in batch.samples if sample.role == "validation")
    lineage_ready = all(sample.strategy_release_id is not None for sample in batch.samples)
    release_id: str | None = None
    release_hash: str | None = None
    vector_schema_version: str | None = None
    vector_schema_hash = batch.feature_schema_hash
    if lineage_ready:
        release_id, release_hash = _shared_release_lineage(batch)
        vector_schema_version, vector_schema_hash = _shared_vector_schema(batch)
    if (
        base_artifact.models
        and tuple(model.label for model in base_artifact.models) != CLASS_LABELS
    ):
        raise ValueError("Artifact v1 không có ba model theo thứ tự lớp canonical.")

    examples = tuple(
        ValidationCalibrationExample(
            logits=_validation_logits(base_artifact, sample.features),
            label=sample.label_value,
            fold_role="validation",
        )
        for sample in validation_samples
    )
    support = tuple(
        (label, sum(example.label == label for example in examples)) for label in CLASS_LABELS
    )
    policy_version: str | None = None
    policy_hash: str | None = None
    temperature: float | None = None
    nll_before: float | None = None
    nll_after: float | None = None
    iterations: int | None = None
    state: Literal["ready_for_evaluation", "abstain"] = "abstain"
    reason_code: str | None = None
    if policy is not None:
        if type(policy) is not TemperatureCalibrationPolicy:
            raise ValueError("policy phải là TemperatureCalibrationPolicy hoặc None.")
        policy_version = policy.policy_version
        policy_hash = _canonical_hash(asdict(policy))
    else:
        reason_code = "calibration_policy_missing"

    if lineage_ready and policy is not None:
        try:
            fit = fit_validation_temperature(examples, policy)
        except TemperatureCalibrationError as error:
            reason_code = error.code.value
        else:
            if fit.validation_nll_after >= fit.validation_nll_before:
                reason_code = TemperatureCalibrationFailureCode.VALIDATION_NLL_NOT_IMPROVED.value
            else:
                state = "ready_for_evaluation"
                reason_code = None
                temperature = fit.temperature
                nll_before = fit.validation_nll_before
                nll_after = fit.validation_nll_after
                iterations = fit.iterations
    elif not lineage_ready and policy is not None:
        reason_code = "authoritative_lineage_missing"

    return LearningScorerArtifact(
        schema_version=SCORER_ARTIFACT_SCHEMA_VERSION,
        state=state,
        reason_code=reason_code,
        workspace_id=str(batch.workspace_id),
        attempt_id=str(batch.attempt_id),
        normalized_dataset_id=str(batch.normalized_dataset_id),
        manifest_hash=batch.manifest_hash,
        fold_plan_hash=batch.fold_plan_hash,
        fold_id=batch.fold_id,
        approval_hash=batch.approval_hash,
        base_artifact_hash=base_artifact.artifact_hash,
        strategy_release_id=release_id,
        strategy_release_content_hash=release_hash,
        vector_schema_version=vector_schema_version,
        vector_schema_hash=vector_schema_hash,
        training_membership_digest=_membership_digest(train_samples),
        validation_membership_digest=_membership_digest(validation_samples),
        training_sample_count=len(train_samples),
        validation_sample_count=len(validation_samples),
        calibrator_method="temperature_scaling",
        calibrator_version=SCORER_CALIBRATOR_VERSION,
        calibration_policy_version=policy_version,
        calibration_policy_hash=policy_hash,
        temperature=temperature,
        validation_class_support=support,
        validation_nll_before=nll_before,
        validation_nll_after=nll_after,
        calibration_iterations=iterations,
        trainer_version=batch.trainer_version,
        trainer_code_version=SCORER_CODE_VERSION,
        dependency_fingerprint=batch.dependency_fingerprint,
    )


def _membership_digest(samples: tuple[AuthorizedTrainerSample, ...]) -> str:
    members = [
        {
            "sample_id": sample.sample_id,
            "sample_hash": sample.sample_hash,
            "vector_hash": sample.vector_hash,
        }
        for sample in sorted(samples, key=lambda item: item.sample_id)
    ]
    return _canonical_hash(members)


def _shared_release_lineage(batch: AuthorizedTrainerBatch) -> tuple[str, str]:
    values = {
        (sample.strategy_release_id, sample.strategy_release_content_hash)
        for sample in batch.samples
    }
    if len(values) != 1:
        raise ValueError("Batch không pin một strategy release duy nhất.")
    release_id, release_hash = next(iter(values))
    if release_id is None or release_hash is None:
        raise ValueError("Batch không có strategy release authoritative.")
    return release_id, release_hash


def _shared_vector_schema(batch: AuthorizedTrainerBatch) -> tuple[str, str]:
    values = {(sample.vector_schema_version, sample.vector_schema_hash) for sample in batch.samples}
    if len(values) != 1:
        raise ValueError("Batch không pin một vector schema duy nhất.")
    version, schema_hash = next(iter(values))
    if version is None or schema_hash is None:
        raise ValueError("Batch không có vector schema authoritative.")
    return version, schema_hash


def _validation_logits(
    artifact: LearningCpuArtifact,
    features: tuple[tuple[str, float], ...],
) -> tuple[float, float, float]:
    feature_map = dict(features)
    if tuple(name for name, _ in features) != artifact.feature_names:
        raise ValueError("Validation feature order không khớp artifact v1.")
    normalized = tuple(
        (feature_map[name] - mean) / scale
        for name, mean, scale in zip(
            artifact.feature_names,
            artifact.feature_means,
            artifact.feature_scales,
            strict=True,
        )
    )
    logits = tuple(
        model.intercept
        + math.fsum(
            coefficient * value
            for coefficient, value in zip(model.coefficients, normalized, strict=True)
        )
        for model in artifact.models
    )
    if len(logits) != len(CLASS_LABELS) or any(not math.isfinite(value) for value in logits):
        raise ValueError("Artifact v1 tạo logit validation không hợp lệ.")
    return logits[0], logits[1], logits[2]


def _canonical_hash(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_CALIBRATION_POLICY",
    "LearningScorerArtifact",
    "SCORER_ARTIFACT_SCHEMA_VERSION",
    "SCORER_CALIBRATOR_VERSION",
    "build_calibrated_scorer_artifact",
]
