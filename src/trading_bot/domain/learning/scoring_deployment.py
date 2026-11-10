"""Hợp đồng bất biến cho governance và runtime deployment của scorer."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACCEPTED_REPORTS = frozenset({"ACCEPTED_SHADOW_ONLY", "ACCEPTED_PAPER_GATE_ELIGIBLE"})
_PAPER_GATE_REPORT = "ACCEPTED_PAPER_GATE_ELIGIBLE"
_MAX_MFA_AGE = timedelta(minutes=15)


class ScorerDeploymentMode(StrEnum):
    """Chế độ scorer đã được reviewer duyệt."""

    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    PAPER_GATE = "PAPER_GATE"


class ScorerDeploymentHealth(StrEnum):
    """Tình trạng read-only dành cho admin, không phải quyền chấm runtime."""

    DISABLED = "DISABLED"
    RIGHTS_RECHECK_REQUIRED = "RIGHTS_RECHECK_REQUIRED"


@dataclass(frozen=True, slots=True)
class ScorerDeploymentTarget:
    """Lineage metadata đã được repository nạp và kiểm tra từ DB authoritative."""

    registry_id: UUID
    registry_revision: int
    active_pointer_version: int
    scorer_artifact_id: UUID
    scorer_artifact_hash: str
    evaluation_report_id: UUID
    evaluation_report_hash: str
    evaluation_status: str
    proof_id: UUID | None
    proof_hash: str | None
    candidate_completeness_verified: bool | None
    report_scope_hash: str
    proof_scope_hash: str | None
    policy_id: str
    policy_hash: str
    current_rights_receipt_hash: str

    def __post_init__(self) -> None:
        for name in (
            "registry_id",
            "scorer_artifact_id",
            "evaluation_report_id",
        ):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} phải là UUID authoritative.")
        for name in ("registry_revision", "active_pointer_version"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == "registry_revision" else 0):
                raise ValueError(f"{name} không hợp lệ.")
        for name in (
            "scorer_artifact_hash",
            "evaluation_report_hash",
            "report_scope_hash",
            "policy_hash",
            "current_rights_receipt_hash",
        ):
            if not isinstance(getattr(self, name), str) or not _SHA256.fullmatch(
                getattr(self, name)
            ):
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
        if not isinstance(self.evaluation_status, str) or not self.evaluation_status.strip():
            raise ValueError("evaluation_status không được để trống.")
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id không được để trống.")
        if (self.proof_id is None) != (self.proof_hash is None):
            raise ValueError("proof_id và proof_hash phải đi cùng nhau.")
        if self.proof_id is not None and type(self.proof_id) is not UUID:
            raise ValueError("proof_id phải là UUID authoritative.")
        if self.proof_hash is not None and (
            not isinstance(self.proof_hash, str) or not _SHA256.fullmatch(self.proof_hash)
        ):
            raise ValueError("proof_hash phải là SHA-256 hex chữ thường.")
        if self.proof_scope_hash is not None and (
            not isinstance(self.proof_scope_hash, str)
            or not _SHA256.fullmatch(self.proof_scope_hash)
        ):
            raise ValueError("proof_scope_hash phải là SHA-256 hex chữ thường.")
        if (
            self.candidate_completeness_verified is not None
            and type(self.candidate_completeness_verified) is not bool
        ):
            raise ValueError("candidate_completeness_verified phải là bool hoặc None.")

    def require_mode(self, mode: ScorerDeploymentMode) -> None:
        """Từ chối mode không được chứng minh bởi report/proof đã lưu."""
        if type(mode) is not ScorerDeploymentMode:
            raise ValueError("mode phải là ScorerDeploymentMode.")
        if mode is ScorerDeploymentMode.DISABLED:
            return
        if self.evaluation_status not in _ACCEPTED_REPORTS:
            raise ValueError("Chỉ report scorer đã ACCEPTED mới được bật scorer.")
        if self.proof_id is None or self.proof_hash is None or self.proof_scope_hash is None:
            raise ValueError("Deployment scorer cần proof TEST authoritative.")
        if self.proof_scope_hash != self.report_scope_hash:
            raise ValueError("Proof TEST không cùng scope với report scorer.")
        if mode is ScorerDeploymentMode.PAPER_GATE:
            if self.evaluation_status != _PAPER_GATE_REPORT:
                raise ValueError("PAPER_GATE cần report ACCEPTED_PAPER_GATE_ELIGIBLE.")
            if self.candidate_completeness_verified is not True:
                raise ValueError("PAPER_GATE cần candidate completeness đã xác minh.")


@dataclass(frozen=True, slots=True)
class ScorerDeploymentSnapshot:
    """Read contract metadata-only dùng lúc runtime resolve và recheck dưới lock."""

    workspace_id: UUID
    mode: ScorerDeploymentMode
    deployment_event_id: UUID | None
    deployment_revision: int
    active_pointer_version: int
    target: ScorerDeploymentTarget | None

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if type(self.mode) is not ScorerDeploymentMode:
            raise ValueError("mode phải là ScorerDeploymentMode.")
        if self.deployment_event_id is not None and type(self.deployment_event_id) is not UUID:
            raise ValueError("deployment_event_id phải là UUID hoặc None.")
        if type(self.deployment_revision) is not int or self.deployment_revision < 0:
            raise ValueError("deployment_revision không được âm.")
        if type(self.active_pointer_version) is not int or self.active_pointer_version < 0:
            raise ValueError("active_pointer_version không được âm.")
        if self.mode is ScorerDeploymentMode.DISABLED:
            if self.target is not None:
                raise ValueError("DISABLED không được trả target có thể chấm điểm.")
        elif type(self.target) is not ScorerDeploymentTarget:
            raise ValueError("Mode đang bật cần target scorer authoritative.")
        if (self.deployment_revision == 0) != (self.deployment_event_id is None):
            raise ValueError("Event đầu và revision deployment không khớp.")

    @property
    def registry_id(self) -> UUID | None:
        return self.target.registry_id if self.target else None

    @property
    def registry_revision(self) -> int | None:
        return self.target.registry_revision if self.target else None

    @property
    def scorer_artifact_id(self) -> UUID | None:
        return self.target.scorer_artifact_id if self.target else None

    @property
    def scorer_artifact_hash(self) -> str | None:
        return self.target.scorer_artifact_hash if self.target else None

    @property
    def evaluation_report_id(self) -> UUID | None:
        return self.target.evaluation_report_id if self.target else None

    @property
    def evaluation_report_hash(self) -> str | None:
        return self.target.evaluation_report_hash if self.target else None

    @property
    def proof_id(self) -> UUID | None:
        return self.target.proof_id if self.target else None

    @property
    def proof_hash(self) -> str | None:
        return self.target.proof_hash if self.target else None

    @property
    def policy_id(self) -> str | None:
        return self.target.policy_id if self.target else None

    @property
    def policy_hash(self) -> str | None:
        return self.target.policy_hash if self.target else None

    @property
    def current_rights_receipt_hash(self) -> str | None:
        return self.target.current_rights_receipt_hash if self.target else None


@dataclass(frozen=True, slots=True)
class ScorerDeploymentAdminSnapshot:
    """CAS/read model cho phép admin disable cả deployment đang bị khóa quyền."""

    workspace_id: UUID
    mode: ScorerDeploymentMode
    deployment_event_id: UUID | None
    deployment_revision: int
    active_pointer_version: int
    health: ScorerDeploymentHealth
    runtime_usable: bool = False

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if type(self.mode) is not ScorerDeploymentMode:
            raise ValueError("mode phải là ScorerDeploymentMode.")
        if self.deployment_event_id is not None and type(self.deployment_event_id) is not UUID:
            raise ValueError("deployment_event_id phải là UUID hoặc None.")
        if type(self.deployment_revision) is not int or self.deployment_revision < 0:
            raise ValueError("deployment_revision không được âm.")
        if type(self.active_pointer_version) is not int or self.active_pointer_version < 0:
            raise ValueError("active_pointer_version không được âm.")
        if type(self.health) is not ScorerDeploymentHealth:
            raise ValueError("health phải là ScorerDeploymentHealth.")
        if type(self.runtime_usable) is not bool or self.runtime_usable:
            raise ValueError("Admin snapshot không bao giờ là runtime target.")
        if (self.deployment_revision == 0) != (self.deployment_event_id is None):
            raise ValueError("Event đầu và revision deployment không khớp.")
        expected_health = (
            ScorerDeploymentHealth.DISABLED
            if self.mode is ScorerDeploymentMode.DISABLED
            else ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
        )
        if self.health is not expected_health:
            raise ValueError("Health admin phải fail-closed theo mode hiện hành.")


@dataclass(frozen=True, slots=True)
class ScorerDeploymentEvent:
    """Sự kiện governance bất biến; workspace/actor chỉ đến từ Principal phía ứng dụng."""

    event_id: UUID
    workspace_id: UUID
    revision: int
    mode: ScorerDeploymentMode
    target: ScorerDeploymentTarget | None
    actor_membership_id: UUID
    mfa_verified_at: datetime
    mfa_method: str
    expected_pointer_version: int
    expected_deployment_revision: int
    reason: str
    idempotency_key: UUID
    previous_event_id: UUID | None
    previous_event_hash: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("event_id", "workspace_id", "actor_membership_id", "idempotency_key"):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} phải là UUID.")
        if type(self.mode) is not ScorerDeploymentMode:
            raise ValueError("mode phải là ScorerDeploymentMode.")
        if self.mode is ScorerDeploymentMode.DISABLED:
            if self.target is not None:
                raise ValueError("DISABLED không được giữ target active.")
        elif type(self.target) is not ScorerDeploymentTarget:
            raise ValueError("Mode đang bật cần target scorer.")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision phải là số nguyên dương.")
        for name in ("expected_pointer_version", "expected_deployment_revision"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} không được âm.")
        if self.revision != self.expected_deployment_revision + 1:
            raise ValueError("revision phải tăng đúng một theo expected deployment revision.")
        if (self.previous_event_id is None) != (self.previous_event_hash is None):
            raise ValueError("previous_event_id và previous_event_hash phải đi cùng nhau.")
        if self.previous_event_id is not None and type(self.previous_event_id) is not UUID:
            raise ValueError("previous_event_id phải là UUID.")
        if self.previous_event_hash is not None and not _SHA256.fullmatch(self.previous_event_hash):
            raise ValueError("previous_event_hash phải là SHA-256.")
        if not isinstance(self.reason, str) or not 12 <= len(self.reason.strip()) <= 2000:
            raise ValueError("reason phải có ít nhất 12 và tối đa 2000 ký tự.")
        if self.mfa_method != "TOTP":
            raise ValueError("MFA method deployment phải đến từ phiên TOTP đã xác minh.")
        object.__setattr__(self, "mfa_verified_at", _aware_utc(self.mfa_verified_at))
        object.__setattr__(self, "created_at", _aware_utc(self.created_at))
        if self.mfa_verified_at > self.created_at:
            raise ValueError("MFA verified-at không được sau created_at.")
        if self.created_at - self.mfa_verified_at > _MAX_MFA_AGE:
            raise ValueError("MFA deployment đã hết hạn gần đây.")
        if self.mode is not ScorerDeploymentMode.DISABLED:
            assert self.target is not None
            self.target.require_mode(self.mode)
            if self.target.active_pointer_version != self.expected_pointer_version:
                raise ValueError("Target active pointer phải khớp phiên bản CAS đã khai báo.")

    @property
    def canonical_json(self) -> str:
        """JSON chuẩn lưu nguyên văn cùng event để kiểm tra hash tại DB."""
        return json.dumps(
            self.canonical_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def canonical_payload(self) -> dict[str, Any]:
        """Payload audit chuẩn, không chứa vector, sample hoặc TEST payload."""
        target = self.target
        return {
            "actor_membership_id": str(self.actor_membership_id),
            "created_at": _iso(self.created_at),
            "event_id": str(self.event_id),
            "expected_deployment_revision": self.expected_deployment_revision,
            "expected_pointer_version": self.expected_pointer_version,
            "idempotency_key": str(self.idempotency_key),
            "intent_hash": self.intent_hash,
            "mfa_method": self.mfa_method,
            "mfa_verified_at": _iso(self.mfa_verified_at),
            "mode": self.mode.value,
            "previous_event_hash": self.previous_event_hash,
            "previous_event_id": (str(self.previous_event_id) if self.previous_event_id else None),
            "reason": self.reason,
            "revision": self.revision,
            "schema_version": "crypidian.learning.scorer-deployment.v1",
            "target": None if target is None else _target_payload(target),
            "workspace_id": str(self.workspace_id),
        }

    @property
    def event_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @property
    def intent_hash(self) -> str:
        """Hash của ý định ổn định, bỏ UUID/thời điểm do server sinh khi retry."""
        payload = {
            "actor_membership_id": str(self.actor_membership_id),
            "expected_deployment_revision": self.expected_deployment_revision,
            "expected_pointer_version": self.expected_pointer_version,
            "idempotency_key": str(self.idempotency_key),
            "mode": self.mode.value,
            "reason": self.reason,
            "schema_version": "crypidian.learning.scorer-deployment-intent.v1",
            "target": None if self.target is None else _target_payload(self.target),
            "workspace_id": str(self.workspace_id),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _target_payload(target: ScorerDeploymentTarget) -> dict[str, object]:
    return {
        "active_pointer_version": target.active_pointer_version,
        "candidate_completeness_verified": target.candidate_completeness_verified,
        "current_rights_receipt_hash": target.current_rights_receipt_hash,
        "evaluation_report_hash": target.evaluation_report_hash,
        "evaluation_report_id": str(target.evaluation_report_id),
        "evaluation_status": target.evaluation_status,
        "policy_hash": target.policy_hash,
        "policy_id": target.policy_id,
        "proof_hash": target.proof_hash,
        "proof_id": str(target.proof_id) if target.proof_id else None,
        "proof_scope_hash": target.proof_scope_hash,
        "registry_id": str(target.registry_id),
        "registry_revision": target.registry_revision,
        "report_scope_hash": target.report_scope_hash,
        "scorer_artifact_hash": target.scorer_artifact_hash,
        "scorer_artifact_id": str(target.scorer_artifact_id),
    }


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Thời điểm MFA/deployment phải có timezone.")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ScorerDeploymentAdminSnapshot",
    "ScorerDeploymentEvent",
    "ScorerDeploymentHealth",
    "ScorerDeploymentMode",
    "ScorerDeploymentSnapshot",
    "ScorerDeploymentTarget",
]
