"""Trang quản trị tiếng Việt cho registry mô hình WP06."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.learning_model_registry_router import _facade
from trading_bot.admin.web_router import _auth_service, _templates
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Permission
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(tags=["admin web"])

_STATE_LABELS = {
    "draft": "Bản nháp",
    "evaluated": "Đã đánh giá · chờ duyệt",
    "rejected": "Đã từ chối",
    "paper_active": "Đang được chọn cho PAPER",
    "retired": "Đã ngừng sử dụng",
}


_ACTION_LABELS = {
    "register": "Đăng ký artifact",
    "evaluate": "Gắn báo cáo đánh giá",
    "approve": "Duyệt mô hình",
    "reject": "Từ chối mô hình",
    "activate": "Chọn cho PAPER",
    "rollback": "Khôi phục PAPER",
    "retire": "Ngừng sử dụng",
}


@router.get("/admin/learning/models")
async def learning_model_registry_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    artifact_id: UUID | None = None,
) -> Response:
    """Hiển thị metadata registry và các thao tác đã được API quản trị bảo vệ."""
    response.headers["Cache-Control"] = "no-store"
    try:
        facade = _facade(request, identity, Permission.LEARNING_MODEL_READ)
        entries = await facade.list_entries()
        pointer = await facade.get_active_pointer()
        events_by_entry = {
            entry.id: tuple(
                {
                    "event": event,
                    "action_label": _ACTION_LABELS.get(
                        event.action.value, "Thao tác chưa nhận diện"
                    ),
                    "from_label": (
                        _STATE_LABELS.get(event.from_state.value, "Trạng thái chưa nhận diện")
                        if event.from_state
                        else "Chưa đăng ký"
                    ),
                    "to_label": _STATE_LABELS.get(
                        event.to_state.value, "Trạng thái chưa nhận diện"
                    ),
                }
                for event in await facade.list_events(entry.id)
            )
            for entry in entries
        }
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except HTTPException:
        raise
    except (PermissionError, LookupError, ValueError) as error:
        raise HTTPException(
            status_code=403, detail="Không đủ quyền xem registry mô hình."
        ) from error

    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_model_registry.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "artifact_id": artifact_id,
            "events_by_entry": events_by_entry,
            "entries": tuple(
                {
                    "entry": entry,
                    "state_label": _STATE_LABELS.get(
                        entry.state.value, "Trạng thái chưa nhận diện"
                    ),
                }
                for entry in entries
            ),
            "active_pointer": pointer,
        },
    )


__all__ = ["router"]
