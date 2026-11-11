"""Nạp target scorer runtime đã được gateway PostgreSQL phân giải đầy đủ."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.application.learning_cpu_trainer import (
    LearningCpuArtifact,
    LearningCpuClassModel,
    LearningCpuValidationReport,
)
from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
from trading_bot.domain.learning.resources import LearningResourcePlan, LearningResourceRequest
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
    ScorerDeploymentTarget,
)


@dataclass(frozen=True, slots=True)
class ResolvedScorerRuntimeTarget:
    """Artifacts đã kiểm hash, quyền hiện hành và target deployment cùng workspace."""

    mode: ScorerDeploymentMode
    deployment_event_id: UUID
    deployment_revision: int
    target: ScorerDeploymentTarget
    base_artifact_id: UUID
    base_artifact_hash: str
    base_artifact: LearningCpuArtifact
    scorer_artifact: LearningScorerArtifact
    source_rights_fingerprint: str
    scored_candidate_coverage_verified: bool

    def __post_init__(self) -> None:
        if (
            type(self.mode) is not ScorerDeploymentMode
            or self.mode is ScorerDeploymentMode.DISABLED
        ):
            raise ValueError("Runtime target phải có mode active authoritative.")
        if type(self.target) is not ScorerDeploymentTarget:
            raise ValueError("Runtime target cần metadata deployment typed.")
        if type(self.deployment_event_id) is not UUID:
            raise ValueError("deployment_event_id phải là UUID authoritative.")
        if type(self.deployment_revision) is not int or self.deployment_revision < 1:
            raise ValueError("deployment_revision phải là số dương.")
        if type(self.base_artifact_id) is not UUID:
            raise ValueError("base_artifact_id phải là UUID authoritative.")
        if (
            type(self.base_artifact) is not LearningCpuArtifact
            or type(self.scorer_artifact) is not LearningScorerArtifact
        ):
            raise ValueError("Runtime target cần artifact base/scorer typed.")
        if (
            self.base_artifact_hash != _sha256(self.base_artifact.canonical_json)
            or self.scorer_artifact.artifact_hash != self.target.scorer_artifact_hash
            or self.scorer_artifact.base_artifact_hash != self.base_artifact_hash
        ):
            raise ValueError("Artifact scorer/base không khớp target deployment.")
        if type(self.scored_candidate_coverage_verified) is not bool:
            raise ValueError("Coverage verification phải là bool authoritative.")
        if not _is_sha256(self.source_rights_fingerprint):
            raise ValueError("source_rights_fingerprint phải là SHA-256.")


class PostgresScorerRuntimeTargetRepository:
    """Chỉ nhận artifact/runtime metadata từ SECURITY DEFINER gateway đã ghim target."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_active(
        self,
        *,
        workspace_id: UUID,
    ) -> ResolvedScorerRuntimeTarget | None:
        """Đọc active target hoàn toàn qua gateway; NULL row nghĩa là DISABLED."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID authoritative.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        result = await self._session.execute(
            text(
                "SELECT * FROM public.wp06_resolve_scorer_runtime_target("
                "target_workspace => CAST(:workspace_id AS uuid), "
                "target_mode => NULL::text, target => NULL::jsonb, "
                "target_deployment_event_id => NULL::uuid, "
                "target_deployment_revision => NULL::integer, "
                "target_is_pending => false)"
            ),
            {"workspace_id": str(workspace_id)},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if not isinstance(row, Mapping) or not isinstance(row.get("mode"), str):
            raise ScorerDeploymentRightsUnavailable("Gateway scorer không trả mode hợp lệ.")
        try:
            mode = ScorerDeploymentMode(row["mode"])
        except ValueError as error:
            raise ScorerDeploymentRightsUnavailable(
                "Gateway scorer trả mode không hỗ trợ."
            ) from error
        return self._target_from_row(row, workspace_id=workspace_id, mode=mode)

    @staticmethod
    def _target_from_row(
        row: object,
        *,
        workspace_id: UUID,
        mode: ScorerDeploymentMode,
    ) -> ResolvedScorerRuntimeTarget:
        if not isinstance(row, Mapping):
            raise ScorerDeploymentRightsUnavailable("Gateway trả row scorer sai schema.")
        expected_names = {
            "workspace_id",
            "mode",
            "deployment_event_id",
            "deployment_revision",
            "registry_id",
            "registry_revision",
            "active_pointer_version",
            "scorer_artifact_id",
            "scorer_artifact_hash",
            "base_artifact_id",
            "base_artifact_hash",
            "base_artifact_canonical",
            "scorer_artifact_canonical",
            "evaluation_report_id",
            "evaluation_report_hash",
            "evaluation_status",
            "candidate_completeness_verified",
            "scored_candidate_coverage_verified",
            "report_scope_hash",
            "proof_id",
            "proof_hash",
            "proof_scope_hash",
            "policy_id",
            "policy_hash",
            "current_rights_receipt_hash",
            "source_rights_fingerprint",
        }
        if set(row.keys()) != expected_names:
            raise ScorerDeploymentRightsUnavailable("Gateway scorer trả schema không đầy đủ.")
        if row["workspace_id"] != workspace_id or row["mode"] != mode.value:
            raise ScorerDeploymentRightsUnavailable("Gateway scorer trả workspace/mode khác.")
        try:
            base_artifact = learning_cpu_artifact_from_canonical_json(
                row["base_artifact_canonical"], expected_hash=row["base_artifact_hash"]
            )
            scorer_artifact = learning_scorer_artifact_from_canonical_json(
                row["scorer_artifact_canonical"], expected_hash=row["scorer_artifact_hash"]
            )
            target = ScorerDeploymentTarget(
                registry_id=row["registry_id"],
                registry_revision=row["registry_revision"],
                active_pointer_version=row["active_pointer_version"],
                scorer_artifact_id=row["scorer_artifact_id"],
                scorer_artifact_hash=row["scorer_artifact_hash"],
                evaluation_report_id=row["evaluation_report_id"],
                evaluation_report_hash=row["evaluation_report_hash"],
                evaluation_status=row["evaluation_status"],
                proof_id=row["proof_id"],
                proof_hash=row["proof_hash"],
                candidate_completeness_verified=row["candidate_completeness_verified"],
                report_scope_hash=row["report_scope_hash"],
                proof_scope_hash=row["proof_scope_hash"],
                policy_id=row["policy_id"],
                policy_hash=row["policy_hash"],
                current_rights_receipt_hash=row["current_rights_receipt_hash"],
            )
            target.require_mode(mode)
            resolved = ResolvedScorerRuntimeTarget(
                mode=mode,
                deployment_event_id=row["deployment_event_id"],
                deployment_revision=row["deployment_revision"],
                target=target,
                base_artifact_id=row["base_artifact_id"],
                base_artifact_hash=row["base_artifact_hash"],
                base_artifact=base_artifact,
                scorer_artifact=scorer_artifact,
                source_rights_fingerprint=row["source_rights_fingerprint"],
                scored_candidate_coverage_verified=row["scored_candidate_coverage_verified"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ScorerDeploymentRightsUnavailable(
                "Target/artifact scorer không vượt qua kiểm canonical và lineage."
            ) from error
        if (
            scorer_artifact.workspace_id != str(workspace_id)
            or base_artifact.workspace_id != str(workspace_id)
            or scorer_artifact.artifact_hash != row["scorer_artifact_hash"]
            or scorer_artifact.base_artifact_hash != row["base_artifact_hash"]
            or base_artifact.artifact_hash != row["base_artifact_hash"]
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Artifact scorer/base không cùng workspace hoặc hash target."
            )
        return resolved


def learning_cpu_artifact_from_canonical_json(
    canonical_artifact: object,
    *,
    expected_hash: object,
) -> LearningCpuArtifact:
    """Khôi phục v1 từ đúng canonical bytes do gateway cấp và kiểm hash."""
    payload = _canonical_payload(canonical_artifact, expected_hash, "base artifact")
    if set(payload) != {field.name for field in fields(LearningCpuArtifact)}:
        raise ValueError("Base artifact sai schema.")
    models = tuple(
        LearningCpuClassModel(
            label=_strict_int(item["label"]),
            intercept=_strict_float(item["intercept"]),
            coefficients=tuple(_strict_float(value) for value in item["coefficients"]),
            training_positive_count=_strict_int(item["training_positive_count"]),
        )
        for item in _strict_list(payload["models"])
    )
    raw_validation = _strict_dict(payload["validation_report"])
    validation = LearningCpuValidationReport(
        sample_count=_strict_int(raw_validation["sample_count"]),
        accuracy=_strict_float(raw_validation["accuracy"]),
        majority_baseline_accuracy=_strict_float(raw_validation["majority_baseline_accuracy"]),
        macro_recall=_strict_float(raw_validation["macro_recall"]),
        log_loss=_strict_float(raw_validation["log_loss"]),
        majority_class=_strict_int(raw_validation["majority_class"]),
        confusion_matrix=_strict_matrix(raw_validation["confusion_matrix"]),
        class_support=_strict_pairs(raw_validation["class_support"]),
    )
    request = LearningResourceRequest(**_strict_int_mapping(payload["resource_request"]))
    plan = LearningResourcePlan(**_strict_int_mapping(payload["resource_plan"]))
    values = dict(payload)
    values["resource_request"] = request
    values["resource_plan"] = plan
    values["models"] = models
    values["training_class_counts"] = _strict_pairs(payload["training_class_counts"])
    values["validation_report"] = validation
    values["feature_names"] = tuple(
        _strict_text(value) for value in _strict_list(payload["feature_names"])
    )
    values["feature_means"] = tuple(
        _strict_float(value) for value in _strict_list(payload["feature_means"])
    )
    values["feature_scales"] = tuple(
        _strict_float(value) for value in _strict_list(payload["feature_scales"])
    )
    values["seed"] = _strict_int(payload["seed"])
    values["optimization_steps"] = _strict_int(payload["optimization_steps"])
    values["actual_operation_units"] = _strict_int(payload["actual_operation_units"])
    artifact = LearningCpuArtifact(**values)
    if artifact.canonical_json != canonical_artifact:
        raise ValueError("Base artifact reconstruction không canonical.")
    return artifact


def learning_scorer_artifact_from_canonical_json(
    canonical_artifact: object,
    *,
    expected_hash: object,
) -> LearningScorerArtifact:
    """Khôi phục scorer v2 từ đúng canonical bytes do gateway cấp và kiểm hash."""
    payload = _canonical_payload(canonical_artifact, expected_hash, "scorer artifact")
    if set(payload) != {field.name for field in fields(LearningScorerArtifact)}:
        raise ValueError("Scorer artifact sai schema.")
    values = dict(payload)
    values["validation_class_support"] = _strict_pairs(payload["validation_class_support"])
    artifact = LearningScorerArtifact(**values)
    if artifact.canonical_json != canonical_artifact or artifact.artifact_hash != expected_hash:
        raise ValueError("Scorer artifact reconstruction không canonical.")
    return artifact


def _canonical_payload(raw: object, expected_hash: object, name: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not _is_sha256(expected_hash):
        raise ValueError(f"{name} bytes/hash không hợp lệ.")
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
        canonical = _canonical_json(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{name} JSON không hợp lệ.") from error
    if (
        type(payload) is not dict
        or canonical != raw
        or hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_hash
    ):
        raise ValueError(f"{name} canonical/hash không khớp.")
    return payload


def _strict_dict(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("Artifact nested value phải là object.")
    return value


def _strict_list(value: object) -> list[Any]:
    if type(value) is not list:
        raise ValueError("Artifact array sai schema.")
    return value


def _strict_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Artifact text sai schema.")
    return value


def _strict_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Artifact integer sai schema.")
    return value


def _strict_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Artifact number sai schema.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Artifact number không hữu hạn.")
    return parsed


def _strict_pairs(value: object) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for pair in _strict_list(value):
        item = _strict_list(pair)
        if len(item) != 2:
            raise ValueError("Artifact pair sai schema.")
        result.append((_strict_int(item[0]), _strict_int(item[1])))
    return tuple(result)


def _strict_matrix(value: object) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(_strict_int(item) for item in _strict_list(row)) for row in _strict_list(value)
    )


def _strict_int_mapping(value: object) -> dict[str, int]:
    return {key: _strict_int(item) for key, item in _strict_dict(value).items()}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant không hợp lệ: {value}.")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "PostgresScorerRuntimeTargetRepository",
    "ResolvedScorerRuntimeTarget",
    "learning_cpu_artifact_from_canonical_json",
    "learning_scorer_artifact_from_canonical_json",
]
