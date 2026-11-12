"""Unit test cho Real-time API Endpoint GET /admin/api/dashboard/realtime (WP-04)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT1_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
BOT2_ID = UUID("88228dd5-7d8e-518f-9a34-059e851b57d3")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


class MockBotAdminService:
    """Stub service quản trị bot."""

    def __init__(self) -> None:
        self.bots = [
            {
                "id": str(BOT1_ID),
                "name": "Binance BTC Breakout H03",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            },
            {
                "id": str(BOT2_ID),
                "name": "Binance ETH Trend H01",
                "status": "PAUSED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            },
        ]

    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        assert principal.workspace_id == WORKSPACE
        return self.bots

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        assert principal.workspace_id == WORKSPACE
        return next((b for b in self.bots if b["id"] == str(bot_id)), self.bots[0])


class MockOperationsService:
    """Stub service thao tác runtime của bot và workspace."""

    def __init__(self) -> None:
        self.kill_state = "ACTIVE"
        self.revision = 3
        self.runtimes = [
            {
                "id": str(BOT1_ID),
                "name": "Binance BTC Breakout H03",
                "status": "ENABLED",
                "entry_state": "ENABLED",
                "readiness": "READY",
                "entry_allowed": True,
                "control_revision": 5,
            },
            {
                "id": str(BOT2_ID),
                "name": "Binance ETH Trend H01",
                "status": "PAUSED",
                "entry_state": "PAUSED",
                "readiness": "DEGRADED",
                "entry_allowed": False,
                "control_revision": 2,
            },
        ]

    def for_principal(self, principal: Principal) -> MockOperationsService:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return self.runtimes

    async def bot_status(self, bot_id: UUID) -> dict[str, Any]:
        return next((r for r in self.runtimes if r["id"] == str(bot_id)), self.runtimes[0])

    async def workspace_status(self) -> dict[str, Any]:
        return {
            "kill_state": self.kill_state,
            "revision": self.revision,
            "account_mode": "PAPER",
            "bots": self.runtimes,
        }


class MockReportingTotals:
    """Đối tượng giả lập ReportTotals."""

    def __init__(self, closed: int, won: int, net_pnl: Decimal) -> None:
        self.closed_trade_count = closed
        self.winning_trade_count = won
        self.net_pnl = net_pnl


class MockReportingReport:
    """Đối tượng giả lập WorkspaceAggregateReport."""

    def __init__(self, totals_dict: dict[str, MockReportingTotals]) -> None:
        self.totals = totals_dict


class MockReportingService:
    """Stub service báo cáo tổng hợp."""

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.called_query: Any = None

    async def get_workspace_report(self, query: Any, principal: Principal) -> Any:
        assert principal.workspace_id == WORKSPACE
        self.called_query = query
        if self.should_fail:
            from trading_bot.application.bot_reporting import BotReportError

            raise BotReportError(500, "LEDGER_READ_FAILED", "Không đọc được sổ cái")
        return MockReportingReport(
            {
                "USDT": MockReportingTotals(
                    closed=10, won=7, net_pnl=Decimal("152.500000000000000000")
                )
            }
        )


def _create_authenticated_client(
    admin_service: MockBotAdminService | None = None,
    ops_service: MockOperationsService | None = None,
    reporting_service: MockReportingService | None = None,
) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        user_id=USER_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@crypidian.test"
    )
    app.state.bot_admin_service = admin_service or MockBotAdminService()
    app.state.bot_operations_service = ops_service or MockOperationsService()
    if reporting_service is not None:
        app.state.bot_reporting_service = reporting_service
    return TestClient(app)


class MockUnauthenticatedAuthService:
    """Stub service trả về 401 khi chưa xác thực."""

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        from trading_bot.auth.service import AuthenticationFailure

        raise AuthenticationFailure(401, "Chưa đăng nhập hoặc phiên đã hết hạn.")


def test_realtime_api_requires_authentication() -> None:
    """Endpoint từ chối truy cập nếu không có phiên đăng nhập hợp lệ."""
    app = create_app(AppSettings(app_env="test"))
    app.state.auth_service = MockUnauthenticatedAuthService()
    client = TestClient(app)
    response = client.get("/admin/api/dashboard/realtime")
    # Chưa đăng nhập -> 401 Unauthorized
    assert response.status_code == 401


def test_realtime_api_returns_expected_structure_and_headers() -> None:
    """Endpoint trả về đúng cấu trúc JSON, no-store cache header và tính toán KPI."""
    client = _create_authenticated_client()
    response = client.get("/admin/api/dashboard/realtime")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"

    data = response.json()
    assert "server_time" in data
    assert "iso" in data["server_time"]
    assert "vn" in data["server_time"]
    assert "vn_time" in data["server_time"]

    assert data["bot_count"] == 2
    assert data["ready_count"] == 1  # 1 bot READY, 1 bot DEGRADED

    workspace = data["workspace"]
    assert workspace["kill_state"] == "ACTIVE"
    assert workspace["revision"] == 3

    bots = data["bots"]
    assert len(bots) == 2

    bot1 = next(b for b in bots if b["id"] == str(BOT1_ID))
    assert bot1["name"] == "Binance BTC Breakout H03"
    assert bot1["status"] == "ENABLED"
    assert bot1["account_mode"] == "PAPER"
    assert bot1["readiness"] == "READY"
    assert bot1["entry_allowed"] is True
    assert bot1["control_revision"] == 5

    bot2 = next(b for b in bots if b["id"] == str(BOT2_ID))
    assert bot2["name"] == "Binance ETH Trend H01"
    assert bot2["status"] == "PAUSED"
    assert bot2["readiness"] == "DEGRADED"
    assert bot2["entry_allowed"] is False

    assert data["metrics"] == {}


def test_realtime_api_with_reporting_metrics() -> None:
    """Endpoint trích xuất thành công KPI hiệu suất khi reporting service khả dụng."""
    reporting_svc = MockReportingService(should_fail=False)
    client = _create_authenticated_client(reporting_service=reporting_svc)

    response = client.get("/admin/api/dashboard/realtime")
    assert response.status_code == 200

    data = response.json()
    metrics = data["metrics"]
    assert metrics["total_closed_trades"] == 10
    assert metrics["net_pnl"] == "152.500000000000000000"
    assert metrics["win_rate"] == 70.0


def test_realtime_api_handles_reporting_service_failure_gracefully() -> None:
    """Endpoint không bị crash nếu service báo cáo ném ngoại lệ."""
    reporting_svc = MockReportingService(should_fail=True)
    client = _create_authenticated_client(reporting_service=reporting_svc)

    response = client.get("/admin/api/dashboard/realtime")
    assert response.status_code == 200

    data = response.json()
    assert data["metrics"] == {}
    assert data["bot_count"] == 2


def test_realtime_api_reflects_runtime_status_changes() -> None:
    """Endpoint phản ánh chính xác khi workspace bị KILLED."""
    ops = MockOperationsService()
    ops.kill_state = "KILLED"
    ops.revision = 4
    for r in ops.runtimes:
        r["entry_allowed"] = False

    client = _create_authenticated_client(ops_service=ops)
    response = client.get("/admin/api/dashboard/realtime")
    assert response.status_code == 200

    data = response.json()
    assert data["workspace"]["kill_state"] == "KILLED"
    assert data["workspace"]["revision"] == 4
    assert all(b["entry_allowed"] is False for b in data["bots"])


def test_realtime_api_delta_fingerprint_returns_unchanged() -> None:
    """Endpoint trả về status: unchanged khi client gửi đúng fingerprint hiện tại."""
    ops = MockOperationsService()
    client = _create_authenticated_client(ops_service=ops)

    # Lần 1: Chưa có fingerprint -> nhận full data kèm fingerprint
    resp1 = client.get("/admin/api/dashboard/realtime")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "updated"
    fp = data1.get("fingerprint")
    assert fp is not None

    # Lần 2: Gửi kèm fingerprint -> trả về status: unchanged
    resp2 = client.get(f"/admin/api/dashboard/realtime?fp={fp}")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "unchanged"
    assert data2["fingerprint"] == fp
    assert "bots" not in data2

    # Khi trạng thái thay đổi -> trả về updated với fingerprint mới
    ops.revision = 5
    resp3 = client.get(f"/admin/api/dashboard/realtime?fp={fp}")
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["status"] == "updated"
    assert data3["fingerprint"] != fp
    assert "bots" in data3


def test_bot_detail_realtime_api_delta_fingerprint() -> None:
    """Endpoint bot detail realtime trả về status: unchanged khi khớp fingerprint."""
    client = _create_authenticated_client()

    resp1 = client.get(f"/admin/api/bots/{BOT1_ID}/realtime")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "updated"
    fp = data1.get("fingerprint")
    assert fp is not None

    resp2 = client.get(f"/admin/api/bots/{BOT1_ID}/realtime?fp={fp}")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "unchanged"
    assert data2["fingerprint"] == fp
    assert "metrics" not in data2


def test_queue_ai_retraining_alias_endpoint() -> None:
    """Endpoint alias POST /admin/api/trades/{trade_id}/queue-ai-retraining hoạt động chuẩn xác."""
    from uuid import uuid4

    client = _create_authenticated_client()
    trade_id = uuid4()
    resp = client.post(
        f"/admin/api/trades/{trade_id}/queue-ai-retraining",
        json={
            "bot_id": str(BOT1_ID),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "reason": "Slippage quá cao",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "Đã tiếp nhận ca sai lệch" in data["message"]
    assert data["sample"]["trade_id"] == str(trade_id)
