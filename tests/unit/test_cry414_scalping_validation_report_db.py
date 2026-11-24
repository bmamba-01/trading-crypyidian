"""Unit test cho CRY-414: lưu báo cáo kiểm định SIG-5 vào DB, xác minh hash & API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.admin.backtest_replay_router import api_router as scalping_validation_api_router
from trading_bot.application.scalping_validation_reports import (
    ScalpingValidationReportError,
    ScalpingValidationReportService,
    verify_validation_report_hash,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings


def _sample_report() -> dict[str, Any]:
    body: dict[str, Any] = {
        "costs_version": "sig5-costs-v1",
        "release_id": "h18-v1",
        "release_content_hash": "a" * 64,
        "dataset_hashes": ["b" * 64],
        "symbol": "BTCUSDT",
        "evaluation_start": "2026-11-01T00:00:00+00:00",
        "range_end": "2026-11-15T00:00:00+00:00",
        "scenarios": {"base": {"fee_rate": "0.0002", "slippage_rate": "0.0001"}},
        "reports": {
            "base": {"out_of_sample_n": 120, "expectancy_r": "0.25"},
            "stress-slippage-x2-taker": {"out_of_sample_n": 120, "expectancy_r": "0.14"},
        },
        "verdict": {"recommend_paper": True, "reasons": []},
    }
    body["validation_hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _principal(role: Role, *, ws_id: UUID | None = None) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=ws_id or uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC),
    )


def test_verify_validation_report_hash_rejects_tampered_or_incomplete() -> None:
    """Hash hợp lệ đi qua; thiếu trường hoặc sửa payload bị từ chối fail-closed."""
    report = _sample_report()
    verify_validation_report_hash(report)

    tampered = dict(report)
    tampered["symbol"] = "ETHUSDT"
    with pytest.raises(ScalpingValidationReportError) as exc_hash:
        verify_validation_report_hash(tampered)
    assert exc_hash.value.code == "VALIDATION_HASH_MISMATCH"

    incomplete = dict(report)
    del incomplete["verdict"]
    with pytest.raises(ScalpingValidationReportError) as exc_inc:
        verify_validation_report_hash(incomplete)
    assert exc_inc.value.code == "VALIDATION_REPORT_INCOMPLETE"


@pytest.mark.asyncio
async def test_validation_report_service_rbac_idempotency_and_http_api() -> None:
    """Kiểm tra phân quyền STRATEGY_WRITE/WORKSPACE_READ, idempotency và HTTP API."""
    ws_id = uuid4()
    operator = _principal(Role.OPERATOR, ws_id=ws_id)
    strategy_admin = _principal(Role.STRATEGY_ADMIN, ws_id=ws_id)
    report = _sample_report()
    store: dict[str, dict[str, Any]] = {}

    class _FakeRepo:
        async def save_report(
            self,
            *,
            workspace_id: UUID,
            report_body: dict[str, Any],
            actor_user_id: UUID | None,
            now: datetime,
        ) -> dict[str, Any]:
            key = f"{workspace_id}:{report_body['validation_hash']}"
            if key in store:
                return dict(store[key])
            rec = {
                "id": str(uuid4()),
                "workspace_id": str(workspace_id),
                "release_id": report_body["release_id"],
                "release_content_hash": report_body["release_content_hash"],
                "symbol": report_body["symbol"],
                "costs_version": report_body["costs_version"],
                "evaluation_start": report_body["evaluation_start"],
                "range_end": report_body["range_end"],
                "recommend_paper": bool(report_body["verdict"]["recommend_paper"]),
                "out_of_sample_n": int(
                    report_body["reports"]["stress-slippage-x2-taker"]["out_of_sample_n"]
                ),
                "stress_expectancy_r": str(
                    report_body["reports"]["stress-slippage-x2-taker"]["expectancy_r"]
                ),
                "validation_hash": report_body["validation_hash"],
                "report_payload": report_body,
                "created_by_user_id": str(actor_user_id) if actor_user_id else None,
                "created_at": now.isoformat(),
            }
            store[key] = rec
            return dict(rec)

        async def list_reports(self, *, workspace_id: UUID, **_kwargs: Any) -> list[dict[str, Any]]:
            return [dict(r) for r in store.values() if r["workspace_id"] == str(workspace_id)]

    class _FakeUow:
        session = object()

        async def __aenter__(self) -> _FakeUow:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    service = ScalpingValidationReportService(
        sessions=object(),  # type: ignore[arg-type]
        unit_of_work=lambda *_args, **_kwargs: _FakeUow(),
        repository=lambda _s: _FakeRepo(),
    )

    with pytest.raises(ScalpingValidationReportError) as exc_op:
        await service.save_report(operator, report_body=report)
    assert exc_op.value.status_code == 403

    first = await service.save_report(strategy_admin, report_body=report)
    second = await service.save_report(strategy_admin, report_body=report)
    assert first["id"] == second["id"]

    class _FakeAuthService:
        async def verify_csrf(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    app = FastAPI()
    app.state.settings = AppSettings(_env_file=None)  # type: ignore[call-arg]
    app.state.auth_service = _FakeAuthService()
    app.state.scalping_validation_report_service = service
    app.include_router(scalping_validation_api_router)
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=strategy_admin,
        email="strategy@crypidian.io",
    )
    client = TestClient(app)
    post_resp = client.post(
        "/api/v1/backtests/scalping-validations",
        headers={"X-CSRF-Token": "test-csrf"},
        json={"report": report},
    )
    assert post_resp.status_code == 201
    assert post_resp.json()["report"]["recommend_paper"] is True

    get_resp = client.get("/api/v1/backtests/scalping-validations")
    assert get_resp.status_code == 200
    assert len(get_resp.json()["reports"]) == 1
