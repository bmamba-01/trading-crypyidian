from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.admin.backtest_replay_router import router
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


class AuthServiceStub:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "csrf-token-test-123"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != "csrf-token-test-123":
            from trading_bot.auth.service import AuthenticationFailure

            raise AuthenticationFailure(403, "Thiếu mã CSRF")


class BacktestJobServiceStub:
    def __init__(self) -> None:
        self.submissions: list[tuple[Principal, object]] = []
        self.cancellations: list[tuple[Principal, UUID]] = []
        self.jobs = [
            SimpleNamespace(
                id=UUID("e647a8d0-775d-4b44-8cbe-6ff2a558ca57"),
                status="RUNNING",
                stage="replay",
                progress=45,
                bundle_hash="a" * 64,
                strategy_release_id="h01-v1",
                execution_interval="1h",
                created_at=datetime(2026, 11, 4, 3, tzinfo=UTC),
                updated_at=datetime(2026, 11, 4, 3, 1, tzinfo=UTC),
                error_code=None,
                result_run_ids=(),
                cancel_requested=False,
            )
        ]
        self.bundles = (
            SimpleNamespace(
                bundle_hash="a" * 64,
                symbol="BTCUSDT",
                evaluation_start=datetime(2026, 11, 3, tzinfo=UTC),
                range_end=datetime(2026, 11, 4, tzinfo=UTC),
                intervals=("1h", "4h"),
            ),
        )

    async def submit(self, principal: Principal, request: object) -> object:
        self.submissions.append((principal, request))
        return SimpleNamespace(id=UUID("f6ca67a9-b6d2-4428-a72c-4587f2320766"))

    async def list(self, principal: Principal) -> tuple[object, ...]:
        assert principal.workspace_id == WORKSPACE
        return tuple(self.jobs)

    async def list_bundles(self, principal: Principal) -> tuple[object, ...]:
        assert principal.workspace_id == WORKSPACE
        return self.bundles

    async def cancel(self, principal: Principal, job_id: UUID) -> object:
        self.cancellations.append((principal, job_id))
        return self.jobs[0]


def _client(*, roles: frozenset[Role] = frozenset({Role.OWNER})) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        roles=roles,
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    if not any(getattr(route, "path", None) == "/admin/backtests" for route in app.routes):
        app.include_router(router)
    app.state.auth_service = AuthServiceStub()
    app.state.backtest_replay_job_service = BacktestJobServiceStub()
    return TestClient(app)


def test_backtest_replay_page_renders_vietnamese_form_and_job_progress() -> None:
    response = _client().get("/admin/backtests")

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "Kiểm thử dữ liệu lịch sử (Backtest)" in response.text
    assert "Chạy kiểm thử" in response.text
    assert "Tiến độ" in response.text
    assert "45%" in response.text
    assert "04/11/26 10:00" in response.text
    assert 'name="csrf_token"' in response.text
    assert 'action="/admin/backtests/submit"' in response.text
    assert 'name="bundle_hash"' in response.text
    assert 'name="execution_interval"' in response.text
    assert 'name="learning_intent"' in response.text
    assert "Kết quả đang quarantine" not in response.text
    assert "intent được ghim trước replay" in response.text
    assert "BTCUSDT" in response.text
    assert "EMA theo xu hướng" in response.text


