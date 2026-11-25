"""Hồ sơ readiness bất biến G0–G5 và server-side release gate (OPS07F / Task 10 / CRY-468).

Nguyên tắc cốt lõi:
- Dossier bất biến, có `schema_version` và `dossier_hash` (SHA-256) gắn chặt build, config,
  risk profile, venue, environment, account, symbol và strategy scope.
- Đánh giá độc lập từng gate G0–G5; bất kỳ trạng thái false/unknown/missing/expired/revoked
  nào đều giữ Live khóa và trả về mã lý do + actionable owner.
- Tách biệt hoàn toàn `CODE_RELEASE` approval và `RISK_CAPITAL` approval; cấm tự duyệt
  (self-approval) và cấm tuyệt đối AI agent tạo chữ ký hoặc chuyển trạng thái gate.
- G6 (Canary Live) KHÔNG được mở trong module này: ngay cả khi G0–G5 đều đạt,
  `live_dispatch_allowed` vẫn là `False` cho tới khi có quyết định G6 riêng.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

SUPPORTED_DOSSIER_SCHEMA_VERSION = "ops07f-dossier-v1"


class ReadinessGateId(StrEnum):
    """Sáu cổng readiness độc lập từ G0 đến G5 (G6 là bước kích hoạt riêng)."""

    G0 = "G0"  # Account eligibility & role/approval matrix
    G1 = "G1"  # Key, secret & network egress boundary
    G2 = "G2"  # Read-only preflight & venue reconciliation
    G3 = "G3"  # Risk semantics, signed hard limits & atomic enforcement
    G4 = "G4"  # Production topology, encrypted backup/restore & Demo window/sample
    G5 = "G5"  # Fail-closed fault matrix, security findings & operations runbook/drill


class EvidenceStatus(StrEnum):
    """Trạng thái hợp lệ của một bằng chứng thuộc gate."""

    VERIFIED = "VERIFIED"
    MISSING = "MISSING"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    FAILED = "FAILED"


class ApprovalScope(StrEnum):
    """Tách biệt phê duyệt phát hành mã nguồn với phê duyệt rủi ro/vốn."""

    CODE_RELEASE = "CODE_RELEASE"
    RISK_CAPITAL = "RISK_CAPITAL"


class ActorKind(StrEnum):
    """Phân loại chủ thể ký duyệt; AI_AGENT luôn bị chặn."""

    HUMAN_OWNER = "HUMAN_OWNER"
    HUMAN_RISK_APPROVER = "HUMAN_RISK_APPROVER"
    HUMAN_RELEASE_REVIEWER = "HUMAN_RELEASE_REVIEWER"
    AI_AGENT = "AI_AGENT"


class DossierReasonCode(StrEnum):
    """Mã lý do chuẩn hóa cho quyết định release gate G0–G5."""

    G0_G5_PASSED_LIVE_STILL_LOCKED = "G0_G5_PASSED_LIVE_STILL_LOCKED"
    DOSSIER_VERSION_UNSUPPORTED = "DOSSIER_VERSION_UNSUPPORTED"
    DOSSIER_EXPIRED = "DOSSIER_EXPIRED"
    DOSSIER_SCOPE_MISMATCH = "DOSSIER_SCOPE_MISMATCH"
    DOSSIER_HASH_MISMATCH = "DOSSIER_HASH_MISMATCH"
    EVIDENCE_MISSING_OR_INVALID = "EVIDENCE_MISSING_OR_INVALID"
    EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
    OPEN_SECURITY_FINDINGS = "OPEN_SECURITY_FINDINGS"
    DEMO_WINDOW_OR_SAMPLE_INSUFFICIENT = "DEMO_WINDOW_OR_SAMPLE_INSUFFICIENT"
    APPROVAL_MISSING_OR_INVALID = "APPROVAL_MISSING_OR_INVALID"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"
    AI_APPROVAL_FORBIDDEN = "AI_APPROVAL_FORBIDDEN"
    ROLE_REVOKED = "ROLE_REVOKED"


_GATE_ACTIONABLE_OWNER: dict[ReadinessGateId, str] = {
    ReadinessGateId.G0: "Owner",
    ReadinessGateId.G1: "Security / Platform Owner",
    ReadinessGateId.G2: "Execution / Ops Engineer",
    ReadinessGateId.G3: "Risk Owner",
    ReadinessGateId.G4: "Platform & Risk Owner",
    ReadinessGateId.G5: "Ops & Security Reviewer",
}


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} phải có timezone.")


@dataclass(frozen=True, slots=True)
class GateEvidenceItem:
    """Bằng chứng đã kiểm chứng cho một gate G0–G5; không chứa secret/PII."""

    gate: ReadinessGateId
    evidence_id: str
    source_ref: str
    command_or_check: str
    status: EvidenceStatus
    verified_by: str
    verified_at: datetime
    expires_at: datetime
    evidence_hash: str
    open_critical_findings: int = 0
    demo_days_observed: int | None = None
    demo_samples_observed: int | None = None

    def __post_init__(self) -> None:
        _require_aware(self.verified_at, "verified_at")
        _require_aware(self.expires_at, "expires_at")
        if len(self.evidence_hash) != 64:
            raise ValueError("evidence_hash phải là SHA-256 64 ký tự hex.")


@dataclass(frozen=True, slots=True)
class SignedApprovalRecord:
    """Chữ ký phê duyệt gắn chặt với `dossier_hash` và `approval_scope`."""

    approval_scope: ApprovalScope
    actor_id: str
    actor_kind: ActorKind
    membership_id: str
    role: str
    role_revoked: bool
    approved_dossier_hash: str
    signed_at: datetime
    expires_at: datetime
    signature_digest: str

    def __post_init__(self) -> None:
        _require_aware(self.signed_at, "signed_at")
        _require_aware(self.expires_at, "expires_at")


def sign_dossier_approval(
    *,
    dossier_hash: str,
    approval_scope: ApprovalScope,
    actor_id: str,
    signing_key: str,
) -> str:
    """Tạo chữ ký HMAC-SHA256 gắn chặt (dossier_hash, approval_scope, actor_id)."""
    msg = f"{dossier_hash}:{approval_scope.value}:{actor_id}".encode()
    return hmac.new(signing_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveReadinessDossier:
    """Hồ sơ readiness bất biến G0–G5; không đặt ngưỡng mặc định cho Demo window/sample."""

    schema_version: str
    build_hash: str
    config_hash: str
    risk_profile_hash: str
    venue: str
    environment: str
    account_id: str
    symbols: tuple[str, ...]
    strategy_release_ids: tuple[str, ...]
    required_demo_days: int
    required_demo_samples: int
    created_at: datetime
    expires_at: datetime
    evidences: tuple[GateEvidenceItem, ...]
    approvals: tuple[SignedApprovalRecord, ...]

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        if self.required_demo_days <= 0:
            raise ValueError("required_demo_days phải dương và do Risk Owner chỉ định.")
        if self.required_demo_samples <= 0:
            raise ValueError("required_demo_samples phải dương và do Risk Owner chỉ định.")
        if not self.symbols or not self.strategy_release_ids:
            raise ValueError("symbols và strategy_release_ids không được rỗng.")

    def compute_dossier_hash(self) -> str:
        """Tính SHA-256 bất biến cho toàn bộ scope và bằng chứng (trước bước ký duyệt)."""
        payload = {
            "schema_version": self.schema_version,
            "build_hash": self.build_hash,
            "config_hash": self.config_hash,
            "risk_profile_hash": self.risk_profile_hash,
            "venue": self.venue.lower(),
            "environment": self.environment.upper(),
            "account_id": self.account_id,
            "symbols": sorted(self.symbols),
            "strategy_release_ids": sorted(self.strategy_release_ids),
            "required_demo_days": self.required_demo_days,
            "required_demo_samples": self.required_demo_samples,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "evidences": [
                {
                    "gate": ev.gate.value,
                    "evidence_id": ev.evidence_id,
                    "source_ref": ev.source_ref,
                    "status": ev.status.value,
                    "verified_by": ev.verified_by,
                    "verified_at": ev.verified_at.astimezone(UTC).isoformat(),
                    "expires_at": ev.expires_at.astimezone(UTC).isoformat(),
                    "evidence_hash": ev.evidence_hash,
                    "open_critical_findings": ev.open_critical_findings,
                    "demo_days_observed": ev.demo_days_observed,
                    "demo_samples_observed": ev.demo_samples_observed,
                }
                for ev in sorted(self.evidences, key=lambda x: (x.gate.value, x.evidence_id))
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveReadinessGateRequest:
    """Ngữ cảnh thực thi server-side cần đối chiếu với dossier."""

    build_hash: str
    config_hash: str
    risk_profile_hash: str
    venue: str
    environment: str
    account_id: str
    symbols: tuple[str, ...]
    strategy_release_ids: tuple[str, ...]
    expected_dossier_hash: str
    executor_actor_id: str


@dataclass(frozen=True, slots=True)
class SingleGateEvaluation:
    """Kết quả đánh giá độc lập cho một gate G0..G5."""

    gate: ReadinessGateId
    passed: bool
    reason_code: DossierReasonCode
    actionable_owner: str
    detail_vi: str


@dataclass(frozen=True, slots=True)
class LiveReadinessGateVerdict:
    """Kết luận tổng hợp của server-side release gate G0–G5."""

    all_g0_g5_passed: bool
    live_dispatch_allowed: bool  # LUÔN False sau G5 (G6 là quyết định kích hoạt riêng)
    reason_code: DossierReasonCode
    detail_vi: str
    dossier_hash: str
    evaluated_at: datetime
    gate_results: dict[ReadinessGateId, SingleGateEvaluation]


def evaluate_single_gate(
    dossier: LiveReadinessDossier,
    gate: ReadinessGateId,
    *,
    as_of: datetime,
) -> SingleGateEvaluation:
    """Đánh giá độc lập một gate trong G0–G5; thiếu/hết hạn/finding mở đều fail-closed."""
    _require_aware(as_of, "as_of")
    owner = _GATE_ACTIONABLE_OWNER[gate]
    items = [ev for ev in dossier.evidences if ev.gate is gate]
    if not items:
        return SingleGateEvaluation(
            gate=gate,
            passed=False,
            reason_code=DossierReasonCode.EVIDENCE_MISSING_OR_INVALID,
            actionable_owner=owner,
            detail_vi=f"Thiếu bằng chứng kiểm chứng cho cổng {gate.value}.",
        )

    now_utc = as_of.astimezone(UTC)
    for ev in items:
        if ev.status is not EvidenceStatus.VERIFIED:
            return SingleGateEvaluation(
                gate=gate,
                passed=False,
                reason_code=DossierReasonCode.EVIDENCE_MISSING_OR_INVALID,
                actionable_owner=owner,
                detail_vi=(
                    f"Bằng chứng {ev.evidence_id} của {gate.value} chưa VERIFIED ({ev.status})."
                ),
            )
        if now_utc >= ev.expires_at.astimezone(UTC):
            return SingleGateEvaluation(
                gate=gate,
                passed=False,
                reason_code=DossierReasonCode.EVIDENCE_EXPIRED,
                actionable_owner=owner,
                detail_vi=f"Bằng chứng {ev.evidence_id} của {gate.value} đã hết hạn.",
            )
        if ev.open_critical_findings > 0:
            return SingleGateEvaluation(
                gate=gate,
                passed=False,
                reason_code=DossierReasonCode.OPEN_SECURITY_FINDINGS,
                actionable_owner=owner,
                detail_vi=(
                    f"Cổng {gate.value} còn {ev.open_critical_findings} "
                    "finding nghiêm trọng chưa đóng."
                ),
            )

    if gate is ReadinessGateId.G4:
        max_days = max((ev.demo_days_observed or 0) for ev in items)
        max_samples = max((ev.demo_samples_observed or 0) for ev in items)
        if max_days < dossier.required_demo_days or max_samples < dossier.required_demo_samples:
            return SingleGateEvaluation(
                gate=gate,
                passed=False,
                reason_code=DossierReasonCode.DEMO_WINDOW_OR_SAMPLE_INSUFFICIENT,
                actionable_owner=owner,
                detail_vi=(
                    f"Cổng G4 chưa đạt cửa sổ/mẫu Demo ({max_days}/{dossier.required_demo_days} "
                    f"ngày, {max_samples}/{dossier.required_demo_samples} mẫu)."
                ),
            )

    return SingleGateEvaluation(
        gate=gate,
        passed=True,
        reason_code=DossierReasonCode.G0_G5_PASSED_LIVE_STILL_LOCKED,
        actionable_owner=owner,
        detail_vi=f"Cổng {gate.value} đạt đủ bằng chứng hợp lệ.",
    )


def evaluate_live_readiness_gate(
    *,
    dossier: LiveReadinessDossier,
    request: LiveReadinessGateRequest,
    signing_key: str,
    as_of: datetime,
) -> LiveReadinessGateVerdict:
    """Đánh giá server-side toàn bộ dossier G0–G5. Luôn giữ `live_dispatch_allowed=False`."""
    _require_aware(as_of, "as_of")
    actual_hash = dossier.compute_dossier_hash()
    gate_results = {
        gate: evaluate_single_gate(dossier, gate, as_of=as_of) for gate in ReadinessGateId
    }

    def _deny(code: DossierReasonCode, detail_vi: str) -> LiveReadinessGateVerdict:
        return LiveReadinessGateVerdict(
            all_g0_g5_passed=False,
            live_dispatch_allowed=False,
            reason_code=code,
            detail_vi=detail_vi,
            dossier_hash=actual_hash,
            evaluated_at=as_of,
            gate_results=gate_results,
        )

    # 1. Schema version, hash pin, expiry & scope binding
    if dossier.schema_version != SUPPORTED_DOSSIER_SCHEMA_VERSION:
        return _deny(
            DossierReasonCode.DOSSIER_VERSION_UNSUPPORTED,
            f"Phiên bản dossier không được hỗ trợ: {dossier.schema_version}.",
        )
    if request.expected_dossier_hash != actual_hash:
        return _deny(
            DossierReasonCode.DOSSIER_HASH_MISMATCH,
            "Mã băm dossier không khớp (payload đã bị thay đổi hoặc sai phiên bản).",
        )
    now_utc = as_of.astimezone(UTC)
    if now_utc >= dossier.expires_at.astimezone(UTC):
        return _deny(
            DossierReasonCode.DOSSIER_EXPIRED,
            "Hồ sơ readiness G0–G5 đã hết hạn hiệu lực.",
        )
    if (
        dossier.build_hash != request.build_hash
        or dossier.config_hash != request.config_hash
        or dossier.risk_profile_hash != request.risk_profile_hash
        or dossier.venue.lower() != request.venue.lower()
        or dossier.environment.upper() != request.environment.upper()
        or dossier.account_id != request.account_id
        or set(dossier.symbols) != set(request.symbols)
        or set(dossier.strategy_release_ids) != set(request.strategy_release_ids)
    ):
        return _deny(
            DossierReasonCode.DOSSIER_SCOPE_MISMATCH,
            "Scope hồ sơ không khớp build, config, risk profile, account, symbol hoặc strategy.",
        )

    # 2. Kiểm tra từng gate G0–G5 độc lập
    for gate in ReadinessGateId:
        res = gate_results[gate]
        if not res.passed:
            return _deny(res.reason_code, res.detail_vi)

    # 3. Kiểm tra chữ ký phê duyệt tách biệt CODE_RELEASE & RISK_CAPITAL
    approvals_by_scope: dict[ApprovalScope, SignedApprovalRecord] = {}
    for app in dossier.approvals:
        if app.actor_kind is ActorKind.AI_AGENT:
            return _deny(
                DossierReasonCode.AI_APPROVAL_FORBIDDEN,
                "AI agent bị cấm tạo chữ ký, nới cap hoặc chuyển trạng thái gate.",
            )
        if app.role_revoked:
            return _deny(
                DossierReasonCode.ROLE_REVOKED,
                f"Quyền của người duyệt {app.actor_id} đã bị thu hồi.",
            )
        if now_utc >= app.expires_at.astimezone(UTC):
            return _deny(
                DossierReasonCode.APPROVAL_MISSING_OR_INVALID,
                f"Chữ ký duyệt {app.approval_scope.value} của {app.actor_id} đã hết hạn.",
            )
        if app.approved_dossier_hash != actual_hash:
            return _deny(
                DossierReasonCode.DOSSIER_HASH_MISMATCH,
                f"Chữ ký của {app.actor_id} không gắn với hash hiện hành của dossier.",
            )
        expected_sig = sign_dossier_approval(
            dossier_hash=actual_hash,
            approval_scope=app.approval_scope,
            actor_id=app.actor_id,
            signing_key=signing_key,
        )
        if not app.signature_digest or not hmac.compare_digest(app.signature_digest, expected_sig):
            return _deny(
                DossierReasonCode.APPROVAL_MISSING_OR_INVALID,
                f"Chữ ký duyệt {app.approval_scope.value} của {app.actor_id} không hợp lệ.",
            )
        if app.actor_id == request.executor_actor_id:
            return _deny(
                DossierReasonCode.SELF_APPROVAL_FORBIDDEN,
                "Vi phạm maker-checker: người duyệt trùng với người thực hiện.",
            )
        approvals_by_scope[app.approval_scope] = app

    if (
        ApprovalScope.CODE_RELEASE not in approvals_by_scope
        or ApprovalScope.RISK_CAPITAL not in approvals_by_scope
    ):
        return _deny(
            DossierReasonCode.APPROVAL_MISSING_OR_INVALID,
            "Thiếu đủ hai chữ ký tách biệt: CODE_RELEASE và RISK_CAPITAL.",
        )

    code_app = approvals_by_scope[ApprovalScope.CODE_RELEASE]
    risk_app = approvals_by_scope[ApprovalScope.RISK_CAPITAL]
    if code_app.actor_id == risk_app.actor_id:
        return _deny(
            DossierReasonCode.SELF_APPROVAL_FORBIDDEN,
            "Người phê duyệt RISK_CAPITAL phải độc lập với người phê duyệt CODE_RELEASE.",
        )

    # 4. G0–G5 đạt đầy đủ, NHƯNG Live vẫn khóa chờ quyết định G6 riêng
    return LiveReadinessGateVerdict(
        all_g0_g5_passed=True,
        live_dispatch_allowed=False,
        reason_code=DossierReasonCode.G0_G5_PASSED_LIVE_STILL_LOCKED,
        detail_vi="Đạt toàn bộ cổng G0–G5; Live tiếp tục khóa cho tới khi có phê duyệt G6 riêng.",
        dossier_hash=actual_hash,
        evaluated_at=as_of,
        gate_results=gate_results,
    )
