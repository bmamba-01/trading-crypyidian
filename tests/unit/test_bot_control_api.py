"""MB07a.2: Admin API điều khiển bot — CSRF, ma trận RBAC (ruling 30), MFA, audit, lỗi không rò rỉ.

Router + `BotOperationsService` + `BotRuntimeControlService` là code thật; chỉ tầng DB là fake
(`tests/unit/bot_operations_fakes.py`). Principal theo bộ dữ liệu QA mục 2.2.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.bot_operations_fakes import (
    BOT_A,
    BOT_B,
    FOREIGN_BOT,
    Store,
    build_operations,
    lock_timeout_error,
    member,
    seeded_store,
)

from trading_bot.admin.bot_control_router import error_status
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal
from trading_bot.auth.router import current_identity, get_auth_service, require_csrf
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

SESSION_TOKEN = "s3ss10n-t0ken-value-that-must-never-leak-000000"
CSRF_TOKEN = "csrf-token-value-that-must-never-leak-111111"


def _client(store: Store, actor: Principal | None, *, csrf: bool = True) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    if actor is not None:
        identity = AuthIdentity(principal=actor, email="qa@example.test")
        app.dependency_overrides[current_identity] = lambda: identity
        if csrf:
            app.dependency_overrides[require_csrf] = lambda: identity
    app.state.bot_operations_service = build_operations(store)
    client = TestClient(app)
    client.cookies.set(
        "crypidian_session", f"{actor.workspace_id if actor else ''}:{SESSION_TOKEN}"
    )
    return client


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    return client.post(path, json=body, headers={"X-CSRF-Token": CSRF_TOKEN})


def _pause(bot: Any = BOT_A) -> tuple[str, dict[str, Any]]:
    return f"/api/v1/bots/{bot}/runtime/pause", {"reason": "Bảo trì"}


MUTATIONS: list[tuple[str, dict[str, Any]]] = [
    _pause(),
    (f"/api/v1/bots/{BOT_A}/runtime/resume", {"reason": "Xong", "expected_revision": 1}),
    ("/api/v1/workspace/runtime/kill", {"reason": "Sự cố"}),
    ("/api/v1/workspace/runtime/release", {"reason": "Hết", "expected_revision": 1}),
    (f"/api/v1/bots/{BOT_A}/disable", {"reason": "Ngừng"}),
]


@pytest.mark.parametrize(("path", "body"), MUTATIONS)
def test_every_control_mutation_requires_csrf(path: str, body: dict[str, Any]) -> None:
    store = seeded_store()
    client = _client(store, member(store, "owner"), csrf=False)

    class AuthStub:
        async def verify_csrf(self, cookie: str | None, token: str | None) -> None:
            del cookie, token
            raise AuthenticationFailure(403, "Thiếu mã CSRF")

    client.app.dependency_overrides[get_auth_service] = lambda: AuthStub()  # type: ignore[attr-defined]

    response = client.post(path, json=body)

    assert response.status_code == 403
    assert store.events == []
    assert store.audits == []


@pytest.mark.parametrize(("path", "body"), MUTATIONS)
def test_suspended_member_session_is_rejected_before_service(
    path: str, body: dict[str, Any]
) -> None:
    store = seeded_store()
    client = _client(store, None)

    class AuthStub:
        async def authenticate(self, cookie: str | None) -> AuthIdentity:
            del cookie
            # `AuthService.authenticate` lọc membership ACTIVE: member bị khóa nhận 401.
            raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")

    client.app.dependency_overrides[get_auth_service] = lambda: AuthStub()  # type: ignore[attr-defined]

    response = _post(client, path, body)

    assert response.status_code == 401
    assert store.events == []
    assert store.audits == []


def test_service_not_wired_returns_503() -> None:
    store = seeded_store()
    client = _client(store, member(store, "owner"))
    client.app.state.bot_operations_service = None  # type: ignore[attr-defined]

    response = client.get(f"/api/v1/bots/{BOT_A}/runtime")

    assert response.status_code == 503
    # Fix round 1 minor (6): 503 dùng cùng dạng body ổn định `{"code", "message"}`.
    assert response.json()["detail"]["code"] == "SERVICE_UNAVAILABLE"
    assert set(response.json()["detail"]) == {"code", "message"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/api/v1/bots/{BOT_A}/runtime/pause", {"reason": "Bảo trì"}),
        ("POST", f"/api/v1/bots/{BOT_A}/runtime/resume", {"reason": "x", "expected_revision": 1}),
        ("POST", "/api/v1/workspace/runtime/kill", {"reason": "Sự cố"}),
        ("POST", "/api/v1/workspace/runtime/release", {"reason": "x", "expected_revision": 1}),
        ("POST", f"/api/v1/bots/{BOT_A}/disable", {"reason": "Ngừng"}),
        ("GET", f"/api/v1/bots/{BOT_A}/runtime", None),
        ("GET", "/api/v1/workspace/runtime", None),
    ],
)
def test_lock_timeout_under_load_is_503_unavailable_not_500(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Important #1: 55P03 khi runtime giữ L1 > lock_timeout → 503 có mã, có audit, không SQL."""
    store = seeded_store()
    client = _client(store, member(store, "owner"))
    store.fail_with = lock_timeout_error()

    response = client.request(method, path, json=body, headers={"X-CSRF-Token": CSRF_TOKEN})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "RUNTIME_CONTROL_UNAVAILABLE"
    assert "SQL-PARAM-MUST-NOT-LEAK" not in response.text
    if method == "POST":
        assert store.audits[-1]["reason"] == "RUNTIME_CONTROL_UNAVAILABLE"


