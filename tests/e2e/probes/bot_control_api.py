# ruff: noqa: F821
"""Probe MB-07a: Admin API điều khiển bot + webhook Telegram trên PostgreSQL thật (Compose).

Được nối SAU mã probe MB-05 (`multibot_runtime.py`, đã tắt khối `__main__`) nên dùng lại
`seed`, `genesis`, `runtime_*`, `_ws`, `_connect`, `_insert`, `STATE`, `PHASES`, `_main`.

- `mb07a_auth_seed` (migrator, superuser): phiên đăng nhập cho từng role QA (hash token + CSRF như
  `AuthService`), phiên của member bị khóa, phiên risk_admin MFA cũ, binding Telegram (operator,
  owner ở workspace chính; owner ở workspace khác).
- các pha `mb07a_*` (admin-web): `create_app` thật với lifespan thật (engine role
  `crypidian_admin_web` + timeout admin-web), gọi HTTP qua `TestClient` (cookie phiên + CSRF) và
  webhook Telegram (secret header). Không có gọi mạng Telegram (sender ghi bộ nhớ).
- `mb07a_inspect` (migrator): audit, receipt, trạng thái bot, quét secret/token trong audit.
"""

import hashlib as _hashlib
import secrets as _secrets
from contextlib import contextmanager as _contextmanager

TG_SECRET = "mb07a-e2e-webhook-secret_0123456789abcdefghijklmnop"
TG_USERS = {"operator": 7_000_001, "owner": 7_000_002, "other_owner": 7_000_003}
AUTH_ROLES = ("owner", "risk_admin", "operator", "analyst_reviewer", "strategy_admin")


def _hash(token: str) -> str:
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()


def mb07a_auth_seed() -> dict[str, Any]:
    """Phiên + binding Telegram; token thô chỉ nằm trong STATE của test (không ghi DB)."""
    now = datetime.now(UTC)
    sessions: dict[str, dict[str, str]] = {}
    main, other = _ws("main"), _ws("other")
    with _connect() as connection, connection.transaction():

        def session(key: str, workspace: str, member: str, mfa_age: timedelta) -> None:
            token, csrf = _secrets.token_urlsafe(32), _secrets.token_urlsafe(24)
            _insert(
                connection,
                "session",
                id=uuid4(),
                workspace_id=UUID(workspace),
                membership_id=UUID(member),
                token_hash=_hash(token),
                csrf_token_hash=_hash(csrf),
                expires_at=now + timedelta(hours=2),
                mfa_verified_at=now - mfa_age,
            )
            sessions[key] = {"workspace": workspace, "token": token, "csrf": csrf}

        for role in AUTH_ROLES:
            session(role, main["workspace"], main["members"][role], timedelta(minutes=1))
        session(
            "risk_admin_stale", main["workspace"], main["members"]["risk_admin"], timedelta(hours=1)
        )
        # Member bị khóa giữ role workspace-wide và phiên còn hạn: chỉ trạng thái membership chặn.
        user, member = uuid4(), uuid4()
        _insert(
            connection,
            "user_account",
            id=user,
            email_normalized=f"mb07a-suspended-{user.hex[:6]}@example.test",
            password_hash="unused",
            status="ACTIVE",
        )
        _insert(
            connection,
            "user_membership",
            id=member,
            workspace_id=UUID(main["workspace"]),
            user_id=user,
            status="SUSPENDED",
        )
        _insert(
            connection,
            "role_binding",
            id=uuid4(),
            workspace_id=UUID(main["workspace"]),
            membership_id=member,
            role="risk_admin",
        )
        session("suspended", main["workspace"], str(member), timedelta(minutes=1))
        for key, workspace, member_id in (
            ("operator", main["workspace"], main["members"]["operator"]),
            ("owner", main["workspace"], main["members"]["owner"]),
            ("other_owner", other["workspace"], other["members"]["owner"]),
        ):
            _insert(
                connection,
                "telegram_binding",
                id=uuid4(),
                workspace_id=UUID(workspace),
                membership_id=UUID(member_id),
                telegram_user_id=str(TG_USERS[key]),
                verified_at=now,
            )
    return {"mb07a_sessions": sessions}


