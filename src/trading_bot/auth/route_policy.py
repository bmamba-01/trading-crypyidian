"""Registry và dependency phân quyền deny-by-default tập trung cho mọi HTTP route (SEC-18).

Mọi HTTP route trong ``app.routes`` bắt buộc khai báo một ``RoutePolicy`` tường minh thông qua
các hàm tạo dependency của module này (``permission_route``, ``bot_scoped_route``, ``owner_route``,
``roles_route``, ``authenticated_route``, hoặc ``public_route`` kèm lý do thuộc allowlist).

Nguyên tắc vận hành:
1. **Deny-by-default:** Bất kỳ ``APIRoute`` nào thiếu ``RoutePolicy``, hoặc route không nằm
   trong allowlist nhưng gán ``PUBLIC``, hoặc ``Mount``/``Route`` ngoài danh sách khung cho phép
   đều bị ``audit_app_routes`` / ``assert_all_routes_have_policy`` từ chối ngay.
2. **Bảo toàn BOLA & Audit:** Các route gắn ``bot_id`` khai báo ``BotScopePolicy`` để nối kiểm tra
   trần vai trò (``BotPermission``) và phạm vi cấp quyền theo từng bot (``BotGrantRepository``).
   Những thao tác control-plane cần che 404 (SEC-19) hoặc ghi ``audit_event`` từ chối ở service
   dùng ``EnforcementMode.SERVICE_AUDITED`` (chỉ giới hạn trong ``ALLOWED_SERVICE_AUDITED_PATHS``).
3. **CSRF đồng nhất:** Mọi route mutation không công khai phải chỉ rõ ``CsrfMode.HEADER`` (API qua
   ``require_csrf``), ``CsrfMode.FORM`` (form HTML qua ``_verify_form_csrf``), hoặc lý do miễn trừ
   tường minh cho thao tác chỉ đọc giả lập.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from starlette.routing import Mount, Route

from trading_bot.auth.bot_policy import BotPermission, role_allows_bot_permission
from trading_bot.auth.dependencies import current_identity, require_csrf
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.auth.service import AuthIdentity

ROUTE_POLICY_ATTR = "__route_policy__"
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Danh sách route công khai tối thiểu được phép bỏ qua xác thực phiên (kèm kiểm soát riêng).
ALLOWED_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health/live",
        "/health/ready",
        "/admin/login",
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/bootstrap/verify",
        "/api/v1/auth/login",
        "/api/v1/telegram/webhook",
    }
)

# Chỉ vòng đời phiên tự phục vụ mới được dùng RouteAccess.AUTHENTICATED không kèm Permission.
ALLOWED_AUTHENTICATED_SESSION_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/me",
        "/api/v1/auth/csrf",
        "/api/v1/auth/reauthenticate",
        "/api/v1/auth/logout",
        "/api/v1/auth/sessions",
        "/api/v1/auth/sessions/{session_id}/revoke",
    }
)

# Các route control có service tự ghi audit từ chối hoặc che 404 theo SEC-19.
ALLOWED_SERVICE_AUDITED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/bots/{bot_id}/runtime/pause",
        "/api/v1/bots/{bot_id}/runtime/resume",
        "/api/v1/bots/{bot_id}/disable",
        "/api/v1/bots/{bot_id}/enable",
        "/api/v1/workspace/runtime/kill",
        "/api/v1/workspace/runtime/release",
        "/admin/bots/{bot_id}/pause",
        "/admin/bots/{bot_id}/resume",
    }
)

# Starlette routes nội bộ của FastAPI (docs/reDoc đã tắt trong app.py).
ALLOWED_FRAMEWORK_ROUTES: dict[str, type[Any]] = {
    "/openapi.json": Route,
    "/static": Mount,
}


class RouteAccess(StrEnum):
    """Chế độ kiểm soát truy cập khai báo cho từng HTTP route."""

    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    PERMISSION = "permission"
    BOT_SCOPED = "bot_scoped"
    OWNER_ONLY = "owner_only"
    ROLES = "roles"


class CsrfMode(StrEnum):
    """Cơ chế chống CSRF áp dụng cho route."""

    NONE = "none"
    HEADER = "header"
    FORM = "form"
    LOGIN_COOKIE = "login_cookie"
    WEBHOOK_SECRET = "webhook_secret"
    BOOTSTRAP_TOKEN = "bootstrap_token"
    PUBLIC_AUTH = "public_auth"


class EnforcementMode(StrEnum):
    """Vị trí thi hành quyết định từ chối chi tiết."""

    DEPENDENCY = "dependency"
    SERVICE_AUDITED = "service_audited"


@dataclass(frozen=True, slots=True)
class BotScopePolicy:
    """Khai báo phạm vi kiểm soát quyền cấp đối tượng theo ``bot_id`` (SEC-19)."""

    permission: BotPermission
    param_name: str | None = "bot_id"
    param_location: Literal["path", "query", "body", "service"] = "path"

    def __post_init__(self) -> None:
        if self.param_location != "service" and not (self.param_name or "").strip():
            raise ValueError("BotScopePolicy yêu cầu param_name khi không ở chế độ service.")


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """Hợp đồng phân quyền gắn trực tiếp lên từng HTTP route."""

    access: RouteAccess
    permissions: tuple[Permission, ...] = ()
    require_all_permissions: bool = False
    roles: tuple[Role, ...] = ()
    bot_scope: BotScopePolicy | None = None
    csrf: CsrfMode = CsrfMode.NONE
    enforcement: EnforcementMode = EnforcementMode.DEPENDENCY
    public_reason: str | None = None
    csrf_exempt_reason: str | None = None
    denied_detail: str = "Không đủ quyền thực hiện thao tác này."
    denied_code: str | None = None

    def __post_init__(self) -> None:
        if self.access is RouteAccess.PUBLIC:
            if not (self.public_reason or "").strip():
                raise ValueError("Route PUBLIC bắt buộc khai báo public_reason.")
            if self.permissions or self.roles or self.bot_scope is not None:
                raise ValueError("Route PUBLIC không được mang permission/role/bot_scope.")
            return

        if self.public_reason is not None:
            raise ValueError("Chỉ route PUBLIC mới dùng public_reason.")

        if self.access is RouteAccess.AUTHENTICATED:
            if self.permissions or self.roles or self.bot_scope is not None:
                raise ValueError("Route AUTHENTICATED không nhận permission/role/bot_scope.")
        elif self.access is RouteAccess.PERMISSION:
            if not self.permissions:
                raise ValueError("Route PERMISSION phải khai báo ít nhất một Permission.")
        elif self.access is RouteAccess.BOT_SCOPED:
            if self.bot_scope is None:
                raise ValueError("Route BOT_SCOPED phải khai báo bot_scope.")
        elif self.access is RouteAccess.ROLES:
            if not self.roles:
                raise ValueError("Route ROLES phải khai báo ít nhất một Role.")
        elif self.access is RouteAccess.OWNER_ONLY:
            if self.roles and self.roles != (Role.OWNER,):
                raise ValueError("Route OWNER_ONLY chỉ chấp nhận Role.OWNER.")


class RoutePolicyViolation(AssertionError):
    """Ném khi phát hiện route vi phạm nguyên tắc deny-by-default SEC-18."""


@dataclass(frozen=True, slots=True)
class PolicyViolationItem:
    """Chi tiết một vi phạm khi quét ``app.routes``."""

    path: str
    methods: tuple[str, ...]
    reason: str

    def __str__(self) -> str:
        method_list = ",".join(self.methods) or "MOUNT"
        return f"[{method_list}] {self.path}: {self.reason}"


@dataclass(frozen=True, slots=True)
class AuditedRoute:
    """Bản ghi route đã qua kiểm định chính sách SEC-18."""

    path: str
    methods: frozenset[str]
    endpoint_name: str
    policy: RoutePolicy


@dataclass(frozen=True, slots=True)
class RouteAuditReport:
    """Kết quả quét toàn bộ ``app.routes``."""

    audited: tuple[AuditedRoute, ...]
    violations: tuple[PolicyViolationItem, ...]


class _PolicyHTTPException(HTTPException):
    """Ném khi từ chối ở tầng dependency kèm mã lý do an toàn nếu route yêu cầu."""

    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        reason_code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.reason_code = reason_code


def _deny_http_exception(policy: RoutePolicy) -> HTTPException:
    if policy.denied_code is not None:
        return _PolicyHTTPException(
            status_code=403,
            detail=policy.denied_detail,
            reason_code=policy.denied_code,
            headers={"Cache-Control": "no-store"},
        )
    return HTTPException(
        status_code=403,
        detail=policy.denied_detail,
        headers={"Cache-Control": "no-store"},
    )


def _enforce_principal_policy(principal: Principal, policy: RoutePolicy) -> None:
    """Thi hành chính sách ở tầng dependency FastAPI (fail-closed)."""
    if policy.access is RouteAccess.AUTHENTICATED:
        return

    if not principal.roles:
        raise _deny_http_exception(policy)

    if policy.enforcement is EnforcementMode.SERVICE_AUDITED:
        return

    allowed = True
    if policy.access is RouteAccess.OWNER_ONLY:
        allowed = Role.OWNER in principal.roles
    elif policy.access is RouteAccess.ROLES:
        allowed = bool(principal.roles & frozenset(policy.roles))
    elif policy.permissions:
        if policy.require_all_permissions:
            allowed = all(authorize(principal, perm) for perm in policy.permissions)
        else:
            allowed = any(authorize(principal, perm) for perm in policy.permissions)

    if allowed and policy.bot_scope is not None:
        allowed = role_allows_bot_permission(principal.roles, policy.bot_scope.permission)

    if not allowed:
        raise _deny_http_exception(policy)


def require_route_policy(policy: RoutePolicy) -> Any:
    """Tạo FastAPI ``Depends(...)`` gắn ``RoutePolicy`` lên route và thi hành khi gọi HTTP."""
    if policy.access is RouteAccess.PUBLIC:

        def _public_policy_guard() -> None:
            return None

        setattr(_public_policy_guard, ROUTE_POLICY_ATTR, policy)
        return Depends(_public_policy_guard)

    if policy.csrf is CsrfMode.HEADER:

        async def _header_csrf_policy_guard(
            identity: Annotated[AuthIdentity, Depends(require_csrf)],
        ) -> AuthIdentity:
            _enforce_principal_policy(identity.principal, policy)
            return identity

        setattr(_header_csrf_policy_guard, ROUTE_POLICY_ATTR, policy)
        return Depends(_header_csrf_policy_guard)

    async def _session_policy_guard(
        identity: Annotated[AuthIdentity, Depends(current_identity)],
    ) -> AuthIdentity:
        _enforce_principal_policy(identity.principal, policy)
        return identity

    setattr(_session_policy_guard, ROUTE_POLICY_ATTR, policy)
    return Depends(_session_policy_guard)


def public_route(*, reason: str, csrf: CsrfMode = CsrfMode.NONE) -> Any:
    """Khai báo route công khai thuộc allowlist kèm lý do bắt buộc."""
    return require_route_policy(
        RoutePolicy(access=RouteAccess.PUBLIC, csrf=csrf, public_reason=reason)
    )


def authenticated_route(*, csrf: CsrfMode = CsrfMode.NONE) -> Any:
    """Khai báo route chỉ yêu cầu phiên hợp lệ (vòng đời phiên tự phục vụ)."""
    return require_route_policy(RoutePolicy(access=RouteAccess.AUTHENTICATED, csrf=csrf))


def owner_route(
    *,
    csrf: CsrfMode = CsrfMode.NONE,
    enforcement: EnforcementMode = EnforcementMode.DEPENDENCY,
    denied_detail: str = "Chỉ chủ sở hữu (owner) được thực hiện thao tác này.",
    denied_code: str | None = None,
) -> Any:
    """Khai báo route chỉ dành riêng cho vai trò ``Role.OWNER``."""
    return require_route_policy(
        RoutePolicy(
            access=RouteAccess.OWNER_ONLY,
            roles=(Role.OWNER,),
            csrf=csrf,
            enforcement=enforcement,
            denied_detail=denied_detail,
            denied_code=denied_code,
        )
    )


def permission_route(
    *permissions: Permission,
    require_all: bool = False,
    bot_scope: BotScopePolicy | None = None,
    csrf: CsrfMode = CsrfMode.NONE,
    csrf_exempt_reason: str | None = None,
    enforcement: EnforcementMode = EnforcementMode.DEPENDENCY,
    denied_detail: str = "Không đủ quyền thực hiện thao tác này.",
) -> Any:
    """Khai báo route yêu cầu ``Permission`` ở mức workspace (và tùy chọn ``bot_scope``)."""
    return require_route_policy(
        RoutePolicy(
            access=RouteAccess.PERMISSION,
            permissions=permissions,
            require_all_permissions=require_all,
            bot_scope=bot_scope,
            csrf=csrf,
            csrf_exempt_reason=csrf_exempt_reason,
            enforcement=enforcement,
            denied_detail=denied_detail,
        )
    )


def bot_scoped_route(
    permission: BotPermission,
    *,
    param_name: str | None = "bot_id",
    param_location: Literal["path", "query", "body", "service"] = "path",
    workspace_permissions: tuple[Permission, ...] = (Permission.WORKSPACE_READ,),
    csrf: CsrfMode = CsrfMode.NONE,
    enforcement: EnforcementMode = EnforcementMode.DEPENDENCY,
    denied_detail: str = "Không đủ quyền thao tác bot này.",
) -> Any:
    """Khai báo route gắn phạm vi từng bot (BOLA / SEC-19) qua ``BotPermission``."""
    return require_route_policy(
        RoutePolicy(
            access=RouteAccess.BOT_SCOPED,
            permissions=workspace_permissions,
            bot_scope=BotScopePolicy(
                permission=permission,
                param_name=param_name,
                param_location=param_location,
            ),
            csrf=csrf,
            enforcement=enforcement,
            denied_detail=denied_detail,
        )
    )


def roles_route(
    *roles: Role,
    csrf: CsrfMode = CsrfMode.NONE,
    denied_detail: str = "Vai trò hiện tại không được phép thực hiện thao tác này.",
) -> Any:
    """Khai báo route cho danh sách ``Role`` cụ thể."""
    return require_route_policy(
        RoutePolicy(
            access=RouteAccess.ROLES,
            roles=roles,
            csrf=csrf,
            denied_detail=denied_detail,
        )
    )


def _walk_dependants(root: Dependant | None) -> list[Dependant]:
    if root is None:
        return []
    result: list[Dependant] = []
    stack = [root]
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(node.dependencies)
    return result


def extract_route_policy(route_context: RouteContext) -> RoutePolicy | None:
    """Trích xuất ``RoutePolicy`` gắn trên một ``RouteContext`` từ FastAPI."""
    found: list[RoutePolicy] = []
    dependant = getattr(route_context, "dependant", None)
    for node in _walk_dependants(dependant):
        policy = getattr(node.call, ROUTE_POLICY_ATTR, None)
        if isinstance(policy, RoutePolicy):
            found.append(policy)
    endpoint = route_context.endpoint
    if endpoint is not None:
        ep_policy = getattr(endpoint, ROUTE_POLICY_ATTR, None)
        if isinstance(ep_policy, RoutePolicy):
            found.append(ep_policy)
    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        raise ValueError(f"Route {route_context.path} khai báo nhiều hơn một RoutePolicy.")
    return unique[0] if unique else None


def _dependant_calls(route_context: RouteContext) -> set[Callable[..., Any]]:
    dependant = getattr(route_context, "dependant", None)
    return {node.call for node in _walk_dependants(dependant) if node.call is not None}


def audit_app_routes(app: FastAPI) -> RouteAuditReport:
    """Quét toàn bộ ``app.routes`` và trả về báo cáo tuân thủ SEC-18 (deny-by-default)."""
    audited: list[AuditedRoute] = []
    violations: list[PolicyViolationItem] = []

    for ctx in iter_route_contexts(app.routes):
        path = ctx.path or ""
        methods = frozenset(ctx.methods or ())
        method_tuple = tuple(sorted(methods))

        if not isinstance(ctx.original_route, APIRoute):
            expected_type = ALLOWED_FRAMEWORK_ROUTES.get(path)
            if expected_type is None or not isinstance(ctx.original_route, expected_type):
                violations.append(
                    PolicyViolationItem(
                        path=path,
                        methods=method_tuple,
                        reason=(
                            "Starlette Route/Mount ngoài danh sách khung cho phép "
                            f"({type(ctx.original_route).__name__})."
                        ),
                    )
                )
            continue

        try:
            policy = extract_route_policy(ctx)
        except ValueError as error:
            violations.append(
                PolicyViolationItem(path=path, methods=method_tuple, reason=str(error))
            )
            continue

        if policy is None:
            violations.append(
                PolicyViolationItem(
                    path=path,
                    methods=method_tuple,
                    reason="Thiếu khai báo RoutePolicy (vi phạm deny-by-default).",
                )
            )
            continue

        route_errors = _validate_route_policy(ctx, path, methods, policy)
        if route_errors:
            for reason in route_errors:
                violations.append(
                    PolicyViolationItem(path=path, methods=method_tuple, reason=reason)
                )
            continue

        endpoint = ctx.endpoint
        endpoint_name = (
            f"{endpoint.__module__}.{endpoint.__name__}" if endpoint is not None else "<unknown>"
        )
        audited.append(
            AuditedRoute(
                path=path,
                methods=methods,
                endpoint_name=endpoint_name,
                policy=policy,
            )
        )

    return RouteAuditReport(audited=tuple(audited), violations=tuple(violations))


def _validate_route_policy(
    ctx: RouteContext, path: str, methods: frozenset[str], policy: RoutePolicy
) -> list[str]:
    errors: list[str] = []
    calls = _dependant_calls(ctx)
    is_mutation = bool(methods & MUTATION_METHODS)

    if policy.access is RouteAccess.PUBLIC:
        if path not in ALLOWED_PUBLIC_PATHS:
            errors.append("Route PUBLIC không nằm trong ALLOWED_PUBLIC_PATHS.")
        if path.startswith("/admin") and path != "/admin/login":
            errors.append("Route /admin* (trừ /admin/login) không được phép là PUBLIC.")
        if current_identity in calls or require_csrf in calls:
            errors.append("Route PUBLIC không được gắn dependency phiên người dùng.")
    else:
        if current_identity not in calls and require_csrf not in calls:
            errors.append(
                "Route bảo vệ thiếu dependency xác thực phiên (current_identity/require_csrf)."
            )
        if policy.access is RouteAccess.AUTHENTICATED:
            if path not in ALLOWED_AUTHENTICATED_SESSION_PATHS:
                errors.append(
                    "RouteAccess.AUTHENTICATED chỉ dành cho endpoint vòng đời phiên /api/v1/auth/*."
                )
        if is_mutation:
            if policy.csrf is CsrfMode.NONE and not (policy.csrf_exempt_reason or "").strip():
                errors.append(
                    "Route mutation thiếu chính sách CSRF (csrf hoặc csrf_exempt_reason)."
                )
            if policy.csrf is CsrfMode.HEADER and require_csrf not in calls:
                errors.append("Route khai báo CsrfMode.HEADER nhưng thiếu dependency require_csrf.")

    if policy.bot_scope is not None:
        if policy.bot_scope.param_location == "path" and policy.bot_scope.param_name:
            marker = f"{{{policy.bot_scope.param_name}}}"
            if marker not in path:
                errors.append(f"bot_scope khai báo tham số path '{marker}' nhưng route không có.")

    if policy.enforcement is EnforcementMode.SERVICE_AUDITED:
        if path not in ALLOWED_SERVICE_AUDITED_PATHS:
            errors.append(
                "EnforcementMode.SERVICE_AUDITED chỉ dùng cho ALLOWED_SERVICE_AUDITED_PATHS."
            )

    return errors


def assert_all_routes_have_policy(app: FastAPI) -> tuple[AuditedRoute, ...]:
    """Xác nhận mọi route trong ``app.routes`` đều có chính sách hợp lệ; fail nếu vi phạm."""
    report = audit_app_routes(app)
    if report.violations:
        formatted = "\n".join(f" - {item}" for item in report.violations)
        raise RoutePolicyViolation(
            f"Phát hiện {len(report.violations)} route vi phạm chính sách SEC-18:\n{formatted}"
        )
    return report.audited


def iter_app_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Liệt kê bộ ba ``(method, path, route)`` của mọi ``APIRoute`` trong ``app.routes``."""
    items: list[tuple[str, str, APIRoute]] = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.route
        if not isinstance(route, APIRoute):
            continue
        path = str(getattr(ctx, "path", route.path))
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            items.append((method, path, route))
    return items


def verify_app_routes_registered(app: FastAPI) -> None:
    """Kiểm tra deny-by-default mọi route; ném ``AssertionError`` nếu thiếu policy."""
    try:
        assert_all_routes_have_policy(app)
    except RoutePolicyViolation as error:
        raise AssertionError(str(error)) from error


__all__ = [
    "ALLOWED_AUTHENTICATED_SESSION_PATHS",
    "ALLOWED_FRAMEWORK_ROUTES",
    "ALLOWED_PUBLIC_PATHS",
    "ALLOWED_SERVICE_AUDITED_PATHS",
    "MUTATION_METHODS",
    "ROUTE_POLICY_ATTR",
    "AuditedRoute",
    "BotScopePolicy",
    "CsrfMode",
    "EnforcementMode",
    "PolicyViolationItem",
    "RouteAccess",
    "RouteAuditReport",
    "RoutePolicy",
    "RoutePolicyViolation",
    "assert_all_routes_have_policy",
    "audit_app_routes",
    "authenticated_route",
    "bot_scoped_route",
    "extract_route_policy",
    "iter_app_routes",
    "owner_route",
    "permission_route",
    "public_route",
    "require_route_policy",
    "roles_route",
    "verify_app_routes_registered",
]
