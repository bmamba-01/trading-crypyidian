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
import re as _re
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
            if key == "other_owner":
                session(key, workspace, member_id, timedelta(minutes=1))
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
        telegram_bot_username="crypidian_test_bot",
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


def mb07b_seed_fresh_mark() -> dict[str, Any]:
    """Seed mark PAPER mới bằng migrator trước khi admin tạo preview."""
    workspace = UUID(_ws("main")["workspace"])
    with _connect() as connection, connection.transaction():
        connection.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        instrument_row = connection.execute(
            "SELECT id FROM market_instrument WHERE workspace_id=%s AND symbol='BTCUSDT' "
            "ORDER BY available_at DESC LIMIT 1",
            (workspace,),
        ).fetchone()
        if instrument_row is None:
            raise AssertionError(
                "Không có BTCUSDT instrument để seed mark cho workspace "
                f"{workspace}; state={STATE.get('workspaces', {}).get('main')}"
            )
        instrument = instrument_row[0]
        close_time = now - timedelta(seconds=2)
        opened = close_time - timedelta(minutes=1)
        connection.execute(
            "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
            "logical_key, idempotency_key, revision, checksum, open_time, close_time, event_at, "
            "received_at, available_at, open, high, low, close, volume, is_closed) "
            "VALUES (%s,%s,%s,'BTCUSDT','1m','BINANCE_USDM',%s,%s,1,%s,%s,%s,%s,%s,%s,"
            "100,103,99,102,1,TRUE)",
            (
                uuid4(),
                workspace,
                instrument,
                f"mb07b-flatten:{uuid4().hex}",
                f"mb07b-flatten-bar:{uuid4().hex}",
                "f" * 64,
                opened,
                close_time,
                close_time,
                now - timedelta(seconds=1),
                now - timedelta(seconds=1),
            ),
        )
    return {"mark_seeded": True}


def mb07b_flatten_api() -> dict[str, Any]:
    """Dùng admin-web thật cho preview/confirm/status; runtime chạy Compose process riêng."""
    import time

    with _admin_client() as client:
        reason = "MB-07b E2E flatten PAPER"
        preview_response = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/preview",
            {"reason": reason},
        )
        assert preview_response["status"] == 200, preview_response
        preview = preview_response["body"]["preview"]
        confirmed = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/confirm",
            {
                "preview_id": preview["id"],
                "preview_hash": preview["preview_hash"],
                "idempotency_key": uuid4().hex,
                "reason": reason,
            },
        )
        assert confirmed["status"] == 200, confirmed
        operation_id = confirmed["body"]["operation"]["id"]
        deadline = time.monotonic() + 45
        operation: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = _call(
                client,
                "owner",
                "GET",
                f"/api/v1/workspace/flatten/operations/{operation_id}",
            )
            assert response["status"] == 200, response
            operation = response["body"]["operation"]
            if operation["status"] in {"COMPLETED", "BLOCKED", "PARTIAL"}:
                break
            time.sleep(0.5)
        return {"preview": preview, "operation": operation}


def mb07b_flatten_inspect() -> dict[str, Any]:
    operation_id = UUID(STATE["mb07b_operation_id"])
    workspace = _ws("main")
    with _connect() as connection:
        operation = connection.execute(
            "SELECT status FROM flatten_operation WHERE workspace_id=%s AND id=%s",
            (workspace["workspace"], operation_id),
        ).fetchone()
        items = connection.execute(
            "SELECT target_type, status, result FROM flatten_operation_item "
            "WHERE workspace_id=%s AND operation_id=%s ORDER BY target_type, target_id",
            (workspace["workspace"], operation_id),
        ).fetchall()
        exits = connection.execute(
            "SELECT count(*) FROM fill WHERE workspace_id=%s AND account_id=%s "
            "AND fill_role='EXIT'",
            (workspace["workspace"], workspace["accounts"]["a"]),
        ).fetchone()[0]
    return {
        "status": operation[0],
        "items": [list(row) for row in items],
        "exit_fill_count": exits,
    }


