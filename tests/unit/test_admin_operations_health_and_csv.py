"""Unit test cho /admin/api/operations/health (OPS07A) và /admin/reports/export.csv (WB-07D)."""

from __future__ import annotations

import csv
import io
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
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


class _StubBotAdminService:
    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return [
            {
                "id": str(BOT1_ID),
                "name": "BTC Trend H1",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            }
        ]


class _StubOperationsService:
    async def list_bots(self) -> list[dict[str, Any]]:
        return [
            {
                "id": str(BOT1_ID),
                "name": "BTC Trend H1",
                "status": "ENABLED",
                "readiness": "READY",
                "control_revision": 1,
                "error_count": 0,
            }
        ]

    async def workspace_status(self) -> dict[str, Any]:
        return {"kill_state": "ACTIVE", "revision": 2}


class _StubReportObj:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return self._payload


class _StubReportingService:
    async def get_bot_report(self, query: Any, principal: Principal) -> _StubReportObj:
        return _StubReportObj(
            {
                "scope": "bot",
                "trades": [
                    {
                        "id": "trade-001",
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "leverage": 5,
                        "entry_price": "90000",
                        "exit_price": "91000",
                        "net_pnl": "120.50",
                        "mfe_pct": "1.80",
                        "mae_pct": "-0.40",
                        "entry_reason": "=CMD|'/C calc'!A0",
                        "exit_reason": "TAKE_PROFIT",
                        "closed_at": datetime(2026, 11, 14, 9, 0, tzinfo=UTC).isoformat(),
                    }
                ],
            }
        )

    async def get_workspace_report(self, query: Any, principal: Principal) -> _StubReportObj:
        return await self.get_bot_report(query, principal)


class _StubOperationsRoot:
    def __init__(self, scoped: _StubOperationsService) -> None:
        self._scoped = scoped

    def for_principal(self, principal: Principal) -> _StubOperationsService:
        return self._scoped


def _build_client() -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        user_id=USER_ID,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    identity = AuthIdentity(
        principal=principal,
        email="owner@crypidian.io",
    )
    app.dependency_overrides[current_identity] = lambda: identity
    app.state.bot_admin_service = _StubBotAdminService()
    app.state.bot_operations_service = _StubOperationsRoot(_StubOperationsService())
    app.state.bot_reporting_service = _StubReportingService()
    return TestClient(app)


def test_operations_health_endpoint_returns_redacted_snapshot() -> None:
    """GET /admin/api/operations/health trả về JSON chuẩn với correlation_id và không lộ secret."""
    client = _build_client()
    resp = client.get("/admin/api/operations/health", headers={"X-Correlation-Id": "corr-ui-99"})
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["status"] == "HEALTHY"
    assert body["correlation_id"] == "corr-ui-99"
    assert body["breaker"]["state"] == "CLOSED"
    assert body["breaker"]["entry_allowed"] is True
    assert Decimal(body["loss_windows"]["realized_loss_today"]) >= Decimal("0")


def test_reports_csv_export_sanitizes_formula_injection_and_includes_mfe_mae() -> None:
    """GET /admin/reports/export.csv xuất CSV đầy đủ MFE/MAE và chống formula injection."""
    client = _build_client()
    resp = client.get(
        "/admin/reports/export.csv",
        params={
            "scope": "bot",
            "bot_id": str(BOT1_ID),
            "start": "2026-11-13T00:00:00Z",
            "end": "2026-11-14T23:59:59Z",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment;" in resp.headers.get("content-disposition", "")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["mfe_pct"] == "1.80"
    assert rows[0]["mae_pct"] == "-0.40"
    # Công thức nguy hiểm bắt đầu bằng '=' phải được prefix bằng dấu nháy đơn
    assert rows[0]["entry_reason"].startswith("'=CMD")