# --- ma trận pause/resume bot ---------------------------------------------------------------
PAUSE_A = {
    "owner": 200,
    "risk_admin": 200,
    "operator": 200,
    "strategy_admin": 404,
    "analyst_reviewer": 404,
    "operator_ungranted": 404,
    "other_workspace_owner": 404,
}


@pytest.mark.parametrize(("kind", "expected"), PAUSE_A.items())
def test_pause_bot_matrix(kind: str, expected: int) -> None:
    store = seeded_store()
    client = _client(store, member(store, kind))

    response = _post(client, *_pause())

    assert response.status_code == expected, response.json()
    if expected == 200:
        assert response.json()["control"] == {
            "bot_id": str(BOT_A),
            "entry_state": "PAUSED",
            "revision": 1,
            "changed": True,
        }
        assert store.audits[-1]["action"] == "bot.runtime.paused"
    else:
        assert response.json() == {
            "detail": {"code": "BOT_NOT_FOUND", "message": "Không tìm thấy bot."}
        }
        assert store.audits[-1]["action"] == "bot.runtime.control_denied"
    assert response.headers["Cache-Control"] == "no-store"


def test_operator_pause_of_ungranted_bot_matches_unknown_bot_response() -> None:
    store = seeded_store()
    client = _client(store, member(store, "operator"))

    ungranted = _post(client, *_pause(BOT_B))
    unknown = _post(client, *_pause(FOREIGN_BOT))

    assert ungranted.status_code == unknown.status_code == 404
    assert ungranted.json() == unknown.json()


def test_pause_is_idempotent_noop_and_audited() -> None:
    store = seeded_store()
    client = _client(store, member(store, "operator"))

    _post(client, *_pause())
    again = _post(client, *_pause())

    assert again.status_code == 200
    assert again.json()["control"]["changed"] is False
    assert [audit["action"] for audit in store.audits] == [
        "bot.runtime.paused",
        "bot.runtime.paused.noop",
    ]


RESUME_A = dict(PAUSE_A)


@pytest.mark.parametrize(("kind", "expected"), RESUME_A.items())
def test_resume_bot_matrix_with_recent_mfa(kind: str, expected: int) -> None:
    store = seeded_store()
    owner = member(store, "owner")
    _post(_client(store, owner), *_pause())
    client = _client(store, member(store, kind))

    response = _post(
        client,
        f"/api/v1/bots/{BOT_A}/runtime/resume",
        {"reason": "Đã kiểm tra", "expected_revision": 1},
    )

    assert response.status_code == expected, response.json()
    if expected == 200:
        assert response.json()["control"]["entry_state"] == "ENABLED"
        assert response.json()["control"]["revision"] == 2