PHASES["mb07b_seed_fresh_mark"] = mb07b_seed_fresh_mark
PHASES["mb07b_flatten_api"] = mb07b_flatten_api
PHASES["mb07b_flatten_inspect"] = mb07b_flatten_inspect


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
        draft = _call(
            client,
            "owner",
            "POST",
            f"/api/v1/bots/{a}/config-drafts",
            {"overlay": {"schema_version": 1}, "expected_parent_version_id": None},
        )
        out["enable_config_draft"] = [draft["status"], draft["body"]]
        draft_id = draft["body"].get("draft", {}).get("id")
        if draft_id is not None:
            out["enable_config_validation"] = _call(
                client,
                "owner",
                "POST",
                f"/api/v1/bots/{a}/config-drafts/{draft_id}/validate",
            )
            out["enable_config_publish"] = _call(
                client,
                "owner",
                "POST",
                f"/api/v1/bots/{a}/config-drafts/{draft_id}/publish",
                {"expected_revision": 1},
            )
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
        workspace_id = _ws()["workspace"]
        with _connect() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true), "
                "set_config('app.membership_id', %s, true), set_config('app.bot_id', '', true)",
                (workspace_id, _ws()["members"]["owner"]),
            )
            account = connection.execute(
                "SELECT ta.status, ta.mode, ta.venue, bi.status FROM trading_account ta "
                "JOIN bot_instance bi ON bi.workspace_id = ta.workspace_id "
                "AND bi.trading_account_id = ta.id WHERE bi.id = %s",
                (a,),
            ).fetchone()
            runtime_state = connection.execute(
                "SELECT readiness, activity_status, applied_bot_control_revision, "
                "applied_workspace_control_revision FROM bot_runtime_state "
                "WHERE workspace_id = %s AND bot_id = %s",
                (workspace_id, a),
            ).fetchone()
            bot_revision = connection.execute(
                "SELECT coalesce(max(revision), 0) FROM bot_runtime_control "
                "WHERE workspace_id = %s AND bot_id = %s",
                (workspace_id, a),
            ).fetchone()[0]
            workspace_revision = connection.execute(
                "SELECT coalesce(max(revision), 0) FROM workspace_runtime_control "
                "WHERE workspace_id = %s",
                (workspace_id,),
            ).fetchone()[0]
            incidents = connection.execute(
                "SELECT count(*) FROM account_activity_incident WHERE workspace_id = %s "
                "AND bot_id = %s AND status = 'OPEN'",
                (workspace_id, a),
            ).fetchone()[0]
            config = connection.execute(
                "SELECT count(*) FROM bot_config_version WHERE workspace_id = %s "
                "AND bot_id = %s AND status = 'PUBLISHED'",
                (workspace_id, a),
            ).fetchone()[0]
            out["enable_snapshot"] = {
                "account": list(account),
                "runtime": list(runtime_state) if runtime_state else None,
                "bot_revision": bot_revision,
                "workspace_revision": workspace_revision,
                "incidents": incidents,
                "published_configs": config,
            }
        enable = _call(
            client,
            "owner",
            "POST",
            f"/api/v1/bots/{a}/enable",
            {"reason": "Đã đối soát sau disable"},
        )
        out["owner_enable_a"] = [enable["status"], enable["body"]]
        out["status_a_after_enable"] = _call(client, "owner", "GET", f"/api/v1/bots/{a}/runtime")[
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
        # Fix round 1 minor 1: lớp DB chặn admin-web enable lại account đã DISABLED.
        account_a = ws["accounts"]["a"]
        try:
            # force_rollback: kể cả khi trigger không chặn, dữ liệu không đổi.
            with connection.transaction(force_rollback=True):
                connection.execute("SET LOCAL ROLE crypidian_admin_web")
                connection.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace,))
                connection.execute(
                    "UPDATE trading_account SET status = 'ENABLED' WHERE id = %s", (account_a,)
                )
            reenable = "ALLOWED"
        except psycopg.Error as error:
            reenable = (
                "MB07A_ACCOUNT_STATUS_TRANSITION_DENIED"
                if "MB07A_ACCOUNT_STATUS_TRANSITION_DENIED" in str(error)
                else f"{type(error).__name__}:{error.sqlstate}"
            )
        finally:
            # Fix round 2 (6): role luôn trả về superuser của probe, kể cả nhánh lỗi.
            connection.execute("RESET ROLE")
        role_after = connection.execute("SELECT current_user").fetchone()[0]
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
        "admin_web_reenable": reenable,
        "role_after_reenable": role_after,
        "audit_leak": any(token in dump for token in tokens),
        # Text tin nhắn người dùng không vào audit (ruling 41); chỉ mã lý do/hằng của handler.
        "user_text_in_audit": "MB07A-USER-TEXT" in dump,
    }