@_contextmanager
def _admin_client() -> Any:
    """App admin-web thật (lifespan, engine role thật) + webhook Telegram cho workspace chính."""
    from fastapi.testclient import TestClient

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    secret_file = Path("/tmp/mb07a-telegram-secret")
    secret_file.write_text(TG_SECRET, encoding="utf-8")
    settings = AppSettings(
        telegram_workspace_id=UUID(_ws("main")["workspace"]),
        telegram_webhook_secret_file=secret_file,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _call(
    client: Any, who: str, method: str, path: str, body: Any = None, csrf: bool = True
) -> Any:
    from trading_bot.auth.cookies import session_cookie_name

    session = STATE["mb07a_sessions"][who]
    client.cookies.clear()
    client.cookies.set(
        session_cookie_name(client.app.state.settings), f"{session['workspace']}:{session['token']}"
    )
    headers = {"X-CSRF-Token": session["csrf"]} if csrf else {}
    response = client.request(method, path, json=body, headers=headers)
    STATE.setdefault("_bodies", []).append(response.text)
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    detail = payload.get("detail") if isinstance(payload, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return {"status": response.status_code, "code": code, "body": payload}


def _no_secret_leak() -> bool:
    texts = " ".join(STATE.get("_bodies", []))
    tokens = [TG_SECRET] + [
        value
        for session in STATE["mb07a_sessions"].values()
        for value in (session["token"], session["csrf"])
    ]
    return not any(token in texts for token in tokens)


def mb07a_admin_pause() -> dict[str, Any]:
    ws = _ws()
    a, b = ws["bots"]["a"], ws["bots"]["b"]
    reason = {"reason": "MB-07a E2E bảo trì"}
    out: dict[str, Any] = {}
    with _admin_client() as client:
        out["no_csrf"] = _call(
            client, "operator", "POST", f"/api/v1/bots/{a}/runtime/pause", reason, csrf=False
        )["status"]
        out["suspended"] = _call(
            client, "suspended", "POST", f"/api/v1/bots/{a}/runtime/pause", reason
        )["status"]
        pause = _call(client, "operator", "POST", f"/api/v1/bots/{a}/runtime/pause", reason)
        out["operator_pause_a"] = [pause["status"], pause["body"].get("control")]
        again = _call(client, "operator", "POST", f"/api/v1/bots/{a}/runtime/pause", reason)
        out["operator_pause_a_again"] = again["body"]["control"]["changed"]
        for who, bot, key in (
            ("operator", b, "operator_pause_b"),
            ("analyst_reviewer", a, "analyst_pause_a"),
            ("strategy_admin", a, "strategy_admin_pause_a"),
        ):
            result = _call(client, who, "POST", f"/api/v1/bots/{bot}/runtime/pause", reason)
            out[key] = [result["status"], result["code"]]
        unknown = _call(client, "operator", "POST", f"/api/v1/bots/{uuid4()}/runtime/pause", reason)
        out["unknown_matches_ungranted"] = (
            unknown["body"]
            == _call(client, "operator", "POST", f"/api/v1/bots/{b}/runtime/pause", reason)["body"]
        )
        for who in ("operator", "strategy_admin", "analyst_reviewer"):
            result = _call(client, who, "POST", "/api/v1/workspace/runtime/kill", reason)
            out[f"{who}_kill"] = [result["status"], result["code"]]
        status = _call(client, "operator", "GET", f"/api/v1/bots/{a}/runtime")
        out["operator_status_a"] = status["body"]["bot"]
        out["analyst_status_b"] = _call(
            client, "analyst_reviewer", "GET", f"/api/v1/bots/{b}/runtime"
        )["status"]
        listing = _call(client, "analyst_reviewer", "GET", "/api/v1/workspace/runtime")
        out["analyst_list"] = [bot["id"] for bot in listing["body"]["workspace"]["bots"]]
        out["risk_admin_disable"] = _call(
            client, "risk_admin", "POST", f"/api/v1/bots/{a}/disable", reason
        )["code"]
        not_flat = _call(client, "owner", "POST", f"/api/v1/bots/{a}/disable", reason)
        out["owner_disable_not_flat"] = [not_flat["status"], not_flat["body"]["detail"]]
        out["no_secret_leak"] = _no_secret_leak()
    return out


def mb07a_admin_kill() -> dict[str, Any]:
    reason = {"reason": "MB-07a E2E sự cố"}
    out: dict[str, Any] = {}
    with _admin_client() as client:
        kill = _call(client, "risk_admin_stale", "POST", "/api/v1/workspace/runtime/kill", reason)
        out["risk_admin_kill"] = [kill["status"], kill["body"]["control"]]
        again = _call(client, "owner", "POST", "/api/v1/workspace/runtime/kill", reason)
        out["owner_kill_again_changed"] = again["body"]["control"]["changed"]
        release = {"reason": "Mở lại", "expected_revision": 1}
        for who in ("risk_admin_stale", "operator", "strategy_admin"):
            result = _call(client, who, "POST", "/api/v1/workspace/runtime/release", release)
            out[f"{who}_release"] = [result["status"], result["code"]]
        out["no_secret_leak"] = _no_secret_leak()
    return out


def _update(user: int, text: str, update_id: int, **message: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": update_id,
        "date": 1_790_000_000,
        "from": {"id": user, "is_bot": False, "first_name": "QA"},
        "chat": {"id": user, "type": "private"},
        "text": text,
    }
    body.update(message)
    return {"update_id": update_id, "message": body}


def mb07a_release_and_telegram() -> dict[str, Any]:
    ws = _ws()
    a, b = ws["bots"]["a"], ws["bots"]["b"]
    out: dict[str, Any] = {}
    with _admin_client() as client:
        release = _call(
            client,
            "risk_admin",
            "POST",
            "/api/v1/workspace/runtime/release",
            {"reason": "Hết sự cố", "expected_revision": 1},
        )
        out["risk_admin_release"] = [release["status"], release["body"].get("control")]
        sender = client.app.state.telegram_sender

        def hook(update: Any, secret: str = TG_SECRET) -> Any:
            response = client.post(
                "/api/v1/telegram/webhook",
                json=update,
                headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            )
            STATE.setdefault("_bodies", []).append(response.text)
            return [response.status_code, response.json().get("status")]

        operator, owner = TG_USERS["operator"], TG_USERS["owner"]
        out["tg_wrong_secret"] = hook(_update(operator, "/bots", 100), secret="x" * 40)
        out["tg_list"] = hook(_update(operator, "/bots", 101))
        out["tg_list_reply"] = sender.messages[-1][1]
        out["tg_operator_no_target"] = hook(_update(operator, "/bot pause", 102))
        out["tg_operator_no_target_reply"] = sender.messages[-1][1]
        out["tg_owner_ambiguous"] = hook(_update(owner, "/bot pause", 103))
        out["tg_owner_ambiguous_reply"] = sender.messages[-1][1]
        out["tg_unknown"] = hook(_update(operator, f"/bot pause {uuid4()}", 104))
        out["tg_unknown_reply"] = sender.messages[-1][1]
        out["tg_user_text"] = hook(_update(operator, "/bot pause MB07A-USER-TEXT", 112))
        out["tg_operator_pause_b"] = hook(_update(operator, f"/bot pause {b}", 105))
        out["tg_owner_pause_b"] = hook(_update(owner, f"/bot pause {b}", 106))
        out["tg_owner_pause_b_replay"] = hook(_update(owner, f"/bot pause {b}", 106))
        out["tg_owner_resume_b"] = hook(_update(owner, f"/bot resume {b}", 107))
        out["tg_owner_resume_b_reply"] = sender.messages[-1][1]
        out["tg_flatten"] = hook(_update(owner, "/flatten", 108))
        out["tg_flatten_reply"] = sender.messages[-1][1]
        out["tg_other_workspace"] = hook(_update(TG_USERS["other_owner"], f"/bot pause {a}", 109))
        out["tg_group"] = hook(
            _update(owner, f"/bot pause {a}", 110, chat={"id": -100777, "type": "group"})
        )
        out["tg_edited"] = hook(
            {"update_id": 111, "edited_message": _update(owner, f"/bot pause {a}", 111)["message"]}
        )
        status_b = _call(client, "owner", "GET", f"/api/v1/bots/{b}/runtime")["body"]["bot"]
        out["status_b_entry_state"] = status_b["entry_state"]
        status_a = _call(client, "owner", "GET", f"/api/v1/bots/{a}/runtime")["body"]["bot"]
        out["status_a_before_disable"] = status_a["entry_state"]
        disable = _call(
            client,
            "owner",
            "POST",
            f"/api/v1/bots/{a}/disable",
            {"reason": "MB-07a E2E: bot A đã phẳng"},
        )
        out["owner_disable_a"] = [disable["status"], disable["body"]]
        again = _call(client, "owner", "POST", f"/api/v1/bots/{a}/disable", {"reason": "lặp"})
        out["owner_disable_a_again"] = again["body"]
        out["status_a_after_disable"] = _call(client, "owner", "GET", f"/api/v1/bots/{a}/runtime")[
            "body"
        ]["bot"]["status"]
        out["no_secret_leak"] = _no_secret_leak()
        out["replies_leak"] = any(TG_SECRET in text for _, text in sender.messages)
    return out


def mb07a_inspect() -> dict[str, Any]:
    ws = _ws()
    workspace = ws["workspace"]
    with _connect() as connection:
        audits = [
            list(row)
            for row in connection.execute(
                "SELECT action, reason, target_id::text, actor_membership_id::text "
                "FROM audit_event WHERE workspace_id = %s ORDER BY occurred_at, id",
                (workspace,),
            ).fetchall()
        ]
        receipts = connection.execute(
            "SELECT count(*) FROM telegram_update_receipt WHERE workspace_id = %s", (workspace,)
        ).fetchone()[0]
        bot_status = {
            key: connection.execute(
                "SELECT b.status, a.status FROM bot_instance b JOIN trading_account a "
                "ON a.workspace_id = b.workspace_id AND a.id = b.trading_account_id "
                "WHERE b.id = %s",
                (bot,),
            ).fetchone()
            for key, bot in ws["bots"].items()
        }
        controls = [
            list(row)
            for row in connection.execute(
                "SELECT bot_id::text, revision, entry_state, reason_code FROM bot_runtime_control "
                "WHERE workspace_id = %s ORDER BY bot_id, revision",
                (workspace,),
            ).fetchall()
        ]
        dump = connection.execute(
            "SELECT coalesce(string_agg(coalesce(reason, '') || ' ' || action, ' '), '') "
            "FROM audit_event"
        ).fetchone()[0]
    tokens = [TG_SECRET] + [
        value
        for session in STATE["mb07a_sessions"].values()
        for value in (session["token"], session["csrf"])
    ]
    return {
        "audits": audits,
        "receipts": receipts,
        "bot_status": {key: list(value) for key, value in bot_status.items()},
        "controls": controls,
        "audit_leak": any(token in dump for token in tokens),
        # Text tin nhắn người dùng không vào audit (ruling 41); chỉ mã lý do/hằng của handler.
        "user_text_in_audit": "MB07A-USER-TEXT" in dump,
    }


PHASES.update(
    {
        "mb07a_auth_seed": mb07a_auth_seed,
        "mb07a_admin_pause": mb07a_admin_pause,
        "mb07a_admin_kill": mb07a_admin_kill,
        "mb07a_release_and_telegram": mb07a_release_and_telegram,
        "mb07a_inspect": mb07a_inspect,
    }
)
_main()
