"""Trang responsive review/approval candidate split bằng tiếng Việt."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.learning_split_router import _facade
from trading_bot.admin.web_router import _auth_service, _templates
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(tags=["admin web"])


@router.get("/admin/learning/splits")
async def learning_split_admin_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Render preview metadata; không nạp candidate canonical payload."""
    response.headers["Cache-Control"] = "no-store"
    try:
        candidates = await _facade(request, identity).list_candidates()
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except HTTPException:
        raise
    except (PermissionError, LookupError, ValueError) as error:
        raise HTTPException(status_code=403, detail="Không đủ quyền xem split learning.") from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_splits.html",
        context={"identity": identity, "csrf_token": csrf_token, "candidates": candidates},
    )


__all__ = ["router"]