def mb07b_telegram_binding_lifecycle() -> dict[str, Any]:
    """E2E mã hash, xác nhận private, revoke/rebind và từ chối thành viên bị khóa."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from uuid import UUID

    from fastapi.testclient import TestClient

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    secret_file = Path("/tmp/mb07b-telegram-secret")
    secret_file.write_text(TG_SECRET, encoding="utf-8")
    workspace = UUID(_ws("main")["workspace"])
    settings = AppSettings(
        telegram_workspace_id=workspace,
        telegram_webhook_secret_file=secret_file,
        telegram_bot_username="crypidian_test_bot",
    )
    out: dict[str, Any] = {}
    with TestClient(create_app(settings)) as client:

        def set_session_cookie(who: str = "owner") -> None:
            from trading_bot.auth.cookies import session_cookie_name

            owner_session = STATE["mb07a_sessions"][who]
            client.cookies.clear()
            client.cookies.set(
                session_cookie_name(client.app.state.settings),
                f"{owner_session['workspace']}:{owner_session['token']}",
            )

        def page_csrf(who: str = "owner") -> str:
            set_session_cookie(who)
            page = client.get("/admin/telegram")
            assert page.status_code == 200, page.text
            match = _re.search(r'name="csrf_token" value="([^"]+)"', page.text)
            assert match is not None
            return match.group(1)

        def post_form(path: str, values: dict[str, str], who: str = "owner") -> Any:
            set_session_cookie(who)
            response = client.post(
                path,
                data={"csrf_token": page_csrf(who), **values},
                follow_redirects=False,
            )
            return response

        def create_link(label: str, who: str = "owner") -> tuple[str, str]:
            response = post_form("/admin/telegram/link", {"reason": label}, who=who)
            assert response.status_code == 200, response.text
            match = _re.search(
                r'href="https://t\.me/crypidian_test_bot\?start=([A-Za-z0-9_-]+)"',
                response.text,
            )
            assert match is not None, response.text
            return match.group(1), response.text

        def revoke(member_id: str, label: str) -> None:
            response = post_form(f"/admin/telegram/revoke/{member_id}", {"reason": label})
            assert response.status_code == 303, response.text

        def link_revoke_race(member_id: str) -> tuple[str, bool]:
            """Đợi revoke tuần tự hóa với link đang chờ khóa membership."""
            from trading_bot.auth.cookies import session_cookie_name

            def post_link(target: Any) -> Any:
                owner_session = STATE["mb07a_sessions"]["owner"]
                target.cookies.set(
                    session_cookie_name(target.app.state.settings),
                    f"{owner_session['workspace']}:{owner_session['token']}",
                )
                page = target.get("/admin/telegram")
                csrf = _re.search(r'name="csrf_token" value="([^"]+)"', page.text)
                assert csrf is not None, page.text
                return target.post(
                    "/admin/telegram/link",
                    data={"csrf_token": csrf.group(1), "reason": "E2E race link/revoke"},
                )

            def post_revoke(target: Any) -> Any:
                owner_session = STATE["mb07a_sessions"]["owner"]
                target.cookies.set(
                    session_cookie_name(target.app.state.settings),
                    f"{owner_session['workspace']}:{owner_session['token']}",
                )
                page = target.get("/admin/telegram")
                csrf = _re.search(r'name="csrf_token" value="([^"]+)"', page.text)
                assert csrf is not None, page.text
                return target.post(
                    f"/admin/telegram/revoke/{member_id}",
                    data={"csrf_token": csrf.group(1), "reason": "E2E race revoke"},
                    follow_redirects=False,
                )

            def wait_for_membership_waiters(expected: int) -> bool:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with _connect() as connection:
                        count = connection.execute(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type='Lock' AND query LIKE '%user_membership%'"
                        ).fetchone()[0]
                    if count >= expected:
                        return True
                    time.sleep(0.025)
                return False

            pool = ThreadPoolExecutor(max_workers=2)
            try:
                with (
                    TestClient(create_app(settings)) as first_client,
                    TestClient(create_app(settings)) as second_client,
                ):
                    with _connect() as connection, connection.transaction():
                        connection.execute(
                            "SELECT set_config('app.workspace_id', %s, true)", (str(workspace),)
                        )
                        connection.execute(
                            "SELECT id FROM user_membership WHERE workspace_id=%s AND id=%s "
                            "FOR UPDATE",
                            (str(workspace), member_id),
                        )
                        link_future = pool.submit(post_link, first_client)
                        first_waiter = wait_for_membership_waiters(1)
                        assert first_waiter, "Tạo link không chờ khóa membership"
                        revoke_future = pool.submit(post_revoke, second_client)
                        both_waiting = wait_for_membership_waiters(2)
                        assert both_waiting, "Revoke không xếp hàng trên cùng khóa membership"
                    link_response = link_future.result(timeout=10)
                    revoke_response = revoke_future.result(timeout=10)
                    assert link_response.status_code == 200, link_response.text
                    assert revoke_response.status_code == 303, revoke_response.text
                    match = _re.search(
                        r'href="https://t\.me/crypidian_test_bot\?start=([A-Za-z0-9_-]+)"',
                        link_response.text,
                    )
                    assert match is not None, link_response.text
                    return match.group(1), both_waiting
            finally:
                pool.shutdown(wait=True)

        def callback_revoke_race(member_id: str, callback: Any) -> tuple[Any, Any, bool, bool]:
            """Kiểm tra callback/revoke khi revoke chờ một binding đang bị khóa."""
            from trading_bot.auth.cookies import session_cookie_name

            def post_revoke(target: Any) -> Any:
                owner_session = STATE["mb07a_sessions"]["owner"]
                target.cookies.set(
                    session_cookie_name(target.app.state.settings),
                    f"{owner_session['workspace']}:{owner_session['token']}",
                )
                page = target.get("/admin/telegram")
                csrf = _re.search(r'name="csrf_token" value="([^"]+)"', page.text)
                assert csrf is not None, page.text
                return target.post(
                    f"/admin/telegram/revoke/{member_id}",
                    data={"csrf_token": csrf.group(1), "reason": "E2E race callback/revoke"},
                    follow_redirects=False,
                )

            def wait_for_query(pattern: str) -> bool:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with _connect() as connection:
                        count = connection.execute(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type='Lock' AND query ILIKE %s",
                            (pattern,),
                        ).fetchone()[0]
                    if count:
                        return True
                    time.sleep(0.025)
                return False

            pool = ThreadPoolExecutor(max_workers=2)
            try:
                with (
                    TestClient(create_app(settings)) as revoke_client,
                    TestClient(create_app(settings)) as callback_client,
                ):
                    with _connect() as connection, connection.transaction():
                        connection.execute(
                            "SELECT set_config('app.workspace_id', %s, true), "
                            "set_config('app.membership_id', %s, true)",
                            (str(workspace), member_id),
                        )
                        connection.execute(
                            "SELECT id FROM telegram_binding WHERE workspace_id=%s "
                            "AND membership_id=%s AND revoked_at IS NULL FOR UPDATE",
                            (str(workspace), member_id),
                        ).fetchone()
                        revoke_future = pool.submit(post_revoke, revoke_client)
                        revoke_waiting = wait_for_query("%UPDATE telegram_binding%")
                        assert revoke_waiting, "Revoke không chờ binding đang bị khóa"
                        callback_future = pool.submit(callback, callback_client)
                        callback_waiting = wait_for_query("%user_membership%")
                        assert callback_waiting, "Callback không tuần tự hóa trên membership"
                    revoke_response = revoke_future.result(timeout=10)
                    callback_response = callback_future.result(timeout=10)
                    return revoke_response, callback_response, revoke_waiting, callback_waiting
            finally:
                pool.shutdown(wait=True)

        def telegram_message(text: str, user: int, update_id: int) -> Any:
            return client.post(
                "/api/v1/telegram/webhook",
                json={
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id,
                        "date": 1_790_000_000,
                        "from": {"id": user, "is_bot": False},
                        "chat": {"id": user, "type": "private"},
                        "text": text,
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
            )

        def confirm(user: int, update_id: int) -> Any:
            sender = client.app.state.telegram_sender
            callback_data = sender.confirmations[-1][2]
            return client.post(
                "/api/v1/telegram/webhook",
                json={
                    "update_id": update_id,
                    "callback_query": {
                        "id": f"binding-{update_id}",
                        "from": {"id": user, "is_bot": False},
                        "message": {
                            "message_id": update_id,
                            "chat": {"id": user, "type": "private"},
                        },
                        "data": callback_data,
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
            )

        owner_membership = _ws("main")["members"]["owner"]
        revoke(owner_membership, "Chuẩn bị kiểm thử rebind")
        raced_code, race_serialized = link_revoke_race(owner_membership)
        raced_start = telegram_message(f"/start {raced_code}", TG_USERS["owner"], 4999)
        assert raced_start.json()["status"] == "link_invalid", raced_start.text
        first_code, first_page = create_link("Liên kết Telegram đầu tiên")
        assert first_code not in first_page.replace(f"?start={first_code}", "")
        with _connect() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true), "
                "set_config('app.membership_id', %s, true)",
                (str(workspace), owner_membership),
            )
            first_count = connection.execute(
                "SELECT count(*) FROM telegram_link_request WHERE workspace_id = %s "
                "AND consumed_at IS NULL AND expires_at > clock_timestamp()",
                (str(workspace),),
            ).fetchone()[0]
        assert first_count == 1, {
            "first_count": first_count,
            "db_settings": [
                client.app.state.settings.database_user,
                client.app.state.settings.database_host,
            ],
        }
        first_start = telegram_message(f"/start {first_code}", TG_USERS["owner"], 5001)
        assert first_start.json()["status"] == "link_confirmation_required", first_start.text
        first_confirmation = confirm(TG_USERS["owner"], 5002)
        assert first_confirmation.json()["status"] == "linked", first_confirmation.text
        replay = telegram_message(f"/start {first_code}", TG_USERS["owner"], 5003)
        assert replay.json()["status"] == "link_invalid", replay.text

        revoke(owner_membership, "Thu hồi trước lần rebind")
        second_code, _ = create_link("Tạo mã cho lần rebind")
        telegram_message(f"/start {second_code}", 7_000_004, 5004)
        wrong_user_confirmation = confirm(7_000_005, 5005)
        assert wrong_user_confirmation.json()["status"] == "link_confirmation_invalid", (
            wrong_user_confirmation.text
        )
        second_confirmation = confirm(7_000_004, 5006)
        assert second_confirmation.json()["status"] == "linked", second_confirmation.text
        callback_replay = confirm(7_000_004, 5007)
        assert callback_replay.json()["status"] == "link_confirmation_invalid", callback_replay.text

        duplicate_code, _ = create_link("Kiểm tra membership đã liên kết")
        duplicate_start = telegram_message(f"/start {duplicate_code}", 7_000_005, 5010)
        assert duplicate_start.json()["status"] == "link_confirmation_required", (
            duplicate_start.text
        )
        duplicate_confirmation = confirm(7_000_005, 5011)
        assert duplicate_confirmation.json()["status"] == "link_confirmation_invalid", (
            duplicate_confirmation.text
        )

        expired_code, _ = create_link("Kiểm tra mã hết hạn")
        with _connect() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true), "
                "set_config('app.membership_id', %s, true)",
                (str(workspace), owner_membership),
            )
            connection.execute(
                "UPDATE telegram_link_request SET expires_at = "
                "created_at + interval '1 microsecond' "
                "WHERE workspace_id = %s AND token_digest = %s",
                (str(workspace), _hash(expired_code)),
            )
        expired_start = telegram_message(f"/start {expired_code}", 7_000_004, 5012)
        assert expired_start.json()["status"] == "link_invalid", expired_start.text

        other_workspace_code, _ = create_link("Kiểm tra ràng buộc workspace", who="other_owner")
        wrong_workspace = telegram_message(f"/start {other_workspace_code}", 7_000_006, 5013)
        assert wrong_workspace.json()["status"] == "link_invalid", wrong_workspace.text

        with _connect() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true), "
                "set_config('app.membership_id', %s, true)",
                (str(workspace), owner_membership),
            )
            first_digest = _hash(first_code)
            second_digest = _hash(second_code)
            stored = connection.execute(
                "SELECT token_digest, consumed_at IS NOT NULL FROM telegram_link_request "
                "WHERE workspace_id = %s AND token_digest = ANY(%s) ORDER BY token_digest",
                (str(workspace), [first_digest, second_digest]),
            ).fetchall()
            binding_audits = connection.execute(
                "SELECT action, reason FROM audit_event WHERE workspace_id = %s "
                "AND action LIKE 'telegram.binding.%%' ORDER BY occurred_at",
                (str(workspace),),
            ).fetchall()
            history = connection.execute(
                "SELECT telegram_user_id, revoked_at IS NULL FROM telegram_binding "
                "WHERE workspace_id = %s AND membership_id = %s ORDER BY created_at, id",
                (str(workspace), owner_membership),
            ).fetchall()
        assert len(stored) == 2, stored
        assert all(row[1] for row in stored), stored
        assert first_code not in str(stored)
        assert second_code not in str(stored)
        assert first_code not in str(binding_audits)
        assert second_code not in str(binding_audits)
        assert history == [
            (str(TG_USERS["owner"]), False),
            (str(TG_USERS["owner"]), False),
            ("7000004", True),
        ], history

        start_race_code, _ = create_link("Kiểm tra race tạo challenge với revoke")

        def start_during_revoke(target: Any) -> Any:
            return target.post(
                "/api/v1/telegram/webhook",
                json={
                    "update_id": 6001,
                    "message": {
                        "message_id": 6001,
                        "date": 1_790_000_000,
                        "from": {"id": 7_000_008, "is_bot": False},
                        "chat": {"id": 7_000_008, "type": "private"},
                        "text": f"/start {start_race_code}",
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
            )

        start_revoke, start_callback, start_revoke_waited, start_callback_waited = (
            callback_revoke_race(owner_membership, start_during_revoke)
        )
        assert start_revoke.status_code == 303, start_revoke.text
        assert start_revoke_waited
        assert start_callback_waited
        assert start_callback.status_code == 200, start_callback.text
        assert start_callback.json()["status"] == "link_invalid", start_callback.text

        rebind_code, _ = create_link("Tái liên kết trước race xác nhận/revoke")
        rebind_start = telegram_message(f"/start {rebind_code}", 7_000_009, 6002)
        assert rebind_start.json()["status"] == "link_confirmation_required", rebind_start.text
        rebind_confirmation = confirm(7_000_009, 6003)
        assert rebind_confirmation.json()["status"] == "linked", rebind_confirmation.text
        confirm_race_code, _ = create_link("Kiểm tra race xác nhận với revoke")
        confirm_race_start = telegram_message(f"/start {confirm_race_code}", 7_000_010, 6004)
        assert confirm_race_start.json()["status"] == "link_confirmation_required", (
            confirm_race_start.text
        )
        confirm_callback_data = client.app.state.telegram_sender.confirmations[-1][2]

        def confirm_during_revoke(target: Any) -> Any:
            return target.post(
                "/api/v1/telegram/webhook",
                json={
                    "update_id": 6005,
                    "callback_query": {
                        "id": "binding-race-confirm-6005",
                        "from": {"id": 7_000_010, "is_bot": False},
                        "message": {
                            "message_id": 6005,
                            "chat": {"id": 7_000_010, "type": "private"},
                        },
                        "data": confirm_callback_data,
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
            )

        confirm_revoke, confirm_callback, confirm_revoke_waited, confirm_callback_waited = (
            callback_revoke_race(owner_membership, confirm_during_revoke)
        )
        assert confirm_revoke.status_code == 303, confirm_revoke.text
        assert confirm_revoke_waited
        assert confirm_callback_waited
        assert confirm_callback.status_code == 200, confirm_callback.text
        assert confirm_callback.json()["status"] == "link_confirmation_invalid", (
            confirm_callback.text
        )

        out.update(
            rebind_history=len(history),
            active_owner_binding=history[-1][0],
            link_audit_count=len(binding_audits),
            link_revoke_race_serialized=race_serialized,
            callback_start_revoke_serialized=(start_revoke_waited, start_callback_waited),
            callback_confirm_revoke_serialized=(confirm_revoke_waited, confirm_callback_waited),
            operator_membership=_ws("main")["members"]["operator"],
            operator_telegram_user=TG_USERS["operator"],
            wrong_workspace_status=wrong_workspace.json()["status"],
        )
    return out


def mb07b_telegram_lock_seed() -> dict[str, Any]:
    """Khóa operator bằng migration role để kiểm tra webhook đọc trạng thái mới nhất."""
    workspace = _ws("main")["workspace"]
    membership_id = _ws("main")["members"]["operator"]
    with _connect() as connection, connection.transaction():
        connection.execute(
            "UPDATE user_membership SET status = 'SUSPENDED' WHERE workspace_id = %s AND id = %s",
            (workspace, membership_id),
        )
    return {"workspace": workspace, "membership": membership_id}


def mb07b_telegram_lock_webhook() -> dict[str, Any]:
    """Xác nhận member bị khóa bị từ chối ngay và owner vẫn có thể thu hồi binding."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    secret_file = Path("/tmp/mb07b-telegram-secret")
    secret_file.write_text(TG_SECRET, encoding="utf-8")
    workspace = _ws("main")["workspace"]
    member_id = _ws("main")["members"]["operator"]
    settings = AppSettings(
        telegram_workspace_id=UUID(workspace),
        telegram_webhook_secret_file=secret_file,
        telegram_bot_username="crypidian_test_bot",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/telegram/webhook",
            json={
                "update_id": 5009,
                "message": {
                    "message_id": 5009,
                    "date": 1_790_000_000,
                    "from": {"id": TG_USERS["operator"], "is_bot": False},
                    "chat": {"id": TG_USERS["operator"], "type": "private"},
                    "text": "/bots",
                },
            },
            headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
        )
        assert response.json()["status"] == "unbound", response.text
        owner_session = STATE["mb07a_sessions"]["owner"]
        from trading_bot.auth.cookies import session_cookie_name

        client.cookies.set(
            session_cookie_name(client.app.state.settings),
            f"{owner_session['workspace']}:{owner_session['token']}",
        )
        page = client.get("/admin/telegram")
        csrf_match = _re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert csrf_match is not None
        revoked = client.post(
            f"/admin/telegram/revoke/{member_id}",
            data={"csrf_token": csrf_match.group(1), "reason": "Thu hồi binding đã khóa"},
            follow_redirects=False,
        )
        assert revoked.status_code == 303, revoked.text
        with _connect() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true), "
                "set_config('app.membership_id', %s, true)",
                (workspace, _ws("main")["members"]["owner"]),
            )
            active = connection.execute(
                "SELECT count(*) FROM telegram_binding WHERE workspace_id = %s "
                "AND membership_id = %s AND revoked_at IS NULL",
                (workspace, member_id),
            ).fetchone()[0]
    return {"webhook_status": response.json()["status"], "active_binding_count": active}


