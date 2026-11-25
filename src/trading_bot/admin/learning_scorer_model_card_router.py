"""Trang model card tiếng Việt, gắn report TEST với latency PAPER đã kiểm quyền."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.adapters.database.repositories.learning_scorer_model_card import (
    LearningScorerActionLatencyUnavailable,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentConflict,
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.admin.learning_paper_performance_router import paper_bot_options
from trading_bot.admin.web_router import _admin_service, _templates
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.route_policy import BotScopePolicy, permission_route
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.learning.scorer_model_card import ScorerActionLatencySummary
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

router = APIRouter(tags=["admin web"])

_WINDOW_LABELS = {"24h": "24 giờ", "7d": "7 ngày", "30d": "30 ngày"}
_MODE_LABELS = {
    ScorerDeploymentMode.DISABLED: "Đang tắt",
    ScorerDeploymentMode.SHADOW: "Chỉ quan sát",
    ScorerDeploymentMode.PAPER_GATE: "Cổng PAPER",
}
_HEALTH_LABELS = {
    ScorerDeploymentHealth.DISABLED: "Đang tắt",
    ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED: "Cần xác minh lại quyền hiện hành",
}
_STAGE_LABELS = {
    "signal_available_to_intent": "Tín hiệu khả dụng → ý định lệnh",
    "evidence_persisted_to_intent": "Bằng chứng đã ghi → ý định lệnh",
    "decision_persisted_to_intent": "Quyết định của bộ chấm điểm đã ghi → ý định lệnh",
}


@router.get(
    "/admin/learning/model-card",
    dependencies=[
        permission_route(
            Permission.LEARNING_MODEL_READ,
            bot_scope=BotScopePolicy(
                BotPermission.REPORT_READ, param_name=None, param_location="service"
            ),
            denied_detail="Không đủ quyền xem model card scorer.",
        )
    ],
)
async def learning_scorer_model_card_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    report_id: Annotated[UUID, Query()],
    bot_id: Annotated[UUID | None, Query()] = None,
    window: Annotated[str, Query(pattern="^(24h|7d|30d)$")] = "7d",
) -> Response:
    """Hiển thị lineage TEST và aggregate latency PAPER theo bot/window đã chọn."""
    response.headers["Cache-Control"] = "no-store"
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_MODEL_READ):
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền xem model card scorer.",
            headers={"Cache-Control": "no-store"},
        )

    report_read_model = getattr(request.app.state, "learning_scoring_report_read_model", None)
    if report_read_model is None:
        raise _http_error(503, "Read model báo cáo scorer chưa sẵn sàng.")
    try:
        report = await report_read_model.load_report_summary(principal.workspace_id, report_id)
    except (SQLAlchemyError, ValueError) as error:
        raise _http_error(503, "Chưa thể xác minh quyền báo cáo scorer hiện hành.") from error
    if report is None or type(report) is not ScorerReportSummary:
        raise _http_error(404, "Không tìm thấy báo cáo scorer trong phạm vi được cấp quyền.")

    deployment_service = getattr(request.app.state, "learning_scoring_deployment_service", None)
    deployment_snapshot: ScorerDeploymentAdminSnapshot | None = None
    deployment_unavailable = deployment_service is None
    if deployment_service is not None:
        try:
            deployment_snapshot = await deployment_service.for_principal(
                principal
            ).load_admin_scoring_deployment()
            if type(deployment_snapshot) is not ScorerDeploymentAdminSnapshot:
                deployment_snapshot = None
                deployment_unavailable = True
        except PermissionError as error:
            raise _http_error(403, "Không đủ quyền xem deployment hiện hành.") from error
        except (
            ScorerDeploymentConflict,
            ScorerDeploymentRightsUnavailable,
            SQLAlchemyError,
            ValueError,
        ):
            deployment_unavailable = True

    try:
        bots = await _admin_service(request).list_bots(principal)
    except PermissionError as error:
        raise _http_error(403, "Không đủ quyền xem bot PAPER.") from error
    except SQLAlchemyError as error:
        raise _http_error(503, "Chưa thể đọc danh sách bot PAPER.") from error
    bot_options = paper_bot_options(cast(Sequence[Mapping[str, Any]], bots))
    allowed_bot_ids = {UUID(item["id"]) for item in bot_options}
    if bot_id is not None and bot_id not in allowed_bot_ids:
        raise _http_error(404, "Không tìm thấy bot PAPER trong phạm vi được cấp quyền.")

    latency_summary: ScorerActionLatencySummary | None = None
    latency_unavailable = False
    if bot_id is not None:
        model_card = getattr(request.app.state, "learning_scorer_model_card_read_model", None)
        if model_card is None:
            latency_unavailable = True
        else:
            try:
                latency_summary = await model_card.load_action_latency(
                    principal=principal,
                    bot_id=bot_id,
                    report=report,
                    window_code=window,
                )
            except PermissionError as error:
                raise _http_error(403, "Không đủ quyền báo cáo cho bot PAPER đã chọn.") from error
            except LearningScorerActionLatencyUnavailable, SQLAlchemyError, ValueError:
                # Không hiển thị aggregate dở dang khi rights hoặc projection không xác minh được.
                latency_unavailable = True

    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_scorer_model_card.html",
        context={
            "identity": identity,
            "report": report,
            "deployment": deployment_snapshot,
            "deployment_unavailable": deployment_unavailable,
            "deployment_mode_labels": _MODE_LABELS,
            "deployment_health_labels": _HEALTH_LABELS,
            "bot_options": bot_options,
            "selected_bot_id": str(bot_id) if bot_id else "",
            "window_code": window,
            "window_label": _WINDOW_LABELS[window],
            "latency_summary": latency_summary,
            "latency_unavailable": latency_unavailable,
            "stage_labels": _STAGE_LABELS,
        },
        headers={"Cache-Control": "no-store"},
    )


def _http_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"Cache-Control": "no-store"},
    )