@pytest.mark.parametrize("kind", ["owner", "risk_admin", "operator"])
def test_resume_without_recent_mfa_is_forbidden(kind: str) -> None:
    store = seeded_store()
    _post(_client(store, member(store, "owner")), *_pause())
    client = _client(store, member(store, kind, mfa_age=timedelta(minutes=16)))

    response = _post(
        client, f"/api/v1/bots/{BOT_A}/runtime/resume", {"reason": "x", "expected_revision": 1}
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RECENT_MFA_REQUIRED"
    assert store.bot_rows[BOT_A][-1].value == "PAUSED"
    assert store.audits[-1]["reason"] == "RECENT_MFA_REQUIRED"


def test_resume_with_stale_expected_revision_conflicts() -> None:
    store = seeded_store()
    client = _client(store, member(store, "owner"))
    _post(client, *_pause())

    response = _post(
        client, f"/api/v1/bots/{BOT_A}/runtime/resume", {"reason": "x", "expected_revision": 7}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTROL_REVISION_CONFLICT"


@pytest.mark.parametrize(
    "body",
    [{}, {"reason": ""}, {"reason": "x" * 501}, {"reason": "ok", "extra": 1}],
    ids=["missing", "empty", "too-long", "extra-field"],
)
def test_pause_body_is_validated(body: dict[str, Any]) -> None:
    store = seeded_store()
    client = _client(store, member(store, "owner"))

    response = _post(client, f"/api/v1/bots/{BOT_A}/runtime/pause", body)

    assert response.status_code == 422
    assert store.events == []


def test_resume_requires_expected_revision() -> None:
    store = seeded_store()
    client = _client(store, member(store, "owner"))

    response = _post(client, f"/api/v1/bots/{BOT_A}/runtime/resume", {"reason": "x"})

    assert response.status_code == 422


# --- ma trận kill/release workspace ---------------------------------------------------------
KILL = {
    "owner": 200,
    "risk_admin": 200,
    "strategy_admin": 403,
    "operator": 403,
    "operator_ungranted": 403,
    "analyst_reviewer": 403,
}


@pytest.mark.parametrize(("kind", "expected"), KILL.items())
def test_kill_workspace_matrix(kind: str, expected: int) -> None:
    store = seeded_store()
    client = _client(store, member(store, kind))

    response = _post(client, "/api/v1/workspace/runtime/kill", {"reason": "Sự cố sàn"})

    assert response.status_code == expected, response.json()
    if expected == 200:
        assert response.json()["control"]["kill_state"] == "KILLED"
        assert store.audits[-1]["action"] == "workspace.runtime.killed"
    else:
        assert response.json()["detail"]["code"] == "CONTROL_PERMISSION_DENIED"
        assert store.workspace_rows == []
        assert store.audits[-1]["action"] == "bot.runtime.control_denied"


@pytest.mark.parametrize(("kind", "expected"), KILL.items())
def test_release_workspace_matrix(kind: str, expected: int) -> None:
    store = seeded_store()
    _post(
        _client(store, member(store, "risk_admin")),
        "/api/v1/workspace/runtime/kill",
        {"reason": "Sự cố"},
    )
    client = _client(store, member(store, kind))

    response = _post(
        client, "/api/v1/workspace/runtime/release", {"reason": "Ổn", "expected_revision": 1}
    )

    assert response.status_code == expected, response.json()
    if expected == 200:
        assert response.json()["control"] == {
            "workspace_id": str(store_workspace(store)),
            "kill_state": "ACTIVE",
            "revision": 2,
            "changed": True,
        }


def store_workspace(store: Store) -> Any:
    return store.audits[0]["workspace_id"]


def test_release_without_recent_mfa_is_forbidden() -> None:
    store = seeded_store()
    _post(
        _client(store, member(store, "owner")),
        "/api/v1/workspace/runtime/kill",
        {"reason": "Sự cố"},
    )
    client = _client(store, member(store, "risk_admin", mfa_age=timedelta(hours=1)))

    response = _post(
        client, "/api/v1/workspace/runtime/release", {"reason": "Ổn", "expected_revision": 1}
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RECENT_MFA_REQUIRED"
    assert store.workspace_rows[-1].value == "KILLED"


# --- trạng thái -----------------------------------------------------------------------------
STATUS_A = {
    "owner": 200,
    "strategy_admin": 200,
    "risk_admin": 200,
    "operator": 200,
    "analyst_reviewer": 200,
    "operator_ungranted": 404,
    "other_workspace_owner": 404,
}


@pytest.mark.parametrize(("kind", "expected"), STATUS_A.items())
def test_bot_status_matrix(kind: str, expected: int) -> None:
    store = seeded_store()
    client = _client(store, member(store, kind))

    response = client.get(f"/api/v1/bots/{BOT_A}/runtime")

    assert response.status_code == expected
    if expected == 200:
        bot = response.json()["bot"]
        assert bot["id"] == str(BOT_A)
        assert bot["entry_state"] == "ENABLED"
        assert bot["workspace_kill_state"] == "ACTIVE"
        assert set(bot) == {
            "id",
            "name",
            "status",
            "entry_state",
            "control_revision",
            "readiness",
            "readiness_reason_code",
            "workspace_kill_state",
            "workspace_control_revision",
            "entry_allowed",
        }


def test_workspace_runtime_lists_only_readable_bots() -> None:
    store = seeded_store()
    client = _client(store, member(store, "analyst_reviewer"))

    response = client.get("/api/v1/workspace/runtime")

    assert response.status_code == 200
    body = response.json()["workspace"]
    assert body["kill_state"] == "ACTIVE"
    assert [bot["id"] for bot in body["bots"]] == [str(BOT_A)]


# --- disable -------------------------------------------------------------------------------
DISABLE = {
    "owner": 200,
    "risk_admin": 403,
    "strategy_admin": 403,
    "operator": 403,
    "analyst_reviewer": 403,
}


@pytest.mark.parametrize(("kind", "expected"), DISABLE.items())
def test_disable_matrix(kind: str, expected: int) -> None:
    store = seeded_store()
    client = _client(store, member(store, kind))

    response = _post(client, f"/api/v1/bots/{BOT_A}/disable", {"reason": "Ngừng"})

    assert response.status_code == expected, response.json()
    if expected == 200:
        assert response.json()["bot"] == {
            "id": str(BOT_A),
            "status": "DISABLED",
            "changed": True,
        }
    else:
        assert response.json()["detail"]["code"] == "CONTROL_PERMISSION_DENIED"


def test_disable_not_flat_returns_409_with_counts_of_that_bot() -> None:
    store = seeded_store()
    store.not_flat[BOT_A] = (1, 1, 1)
    client = _client(store, member(store, "owner"))

    response = _post(client, f"/api/v1/bots/{BOT_A}/disable", {"reason": "Ngừng"})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "BOT_NOT_FLAT",
        "message": "Bot còn vị thế, lệnh chưa khớp hoặc reservation; hãy pause tới khi phẳng.",
        "open_positions": 1,
        "open_orders": 1,
        "held_reservations": 1,
    }
    assert store.bots[BOT_A]["status"] == "ENABLED"


def test_disable_with_stale_mfa_is_forbidden() -> None:
    store = seeded_store()
    client = _client(store, member(store, "owner", mfa_age=timedelta(minutes=20)))

    response = _post(client, f"/api/v1/bots/{BOT_A}/disable", {"reason": "Ngừng"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RECENT_MFA_REQUIRED"


# --- ánh xạ lỗi và không lộ bí mật ----------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("BOT_NOT_FOUND", 404),
        ("CONTROL_PERMISSION_DENIED", 403),
        ("RECENT_MFA_REQUIRED", 403),
        ("CONTROL_REASON_REQUIRED", 422),
        ("CONTROL_REVISION_CONFLICT", 409),
        ("CONTROL_REVISION_REQUIRED", 409),
        ("BOT_NOT_FLAT", 409),
        ("RUNTIME_CONTROL_UNAVAILABLE", 503),
        ("SOMETHING_ELSE", 409),
    ],
)
def test_error_status_mapping(code: str, status: int) -> None:
    assert error_status(code) == status


def test_no_session_or_csrf_token_in_responses_or_audit() -> None:
    store = seeded_store()
    owner = member(store, "owner")
    client = _client(store, owner)

    responses = [
        _post(client, *_pause()),
        _post(client, *_pause(FOREIGN_BOT)),
        _post(client, "/api/v1/workspace/runtime/kill", {"reason": "Sự cố"}),
        client.get(f"/api/v1/bots/{BOT_A}/runtime"),
        client.get("/api/v1/workspace/runtime"),
        _post(client, f"/api/v1/bots/{BOT_A}/disable", {"reason": "Ngừng"}),
    ]

    dumped = " ".join(response.text for response in responses) + repr(store.audits)
    assert SESSION_TOKEN not in dumped
    assert CSRF_TOKEN not in dumped
    assert all("set-cookie" not in response.headers for response in responses)