def mb07b_enable_pause_disable_race() -> dict[str, Any]:
    """Race enable/pause và disable/pause trên cùng advisory L1 PostgreSQL."""
    import asyncio
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    workspace_id = _ws("main")["workspace"]
    bot_id = _ws("main")["bots"]["a"]
    from pathlib import Path

    secret_file = Path("/tmp/mb07b-enable-race-secret")
    secret_file.write_text(TG_SECRET, encoding="utf-8")
    settings = AppSettings(
        telegram_workspace_id=UUID(workspace_id),
        telegram_webhook_secret_file=secret_file,
        telegram_bot_username="crypidian_test_bot",
    )

    def request(client: Any, role: str, method: str, path: str, body: Any = None) -> Any:
        return _call(client, role, method, path, body)

    def wait_for_advisory_waiter() -> bool:
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            with _connect() as connection:
                count = connection.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND wait_event = 'advisory' "
                    "AND query LIKE '%pg_advisory_xact_lock%'"
                ).fetchone()[0]
            if count >= 1:
                return True
            time.sleep(0.025)
        return False

    def race(
        first_method: str,
        first_role: str,
        first_path: str,
        second_role: str,
        second_path: str,
        first_body: Any,
        second_body: Any,
    ) -> dict[str, Any]:
        with (
            TestClient(create_app(settings)) as first_client,
            TestClient(create_app(settings)) as second_client,
        ):
            service = first_client.app.state.bot_operations_service
            factory = service._lifecycle_guard_factory
            acquired = threading.Event()
            release = threading.Event()

            class HeldGuard:
                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                async def lock_workspace_for_enable(self, workspace: UUID) -> None:
                    await self._inner.lock_workspace_for_enable(workspace)
                    if first_method == "lock_workspace_for_enable":
                        acquired.set()
                        if not await asyncio.to_thread(release.wait, 4.0):
                            raise TimeoutError("Không được nhả L1 sau enable")

                async def ensure_flat_for_disable(self, workspace: UUID, target: UUID) -> None:
                    await self._inner.ensure_flat_for_disable(workspace, target)
                    if first_method == "ensure_flat_for_disable":
                        acquired.set()
                        if not await asyncio.to_thread(release.wait, 4.0):
                            raise TimeoutError("Không được nhả L1 sau disable")

            service._lifecycle_guard_factory = lambda session: HeldGuard(factory(session))
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    request, first_client, first_role, "POST", first_path, first_body
                )
                assert acquired.wait(3.0), "Thao tác đầu không giữ được advisory L1"
                second = pool.submit(
                    request, second_client, second_role, "POST", second_path, second_body
                )
                queued = wait_for_advisory_waiter()
                release.set()
                first_result = first.result(timeout=8)
                second_result = second.result(timeout=8)
            return {
                "first": first_result,
                "second": second_result,
                "queued_on_l1": queued,
            }

    bot_path = f"/api/v1/bots/{bot_id}"
    result: dict[str, Any] = {}
    result["enable_pause"] = race(
        "lock_workspace_for_enable",
        "owner",
        f"{bot_path}/enable",
        "operator",
        f"{bot_path}/runtime/pause",
        {"reason": "E2E race enable so với pause"},
        {"reason": "E2E race enable so với pause"},
    )
    with TestClient(create_app(settings)) as client:
        result["resume"] = request(
            client,
            "owner",
            "POST",
            f"{bot_path}/runtime/resume",
            {"reason": "Chuẩn bị race disable", "expected_revision": 1},
        )
    result["disable_pause"] = race(
        "ensure_flat_for_disable",
        "owner",
        f"{bot_path}/disable",
        "operator",
        f"{bot_path}/runtime/pause",
        {"reason": "E2E race disable so với pause"},
        {"reason": "E2E race disable so với pause"},
    )
    with TestClient(create_app(settings)) as client:
        result["final_status"] = request(client, "owner", "GET", f"{bot_path}/runtime")
    return result


