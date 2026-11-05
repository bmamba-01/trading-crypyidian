"""API quản trị policy nguồn theo Principal xác thực, chỉ xử lý metadata offline."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError

from trading_bot.adapters.database.repositories.source_registry import SourceRegistryConflict
from trading_bot.adapters.database.source_observer import SourceObservationIncompleteError
from trading_bot.admin.research_source_schemas import (
    SourceApprovalRequest,
    SourceCreateRequest,
    SourceDetailResponse,
    SourceEntryResponse,
    SourceRevisionRequest,
    SourceRevisionResponse,
)
from trading_bot.admin.web_router import _auth_service, _parse_form, _templates, _verify_form_csrf
from trading_bot.application.source_policy_admin import PrincipalSourcePolicyAdminService
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.research.source_policy import SourcePolicyState
from trading_bot.domain.research.source_registry import SourcePolicyTransitionError

router = APIRouter(prefix="/api/v1/research-sources", tags=["research sources"])


def _facade(
    request: Request, identity: AuthIdentity, permission: Permission | None = None
) -> PrincipalSourcePolicyAdminService:
    principal = identity.principal
    if permission is None:
        allowed = authorize(principal, Permission.RESEARCH_SOURCE_WRITE) or authorize(
            principal, Permission.RESEARCH_SOURCE_APPROVE
        )
    else:
        allowed = authorize(principal, permission)
    if not allowed:
        raise HTTPException(status_code=403, detail="Không có quyền quản lý nguồn nghiên cứu.")
    service = getattr(request.app.state, "source_policy_admin_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ policy nguồn chưa sẵn sàng.")
    return cast(PrincipalSourcePolicyAdminService, service.for_principal(principal))


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, SourceObservationIncompleteError):
        return HTTPException(
            status_code=503,
            detail="Policy đã ghi; receipt chưa hoàn tất. Hãy thử lại với cùng Idempotency-Key.",
            headers={"Cache-Control": "no-store"},
        )
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Không có quyền thực hiện thao tác này.")
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail="Không tìm thấy nguồn hoặc policy.")
    if isinstance(error, (ValueError, SourceRegistryConflict, SourcePolicyTransitionError)):
        return HTTPException(status_code=409, detail="Policy đã thay đổi hoặc thao tác xung đột.")
    return HTTPException(status_code=500, detail="Không thể xử lý policy nguồn.")


def _no_store_error(error: HTTPException) -> HTTPException:
    """Giữ no-store khi FastAPI tạo response lỗi riêng thay cho Response thành công."""
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.get("", response_model=list[SourceEntryResponse])
async def list_sources(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> list[SourceEntryResponse]:
    response.headers["Cache-Control"] = "no-store"
    try:
        entries = await _facade(request, identity).list_sources()
    except HTTPException as error:
        raise _no_store_error(error) from error
    except (PermissionError, LookupError, ValueError, SourceRegistryConflict) as error:
        raise _no_store_error(_map_error(error)) from error
    return [SourceEntryResponse.from_domain(entry) for entry in entries]


@router.get("/{source_id}", response_model=SourceDetailResponse)
async def get_source(
    source_id: str,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> SourceDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        snapshot = await _facade(request, identity).get_source(source_id)
    except HTTPException as error:
        raise _no_store_error(error) from error
    except (PermissionError, LookupError, ValueError, SourceRegistryConflict) as error:
        raise _no_store_error(_map_error(error)) from error
    if snapshot is None:
        raise _no_store_error(
            HTTPException(status_code=404, detail="Không tìm thấy nguồn hoặc policy.")
        )
    return SourceDetailResponse.from_snapshot(*snapshot)


@router.post("", status_code=201, response_model=SourceRevisionResponse)
async def create_source(
    body: SourceCreateRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> SourceRevisionResponse:
    facade = _facade(request, identity, Permission.RESEARCH_SOURCE_WRITE)
    try:
        command = body.to_command()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Dữ liệu nguồn không hợp lệ.") from error
    try:
        revision = await facade.create_source(command, idempotency_key)
    except (
        PermissionError,
        LookupError,
        ValueError,
        SourceRegistryConflict,
        SourcePolicyTransitionError,
    ) as error:
        raise _map_error(error) from error
    return SourceRevisionResponse.from_domain(revision)


@router.post("/{source_id}/revisions", status_code=201, response_model=SourceRevisionResponse)
async def submit_revision(
    source_id: str,
    body: SourceRevisionRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> SourceRevisionResponse:
    facade = _facade(request, identity, Permission.RESEARCH_SOURCE_WRITE)
    try:
        command = body.to_command()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Dữ liệu policy không hợp lệ.") from error
    try:
        if command.state is SourcePolicyState.RIGHTS_PENDING:
            revision = await facade.submit_pending(source_id, command, idempotency_key)
        else:
            revision = await facade.change_disabled_state(
                source_id, command.state, command.reason, idempotency_key
            )
    except (
        PermissionError,
        LookupError,
        ValueError,
        SourceRegistryConflict,
        SourcePolicyTransitionError,
    ) as error:
        raise _map_error(error) from error
    return SourceRevisionResponse.from_domain(revision)


@router.post(
    "/{source_id}/revisions/{policy_id}/approve",
    status_code=201,
    response_model=SourceRevisionResponse,
)
async def approve_revision(
    source_id: str,
    policy_id: str,
    body: SourceApprovalRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> SourceRevisionResponse:
    facade = _facade(request, identity, Permission.RESEARCH_SOURCE_APPROVE)
    try:
        revision = await facade.approve_pending(
            source_id, policy_id, body.to_command(), idempotency_key
        )
    except (
        PermissionError,
        LookupError,
        ValueError,
        SourceRegistryConflict,
        SourcePolicyTransitionError,
    ) as error:
        raise _map_error(error) from error
    return SourceRevisionResponse.from_domain(revision)


admin_router = APIRouter(prefix="/admin/research-sources", tags=["admin sources"])
STATE_LABELS = {
    "discovered": "Nguồn ứng viên",
    "rights_pending": "Chờ duyệt quyền",
    "verified": "Đã duyệt quyền",
    "blocked": "Đã chặn",
    "revoked": "Đã thu hồi",
    "expired": "Hết hạn",
}
PURPOSE_LABELS = {
    "metadata": "Siêu dữ liệu",
    "display": "Hiển thị",
    "embedding": "Biểu diễn nhúng",
    "training": "Huấn luyện",
    "redistribution": "Phân phối lại",
    "backtest_research": "Nghiên cứu kiểm thử lịch sử",
    "paper_demo": "Mô phỏng giao dịch",
}


@admin_router.get("")
async def source_console(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    source_id: str | None = None,
) -> Response:
    """Hiển thị metadata đã được kiểm quyền; evidence luôn là văn bản an toàn."""
    try:
        facade = _facade(request, identity)
        entries = await facade.list_sources()
        snapshot = await facade.get_source(source_id) if source_id else None
        if source_id and snapshot is None:
            raise HTTPException(404, "Không tìm thấy nguồn hoặc policy.")
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except HTTPException as error:
        raise _no_store_error(error) from error
    except (PermissionError, LookupError, ValueError, SourceRegistryConflict) as error:
        raise _no_store_error(_map_error(error)) from error
    detail = SourceDetailResponse.from_snapshot(*snapshot) if snapshot else None
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/research_sources.html",
        context={
            "identity": identity,
            "entries": [SourceEntryResponse.from_domain(entry) for entry in entries],
            "detail": detail,
            "csrf_token": csrf_token,
            "new_key": lambda: str(uuid4()),
            "can_write": authorize(identity.principal, Permission.RESEARCH_SOURCE_WRITE),
            "can_approve": authorize(identity.principal, Permission.RESEARCH_SOURCE_APPROVE),
            "state_labels": STATE_LABELS,
            "purpose_labels": PURPOSE_LABELS,
        },
        headers={"Cache-Control": "no-store"},
    )


@admin_router.post("/{action}")
async def mutate_source_form(
    action: str,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Kiểm CSRF trước mọi mutation rồi dùng schema và facade chung với API."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    if action not in {"create", "revision", "approve"}:
        raise HTTPException(404, "Không tìm thấy thao tác.")
    facade = _facade(
        request,
        identity,
        Permission.RESEARCH_SOURCE_APPROVE
        if action == "approve"
        else Permission.RESEARCH_SOURCE_WRITE,
    )
    values.pop("csrf_token", None)
    try:
        key = UUID(values.pop("idempotency_key"))
        source_id = values.get("source_id", "")
        if action == "create":
            budget_fields = ("window_seconds", "max_requests", "max_bytes", "max_cost", "cost_unit")
            payload: dict[str, object] = dict(values)
            payload["budget"] = {name: payload.pop(name) for name in budget_fields}
            await facade.create_source(
                SourceCreateRequest.model_validate(payload).to_command(), key
            )
        else:
            values.pop("source_id")
            if "valid_until" in values and not values["valid_until"]:
                values.pop("valid_until")
            payload = dict(values)
            budget_fields = ("window_seconds", "max_requests", "max_bytes", "max_cost", "cost_unit")
            if action == "revision" and any(name in payload for name in budget_fields):
                payload["budget"] = {name: payload.pop(name) for name in budget_fields}
            if "allowed_purposes" in payload:
                raise HTTPException(422, "Chọn mục đích bằng các ô tương ứng.")
            if action == "approve" or payload.get("state") == "rights_pending":
                selected = []
                for purpose in PURPOSE_LABELS:
                    field = "purpose_" + purpose
                    if field in payload:
                        if payload.pop(field) != "on":
                            raise HTTPException(422, "Lựa chọn mục đích không hợp lệ.")
                        selected.append(purpose)
                payload["allowed_purposes"] = selected
            if action == "approve":
                policy_id = str(payload.pop("policy_id"))
                snapshot = await facade.get_source(source_id)
                if snapshot is None:
                    raise LookupError("Không tìm thấy nguồn.")
                # Service kiểm latest/idempotent; đọc revision gốc để retry vẫn giữ payload.
                pending = next(
                    (
                        item
                        for item in snapshot[0].revisions
                        if item.policy.policy_id == policy_id
                        and item.state is SourcePolicyState.RIGHTS_PENDING
                    ),
                    None,
                )
                if pending is None:
                    raise ValueError("Không tìm thấy policy chờ duyệt.")
                expiry_action = payload.pop("expiry_action", "preserve")
                if expiry_action == "preserve":
                    payload["valid_until"] = pending.policy.valid_until
                elif expiry_action == "clear":
                    payload["valid_until"] = None
                elif expiry_action != "change" or not payload.get("valid_until"):
                    raise HTTPException(422, "Chọn rõ thao tác và thời hạn mới hợp lệ.")
                command = SourceApprovalRequest.model_validate(payload).to_command()
                await facade.approve_pending(source_id, policy_id, command, key)
            else:
                revision = SourceRevisionRequest.model_validate(payload).to_command()
                if revision.state is SourcePolicyState.RIGHTS_PENDING:
                    await facade.submit_pending(source_id, revision, key)
                else:
                    await facade.change_disabled_state(
                        source_id, revision.state, revision.reason, key
                    )
    except (KeyError, ValidationError) as error:
        raise HTTPException(422, "Dữ liệu form không hợp lệ.") from error
    except (
        PermissionError,
        LookupError,
        ValueError,
        SourceRegistryConflict,
        SourcePolicyTransitionError,
    ) as error:
        raise _map_error(error) from error
    return Response(
        status_code=303,
        headers={
            "Location": "/admin/research-sources?" + urlencode({"source_id": source_id}),
            "Cache-Control": "no-store",
        },
    )
