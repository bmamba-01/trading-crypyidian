"""MB-07b: trang bot phải dùng service đã kiểm quyền và autoescape template."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


class BotAdminStub:
    async def list_bots(self, principal: Principal) -> list[dict[str, object]]:
        assert principal.workspace_id == WORKSPACE
        return [
            {
                "id": str(BOT_ID),
                "name": '<script>alert("x")</script>',
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
                "current_config": None,
            }
        ]

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, object]:
        if bot_id != BOT_ID:
            from trading_bot.admin.bot_service import BotAdminError

            raise BotAdminError(404, "Không tìm thấy bot.")
        return (await self.list_bots(principal))[0]


class OperationsStub:
    def __init__(self) -> None:
        self.actions: list[tuple[str, UUID, str]] = []

    async def list_bots(self) -> list[dict[str, object]]:
        return [
            {
                "id": str(BOT_ID),
                "name": "Bot test",
                "status": "ENABLED",
                "entry_state": "ENABLED",
                "readiness": "READY",
                "entry_allowed": True,
                "control_revision": 4,
            }
        ]

    async def bot_status(self, bot_id: UUID) -> dict[str, object]:
        if bot_id != BOT_ID:
            from trading_bot.application.bot_runtime_control import BotRuntimeControlError

            raise BotRuntimeControlError("BOT_NOT_FOUND")
        return {
            "id": str(BOT_ID),
            "readiness": "READY",
            "entry_allowed": True,
            "control_revision": 4,
        }

    async def workspace_status(self) -> dict[str, object]:
        return {"kill_state": "ACTIVE", "revision": 1, "bots": []}

    def for_principal(self, _principal: Principal) -> OperationsStub:
        return self

    async def pause_bot(self, bot_id: UUID, reason: str) -> object:
        self.actions.append(("pause", bot_id, reason))
        return object()

    async def resume_bot(self, bot_id: UUID, reason: str, _revision: int) -> object:
        self.actions.append(("resume", bot_id, reason))
        return object()

    async def disable_bot(self, bot_id: UUID, reason: str) -> object:
        self.actions.append(("disable", bot_id, reason))
        return object()

    async def kill_workspace(self, reason: str) -> object:
        self.actions.append(("kill", WORKSPACE, reason))
        return object()

    async def release_workspace(self, reason: str, _revision: int) -> object:
        self.actions.append(("release", WORKSPACE, reason))
        return object()


def _client() -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=WORKSPACE,
        membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    app.state.bot_admin_service = BotAdminStub()
    app.state.bot_operations_service = OperationsStub()
    return TestClient(app)


def test_admin_bot_list_uses_scoped_service_and_escapes_html() -> None:
    response = _client().get("/admin/bots")

    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>alert" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_admin_bot_detail_conceals_bot_outside_scope() -> None:
    response = _client().get("/admin/bots/889ef59a-210a-4d79-9e4f-4734711a9ac0")

    assert response.status_code == 404
    assert "&lt;script&gt;" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


class ReportStub:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_bot_report(self, query: object, principal: Principal) -> object:
        del principal
        self.scopes.append("bot")
        return type("Report", (), {"as_dict": lambda _self: {"scope": "bot"}})()

    async def get_workspace_report(self, query: object, principal: Principal) -> object:
        del principal
        self.scopes.append("workspace")
        return type("Report", (), {"as_dict": lambda _self: {"scope": "workspace"}})()


def test_admin_reports_requires_explicit_scope_before_workspace_aggregation() -> None:
    client = _client()
    report_service = ReportStub()
    client.app.state.bot_reporting_service = report_service
    empty = client.get("/admin/reports")
    assert empty.status_code == 200
    assert report_service.scopes == []

    response = client.get(
        "/admin/reports",
        params={
            "scope": "workspace",
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-11-01T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert "workspace" in response.text
    assert report_service.scopes == ["workspace"]


class FlattenPrincipalStub:
    def __init__(self) -> None:
        self.preview_calls: list[str] = []
        self.confirm_calls: list[dict[str, object]] = []

    async def create_preview(self, reason: str) -> object:
        self.preview_calls.append(reason)
        return SimpleNamespace(
            preview_id=UUID("8a000000-0000-0000-0000-000000000001"),
            preview_hash="a" * 64,
            snapshot_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            bot_count=1,
            position_count=1,
            open_order_count=0,
            targets=(
                {
                    "bot_id": str(BOT_ID),
                    "target_type": "POSITION",
                    "target_id": "position-1",
                    "symbol": "BTCUSDT",
                    "status": "OPEN",
                    "quantity": "0.1",
                    "mark_price": "65000",
                    "error_code": "",
                },
            ),
        )

    async def confirm(self, **values: object) -> object:
        self.confirm_calls.append(values)
        return SimpleNamespace(operation_id=UUID("9a000000-0000-0000-0000-000000000001"))

    async def get_operation(self, operation_id: UUID) -> dict[str, object]:
        return {"id": str(operation_id), "status": "QUEUED", "items": []}


class FlattenServiceStub:
    def __init__(self) -> None:
        self.principal_service = FlattenPrincipalStub()

    def for_principal(self, _principal: Principal) -> FlattenPrincipalStub:
        return self.principal_service


def test_flatten_html_preview_requires_csrf_and_shows_review_snapshot() -> None:
    client = _client()
    auth = AuthServiceStub()
    flatten = FlattenServiceStub()
    client.app.state.auth_service = auth
    client.app.state.flatten_all_service = flatten

    page = client.get("/admin/flatten")
    denied = client.post(
        "/admin/flatten/preview", data={"reason": "đóng vị thế"}, follow_redirects=False
    )
    response = client.post(
        "/admin/flatten/preview",
        data={"csrf_token": "csrf-token-test-123", "reason": "đóng vị thế"},
    )

    assert page.status_code == 200
    assert denied.status_code == 403
    assert response.status_code == 200
    assert "65000" in response.text
    assert "Xác nhận flatten" in response.text
    assert flatten.principal_service.preview_calls == ["đóng vị thế"]
    assert auth.verified_tokens == [None, "csrf-token-test-123"]


class AuthServiceStub:
    def __init__(self) -> None:
        self.verified_tokens: list[str | None] = []

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "csrf-token-test-123"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        self.verified_tokens.append(token)
        if token != "csrf-token-test-123":
            from trading_bot.auth.service import AuthenticationFailure

            raise AuthenticationFailure(403, "Thiếu mã CSRF")


class CreateBotStub(BotAdminStub):
    def __init__(self) -> None:
        self.created: list[tuple[Principal, str, str]] = []

    async def create_bot(
        self, principal: Principal, *, name: str, max_notional: object
    ) -> tuple[dict[str, object], bool]:
        self.created.append((principal, name, str(max_notional)))
        return {"id": str(BOT_ID), "name": name}, True


class TelegramBindingsStub:
    def __init__(self) -> None:
        self.actions: list[tuple[object, ...]] = []

    async def list_bindings(self, _principal: Principal) -> list[dict[str, str]]:
        return [{"id": "binding-id", "membership_id": str(MEMBERSHIP), "created_at": "now"}]

    async def create_link(self, principal: Principal, *, reason: str) -> object:
        self.actions.append(("link", principal.membership_id, reason))
        return SimpleNamespace(
            url="https://t.me/crypidian_test_bot?start=" + "a" * 43,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def revoke(self, principal: Principal, membership_id: UUID, *, reason: str) -> int:
        self.actions.append(("revoke", principal.workspace_id, membership_id, reason))
        return 1


def test_create_bot_form_requires_csrf_before_calling_service() -> None:
    client = _client()
    admin = CreateBotStub()
    auth = AuthServiceStub()
    client.app.state.bot_admin_service = admin
    client.app.state.auth_service = auth

    response = client.post(
        "/admin/bots/new",
        data={"name": "paper-alpha", "max_notional": "1000"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert admin.created == []
    assert auth.verified_tokens == [None]


def test_create_bot_form_verifies_csrf_then_redirects_to_scoped_detail() -> None:
    client = _client()
    admin = CreateBotStub()
    auth = AuthServiceStub()
    client.app.state.bot_admin_service = admin
    client.app.state.auth_service = auth

    form = client.get("/admin/bots/new")
    response = client.post(
        "/admin/bots/new",
        data={
            "csrf_token": "csrf-token-test-123",
            "name": "paper-alpha",
            "max_notional": "1000.25",
        },
        follow_redirects=False,
    )

    assert form.status_code == 200
    assert 'value="csrf-token-test-123"' in form.text
    assert response.status_code == 303
    assert response.headers["Location"] == f"/admin/bots/{BOT_ID}"
    assert admin.created[0][1:] == ("paper-alpha", "1000.25")
    assert auth.verified_tokens == ["csrf-token-test-123"]


def test_pause_bot_form_rejects_missing_csrf_before_service_call() -> None:
    client = _client()
    operations = OperationsStub()
    client.app.state.bot_operations_service = operations
    client.app.state.auth_service = AuthServiceStub()

    response = client.post(
        f"/admin/bots/{BOT_ID}/pause",
        data={"reason": "Kiểm tra"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert operations.actions == []


def test_pause_bot_form_calls_shared_service_after_csrf_verification() -> None:
    client = _client()
    operations = OperationsStub()
    client.app.state.bot_operations_service = operations
    client.app.state.auth_service = AuthServiceStub()

    response = client.post(
        f"/admin/bots/{BOT_ID}/pause",
        data={"csrf_token": "csrf-token-test-123", "reason": "Bảo trì"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == f"/admin/bots/{BOT_ID}"
    assert operations.actions == [("pause", BOT_ID, "Bảo trì")]


def test_config_draft_form_rejects_missing_csrf_before_mutation() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()

    response = client.post(
        f"/admin/bots/{BOT_ID}/config-drafts",
        data={"overlay_json": '{"schema_version": 1}'},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_validate_config_draft_form_requires_csrf() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    draft_id = UUID("4806de3a-bd5c-4b7e-8d25-e90ef844c5d8")

    response = client.post(
        f"/admin/bots/{BOT_ID}/config-drafts/{draft_id}/validate",
        data={"csrf_token": ""},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_telegram_page_hides_user_id_and_renders_csrf_form() -> None:
    client = _client()
    client.app.state.telegram_binding_service = TelegramBindingsStub()
    client.app.state.auth_service = AuthServiceStub()

    response = client.get("/admin/telegram")

    assert response.status_code == 200
    assert str(MEMBERSHIP) in response.text
    assert "Telegram user ID" not in response.text
    assert 'name="csrf_token"' in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_telegram_link_form_checks_csrf_and_returns_single_use_deep_link() -> None:
    client = _client()
    service = TelegramBindingsStub()
    client.app.state.telegram_binding_service = service
    client.app.state.auth_service = AuthServiceStub()

    denied = client.post("/admin/telegram/link", data={"reason": "liên kết"})
    response = client.post(
        "/admin/telegram/link",
        data={"csrf_token": "csrf-token-test-123", "reason": "liên kết thiết bị"},
    )

    assert denied.status_code == 403
    assert response.status_code == 200
    assert "https://t.me/crypidian_test_bot?start=" + "a" * 43 in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert service.actions == [("link", MEMBERSHIP, "liên kết thiết bị")]


def test_telegram_revoke_form_checks_csrf_before_service() -> None:
    client = _client()
    service = TelegramBindingsStub()
    client.app.state.telegram_binding_service = service
    client.app.state.auth_service = AuthServiceStub()

    denied = client.post(f"/admin/telegram/revoke/{MEMBERSHIP}", data={"reason": "x"})
    response = client.post(
        f"/admin/telegram/revoke/{MEMBERSHIP}",
        data={"csrf_token": "csrf-token-test-123", "reason": "thu hồi"},
        follow_redirects=False,
    )

    assert denied.status_code == 403
    assert response.status_code == 303
    assert response.headers["Location"] == "/admin/telegram"
    assert service.actions == [("revoke", WORKSPACE, MEMBERSHIP, "thu hồi")]