async def mb07b_enable_race_runtime_setup() -> dict[str, Any]:
    """Tạo runtime READY/VERIFIED, mở rồi đóng một vị thế để account phẳng."""
    async with runtime_app() as app:
        submitted = await entry(
            app,
            "a",
            0,
            1,
            name="main",
        )
        filled = await fill(app, "a", submitted)
        closed = await stop_out(app, "a", submitted, filled["trade_id"], name="main")
    return {"submitted": submitted, "filled": filled, "closed": closed}


def mb07b_enable_race_prepare() -> dict[str, Any]:
    """Đưa bot A về DISABLED nhưng đủ điều kiện enable, đồng thời giữ runtime phẳng."""
    from fastapi.testclient import TestClient

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    workspace_id = _ws("main")["workspace"]
    bot_id = _ws("main")["bots"]["a"]
    from pathlib import Path

    secret_file = Path("/tmp/mb07b-enable-race-prepare-secret")
    secret_file.write_text(TG_SECRET, encoding="utf-8")
    settings = AppSettings(
        telegram_workspace_id=UUID(workspace_id),
        telegram_webhook_secret_file=secret_file,
        telegram_bot_username="crypidian_test_bot",
    )
    with TestClient(create_app(settings)) as client:
        base = f"/api/v1/bots/{bot_id}"
        draft = _call(
            client,
            "owner",
            "POST",
            f"{base}/config-drafts",
            {"overlay": {"schema_version": 1}, "expected_parent_version_id": None},
        )
        draft_id = draft["body"].get("draft", {}).get("id")
        if draft_id is None:
            return {"draft": draft, "published": {"status": 0}}
        validated = _call(client, "owner", "POST", f"{base}/config-drafts/{draft_id}/validate")
        published = _call(
            client,
            "owner",
            "POST",
            f"{base}/config-drafts/{draft_id}/publish",
            {"expected_revision": 1},
        )
        disabled = _call(
            client,
            "owner",
            "POST",
            f"{base}/disable",
            {"reason": "Chuẩn bị race enable và pause"},
        )
    return {
        "draft": draft,
        "validation": validated,
        "published": published,
        "disabled": disabled,
    }


PHASES.update(
    {
        "mb07a_auth_seed": mb07a_auth_seed,
        "mb07a_admin_pause": mb07a_admin_pause,
        "mb07a_admin_kill": mb07a_admin_kill,
        "mb07a_release_and_telegram": mb07a_release_and_telegram,
        "mb07a_inspect": mb07a_inspect,
        "mb07b_telegram_binding_lifecycle": mb07b_telegram_binding_lifecycle,
        "mb07b_telegram_lock_seed": mb07b_telegram_lock_seed,
        "mb07b_telegram_lock_webhook": mb07b_telegram_lock_webhook,
        "mb07b_enable_race_runtime_setup": mb07b_enable_race_runtime_setup,
        "mb07b_enable_race_prepare": mb07b_enable_race_prepare,
        "mb07b_enable_pause_disable_race": mb07b_enable_pause_disable_race,
    }
)
_main()
