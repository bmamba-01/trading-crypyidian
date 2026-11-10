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
    can_disable = (
        snapshot.mode is not ScorerDeploymentMode.DISABLED and has_approval and recent_totp
    )
    csrf_token: str | None = None
    if can_disable:
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
            "can_disable": can_disable,
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


def _short(value: object | None) -> str:
    if value is None:
        return "Chưa có"
    text = str(value)
    return text if len(text) <= 16 else f"{text[:16]}…"


def _percent(value: float | None) -> str | None:
    return None if value is None else f"{value * 100:.1f}%"


__all__ = ["learning_scoring_admin_page", "router"]
