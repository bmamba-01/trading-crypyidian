"""Trang quản trị responsive cho hàng đợi huấn luyện WP06."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.learning_training_router import _facade
from trading_bot.admin.web_router import _auth_service, _templates
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(tags=["admin web"])

_STATUS_LABELS = {
    "queued": "Đang chờ",
    "running": "Đang huấn luyện",
    "succeeded": "Hoàn tất",
    "failed": "Thất bại",
    "cancelled": "Đã dừng",
    "rejected": "Bị từ chối theo ngân sách",
}
_STAGE_LABELS = {
    "queued": "Trong hàng đợi",
    "training": "Đang huấn luyện",
    "complete": "Đã lưu mô hình",
    "failed": "Đã dừng do lỗi",
    "cancelled": "Đã xác nhận dừng",
}


@router.get("/admin/learning/training")
async def learning_training_admin_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị trạng thái hàng đợi và lát dữ liệu đủ điều kiện."""
    response.headers["Cache-Control"] = "no-store"
    try:
        facade = _facade(request, identity)
        folds = await facade.list_eligible_folds()
        jobs = await facade.list_jobs()
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except HTTPException:
        raise
    except (PermissionError, LookupError, ValueError) as error:
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền xem hàng đợi huấn luyện.",
        ) from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_training.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "eligible_folds": folds,
            "jobs": tuple(
                {
                    "job": job,
                    "status_label": _STATUS_LABELS.get(job.status, "Trạng thái chưa nhận diện"),
                    "stage_label": _STAGE_LABELS.get(job.stage, "Giai đoạn chưa nhận diện"),
                }
                for job in jobs
            ),
        },
    )


__all__ = ["router"]
