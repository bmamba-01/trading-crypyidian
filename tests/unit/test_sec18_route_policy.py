"""Kiểm thử SEC-18: registry route -> permission deny-by-default và quét toàn bộ app.routes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.route_policy import (
    ALLOWED_PUBLIC_PATHS,
    RouteAccess,
    RoutePolicyViolation,
    assert_all_routes_have_policy,
    audit_app_routes,
)
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
RESOURCE_ID = UUID("4806de3a-bd5c-4b7e-8d25-e90ef844c5d8")


def _identity(*roles: Role) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset(roles),
            mfa_verified_at=datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email="sec18@crypidian.test",
    )


class _UnauthAuthService:
    async def authenticate(self, _cookie: str | None) -> AuthIdentity:
        raise AuthenticationFailure(401, "Yêu cầu đăng nhập.")

    async def verify_csrf(self, _cookie: str | None, _token: str | None) -> None:
        raise AuthenticationFailure(403, "Thiếu mã CSRF.")


def _client_for_roles(*roles: Role, authenticated: bool = True) -> TestClient:
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    app.state.auth_service = _UnauthAuthService()
    if authenticated:
        actor = _identity(*roles)
        app.dependency_overrides[current_identity] = lambda: actor
        app.dependency_overrides[require_csrf] = lambda: actor
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Quét tự động toàn bộ app.routes (SEC-18 & SECT-15)
# ---------------------------------------------------------------------------


def _settings_for_process_role(process_role: str) -> AppSettings:
    if process_role == "trading-runtime":
        return AppSettings(
            app_env="test",
            process_role=process_role,
            runtime_workspace_id=WORKSPACE_ID,
        )
    if process_role == "signal-worker":
        return AppSettings(
            app_env="test",
            process_role=process_role,
            signal_workspace_id=WORKSPACE_ID,
            telegram_bot_token_file=Path("/run/secrets/telegram_bot_token"),
        )
    if process_role == "market-signal-worker":
        return AppSettings(
            app_env="test",
            process_role=process_role,
            market_signal_workspace_id=WORKSPACE_ID,
        )
    return AppSettings(app_env="test", process_role=process_role)


@pytest.mark.parametrize(
    "process_role",
    ["admin-web", "trading-runtime", "signal-worker", "market-signal-worker"],
)
def test_every_route_in_app_routes_has_explicit_policy(process_role: str) -> None:
    """Mọi route trong app.routes theo từng process_role đều phải có policy hợp lệ."""
    app = create_app(_settings_for_process_role(process_role))
    report = audit_app_routes(app)
    assert report.violations == (), f"Vi phạm SEC-18 ở {process_role}: {report.violations}"
    audited = assert_all_routes_have_policy(app)
    assert len(audited) >= 2
    if process_role == "admin-web":
        # Đảm bảo quét qua toàn bộ các nhóm router chính của admin-web.
        paths = {item.path for item in audited}
        assert {
            "/admin",
            "/admin/api/dashboard/realtime",
            "/admin/api/chart",
            "/admin/scanner",
            "/admin/calendar",
            "/admin/learning/models",
            "/admin/learning/scoring",
            "/admin/research-sources",
            "/admin/risk",
            "/admin/live",
            "/admin/backtests",
            "/api/v1/settings/current",
            "/api/v1/bots",
            "/api/v1/workspace/runtime/kill",
            "/api/v1/workspace/flatten/confirm",
            "/api/v1/telegram-binding/link",
            "/api/v1/telegram/webhook",
        } <= paths


def test_public_allowlist_never_exposes_admin_console_or_business_mutations() -> None:
    """Chỉ các endpoint công khai đã biện minh mới được phép mang RouteAccess.PUBLIC."""
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    audited = assert_all_routes_have_policy(app)
    public_routes = {item.path for item in audited if item.policy.access is RouteAccess.PUBLIC}
    assert public_routes == set(ALLOWED_PUBLIC_PATHS)
    for item in audited:
        if item.policy.access is RouteAccess.PUBLIC:
            assert item.policy.public_reason
            assert item.path in ALLOWED_PUBLIC_PATHS
        if item.path.startswith("/admin") and item.path != "/admin/login":
            assert item.policy.access is not RouteAccess.PUBLIC
            assert item.policy.access is not RouteAccess.AUTHENTICATED


def test_mutation_routes_declare_csrf_and_policy_in_admin_web() -> None:
    """Mọi route mutation (POST/PUT/PATCH/DELETE) phải có policy và chính sách CSRF rõ ràng."""
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    audited = assert_all_routes_have_policy(app)
    mutations = [item for item in audited if item.methods & {"POST", "PUT", "PATCH", "DELETE"}]
    assert len(mutations) >= 40
    for item in mutations:
        assert item.policy is not None
        if item.policy.access is not RouteAccess.PUBLIC:
            assert item.policy.csrf.value != "none" or item.policy.csrf_exempt_reason


def test_deny_by_default_fails_when_route_missing_policy() -> None:
    """Thêm route mới (mutation hoặc /admin*) không khai báo policy phải bị FAIL ngay."""
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    rogue = APIRouter()

    @rogue.post("/admin/api/rogue-action")
    async def _rogue_mutation() -> dict[str, bool]:
        return {"ok": True}

    @rogue.get("/admin/rogue-page")
    async def _rogue_page() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(rogue)
    report = audit_app_routes(app)
    violating_paths = {v.path for v in report.violations}
    assert "/admin/api/rogue-action" in violating_paths
    assert "/admin/rogue-page" in violating_paths
    with pytest.raises(RoutePolicyViolation):
        assert_all_routes_have_policy(app)


def test_deny_by_default_fails_when_admin_route_marked_public_or_rogue_mount() -> None:
    """Route /admin* không thuộc allowlist bị gán public hoặc Mount lạ phải bị FAIL."""
    from trading_bot.auth.route_policy import public_route

    app = create_app(AppSettings(app_env="test", process_role="admin-web"))

    @app.get("/admin/secret-debug", dependencies=[public_route(reason="Cố tình mở công khai")])
    async def _leak() -> dict[str, bool]:
        return {"leak": True}

    app.mount("/debug-files", FastAPI())

    report = audit_app_routes(app)
    violating_paths = {v.path for v in report.violations}
    assert "/admin/secret-debug" in violating_paths
    assert "/debug-files" in violating_paths


def test_bot_scoped_routes_declare_matching_bot_permission() -> None:
    """Các route nhận bot_id trên path phải khai báo bot_scope khớp tên tham số."""
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    audited = assert_all_routes_have_policy(app)
    by_key = {(tuple(sorted(item.methods)), item.path): item for item in audited}

    expected_bot_scopes = {
        (("GET",), "/api/v1/bots/{bot_id}"): BotPermission.READ,
        (("POST",), "/api/v1/bots/{bot_id}/config-drafts"): BotPermission.CONFIG_WRITE,
        (("PUT",), "/api/v1/bots/{bot_id}/config-drafts/{draft_id}"): BotPermission.CONFIG_WRITE,
        (("POST",), "/api/v1/bots/{bot_id}/config-drafts/{draft_id}/validate"): (
            BotPermission.CONFIG_WRITE
        ),
        (("POST",), "/api/v1/bots/{bot_id}/config-drafts/{draft_id}/publish"): (
            BotPermission.CONFIG_PUBLISH
        ),
        (("POST",), "/api/v1/bots/{bot_id}/config-versions/{version}/rollback"): (
            BotPermission.CONFIG_PUBLISH
        ),
        (("GET",), "/api/v1/bots/{bot_id}/runtime"): BotPermission.READ,
        (("POST",), "/api/v1/bots/{bot_id}/runtime/pause"): BotPermission.RUNTIME_PAUSE,
        (("POST",), "/api/v1/bots/{bot_id}/runtime/resume"): BotPermission.RUNTIME_RESUME,
        (("GET",), "/api/v1/telegram-signals/{bot_id}/subscription"): BotPermission.READ,
        (("PUT",), "/api/v1/telegram-signals/{bot_id}/subscription"): BotPermission.READ,
        (("GET",), "/admin/bots/{bot_id}"): BotPermission.READ,
        (("GET",), "/admin/api/bots/{bot_id}/realtime"): BotPermission.READ,
        (("GET",), "/admin/dashboard/bots/{bot_id}/snapshot"): BotPermission.REPORT_READ,
        (("POST",), "/admin/bots/{bot_id}/config-drafts"): BotPermission.CONFIG_WRITE,
        (("POST",), "/admin/bots/{bot_id}/ai-proposals/{backtest_id}/draft"): (
            BotPermission.CONFIG_WRITE
        ),
        (("POST",), "/admin/bots/{bot_id}/config-drafts/{draft_id}/validate"): (
            BotPermission.CONFIG_WRITE
        ),
        (("POST",), "/admin/bots/{bot_id}/config-drafts/{draft_id}/publish"): (
            BotPermission.CONFIG_PUBLISH
        ),
        (("POST",), "/admin/bots/{bot_id}/pause"): BotPermission.RUNTIME_PAUSE,
        (("POST",), "/admin/bots/{bot_id}/resume"): BotPermission.RUNTIME_RESUME,
    }
    for key, expected_perm in expected_bot_scopes.items():
        route_info = by_key[key]
        assert route_info.policy.bot_scope is not None, key
        assert route_info.policy.bot_scope.permission is expected_perm, key
        assert route_info.policy.bot_scope.param_name == "bot_id", key


# ---------------------------------------------------------------------------
# 2. Kiểm thử ma trận phân quyền runtime (SECT-16 + các route từng thiếu policy)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin"),
        ("GET", "/admin/api/dashboard/realtime"),
        ("GET", f"/admin/dashboard/bots/{BOT_ID}/snapshot"),
        ("GET", "/admin/bots"),
        ("GET", "/admin/bots/new"),
        ("POST", "/admin/bots/new"),
        ("GET", f"/admin/bots/{BOT_ID}"),
        ("GET", f"/admin/api/bots/{BOT_ID}/realtime"),
        ("POST", f"/admin/bots/{BOT_ID}/config-drafts"),
        ("GET", "/admin/flatten"),
        ("POST", "/admin/flatten/preview"),
        ("POST", "/admin/flatten/confirm"),
        ("GET", f"/admin/flatten/operations/{RESOURCE_ID}"),
        ("GET", "/admin/telegram"),
        ("POST", "/admin/telegram/link"),
        ("GET", "/admin/reports"),
        ("GET", "/admin/reports/export.csv?start=2026-11-01T00:00:00Z&end=2026-11-02T00:00:00Z"),
        ("GET", "/admin/strategy-cards"),
        ("GET", "/admin/api/strategy-cards"),
        ("GET", "/admin/api/strategy-releases"),
        ("POST", "/admin/api/strategy-releases/publish"),
        ("GET", "/admin/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope"),
        ("POST", "/admin/api/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/inspect-bar"),
        ("GET", "/admin/chart"),
        ("GET", f"/admin/api/chart?bot_id={BOT_ID}&symbol=BTCUSDT"),
        ("GET", f"/admin/api/chart/overview?bot_id={BOT_ID}&symbol=BTCUSDT"),
        ("GET", f"/admin/api/chart/decision?bot_id={BOT_ID}&time=1763283600"),
        ("GET", f"/admin/api/chart/validate?bot_id={BOT_ID}&symbol=BTCUSDT"),
        ("GET", f"/admin/api/chart/decisions?bot_id={BOT_ID}"),
        ("GET", f"/admin/api/chart/history?bot_id={BOT_ID}&symbol=BTCUSDT"),
        ("GET", "/admin/ai-analyses"),
        ("GET", "/admin/signal-outcomes"),
        ("GET", "/admin/calendar"),
        ("POST", "/admin/calendar"),
        ("GET", "/admin/scanner"),
        ("POST", "/admin/scanner"),
        ("GET", "/admin/risk"),
        ("POST", "/admin/risk"),
        ("GET", "/admin/live"),
        ("GET", "/admin/api/live/snapshot"),
        ("GET", "/admin/backtests"),
        ("GET", "/admin/learning/models"),
        ("GET", "/admin/learning/observations"),
        ("GET", "/admin/learning/paper-performance"),
        ("GET", "/admin/learning/scoring"),
        ("GET", "/admin/learning/training"),
        ("GET", "/admin/learning/splits"),
        ("GET", "/admin/research-sources"),
        ("GET", "/admin/api/operations/health"),
        ("POST", "/admin/api/operations/breaker/resume"),
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/bots"),
        ("POST", "/api/v1/bots"),
        ("GET", "/api/v1/settings/current"),
        ("POST", "/api/v1/workspace/flatten/preview"),
        ("POST", "/api/v1/telegram-binding/link"),
    ],
)
def test_unauthenticated_and_empty_role_denied_by_default(method: str, path: str) -> None:
    """Chưa đăng nhập bị 401; principal rỗng vai trò (frozenset()) bị chặn 403 trên mọi route."""
    unauth = _client_for_roles(authenticated=False)
    res_unauth = unauth.request(method, path, follow_redirects=False)
    assert res_unauth.status_code == 401, (method, path, res_unauth.status_code)

    if path.startswith("/api/v1/auth/"):
        return
    empty_role = _client_for_roles()
    res_empty = empty_role.request(method, path, json={}, follow_redirects=False)
    assert res_empty.status_code == 403, (method, path, res_empty.status_code)


@pytest.mark.parametrize(
    ("role", "method", "path", "expected_status"),
    [
        # Owner-only routes (403 cho các role khác)
        (Role.OPERATOR, "GET", "/admin/bots/new", 403),
        (Role.STRATEGY_ADMIN, "POST", "/admin/bots/new", 403),
        (Role.RISK_ADMIN, "POST", "/api/v1/bots", 403),
        (Role.ANALYST_REVIEWER, "GET", "/admin/flatten", 403),
        (Role.RISK_ADMIN, "POST", "/admin/flatten/preview", 403),
        (Role.OPERATOR, "POST", "/admin/flatten/confirm", 403),
        (Role.STRATEGY_ADMIN, "POST", "/api/v1/workspace/flatten/preview", 403),
        (Role.OPERATOR, "GET", "/admin/telegram", 403),
        (Role.RISK_ADMIN, "POST", "/admin/telegram/link", 403),
        (Role.STRATEGY_ADMIN, "POST", "/api/v1/telegram-binding/link", 403),
        (Role.STRATEGY_ADMIN, "POST", "/admin/strategy-releases/publish", 403),
        (Role.OPERATOR, "POST", "/admin/api/strategy-releases/publish", 403),
        (Role.ANALYST_REVIEWER, "GET", "/admin/backtests", 403),
        (Role.OPERATOR, "POST", "/admin/backtests/submit", 403),
        # Calendar write: chỉ OWNER và STRATEGY_ADMIN
        (Role.OPERATOR, "POST", "/admin/calendar", 403),
        (Role.RISK_ADMIN, "POST", f"/admin/calendar/{RESOURCE_ID}", 403),
        (Role.ANALYST_REVIEWER, "POST", f"/admin/calendar/{RESOURCE_ID}/delete", 403),
        # Risk appetite write: chỉ OWNER và RISK_ADMIN
        (Role.OPERATOR, "POST", "/admin/risk", 403),
        (Role.STRATEGY_ADMIN, "POST", "/admin/risk", 403),
        (Role.ANALYST_REVIEWER, "POST", "/admin/risk", 403),
        # Bot config write/publish: chỉ OWNER và STRATEGY_ADMIN
        (Role.OPERATOR, "POST", f"/admin/bots/{BOT_ID}/config-drafts", 403),
        (Role.RISK_ADMIN, "POST", f"/admin/bots/{BOT_ID}/config-drafts/{RESOURCE_ID}/publish", 403),
        (Role.ANALYST_REVIEWER, "POST", f"/api/v1/bots/{BOT_ID}/config-drafts", 403),
        (Role.OPERATOR, "POST", f"/api/v1/bots/{BOT_ID}/config-drafts/{RESOURCE_ID}/publish", 403),
        # Workspace kill/release form: chỉ OWNER và RISK_ADMIN
        (Role.OPERATOR, "POST", "/admin/workspace/kill", 403),
        (Role.STRATEGY_ADMIN, "POST", "/admin/workspace/release", 403),
        (Role.ANALYST_REVIEWER, "POST", "/admin/workspace/kill", 403),
        # Operations breaker resume: cần RUNTIME_RESUME (OWNER, RISK_ADMIN, OPERATOR)
        (Role.STRATEGY_ADMIN, "POST", "/admin/api/operations/breaker/resume", 403),
        (Role.ANALYST_REVIEWER, "POST", "/admin/api/operations/breaker/resume", 403),
        # Trade retraining label: OWNER, STRATEGY_ADMIN, ANALYST_REVIEWER
        (Role.OPERATOR, "POST", f"/admin/api/trades/{RESOURCE_ID}/ingest-negative-sample", 403),
        (Role.RISK_ADMIN, "POST", f"/admin/api/trades/{RESOURCE_ID}/queue-ai-retraining", 403),
        # Learning & Research role boundaries
        (Role.OPERATOR, "GET", "/admin/learning/models", 403),
        (Role.STRATEGY_ADMIN, "GET", "/admin/learning/observations", 403),
        (Role.OPERATOR, "GET", "/admin/learning/paper-performance", 403),
        (Role.RISK_ADMIN, "GET", "/admin/learning/training", 403),
        (Role.STRATEGY_ADMIN, "GET", "/admin/learning/splits", 403),
        (Role.OPERATOR, "GET", "/admin/research-sources", 403),
        (Role.RISK_ADMIN, "POST", "/admin/research-sources/create", 403),
        (
            Role.ANALYST_REVIEWER,
            "POST",
            f"/admin/learning/scoring/events/{RESOURCE_ID}/paper-block",
            403,
        ),
        # Settings: chỉ OWNER và STRATEGY_ADMIN
        (Role.OPERATOR, "GET", "/api/v1/settings/current", 403),
        (Role.RISK_ADMIN, "POST", "/api/v1/settings/drafts", 403),
        (Role.ANALYST_REVIEWER, "POST", "/api/v1/settings/drafts", 403),
    ],
)
def test_role_endpoint_matrix_denies_unauthorized_roles(
    role: Role, method: str, path: str, expected_status: int
) -> None:
    """Ma trận role×endpoint từ chối đúng HTTP 403 trước khi chạm nghiệp vụ/DB."""
    client = _client_for_roles(role)
    response = client.request(method, path, json={}, follow_redirects=False)
    assert response.status_code == expected_status, (role, method, path, response.text)
    assert response.headers.get("Cache-Control") == "no-store"


def test_public_health_and_login_endpoints_remain_accessible() -> None:
    """Các endpoint public trong allowlist vẫn hoạt động không cần phiên."""
    unauth = _client_for_roles(authenticated=False)
    assert unauth.get("/health/live").status_code == 200
    assert unauth.get("/health/ready").status_code in {200, 503}
    assert unauth.get("/admin/login").status_code == 200


def test_unknown_role_is_denied_fail_closed_with_403() -> None:
    """Vai trò lạ/cũ không thuộc Role enum phải bị từ chối 403 fail-closed, không KeyError 500."""
    unknown = _client_for_roles("legacy_trader")  # type: ignore[arg-type]
    res = unknown.get("/admin/reports", follow_redirects=False)
    assert res.status_code == 403
    assert res.headers.get("Cache-Control") == "no-store"

    res_bot = unknown.get(f"/admin/api/bots/{BOT_ID}/realtime", follow_redirects=False)
    assert res_bot.status_code == 403
    assert res_bot.headers.get("Cache-Control") == "no-store"
