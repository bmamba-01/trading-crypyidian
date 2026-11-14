"""MB-07b: trang bot phải dùng service đã kiểm quyền và autoescape template."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.admin.web_router import _dashboard_performance_chart
from trading_bot.app import create_app
from trading_bot.application.telegram_signals import TelegramSignalFilters
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


def test_bot_detail_shows_telegram_signal_only_opt_in_state() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(
        get_state=lambda _principal, *, bot_id: _resolved(
            SimpleNamespace(
                bot_id=bot_id,
                enabled=True,
                binding_linked=True,
                filters=TelegramSignalFilters(symbols=("BTCUSDT",), timeframes=("1h",)),
            )
        )
    )

    response = client.get(f"/admin/bots/{BOT_ID}")

    assert response.status_code == 200, response.text
    assert "Nhận tín hiệu qua Telegram" in response.text
    assert "Đang nhận thông báo tín hiệu cho bot này." in response.text
    assert "không tạo giao dịch PAPER và không gửi lệnh lên sàn" in response.text
    assert 'value="false"' in response.text
    assert 'name="symbols"' in response.text
    assert "BTCUSDT" in response.text


def test_telegram_signal_filter_form_saves_allowlists_with_csrf() -> None:
    client = _client()
    auth = AuthServiceStub()
    saved: list[tuple[Principal, UUID, TelegramSignalFilters]] = []

    async def set_filters(
        principal: Principal, *, bot_id: UUID, filters: TelegramSignalFilters
    ) -> object:
        saved.append((principal, bot_id, filters))
        return object()

    client.app.state.auth_service = auth
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(set_filters=set_filters)

    response = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signal-filters",
        data={
            "csrf_token": "csrf-token-test-123",
            "symbols": "BTCUSDT, ETHUSDT",
            "timeframes": "1h\n15m",
            "directions": "long",
            "strategy_ids": "H01",
            "setup_codes": "H01_BREAKOUT",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == f"/admin/bots/{BOT_ID}"
    assert len(saved) == 1
    assert saved[0][0].workspace_id == WORKSPACE
    assert saved[0][1:] == (
        BOT_ID,
        TelegramSignalFilters(
            symbols=("BTCUSDT", "ETHUSDT"),
            timeframes=("1h", "15m"),
            directions=("LONG",),
            strategy_ids=("H01",),
            setup_codes=("H01_BREAKOUT",),
        ),
    )
    assert auth.verified_tokens == ["csrf-token-test-123"]


def test_telegram_signal_filter_form_rejects_invalid_values_before_saving() -> None:
    client = _client()
    auth = AuthServiceStub()
    calls: list[object] = []
    client.app.state.auth_service = auth
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(
        set_filters=lambda *_args, **_kwargs: calls.append("saved")
    )

    response = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signal-filters",
        data={"csrf_token": "csrf-token-test-123", "symbols": "BTC-USDT"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == f"/admin/bots/{BOT_ID}?signal_filter_error=invalid"
    assert calls == []


def test_telegram_signal_filter_form_has_a_bounded_request_size() -> None:
    client = _client()
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(
        set_filters=lambda *_args, **_kwargs: None
    )

    response = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signal-filters",
        data={"csrf_token": "csrf-token-test-123", "symbols": "A" * 70_000},
        follow_redirects=False,
    )

    assert response.status_code == 413


def test_bot_detail_links_telegram_binding_when_not_linked() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(
        get_state=lambda _principal, *, bot_id: _resolved(
            SimpleNamespace(bot_id=bot_id, enabled=False, binding_linked=False)
        )
    )

    response = client.get(f"/admin/bots/{BOT_ID}")

    assert response.status_code == 200, response.text
    assert 'href="/admin/telegram"' in response.text
    signal_section = response.text.split("Nhận tín hiệu qua Telegram", 1)[1].split(
        "Cấu hình chiến lược", 1
    )[0]
    assert 'action="/admin/bots/' not in signal_section


def test_telegram_signal_form_requires_csrf_and_updates_subscription() -> None:
    client = _client()
    auth = AuthServiceStub()
    calls: list[tuple[Principal, UUID, bool]] = []

    async def set_enabled(principal: Principal, *, bot_id: UUID, enabled: bool) -> object:
        calls.append((principal, bot_id, enabled))
        return object()

    client.app.state.auth_service = auth
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(set_enabled=set_enabled)

    response = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signals",
        data={"csrf_token": "csrf-token-test-123", "enabled": "true"},
        follow_redirects=False,
    )
    rejected = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signals",
        data={"csrf_token": "bad-token", "enabled": "false"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == f"/admin/bots/{BOT_ID}"
    assert rejected.status_code == 403
    assert auth.verified_tokens == ["csrf-token-test-123", "bad-token"]
    assert len(calls) == 1
    assert calls[0][1:] == (BOT_ID, True)


async def _resolved(value: object) -> object:
    return value


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


def test_admin_report_renders_chart_annotations_and_accessible_fill_table() -> None:
    client = _client()
    trade_id = "cb8cd4ad-1229-4d94-8d0b-b82ca0a7cd07"

    class ChartReportStub:
        async def get_bot_report(self, _query: object, _principal: Principal) -> object:
            trade = {
                "trade_id": trade_id,
                "bot_id": str(BOT_ID),
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quote_asset": "USDT",
                "opened_at": "2026-11-01T10:00:00+00:00",
                "closed_at": "2026-11-01T11:00:00+00:00",
                "net_pnl": "2.5",
                "fee_total": "0.2",
                "funding_total": "0",
                "slippage_total": "0.1",
                "exit_reason": "STOP_LOSS",
                "entry_reason": {
                    "strategy_id": "H03",
                    "detail_vi": "Nến đóng cửa vượt đỉnh kênh.",
                },
                "fills": [
                    {
                        "role": "ENTRY",
                        "occurred_at": "2026-11-01T10:00:00+00:00",
                        "price": "100.25",
                        "quantity": "0.2",
                        "bar_key": "entry-bar",
                    },
                    {
                        "role": "EXIT",
                        "occurred_at": "2026-11-01T11:00:00+00:00",
                        "price": "110.25",
                        "quantity": "0.2",
                        "bar_key": "exit-bar",
                    },
                ],
            }
            self.trade = trade
            return type(
                "Report",
                (),
                {
                    "as_dict": lambda _self: {
                        "scope": "bot",
                        "window": {
                            "start": "2026-11-01T00:00:00+00:00",
                            "end": "2026-11-02T00:00:00+00:00",
                        },
                        "totals": {
                            "USDT": {
                                "win_rate_percent": "60",
                                "profit_factor": None,
                                "expectancy_per_trade": "0.25",
                                "average_winning_trade": "2",
                                "average_losing_trade": "-1",
                                "max_consecutive_wins": 3,
                                "max_consecutive_losses": 2,
                                "average_holding_seconds": 3600,
                            }
                        },
                        "trades": [trade],
                    }
                },
            )()

        async def get_trade_plan_summaries(
            self, _query: object, _principal: object, *, trade_ids: object
        ) -> dict[object, dict[str, object]]:
            return {}

        async def get_trade_chart(
            self,
            _query: object,
            _principal: Principal,
            *,
            trade_id: UUID,
            timeframe: str,
        ) -> dict[str, object]:
            assert str(trade_id) == "cb8cd4ad-1229-4d94-8d0b-b82ca0a7cd07"
            return {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "timeframe": timeframe,
                "trade": self.trade,
                "bars": [
                    {
                        "time": 1793527200,
                        "open": 100,
                        "high": 111,
                        "low": 99,
                        "close": 110,
                        "volume": 20,
                    }
                ],
                "notice": None,
            }

        async def get_workspace_report(self, _query: object, _principal: Principal) -> object:
            raise AssertionError("workspace report was not requested")

    client.app.state.bot_reporting_service = ChartReportStub()
    response = client.get(
        "/admin/reports",
        params={
            "scope": "bot",
            "bot_id": str(BOT_ID),
            "start": "2026-11-01T00:00:00Z",
            "end": "2026-11-02T00:00:00Z",
            "chart_trade_id": trade_id,
            "chart_timeframe": "1h",
        },
    )

    assert response.status_code == 200, response.text
    assert "Vì sao vào lệnh?" in response.text
    assert "Nến đóng cửa vượt đỉnh kênh." in response.text
    assert "Dừng lỗ" in response.text
    assert "100.25" in response.text
    assert "110.25" in response.text
    assert "table-responsive" in response.text
    assert "Tỷ lệ giao dịch có lãi (%)" in response.text
    assert "Hệ số lợi nhuận" in response.text
    assert "Không xác định" in response.text
    assert "0,25 USDT" in response.text
    assert "Thời gian giữ lệnh trung bình (giây)" in response.text
    assert "trade-chart-data" in response.text
    assert 'id="trade-chart-event-detail"' in response.text
    assert "/static/vendor/lightweight-charts-5.2.1.standalone.production.js" in response.text


def test_dashboard_performance_chart_marks_entries_and_exits_without_mixing_assets() -> None:
    chart = _dashboard_performance_chart(
        [
            {
                "name": "Bot BTC",
                "trades": [
                    {
                        "trade_id": "a",
                        "quote_asset": "USDT",
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "opened_at": "2026-11-01T10:00:00+00:00",
                        "closed_at": "2026-11-01T11:00:00+00:00",
                        "net_pnl": "12.5",
                        "exit_reason": "TAKE_PROFIT",
                        "entry_reason": {
                            "strategy_id": "h03-v1",
                            "reason_code": "CHANNEL_BREAKOUT_CONFIRMED",
                            "detail_vi": "Nến đóng cửa vượt đỉnh kênh.",
                        },
                    },
                    {
                        "trade_id": "b",
                        "quote_asset": "USDC",
                        "symbol": "ETHUSDC",
                        "side": "SHORT",
                        "opened_at": "2026-11-01T10:30:00+00:00",
                        "closed_at": "2026-11-01T11:30:00+00:00",
                        "net_pnl": "1000",
                        "exit_reason": "STOP_LOSS",
                    },
                ],
            }
        ]
    )

    assert chart["quote_asset"] == "USDT"
    assert chart["series"][0]["data"] == [
        {"time": 1793527200, "value": 0.0},
        {"time": 1793530800, "value": 12.5},
    ]
    markers = chart["series"][0]["markers"]
    assert len(markers) == 2
    assert markers[0]["text"] == "Vào BTCUSDT"
    assert "chiến thuật h03-v1" in markers[0]["title"]
    assert "Nến đóng cửa vượt đỉnh kênh." in markers[0]["title"]
    assert markers[0]["color"] == "#0ECB81"
    assert markers[1]["color"] == "#0ECB81"
    assert "PnL ròng 12.5 USDT" in markers[1]["title"]
    assert "lúc 2026-11-01T11:00:00+00:00" in markers[1]["title"]
    assert "Chốt lời" in markers[1]["title"]
    assert chart["events"][0]["strategy_id"] == "h03-v1"
    assert chart["events"][0]["entry_detail_vi"] == "Nến đóng cửa vượt đỉnh kênh."
    assert chart["events"][0]["exit_reason_vi"] == "Chốt lời"
    assert chart["events"][0]["closed_at"] == "2026-11-01T11:00:00+00:00"


def test_dashboard_performance_exit_marker_uses_loss_color_for_negative_pnl() -> None:
    chart = _dashboard_performance_chart(
        [
            {
                "name": "Bot BTC",
                "trades": [
                    {
                        "trade_id": "loss",
                        "quote_asset": "USDT",
                        "symbol": "BTCUSDT",
                        "side": "SHORT",
                        "opened_at": "2026-11-01T10:00:00+00:00",
                        "closed_at": "2026-11-01T11:00:00+00:00",
                        "net_pnl": "-4.5",
                        "exit_reason": "STOP_LOSS",
                    }
                ],
            }
        ]
    )

    markers = chart["series"][0]["markers"]
    assert markers[0]["color"] == "#F6465D"
    assert markers[1]["color"] == "#F6465D"


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
    assert "Xác nhận đóng toàn bộ" in response.text
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


class SnapshotReportingStub:
    """Dữ liệu báo cáo chỉ đọc; các trường nội bộ phải bị loại ở biên HTTP."""

    def __init__(self) -> None:
        self.payload: dict[str, object] = {
            "scope": "bot",
            "mode": "PAPER",
            "bot_id": str(BOT_ID),
            "workspace_id": str(WORKSPACE),
            "account_id": "account-private",
            "totals": {
                "USDT": {"net_pnl": "12.345", "closed_trade_count": 12, "owner_id": "private"},
                "USDC": {"net_pnl": "-4.5", "closed_trade_count": 1},
            },
            "open_trades": [
                {
                    "trade_id": "open-trade",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "quote_asset": "USDT",
                    "opened_at": "2026-09-01T00:00:00+00:00",
                    "owner_id": "private",
                    "lineage": {"api_key": "secret-marker"},
                }
            ],
            "trades": [
                {
                    "trade_id": f"closed-{day}",
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "quote_asset": "USDT",
                    "opened_at": "2026-11-01T00:00:00+00:00",
                    "closed_at": f"2026-11-02T{day:02}:00:00+00:00",
                    "net_pnl": "1.2345",
                    "owner_id": "private",
                    "account_id": "account-private",
                    "lineage": {"secret": "secret-marker"},
                }
                for day in range(12)
            ],
            "lineage": {"secret": "secret-marker"},
        }

    async def get_bot_report(self, query: object, principal: Principal) -> object:
        from trading_bot.application.bot_reporting import BotReportQuery, ReportScope

        assert isinstance(query, BotReportQuery)
        assert query.bot_id == BOT_ID
        assert query.workspace_id == principal.workspace_id == WORKSPACE
        assert query.scope is ReportScope.BOT
        assert query.mode == "PAPER"
        assert query.end - query.start == timedelta(days=30)
        assert query.start.utcoffset() == query.end.utcoffset() == timedelta(0)
        assert datetime.now(UTC) - query.end < timedelta(seconds=5)
        return SimpleNamespace(as_dict=lambda: self.payload)


def test_dashboard_snapshot_returns_one_bot_with_utc_time_and_safe_paper_trades() -> None:
    client = _client()
    client.app.state.bot_reporting_service = SnapshotReportingStub()

    async def status(bot_id: UUID) -> dict[str, object]:
        assert bot_id == BOT_ID
        return {
            "id": str(BOT_ID),
            "name": "Bot PAPER",
            "status": "ENABLED",
            "entry_state": "ENABLED",
            "readiness": "READY",
            "readiness_reason_code": None,
            "entry_allowed": True,
            "control_revision": 4,
            "workspace_kill_state": "ACTIVE",
            "workspace_control_revision": 1,
            "owner_id": "private",
            "api_secret": "secret-marker",
        }

    client.app.state.bot_operations_service.bot_status = status
    before = datetime.now(UTC)
    response = client.get(
        f"/admin/dashboard/bots/{BOT_ID}/snapshot",
        params={"workspace_id": "untrusted-workspace", "scope": "workspace"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    observed_at = datetime.fromisoformat(payload["observed_at"])
    assert before <= observed_at <= datetime.now(UTC)
    assert observed_at.utcoffset() == timedelta(0)
    assert payload["source"] == "PAPER_LEDGER"
    assert payload["bot"] == {"id": str(BOT_ID), "name": "Bot PAPER", "status": "ENABLED"}
    assert payload["runtime"] == {
        "entry_state": "ENABLED",
        "control_revision": 4,
        "readiness": "READY",
        "readiness_reason_code": None,
        "workspace_kill_state": "ACTIVE",
        "workspace_control_revision": 1,
        "entry_allowed": True,
    }
    assert payload["totals"] == {
        "USDT": {"net_pnl": "12.345", "closed_trade_count": 12},
        "USDC": {"net_pnl": "-4.5", "closed_trade_count": 1},
    }
    assert payload["open_trades"] == [
        {
            "trade_id": "open-trade",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quote_asset": "USDT",
            "opened_at": "2026-09-01T00:00:00+00:00",
        }
    ]
    assert len(payload["recent_closed_trades"]) == 10
    assert [trade["trade_id"] for trade in payload["recent_closed_trades"]] == [
        f"closed-{day}" for day in range(11, 1, -1)
    ]
    assert payload["recent_closed_trades"][0]["net_pnl"] == "1.2345"
    assert datetime.fromisoformat(payload["window"]["end"]) - datetime.fromisoformat(
        payload["window"]["start"]
    ) == timedelta(days=30)
    for private in ("secret-marker", "private", "owner_id", "account_id", "lineage", "api_secret"):
        assert private not in response.text


def test_dashboard_snapshot_conceals_runtime_and_report_permission_denials() -> None:
    from trading_bot.application.bot_reporting import BotReportError

    client = _client()
    client.app.state.bot_reporting_service = SnapshotReportingStub()
    missing = client.get("/admin/dashboard/bots/889ef59a-210a-4d79-9e4f-4734711a9ac0/snapshot")

    class DeniedReporting:
        async def get_bot_report(self, _query: object, _principal: object) -> object:
            raise BotReportError(403, "REPORT_NOT_FOUND", "secret-marker")

    client.app.state.bot_reporting_service = DeniedReporting()
    denied = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert missing.status_code == denied.status_code == 404
    assert missing.json() == denied.json()
    assert missing.headers["Cache-Control"] == denied.headers["Cache-Control"] == "no-store"
    assert "secret-marker" not in denied.text
    assert str(BOT_ID) not in denied.text


def test_dashboard_snapshot_fails_closed_without_reporting_service() -> None:
    client = _client()
    response = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert "open_trades" not in response.json()


def test_dashboard_snapshot_requires_identity_and_returns_safe_json_errors() -> None:
    from fastapi import HTTPException

    client = _client()

    def expired_identity() -> None:
        raise HTTPException(401, "secret-marker")

    client.app.dependency_overrides[current_identity] = expired_identity
    response = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert "secret-marker" not in response.text


def test_dashboard_snapshot_reports_empty_ledger_without_invented_metrics() -> None:
    client = _client()
    reporting = SnapshotReportingStub()
    reporting.payload.update(totals={}, open_trades=[], trades=[])
    client.app.state.bot_reporting_service = reporting
    response = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"] == {}
    assert payload["open_trades"] == payload["recent_closed_trades"] == []
    assert "unrealized_pnl" not in payload
    assert "heartbeat" not in payload["runtime"]


def test_dashboard_snapshot_conceals_report_integrity_error() -> None:
    from trading_bot.application.bot_reporting import BotReportError

    client = _client()

    class BrokenReporting:
        async def get_bot_report(self, _query: object, _principal: object) -> object:
            raise BotReportError(409, "LEDGER_RECONCILIATION_FAILED", "secret-marker")

    client.app.state.bot_reporting_service = BrokenReporting()
    response = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Dịch vụ tạm thời chưa sẵn sàng. Hãy thử lại sau."}


def test_dashboard_snapshot_returns_json_for_invalid_bot_id() -> None:
    response = _client().get("/admin/dashboard/bots/not-a-uuid/snapshot")

    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert "not-a-uuid" not in response.text


def test_dashboard_snapshot_conceals_runtime_unavailability() -> None:
    from trading_bot.application.bot_runtime_control import BotRuntimeControlError

    client = _client()

    async def unavailable(_bot_id: UUID) -> object:
        raise BotRuntimeControlError("RUNTIME_CONTROL_UNAVAILABLE")

    client.app.state.bot_operations_service.bot_status = unavailable
    response = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Dịch vụ tạm thời chưa sẵn sàng. Hãy thử lại sau."}


def test_dashboard_snapshot_fails_closed_on_reporting_database_failure() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    client = _client()

    class UnavailableReporting:
        async def get_bot_report(self, _query: object, _principal: object) -> object:
            raise SQLAlchemyError("secret-marker")

    client.app.state.bot_reporting_service = UnavailableReporting()
    safe_client = TestClient(client.app, raise_server_exceptions=False)
    response = safe_client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert "secret-marker" not in response.text


def _snapshot_with_seed(seed_reader: object) -> dict[str, object]:
    client = _client()
    reporting = SnapshotReportingStub()
    reporting.get_account_seed = seed_reader  # type: ignore[attr-defined]
    client.app.state.bot_reporting_service = reporting

    async def status(bot_id: UUID) -> dict[str, object]:
        return {"id": str(bot_id), "name": "Bot PAPER", "status": "ENABLED"}

    client.app.state.bot_operations_service.bot_status = status
    response = client.get(f"/admin/dashboard/bots/{BOT_ID}/snapshot")
    assert response.status_code == 200, response.text
    payload: dict[str, object] = response.json()
    return payload


def test_dashboard_snapshot_includes_only_quote_and_initial_equity_of_the_paper_seed() -> None:
    async def seed(query: object, principal: Principal) -> dict[str, object]:
        from trading_bot.application.bot_reporting import BotReportQuery, ReportScope

        assert isinstance(query, BotReportQuery)
        assert query.bot_id == BOT_ID
        assert query.scope is ReportScope.BOT
        assert principal.workspace_id == WORKSPACE
        return {"quote_asset": "USDT", "initial_equity": "10000", "created_at": "private-time"}

    payload = _snapshot_with_seed(seed)

    assert payload["account"] == {"quote_asset": "USDT", "initial_equity": "10000"}


def test_dashboard_snapshot_omits_capital_when_seed_is_missing_or_denied() -> None:
    from trading_bot.application.bot_reporting import BotReportError

    async def missing(query: object, principal: Principal) -> None:
        return None

    async def denied(query: object, principal: Principal) -> None:
        raise BotReportError(404, "BOT_NOT_FOUND", "Không tìm thấy.")

    assert _snapshot_with_seed(missing)["account"] is None
    assert _snapshot_with_seed(denied)["account"] is None
