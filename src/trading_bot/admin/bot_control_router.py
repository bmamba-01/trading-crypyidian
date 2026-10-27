"""HTTP API điều khiển vận hành bot (MB-07a, ruling 31–34).

Router chỉ lo phiên/CSRF, validate body, header và ánh xạ mã lỗi; quyền, MFA, L1, guard phẳng và
audit nằm trong `BotOperationsService` (dùng chung với Telegram) và `BotRuntimeControlService`
của MB-05. Body lỗi `{"detail": {"code", "message"}}` không chứa dữ liệu của bot ngoài scope.
"""

from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from trading_bot.application.bot_lifecycle_guard import BotNotFlatError
from trading_bot.application.bot_operations import BotOperationsService, PrincipalBotOperations
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.execution.runtime_control import BotControlResult, WorkspaceControlResult

router = APIRouter(prefix="/api/v1", tags=["bot operations"])

_ERRORS: dict[str, tuple[int, str]] = {
    "BOT_NOT_FOUND": (404, "Không tìm thấy bot."),
    "CONTROL_PERMISSION_DENIED": (403, "Không đủ quyền cho thao tác này."),
    "RECENT_MFA_REQUIRED": (403, "Cần xác thực MFA gần đây."),
    "CONTROL_REASON_REQUIRED": (422, "Cần lý do (1–500 ký tự)."),
    "CONTROL_REVISION_CONFLICT": (409, "Trạng thái control đã thay đổi; hãy tải lại."),
    "CONTROL_REVISION_REQUIRED": (409, "Cần expected_revision của trạng thái hiện hành."),
    "BOT_NOT_FLAT": (
        409,
        "Bot còn vị thế, lệnh chưa khớp hoặc reservation; hãy pause tới khi phẳng.",
    ),
    "RUNTIME_CONTROL_UNAVAILABLE": (503, "Control plane tạm thời không phản hồi; hãy thử lại."),
}


class ControlReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ControlResumeRequest(ControlReasonRequest):
    expected_revision: int = Field(ge=1)


def error_status(code: str) -> int:
    """Mã HTTP ổn định theo reason code; mã lạ là xung đột trạng thái (409)."""
    return _ERRORS.get(code, (409, ""))[0]


def _raise(error: BotRuntimeControlError | BotNotFlatError) -> NoReturn:
    code = error.reason_code
    status, message = _ERRORS.get(code, (409, "Thao tác bị từ chối."))
    detail: dict[str, object] = {"code": code, "message": message}
    if isinstance(error, BotNotFlatError) and code == "BOT_NOT_FLAT":
        detail |= {
            "open_positions": error.open_positions,
            "open_orders": error.open_orders,
            "held_reservations": error.held_reservations,
        }
    raise HTTPException(status_code=status, detail=detail) from None


def _operations(request: Request, identity: AuthIdentity) -> PrincipalBotOperations:
    service = cast(
        BotOperationsService | None, getattr(request.app.state, "bot_operations_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ vận hành bot chưa sẵn sàng")
    return service.for_principal(identity.principal)


def _bot_control(result: BotControlResult) -> dict[str, object]:
    return {
        "control": {
            "bot_id": str(result.bot_id),
            "entry_state": result.entry_state.value,
            "revision": result.revision,
            "changed": result.changed,
        }
    }


def _workspace_control(result: WorkspaceControlResult) -> dict[str, object]:
    return {
        "control": {
            "workspace_id": str(result.workspace_id),
            "kill_state": result.kill_state.value,
            "revision": result.revision,
            "changed": result.changed,
        }
    }


@router.get("/workspace/runtime")
async def workspace_runtime(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"workspace": (await operations.workspace_status()).as_dict()}
    except BotRuntimeControlError as error:
        _raise(error)


@router.get("/bots/{bot_id}/runtime")
async def bot_runtime(
    bot_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"bot": (await operations.bot_status(bot_id)).as_dict()}
    except BotRuntimeControlError as error:
        _raise(error)


@router.post("/bots/{bot_id}/runtime/pause")
async def pause_bot(
    bot_id: UUID,
    body: ControlReasonRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _bot_control(await operations.pause_bot(bot_id, body.reason))
    except BotRuntimeControlError as error:
        _raise(error)


@router.post("/bots/{bot_id}/runtime/resume")
async def resume_bot(
    bot_id: UUID,
    body: ControlResumeRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _bot_control(
            await operations.resume_bot(bot_id, body.reason, body.expected_revision)
        )
    except BotRuntimeControlError as error:
        _raise(error)


@router.post("/bots/{bot_id}/disable")
async def disable_bot(
    bot_id: UUID,
    body: ControlReasonRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"bot": (await operations.disable_bot(bot_id, body.reason)).as_dict()}
    except (BotRuntimeControlError, BotNotFlatError) as error:
        _raise(error)


@router.post("/workspace/runtime/kill")
async def kill_workspace(
    body: ControlReasonRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _workspace_control(await operations.kill_workspace(body.reason))
    except BotRuntimeControlError as error:
        _raise(error)


@router.post("/workspace/runtime/release")
async def release_workspace(
    body: ControlResumeRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    operations = _operations(request, identity)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _workspace_control(
            await operations.release_workspace(body.reason, body.expected_revision)
        )
    except BotRuntimeControlError as error:
        _raise(error)