def test_backtest_replay_submission_checks_csrf_and_builds_typed_request() -> None:
    client = _client()
    service = client.app.state.backtest_replay_job_service
    data = {
        "csrf_token": "csrf-token-test-123",
        "bundle_hash": "a" * 64,
        "strategy_release_id": "h01-v1",
        "execution_interval": "1h",
        "starting_equity": "10000",
        "per_trade_risk_fraction": "0.01",
        "fee_rate": "0.001",
        "slippage_rate": "0.0005",
        "seed": "7",
        "learning_intent": "TRAINING_BASE",
    }

    response = client.post("/admin/backtests/submit", data=data, follow_redirects=False)
    rejected = client.post(
        "/admin/backtests/submit",
        data={**data, "csrf_token": "invalid"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/admin/backtests?submitted=1"
    assert rejected.status_code == 403
    assert len(service.submissions) == 1
    principal, request = service.submissions[0]
    assert principal.workspace_id == WORKSPACE
    assert request.bundle_hash == "a" * 64
    assert request.starting_equity == Decimal("10000")
    assert request.per_trade_risk_fraction == Decimal("0.01")
    assert request.fee_rate == Decimal("0.001")
    assert request.slippage_rate == Decimal("0.0005")
    assert request.seed == 7
    assert request.learning_intent == "TRAINING_BASE"


def test_backtest_replay_cancel_checks_csrf_and_routes_to_service() -> None:
    client = _client()
    service = client.app.state.backtest_replay_job_service
    job_id = UUID("e647a8d0-775d-4b44-8cbe-6ff2a558ca57")

    response = client.post(
        f"/admin/backtests/{job_id}/cancel",
        data={"csrf_token": "csrf-token-test-123"},
        follow_redirects=False,
    )
    rejected = client.post(
        f"/admin/backtests/{job_id}/cancel",
        data={"csrf_token": "invalid"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/admin/backtests"
    assert rejected.status_code == 403
    assert len(service.cancellations) == 1
    principal, cancelled_job_id = service.cancellations[0]
    assert principal.workspace_id == WORKSPACE
    assert principal.membership_id == MEMBERSHIP
    assert cancelled_job_id == job_id


@pytest.mark.parametrize(
    "values",
    [
        {"bundle_hash": "bad", "strategy_release_id": "h01-v1", "execution_interval": "1h"},
        {"bundle_hash": "a" * 64, "strategy_release_id": "unknown", "execution_interval": "1h"},
        {"bundle_hash": "a" * 64, "strategy_release_id": "h01-v1", "execution_interval": ""},
        {"learning_intent": "TRAINING_AFTER_REPLAY"},
    ],
)
def test_backtest_replay_submission_rejects_invalid_fields_before_service(
    values: dict[str, str],
) -> None:
    client = _client()
    service = client.app.state.backtest_replay_job_service
    data = {
        "csrf_token": "csrf-token-test-123",
        "bundle_hash": "a" * 64,
        "strategy_release_id": "h01-v1",
        "execution_interval": "1h",
        "starting_equity": "10000",
        "per_trade_risk_fraction": "0.01",
        "fee_rate": "0.001",
        "slippage_rate": "0.0005",
        "seed": "7",
        **values,
    }

    response = client.post("/admin/backtests/submit", data=data, follow_redirects=False)

    assert response.status_code in {303, 422}
    assert service.submissions == []


def test_backtest_replay_page_requires_owner_role() -> None:
    response = _client(roles=frozenset({Role.ANALYST_REVIEWER})).get("/admin/backtests")

    assert response.status_code == 403


def test_backtest_replay_page_shows_cancel_requested_state_without_cancel_button() -> None:
    client = _client()
    job = client.app.state.backtest_replay_job_service.jobs[0]
    job.cancel_requested = True

    response = client.get("/admin/backtests")

    assert response.status_code == 200
    assert "Đang yêu cầu hủy" in response.text
    assert '/cancel"' not in response.text


def test_backtest_replay_page_hides_cancel_once_result_persistence_starts() -> None:
    client = _client()
    client.app.state.backtest_replay_job_service.jobs[0].stage = "persist"

    response = client.get("/admin/backtests")

    assert response.status_code == 200
    assert "Đang lưu kết quả" in response.text
    assert '/cancel"' not in response.text


def test_backtest_replay_page_does_not_repeat_terminal_status_as_stage() -> None:
    client = _client()
    job = client.app.state.backtest_replay_job_service.jobs[0]
    job.status = "CANCELLED"
    job.stage = "cancelled"
    job.progress = 100

    response = client.get("/admin/backtests")

    assert response.status_code == 200
    assert "Đã hủy · Đã hủy" not in response.text


def test_learning_intent_replay_hides_run_ids_and_shows_quarantine_status() -> None:
    client = _client()
    job = client.app.state.backtest_replay_job_service.jobs[0]
    job.learning_intent = "TRAINING_BASE"
    job.result_run_ids = ("opaque-training-run-id",)
    job.status = "SUCCEEDED"
    job.stage = "complete"

    response = client.get("/admin/backtests")

    assert response.status_code == 200
    assert "Tạo dữ liệu học (BASE) · quarantine" in response.text
    assert "Kết quả đang quarantine" in response.text
    assert "opaque-training-run-id" not in response.text
