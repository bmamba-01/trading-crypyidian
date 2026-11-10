"""Hợp đồng ghim quyết định cặp cho observer shadow fail-closed."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from trading_bot.domain.learning.model_registry import LearningModelRegistryState

_SHA256 = re.compile(r"[0-9a-f]{64}")


class IncumbentDecisionStatus(StrEnum):
    """Trạng thái quyết định incumbent đã được lưu trong ledger PAPER."""

    CANDIDATE = "CANDIDATE"
    ABSTAIN = "ABSTAIN"
    VETO = "VETO"


class ShadowAbstainReason(StrEnum):
    """Lý do challenger chưa được phép phát score hợp lệ."""

    UNCALIBRATED_SCORE = "UNCALIBRATED_SCORE"
    FEATURE_SCHEMA_MISMATCH = "FEATURE_SCHEMA_MISMATCH"
    SOURCE_RIGHTS_INELIGIBLE = "SOURCE_RIGHTS_INELIGIBLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"


@dataclass(frozen=True, slots=True)
class ShadowSnapshotReference:
    """Tham chiếu snapshot đã persist; không mang vector feature."""

    workspace_id: UUID
    snapshot_id: str
    snapshot_hash: str
    schema_hash: str
    as_of: datetime

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        _nonempty(self.snapshot_id, "snapshot_id")
        _sha256(self.snapshot_hash, "snapshot_hash")
        _sha256(self.schema_hash, "schema_hash")
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))


@dataclass(frozen=True, slots=True)
class ShadowModelReference:
    """Lineage artifact và hai lớp phê duyệt được ghim riêng biệt."""

    workspace_id: UUID
    registry_id: UUID
    artifact_id: UUID
    artifact_hash: str
    manifest_hash: str
    fold_plan_hash: str
    training_approval_hash: str
    registry_approval_event_id: UUID
    registry_approval_event_hash: str
    registry_approval_event_sequence: int
    revision: int
    state: LearningModelRegistryState

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "registry_id",
            "artifact_id",
            "registry_approval_event_id",
        ):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} phải là UUID.")
        for name in (
            "artifact_hash",
            "manifest_hash",
            "fold_plan_hash",
            "training_approval_hash",
            "registry_approval_event_hash",
        ):
            _sha256(getattr(self, name), name)
        if (
            type(self.registry_approval_event_sequence) is not int
            or self.registry_approval_event_sequence < 1
        ):
            raise ValueError("registry_approval_event_sequence phải là số nguyên dương.")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision registry phải là số nguyên dương.")
        if (
            type(self.state) is not LearningModelRegistryState
            or self.state is not LearningModelRegistryState.EVALUATED
        ):
            raise ValueError("Challenger phải được duyệt ở trạng thái EVALUATED.")


@dataclass(frozen=True, slots=True)
class IncumbentDecisionReference:
    """Quyết định incumbent đã persist, ghim strategy release và snapshot."""

    decision_id: UUID
    workspace_id: UUID
    decision_hash: str
    strategy_release_id: str
    strategy_release_hash: str
    status: IncumbentDecisionStatus
    snapshot: ShadowSnapshotReference
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if any(type(value) is not UUID for value in (self.decision_id, self.workspace_id)):
            raise ValueError("Định danh incumbent phải là UUID.")
        _sha256(self.decision_hash, "decision_hash")
        _sha256(self.strategy_release_hash, "strategy_release_hash")
        _nonempty(self.strategy_release_id, "strategy_release_id")
        if type(self.status) is not IncumbentDecisionStatus:
            raise ValueError("status phải là IncumbentDecisionStatus.")
        if type(self.snapshot) is not ShadowSnapshotReference:
            raise ValueError("snapshot phải là ShadowSnapshotReference.")
        if self.snapshot.workspace_id != self.workspace_id:
            raise ValueError("Snapshot incumbent phải cùng workspace.")
        evaluated_at = _utc(self.evaluated_at, "evaluated_at")
        if evaluated_at < self.snapshot.as_of:
            raise ValueError("Quyết định incumbent không được có trước as_of snapshot.")
        object.__setattr__(self, "evaluated_at", evaluated_at)


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """Pair metadata-only; challenger ABSTAIN, không có execution port."""

    observation_id: UUID
    workspace_id: UUID
    snapshot: ShadowSnapshotReference
    challenger_model: ShadowModelReference
    incumbent_decision: IncumbentDecisionReference
    active_registry_id: UUID
    active_registry_revision: int
    active_artifact_hash: str
    active_pointer_version: int
    snapshot_coverage: float
    snapshot_quality_ok: bool
    schema_compatible: bool
    rights_eligible: bool
    rights_fingerprint: str | None
    rights_source_id: str | None
    rights_policy_id: str | None
    rights_policy_sequence: int | None
    rights_known_at: datetime | None
    challenger_reason: ShadowAbstainReason
    observed_at: datetime

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.observation_id, self.workspace_id, self.active_registry_id)
        ):
            raise ValueError("Observation/workspace identifier phải là UUID.")
        if type(self.snapshot) is not ShadowSnapshotReference:
            raise ValueError("snapshot phải là ShadowSnapshotReference.")
        if type(self.challenger_model) is not ShadowModelReference:
            raise ValueError("challenger_model phải là ShadowModelReference.")
        if type(self.incumbent_decision) is not IncumbentDecisionReference:
            raise ValueError("incumbent_decision phải là IncumbentDecisionReference.")
        if any(
            reference.workspace_id != self.workspace_id
            for reference in (
                self.snapshot,
                self.challenger_model,
                self.incumbent_decision,
            )
        ):
            raise ValueError("Lineage observation phải cùng workspace.")
        if self.challenger_model.state is not LearningModelRegistryState.EVALUATED:
            raise ValueError("Challenger phải được duyệt ở trạng thái EVALUATED.")
        if self.incumbent_decision.snapshot != self.snapshot:
            raise ValueError("Snapshot incumbent không khớp snapshot được so sánh.")
        if self.active_registry_id == self.challenger_model.registry_id:
            raise ValueError("Challenger không được trùng model đang được active.")
        if type(self.active_registry_revision) is not int or self.active_registry_revision < 1:
            raise ValueError("active_registry_revision phải là số nguyên dương.")
        _sha256(self.active_artifact_hash, "active_artifact_hash")
        if type(self.active_pointer_version) is not int or self.active_pointer_version < 1:
            raise ValueError("active_pointer_version phải là số nguyên dương.")
        if (
            isinstance(self.snapshot_coverage, bool)
            or not isinstance(self.snapshot_coverage, (int, float))
            or not math.isfinite(self.snapshot_coverage)
            or not 0.0 <= self.snapshot_coverage <= 1.0
        ):
            raise ValueError("snapshot_coverage phải là số hữu hạn trong [0, 1].")
        for name in ("snapshot_quality_ok", "schema_compatible", "rights_eligible"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} phải là boolean.")
        if type(self.challenger_reason) is not ShadowAbstainReason:
            raise ValueError("Challenger cần lý do ABSTAIN đã biết.")
        if (
            not self.schema_compatible
            and self.rights_eligible
            and self.challenger_reason is not ShadowAbstainReason.FEATURE_SCHEMA_MISMATCH
        ):
            raise ValueError("Schema không tương thích phải tạo ABSTAIN FEATURE_SCHEMA_MISMATCH.")
        if (
            not self.rights_eligible
            and self.challenger_reason is not ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE
        ):
            raise ValueError("Nguồn chưa đủ quyền phải tạo ABSTAIN SOURCE_RIGHTS_INELIGIBLE.")
        observed_at = _utc(self.observed_at, "observed_at")
        if observed_at < self.snapshot.as_of:
            raise ValueError("Observation không được có trước snapshot.")
        if observed_at < self.incumbent_decision.evaluated_at:
            raise ValueError("Observation không được có trước quyết định incumbent.")
        object.__setattr__(self, "observed_at", observed_at)
        if self.rights_fingerprint is not None:
            _sha256(self.rights_fingerprint, "rights_fingerprint")
        if self.rights_eligible:
            if self.rights_fingerprint is None:
                raise ValueError("rights_fingerprint bắt buộc khi quyền nguồn đã xác minh.")
            for name in ("rights_source_id", "rights_policy_id"):
                _nonempty(getattr(self, name), name)
            if type(self.rights_policy_sequence) is not int or self.rights_policy_sequence < 1:
                raise ValueError("rights_policy_sequence phải là số nguyên dương.")
            if self.rights_known_at is None:
                raise ValueError("rights_known_at bắt buộc khi quyền nguồn đã xác minh.")
            rights_known_at = _utc(self.rights_known_at, "rights_known_at")
            if rights_known_at > observed_at:
                raise ValueError("rights_known_at không được sau observed_at.")
            object.__setattr__(self, "rights_known_at", rights_known_at)
        elif any(
            value is not None
            for value in (
                self.rights_fingerprint,
                self.rights_source_id,
                self.rights_policy_id,
                self.rights_policy_sequence,
                self.rights_known_at,
            )
        ):
            raise ValueError(
                "rights_fingerprint và receipt policy phải null khi quyền chưa xác minh."
            )

    @property
    def idempotency_key(self) -> str:
        """Khóa retry ổn định theo incumbent, challenger, snapshot và pointer."""
        payload = {
            "active_artifact_hash": self.active_artifact_hash,
            "active_registry_id": str(self.active_registry_id),
            "active_registry_revision": self.active_registry_revision,
            "active_pointer_version": self.active_pointer_version,
            "challenger_registry_id": str(self.challenger_model.registry_id),
            "challenger_revision": self.challenger_model.revision,
            "registry_approval_event_hash": self.challenger_model.registry_approval_event_hash,
            "registry_approval_event_id": str(self.challenger_model.registry_approval_event_id),
            "registry_approval_event_sequence": (
                self.challenger_model.registry_approval_event_sequence
            ),
            "challenger_reason": self.challenger_reason.value,
            "incumbent_decision_id": str(self.incumbent_decision.decision_id),
            "incumbent_decision_hash": self.incumbent_decision.decision_hash,
            "incumbent_strategy_release_hash": self.incumbent_decision.strategy_release_hash,
            "snapshot_hash": self.snapshot.snapshot_hash,
            "snapshot_coverage": self.snapshot_coverage,
            "snapshot_quality_ok": self.snapshot_quality_ok,
            "schema_compatible": self.schema_compatible,
            "rights_eligible": self.rights_eligible,
            "rights_fingerprint": self.rights_fingerprint,
            "rights_source_id": self.rights_source_id,
            "rights_policy_id": self.rights_policy_id,
            "rights_policy_sequence": self.rights_policy_sequence,
            "rights_known_at": (
                _iso(self.rights_known_at) if self.rights_known_at is not None else None
            ),
            "workspace_id": str(self.workspace_id),
        }
        return _digest(_canonical(payload))

    @property
    def content_hash(self) -> str:
        """Hash canonical của lineage đã ghim; không gồm score/vector/order."""
        return _digest(_canonical(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        """Chiếu metadata ổn định, không sao chép snapshot hay outcome payload."""
        return {
            "active_artifact_hash": self.active_artifact_hash,
            "active_registry_id": str(self.active_registry_id),
            "active_registry_revision": self.active_registry_revision,
            "active_pointer_version": self.active_pointer_version,
            "challenger_model": _model_payload(self.challenger_model),
            "challenger_reason": self.challenger_reason.value,
            "challenger_snapshot_state": {
                "coverage": self.snapshot_coverage,
                "quality_ok": self.snapshot_quality_ok,
                "schema_compatible": self.schema_compatible,
            },
            "incumbent_decision": {
                "decision_hash": self.incumbent_decision.decision_hash,
                "decision_id": str(self.incumbent_decision.decision_id),
                "evaluated_at": _iso(self.incumbent_decision.evaluated_at),
                "status": self.incumbent_decision.status.value,
                "strategy_release_hash": self.incumbent_decision.strategy_release_hash,
                "strategy_release_id": self.incumbent_decision.strategy_release_id,
            },
            "observation_id": str(self.observation_id),
            "observed_at": _iso(self.observed_at),
            "source_rights": {
                "eligible": self.rights_eligible,
                "fingerprint": self.rights_fingerprint,
                "known_at": _iso(self.rights_known_at) if self.rights_known_at else None,
                "policy_id": self.rights_policy_id,
                "policy_sequence": self.rights_policy_sequence,
                "source_id": self.rights_source_id,
            },
            "snapshot": {
                "as_of": _iso(self.snapshot.as_of),
                "schema_hash": self.snapshot.schema_hash,
                "snapshot_hash": self.snapshot.snapshot_hash,
                "snapshot_id": self.snapshot.snapshot_id,
            },
            "workspace_id": str(self.workspace_id),
        }


def _model_payload(model: ShadowModelReference) -> dict[str, object]:
    return {
        "artifact_hash": model.artifact_hash,
        "artifact_id": str(model.artifact_id),
        "fold_plan_hash": model.fold_plan_hash,
        "manifest_hash": model.manifest_hash,
        "registry_approval_event_hash": model.registry_approval_event_hash,
        "registry_approval_event_id": str(model.registry_approval_event_id),
        "registry_approval_event_sequence": model.registry_approval_event_sequence,
        "registry_id": str(model.registry_id),
        "revision": model.revision,
        "state": model.state.value,
        "training_approval_hash": model.training_approval_hash,
        "workspace_id": str(model.workspace_id),
    }


def _nonempty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} không được để trống.")


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có timezone.")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "IncumbentDecisionReference",
    "IncumbentDecisionStatus",
    "ShadowAbstainReason",
    "ShadowModelReference",
    "ShadowObservation",
    "ShadowSnapshotReference",
]
