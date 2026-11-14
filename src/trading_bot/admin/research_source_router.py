"""API quản trị policy nguồn theo Principal xác thực, chỉ xử lý metadata offline."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from trading_bot.adapters.database.repositories.source_registry import SourceRegistryConflict
from trading_bot.adapters.database.source_observer import SourceObservationIncompleteError
from trading_bot.admin.research_source_schemas import (
    SourceApprovalRequest,
    SourceCreateRequest,
    SourceDetailResponse,
    SourceEntryResponse,
    SourceLicenseEvidenceResponse,
    SourceRevisionRequest,
    SourceRevisionResponse,
)
from trading_bot.admin.web_router import _auth_service, _parse_form, _templates, _verify_form_csrf
from trading_bot.application.source_policy_admin import (
    LICENSE_EVIDENCE_MEDIA_TYPES,
    LicenseEvidenceUploadError,
    PrincipalSourcePolicyAdminService,
)
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.research.license_scope import scope_sha256
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


class _EvidenceRoute(APIRoute):
    """Giữ no-store cả lỗi dependency/validation trước khi endpoint chạy."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def no_store(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as error:
                response = JSONResponse(
                    status_code=error.status_code,
                    content={"detail": error.detail},
                    headers=error.headers,
                )
            except RequestValidationError:
                response = JSONResponse(
                    status_code=422, content={"detail": "Dữ liệu upload không hợp lệ."}
                )
            except Exception:
                response = JSONResponse(
                    status_code=500, content={"detail": "Không thể ghi bằng chứng."}
                )
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


_evidence_router = APIRouter(route_class=_EvidenceRoute)


@_evidence_router.post(
    "/{source_id}/revisions/{policy_id}/license-evidence",
    status_code=201,
    response_model=SourceLicenseEvidenceResponse,
)
async def upload_license_evidence(
    source_id: str,
    policy_id: str,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> SourceLicenseEvidenceResponse:
    facade = _facade(request, identity, Permission.RESEARCH_SOURCE_WRITE)
    media_type = request.headers.get("content-type", "")
    if media_type not in LICENSE_EVIDENCE_MEDIA_TYPES:
        raise HTTPException(415, "Loại tài liệu không được hỗ trợ.")
    try:
        evidence = await facade.upload_license_evidence(
            source_id,
            policy_id,
            request.stream(),
            media_type=media_type,
            content_length=request.headers.get("content-length"),
            idempotency_key=idempotency_key,
        )
    except LicenseEvidenceUploadError as error:
        raise HTTPException(error.status_code, str(error)) from error
    except (PermissionError, LookupError, ValueError, SourceRegistryConflict) as error:
        raise _map_error(error) from error
    return SourceLicenseEvidenceResponse.from_domain(evidence)


router.include_router(_evidence_router)


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
SCOPE_OUTPUT_LABELS = {
    "derived_features": "Đặc trưng suy ra",
    "trained_model": "Mô hình đã huấn luyện",
    "aggregate_report": "Báo cáo tổng hợp",
    "paper_inference": "Suy luận PAPER",
}
SCOPE_DERIVED_USE_LABELS = {
    "feature_extraction": "Trích xuất đặc trưng",
    "model_training": "Huấn luyện mô hình",
    "paper_inference": "Suy luận PAPER",
    "aggregate_reporting": "Lập báo cáo tổng hợp",
}


def _split_scope_members(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise HTTPException(422, f"Trường {field} không hợp lệ.")
    members = tuple(part.strip() for part in value.split(",") if part.strip())
    if not members:
        raise HTTPException(422, f"Cần nhập ít nhất một giá trị cho {field}.")
    return members


def _selected_form_values(
    payload: dict[str, object], prefix: str, choices: dict[str, str]
) -> tuple[str, ...]:
    selected: list[str] = []
    for value in choices:
        field = f"{prefix}{value}"
        if field in payload:
            if payload.pop(field) != "on":
                raise HTTPException(422, "Lựa chọn phạm vi quyền không hợp lệ.")
            selected.append(value)
    if not selected:
        raise HTTPException(422, "Cần chọn ít nhất một quyền đầu ra và cách sử dụng.")
    return tuple(selected)


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
    pending_evidence = None
    evidence_options: list[dict[str, str]] = []
    if detail and detail.current.state is SourcePolicyState.RIGHTS_PENDING:
        try:
            pending_evidence = await facade.get_pending_license_evidence(
                detail.source_id, detail.current.policy_id
            )
        except (PermissionError, LookupError, ValueError, SourceRegistryConflict) as error:
            raise _no_store_error(_map_error(error)) from error
        if pending_evidence is not None:
            evidence_options.append(
                {
                    "evidence_id": str(pending_evidence.evidence_id),
                    "digest": pending_evidence.digest,
                    "scope_hash": pending_evidence.scope_hash,
                    "label": f"Tài liệu hiện tại · {pending_evidence.digest[:12]}",
                }
            )
        elif detail.current.license_scope is not None:
            current_scope_hash = scope_sha256(detail.current.license_scope.to_domain())
            for revision in detail.revisions:
                if (
                    revision.license_evidence_id is not None
                    and revision.license_scope_hash == current_scope_hash
                    and revision.evidence_ref == detail.current.evidence_ref
                ):
                    evidence_options.append(
                        {
                            "evidence_id": str(revision.license_evidence_id),
                            "digest": revision.license_digest or "",
                            "scope_hash": revision.license_scope_hash or "",
                            "label": (
                                f"Phiên bản {revision.sequence} · "
                                f"{(revision.license_digest or '')[:12]}"
                            ),
                        }
                    )
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
            "scope_output_labels": SCOPE_OUTPUT_LABELS,
            "scope_derived_use_labels": SCOPE_DERIVED_USE_LABELS,
            "pending_evidence": pending_evidence,
            "evidence_options": evidence_options,
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
            if action == "revision" and payload.get("state") == "rights_pending":
                retention = payload.pop("scope_max_retention_days", "")
                if not isinstance(retention, str):
                    raise HTTPException(422, "Thời hạn lưu trữ không hợp lệ.")
                if retention and (not retention.isascii() or not retention.isdecimal()):
                    raise HTTPException(422, "Thời hạn lưu trữ phải là số ngày dương.")
                scope = {
                    "scope_version": "wp06-license-scope-v1",
                    "source_id": source_id,
                    "provider": payload.pop("scope_provider", ""),
                    "product": payload.pop("scope_product", ""),
                    "venue": payload.pop("scope_venue", ""),
                    "contract": payload.pop("scope_contract", ""),
                    "symbols": _split_scope_members(
                        payload.pop("scope_symbols", ""), "mã giao dịch"
                    ),
                    "intervals": _split_scope_members(
                        payload.pop("scope_intervals", ""), "khung thời gian"
                    ),
                    "data_start": payload.pop("scope_data_start", ""),
                    "data_end": payload.pop("scope_data_end", ""),
                    "allowed_purposes": selected,
                    "allowed_outputs": _selected_form_values(
                        payload, "scope_output_", SCOPE_OUTPUT_LABELS
                    ),
                    "max_retention_days": int(retention) if retention else None,
                    "attribution": payload.pop("scope_attribution", ""),
                    "derived_use": _selected_form_values(
                        payload, "scope_derived_", SCOPE_DERIVED_USE_LABELS
                    ),
                }
                payload.update(
                    provider=scope["provider"],
                    product=scope["product"],
                    venue=scope["venue"],
                    contract=scope["contract"],
                    attribution=scope["attribution"],
                    retention_ref="metadata-only-license-scope",
                    license_scope=scope,
                )
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
