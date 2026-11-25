"""Unit tests cho hồ sơ readiness G0–G5 và server-side release gate (OPS07F / Task 10 / CRY-468)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from trading_bot.domain.execution.live_readiness_dossier import (
    SUPPORTED_DOSSIER_SCHEMA_VERSION,
    ActorKind,
    ApprovalScope,
    DossierReasonCode,
    EvidenceStatus,
    GateEvidenceItem,
    LiveReadinessDossier,
    LiveReadinessGateRequest,
    ReadinessGateId,
    SignedApprovalRecord,
    evaluate_live_readiness_gate,
    evaluate_single_gate,
    sign_dossier_approval,
)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
SIGNING_KEY = "cry468-synthetic-signing-key"


def _full_evidences(**g4_overrides) -> tuple[GateEvidenceItem, ...]:
    items = []
    for gate in ReadinessGateId:
        kwargs = {
            "gate": gate,
            "evidence_id": f"ev-{gate.value}",
            "source_ref": f"ops07f/{gate.value}",
            "command_or_check": "pytest",
            "status": EvidenceStatus.VERIFIED,
            "verified_by": "verifier-human",
            "verified_at": NOW - timedelta(hours=2),
            "expires_at": NOW + timedelta(days=7),
            "evidence_hash": "a" * 64,
            "open_critical_findings": 0,
        }
        if gate is ReadinessGateId.G4:
            kwargs["demo_days_observed"] = 14
            kwargs["demo_samples_observed"] = 120
            kwargs.update(g4_overrides)
        items.append(GateEvidenceItem(**kwargs))
    return tuple(items)


def _signed_dossier(
    *,
    evidences: tuple[GateEvidenceItem, ...] | None = None,
    schema_version: str = SUPPORTED_DOSSIER_SCHEMA_VERSION,
    expires_at: datetime = NOW + timedelta(days=3),
    code_actor: str = "release-owner-1",
    risk_actor: str = "risk-approver-2",
    code_kind: ActorKind = ActorKind.HUMAN_OWNER,
    risk_kind: ActorKind = ActorKind.HUMAN_RISK_APPROVER,
    risk_revoked: bool = False,
    omit_risk_approval: bool = False,
) -> LiveReadinessDossier:
    base = LiveReadinessDossier(
        schema_version=schema_version,
        build_hash="build-sha-20261125",
        config_hash="cfg-sha-v1",
        risk_profile_hash="risk-sha-v1",
        venue="binance_usdm",
        environment="LIVE",
        account_id="acc-live-syn",
        symbols=("BTCUSDT", "ETHUSDT"),
        strategy_release_ids=("h01-v1",),
        required_demo_days=14,
        required_demo_samples=100,
        created_at=NOW - timedelta(hours=1),
        expires_at=expires_at,
        evidences=evidences if evidences is not None else _full_evidences(),
        approvals=(),
    )
    h = base.compute_dossier_hash()
    approvals = [
        SignedApprovalRecord(
            approval_scope=ApprovalScope.CODE_RELEASE,
            actor_id=code_actor,
            actor_kind=code_kind,
            membership_id="mem-1",
            role="owner",
            role_revoked=False,
            approved_dossier_hash=h,
            signed_at=NOW - timedelta(minutes=30),
            expires_at=NOW + timedelta(days=3),
            signature_digest=sign_dossier_approval(
                dossier_hash=h,
                approval_scope=ApprovalScope.CODE_RELEASE,
                actor_id=code_actor,
                signing_key=SIGNING_KEY,
            ),
        )
    ]
    if not omit_risk_approval:
        approvals.append(
            SignedApprovalRecord(
                approval_scope=ApprovalScope.RISK_CAPITAL,
                actor_id=risk_actor,
                actor_kind=risk_kind,
                membership_id="mem-2",
                role="risk_admin",
                role_revoked=risk_revoked,
                approved_dossier_hash=h,
                signed_at=NOW - timedelta(minutes=25),
                expires_at=NOW + timedelta(days=3),
                signature_digest=sign_dossier_approval(
                    dossier_hash=h,
                    approval_scope=ApprovalScope.RISK_CAPITAL,
                    actor_id=risk_actor,
                    signing_key=SIGNING_KEY,
                ),
            )
        )
    return replace(base, approvals=tuple(approvals))


def _request(dossier: LiveReadinessDossier, **overrides) -> LiveReadinessGateRequest:
    base = LiveReadinessGateRequest(
        build_hash=dossier.build_hash,
        config_hash=dossier.config_hash,
        risk_profile_hash=dossier.risk_profile_hash,
        venue=dossier.venue,
        environment=dossier.environment,
        account_id=dossier.account_id,
        symbols=dossier.symbols,
        strategy_release_ids=dossier.strategy_release_ids,
        expected_dossier_hash=dossier.compute_dossier_hash(),
        executor_actor_id="operator-deploy-3",
    )
    return replace(base, **overrides)


def test_complete_g0_g5_dossier_passes_but_keeps_live_dispatch_locked() -> None:
    """G0–G5 đủ bằng chứng & 2 chữ ký độc lập -> pass G0–G5 nhưng Live vẫn khóa chờ G6."""
    dossier = _signed_dossier()
    verdict = evaluate_live_readiness_gate(
        dossier=dossier, request=_request(dossier), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert verdict.all_g0_g5_passed is True
    assert verdict.live_dispatch_allowed is False
    assert verdict.reason_code == DossierReasonCode.G0_G5_PASSED_LIVE_STILL_LOCKED
    assert all(r.passed for r in verdict.gate_results.values())


def test_version_hash_expiry_and_scope_mismatch_denials() -> None:
    """Từ chối khi sai schema_version, sai hash, hết hạn hoặc sai build/account."""
    bad_ver = _signed_dossier(schema_version="legacy-v0")
    v_ver = evaluate_live_readiness_gate(
        dossier=bad_ver, request=_request(bad_ver), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_ver.all_g0_g5_passed is False
    assert v_ver.reason_code == DossierReasonCode.DOSSIER_VERSION_UNSUPPORTED

    dossier = _signed_dossier()
    v_hash = evaluate_live_readiness_gate(
        dossier=dossier,
        request=_request(dossier, expected_dossier_hash="0" * 64),
        signing_key=SIGNING_KEY,
        as_of=NOW,
    )
    assert v_hash.reason_code == DossierReasonCode.DOSSIER_HASH_MISMATCH

    expired = _signed_dossier(expires_at=NOW - timedelta(seconds=1))
    v_exp = evaluate_live_readiness_gate(
        dossier=expired, request=_request(expired), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_exp.reason_code == DossierReasonCode.DOSSIER_EXPIRED

    v_scope = evaluate_live_readiness_gate(
        dossier=dossier,
        request=_request(dossier, build_hash="other-build"),
        signing_key=SIGNING_KEY,
        as_of=NOW,
    )
    assert v_scope.reason_code == DossierReasonCode.DOSSIER_SCOPE_MISMATCH


def test_independent_gate_evidence_failures_and_demo_window() -> None:
    """Đánh giá độc lập từng gate: thiếu bằng chứng, hết hạn, finding mở, thiếu mẫu Demo."""
    # Thiếu bằng chứng G2
    no_g2 = _signed_dossier(
        evidences=tuple(ev for ev in _full_evidences() if ev.gate is not ReadinessGateId.G2)
    )
    g2_res = evaluate_single_gate(no_g2, ReadinessGateId.G2, as_of=NOW)
    assert g2_res.passed is False
    assert g2_res.reason_code == DossierReasonCode.EVIDENCE_MISSING_OR_INVALID
    assert evaluate_single_gate(no_g2, ReadinessGateId.G0, as_of=NOW).passed is True

    # Bằng chứng hết hạn & còn finding nghiêm trọng
    ev_expired = tuple(
        replace(ev, expires_at=NOW - timedelta(minutes=1)) if ev.gate is ReadinessGateId.G1 else ev
        for ev in _full_evidences()
    )
    v_ev_exp = evaluate_live_readiness_gate(
        dossier=_signed_dossier(evidences=ev_expired),
        request=_request(_signed_dossier(evidences=ev_expired)),
        signing_key=SIGNING_KEY,
        as_of=NOW,
    )
    assert v_ev_exp.reason_code == DossierReasonCode.EVIDENCE_EXPIRED

    ev_finding = tuple(
        replace(ev, open_critical_findings=1) if ev.gate is ReadinessGateId.G5 else ev
        for ev in _full_evidences()
    )
    d_finding = _signed_dossier(evidences=ev_finding)
    v_finding = evaluate_live_readiness_gate(
        dossier=d_finding, request=_request(d_finding), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_finding.reason_code == DossierReasonCode.OPEN_SECURITY_FINDINGS

    # Demo window/sample chưa đạt ngưỡng Risk Owner
    d_demo = _signed_dossier(evidences=_full_evidences(demo_days_observed=5))
    v_demo = evaluate_live_readiness_gate(
        dossier=d_demo, request=_request(d_demo), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_demo.reason_code == DossierReasonCode.DEMO_WINDOW_OR_SAMPLE_INSUFFICIENT


def test_revoked_role_self_approval_missing_scope_and_ai_forbidden() -> None:
    """Chặn role bị thu hồi, tự duyệt, thiếu chữ ký RISK_CAPITAL và AI agent ký duyệt."""
    d_revoked = _signed_dossier(risk_revoked=True)
    v_revoked = evaluate_live_readiness_gate(
        dossier=d_revoked, request=_request(d_revoked), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_revoked.reason_code == DossierReasonCode.ROLE_REVOKED

    # Người duyệt CODE_RELEASE trùng RISK_CAPITAL
    d_same = _signed_dossier(code_actor="alice", risk_actor="alice")
    v_same = evaluate_live_readiness_gate(
        dossier=d_same, request=_request(d_same), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_same.reason_code == DossierReasonCode.SELF_APPROVAL_FORBIDDEN

    # Người duyệt trùng người thực hiện
    d_ok = _signed_dossier()
    v_exec_self = evaluate_live_readiness_gate(
        dossier=d_ok,
        request=_request(d_ok, executor_actor_id="risk-approver-2"),
        signing_key=SIGNING_KEY,
        as_of=NOW,
    )
    assert v_exec_self.reason_code == DossierReasonCode.SELF_APPROVAL_FORBIDDEN

    # Thiếu chữ ký RISK_CAPITAL
    d_no_risk = _signed_dossier(omit_risk_approval=True)
    v_no_risk = evaluate_live_readiness_gate(
        dossier=d_no_risk, request=_request(d_no_risk), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_no_risk.reason_code == DossierReasonCode.APPROVAL_MISSING_OR_INVALID

    # AI agent cố ký duyệt -> bị chặn tuyệt đối
    d_ai = _signed_dossier(risk_kind=ActorKind.AI_AGENT)
    v_ai = evaluate_live_readiness_gate(
        dossier=d_ai, request=_request(d_ai), signing_key=SIGNING_KEY, as_of=NOW
    )
    assert v_ai.reason_code == DossierReasonCode.AI_APPROVAL_FORBIDDEN
