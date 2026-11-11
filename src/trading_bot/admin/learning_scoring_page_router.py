"""Trang vận hành scorer tiếng Việt; chỉ hiển thị metadata deployment."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentConflict,
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.admin.web_router import _auth_service, _templates
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import MfaMethod, Permission, Principal, authorize
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import (
    RECENT_MFA_LIFETIME,
    AuthenticationFailure,
    AuthIdentity,
)
from trading_bot.domain.learning.scorer_score_event_summary import (
    ScorerEventDecision,
    ScorerEventMode,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

router = APIRouter(tags=["admin web"])

_MODE_LABELS = {
    ScorerDeploymentMode.DISABLED: "Đang tắt",
    ScorerDeploymentMode.SHADOW: "Chỉ quan sát",
    ScorerDeploymentMode.PAPER_GATE: "Cổng PAPER",
}
_HEALTH_LABELS = {
    ScorerDeploymentHealth.DISABLED: "Đang tắt",
    ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED: "Cần xác minh lại quyền hiện hành",
}
_REPORT_STATUS_LABELS = {
    "INCONCLUSIVE": "Chưa kết luận",
    "ACCEPTED_SHADOW_ONLY": "Đạt ngưỡng báo cáo cho SHADOW",
    "ACCEPTED_PAPER_GATE_ELIGIBLE": "Báo cáo đạt ngưỡng PAPER_GATE",
}
_SCORE_EVENT_MODE_LABELS: dict[ScorerEventMode, str] = {
    "SHADOW": "Chỉ quan sát",
    "PAPER_GATE": "Cổng PAPER",
}
_SCORE_EVENT_DECISION_LABELS: dict[ScorerEventDecision, str] = {
    "APPROVE": "Duyệt ứng viên",
    "VETO": "Từ chối ứng viên",
    "ABSTAIN": "Không chấm được",
}
_ACTIVE_REPORT_STATUSES = frozenset({"ACCEPTED_SHADOW_ONLY", "ACCEPTED_PAPER_GATE_ELIGIBLE"})


@router.get("/admin/learning/scoring")
async def learning_scoring_admin_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    score_event_page_number: Annotated[int, Query(alias="score_event_page", ge=1)] = 1,
) -> Response:
    """Hiển thị mode/CAS và chỉ cho reviewer TOTP tắt deployment đang hoạt động."""
    response.headers["Cache-Control"] = "no-store"
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_MODEL_READ):
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền xem trang chấm điểm mô hình.",
            headers={"Cache-Control": "no-store"},
        )

    service: Any = getattr(request.app.state, "learning_scoring_deployment_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Dịch vụ chấm điểm mô hình chưa sẵn sàng.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        facade = service.for_principal(principal)
        snapshot = cast(ScorerDeploymentAdminSnapshot, await facade.load_admin_scoring_deployment())
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền xem deployment chấm điểm.",
            headers={"Cache-Control": "no-store"},
        ) from error
    except (ScorerDeploymentConflict, ScorerDeploymentRightsUnavailable, ValueError) as error:
        raise HTTPException(
            status_code=503,
            detail="Chưa thể đọc trạng thái chấm điểm an toàn.",
            headers={"Cache-Control": "no-store"},
        ) from error

    report_read_model: Any = getattr(request.app.state, "learning_scoring_report_read_model", None)
    if report_read_model is None:
        raise HTTPException(
            status_code=503,
            detail="Read model báo cáo scorer chưa sẵn sàng.",
            headers={"Cache-Control": "no-store"},
        )
    try:
        report_summary = await report_read_model.load_latest_summary(principal.workspace_id)
    except (SQLAlchemyError, ValueError) as error:
        raise HTTPException(
            status_code=503,
            detail="Chưa thể đọc tóm tắt báo cáo scorer an toàn.",
            headers={"Cache-Control": "no-store"},
        ) from error

    score_event_read_model: Any = getattr(
        request.app.state, "learning_scoring_score_event_read_model", None
    )
    if score_event_read_model is None:
        raise HTTPException(
            status_code=503,
            detail="Read model lịch sử điểm chấm chưa sẵn sàng.",
            headers={"Cache-Control": "no-store"},
        )
    try:
        score_event_page = await score_event_read_model.load_page(
            principal.workspace_id,
            page=score_event_page_number,
        )
    except (SQLAlchemyError, ValueError) as error:
        raise HTTPException(
            status_code=503,
            detail="Chưa thể đọc lịch sử điểm chấm an toàn.",
            headers={"Cache-Control": "no-store"},
        ) from error

    has_approval = authorize(principal, Permission.LEARNING_MODEL_APPROVE)
    recent_totp = _has_recent_totp(principal)
    mode_options = tuple(
        _mode_option(
            mode,
            current_mode=snapshot.mode,
            snapshot=snapshot,
            report_summary=report_summary,
            has_approval=has_approval,
            recent_totp=recent_totp,
        )
        for mode in ScorerDeploymentMode
    )
    can_submit = any(item["available"] for item in mode_options)
    active_mode_available = any(
        item["available"] and item["value"] in {"SHADOW", "PAPER_GATE"} for item in mode_options
    )
    csrf_token: str | None = None
    if can_submit:
        try:
            csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
        except AuthenticationFailure as error:
            raise HTTPException(
                status_code=error.status_code,
                detail=error.message,
                headers={"Cache-Control": "no-store"},
            ) from error

    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_scoring.html",
        context={
            "identity": identity,
            "snapshot": snapshot,
            "mode_label": _MODE_LABELS[snapshot.mode],
            "health_label": _HEALTH_LABELS[snapshot.health],
            "has_approval": has_approval,
            "recent_totp": recent_totp,
            "mode_options": mode_options,
            "active_mode_available": active_mode_available,
            "requested_mode": ScorerDeploymentMode.DISABLED.value,
            "can_submit": can_submit,
            "authorization_gate": _authorization_gate(has_approval, recent_totp),
            "csrf_token": csrf_token,
            "event_id_short": _short(snapshot.deployment_event_id),
            "report_summary": report_summary,
            "report_status_label": (
                _REPORT_STATUS_LABELS[report_summary.status] if report_summary else None
            ),
            "report_lineage": (
                {
                    "Report": _short(report_summary.report_hash),
                    "TEST proof": _short(report_summary.proof_hash),
                    "Scorer artifact": _short(report_summary.scorer_artifact_hash),
                    "Base artifact": _short(report_summary.base_artifact_hash),
                    "Policy": _short(report_summary.policy_hash),
                    "Manifest": _short(report_summary.manifest_hash),
                    "Fold plan": _short(report_summary.fold_plan_hash),
                    "Vector schema": _short(report_summary.vector_schema_hash),
                }
                if report_summary
                else {}
            ),
            "class_support_labels": (
                tuple(
                    (
                        {-1: "PnL âm", 0: "PnL trung tính", 1: "PnL dương"}.get(
                            label, f"Nhóm {label}"
                        ),
                        count,
                    )
                    for label, count in report_summary.class_support
                )
                if report_summary
                else ()
            ),
            "score_event_page": score_event_page,
            "score_event_rows": tuple(
                {
                    "event_id_short": _short(item.event_id),
                    "created_at": item.created_at,
                    "mode_label": _SCORE_EVENT_MODE_LABELS[item.mode],
                    "decision_label": _SCORE_EVENT_DECISION_LABELS[item.decision],
                    "decision_class": item.decision.lower(),
                    "reason_code": item.reason_code,
                    "probability": _percent(item.probability),
                    "confidence": _percent(item.confidence),
                    "uncertainty": _percent(item.uncertainty),
                }
                for item in score_event_page.items
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


def _has_recent_totp(principal: Principal) -> bool:
    verified_at = principal.mfa_verified_at
    now = datetime.now(UTC)
    return bool(
        principal.mfa_method is MfaMethod.TOTP
        and isinstance(verified_at, datetime)
        and verified_at.tzinfo is not None
        and verified_at.utcoffset() is not None
        and verified_at <= now
        and now - verified_at <= RECENT_MFA_LIFETIME
    )


def _authorization_gate(has_approval: bool, recent_totp: bool) -> str | None:
    if not has_approval:
        return "Thiếu quyền LEARNING_MODEL_APPROVE để đổi chế độ scorer."
    if not recent_totp:
        return "Cần xác thực TOTP gần đây để đổi chế độ scorer."
    return None


def _mode_option(
    mode: ScorerDeploymentMode,
    *,
    current_mode: ScorerDeploymentMode,
    snapshot: ScorerDeploymentAdminSnapshot,
    report_summary: Any,
    has_approval: bool,
    recent_totp: bool,
) -> dict[str, Any]:
    labels = {
        ScorerDeploymentMode.DISABLED: "DISABLED — Tắt scorer",
        ScorerDeploymentMode.SHADOW: "SHADOW — Chỉ quan sát",
        ScorerDeploymentMode.PAPER_GATE: "PAPER_GATE — Cổng PAPER",
    }
    gates: list[str] = []
    if mode is ScorerDeploymentMode.DISABLED:
        if current_mode is ScorerDeploymentMode.DISABLED:
            gates.append("Scorer hiện đã tắt.")
        else:
            auth_gate = _authorization_gate(has_approval, recent_totp)
            if auth_gate:
                gates.append(auth_gate)
    else:
        gates.extend(_report_mode_gates(mode, report_summary))
        auth_gate = _authorization_gate(has_approval, recent_totp)
        if auth_gate:
            gates.append(auth_gate)
        if current_mode is mode:
            gates.append("Scorer đã ở chế độ này; hãy tải lại để đọc phiên bản hiện hành.")

    notes: tuple[str, ...] = ()
    if mode is not ScorerDeploymentMode.DISABLED:
        notes = (
            "Snapshot deployment hiện ghi "
            f"health={snapshot.health.value}, runtime_usable="
            f"{str(snapshot.runtime_usable).lower()}; máy chủ sẽ xác minh lại "
            "quyền hiện hành và các điều kiện deployment khi nhận POST.",
        )

    return {
        "value": mode.value,
        "label": labels[mode],
        "available": not gates,
        "gates": tuple(gates),
        "notes": notes,
        "gate_id": f"scorer-{mode.value.lower()}-gate",
    }


def _report_mode_gates(mode: ScorerDeploymentMode, report_summary: Any) -> list[str]:
    if report_summary is None:
        return ["Chưa có báo cáo scorer TEST đã lưu trong không gian làm việc này."]

    status = report_summary.status
    gates: list[str] = []
    if mode is ScorerDeploymentMode.SHADOW:
        if status not in _ACTIVE_REPORT_STATUSES:
            gates.append(
                "Báo cáo scorer TEST gần nhất có trạng thái "
                f"{status}; cần ACCEPTED_SHADOW_ONLY hoặc ACCEPTED_PAPER_GATE_ELIGIBLE. "
                f"Policy {report_summary.policy_version} chưa chấp nhận chế độ SHADOW."
            )
    elif status != "ACCEPTED_PAPER_GATE_ELIGIBLE":
        gates.append(
            "Báo cáo scorer TEST gần nhất có trạng thái "
            f"{status}; cần ACCEPTED_PAPER_GATE_ELIGIBLE. "
            f"Policy {report_summary.policy_version} chưa chấp nhận PAPER_GATE."
        )

    if (
        status in _ACTIVE_REPORT_STATUSES
        and getattr(report_summary, "evaluation_report_id", None) is None
    ):
        gates.append("Báo cáo chưa có mã định danh để ghim deployment vào đúng đánh giá.")
    if mode is ScorerDeploymentMode.PAPER_GATE:
        if not report_summary.candidate_completeness_verified:
            gates.append("Báo cáo chưa xác minh đủ bằng chứng về mọi cơ hội TEST.")
        if not report_summary.scored_candidate_coverage_verified:
            gates.append("Báo cáo chưa xác minh đủ độ phủ cơ hội được chấm.")
    return gates


def _short(value: object | None) -> str:
    if value is None:
        return "Chưa có"
    text = str(value)
    return text if len(text) <= 16 else f"{text[:16]}…"


def _percent(value: float | None) -> str | None:
    return None if value is None else f"{value * 100:.1f}%"


__all__ = ["learning_scoring_admin_page", "router"]
