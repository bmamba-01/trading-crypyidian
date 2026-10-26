"""MB-02 Compose E2E: overlay bot version hóa, Admin API và PAPER runtime khi có vị thế mở."""

import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pyotp
from scripts.docker_image import build_image_reference, ensure_image_available

# Bootstrap ngắn đọc gói JSON từ stdin: Windows giới hạn dòng lệnh 32.767 ký tự.
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB02_PHASE=p['phase'],MB02_STATE=p['state']);"
    "exec(compile(p['code'],'multibot_config.py','exec'),{'__name__':'__main__'})"
)
OWNER_EMAIL = "owner@example.test"
OWNER_PASSWORD = "a long owner password for test"
HARD_CAPS = {
    "max_aggregate_open_risk": "250",
    "max_notional": "10000",
    "max_leverage": "5",
    "max_positions": 2,
    "max_symbol_notional": "2000",
    "max_concentration_fraction": "0.2",
    "max_daily_loss": "200",
    "max_rolling_loss": "500",
    # Seed PAPER được tạo ở phase trước; read model lấy as_of = thời điểm tạo seed.
    "max_account_age_seconds": 3600,
    "max_data_age_seconds": 300,
}


def test_bot_overlay_versions_admin_api_and_open_paper_position() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-mb02-e2e-{uuid.uuid4().hex[:10]}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image, "TRADING_BOT_WEB_PORT": str(port)}
    base = ["docker", "compose", "--profile", "paper", "--project-name", project]
    base += ["-f", "compose.yaml"]

    def compose(*args, success=True, stdin=None):
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        assert (result.returncode == 0) is success, (
            args[:6],
            result.stdout[-5000:],
            result.stderr[-5000:],
        )
        return result

    probe = (root / "tests/e2e/probes/multibot_config.py").read_text(encoding="utf-8")

    def run_probe(phase, state, service):
        payload = json.dumps({"phase": phase, "state": json.dumps(state), "code": probe})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            service,
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        return json.loads(
            next(line for line in reversed(result.stdout.splitlines()) if line.startswith("{"))
        )

    def psql(sql, success=True):
        return compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            sql,
            success=success,
        )

    client = build_opener(HTTPCookieProcessor(CookieJar()))

    def api(method, path, payload=None, headers=None):
        request = Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with client.open(request, timeout=20) as response:
                body = response.read()
                return response.status, json.loads(body) if body else {}
        except HTTPError as error:
            body = error.read()
            return error.code, json.loads(body) if body else {}

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180")
        # Downgrade/upgrade khi chưa có version bot: migration đảo chiều sạch.
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "c7a90d3f8e21")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        current = compose("run", "--rm", "--no-deps", "migrator", "alembic", "current")
        # Head tích hợp gồm cả MB-05 (registry runtime); downgrade ở trên vẫn kiểm tra luồng
        # đảo chiều sạch qua migration MB-05 (chưa có hàng runtime).
        assert "e4f6a8c0d2b5" in current.stdout
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")

        # --- Owner, MFA, risk profile và root settings ------------------------------------
        status, bootstrap = api(
            "POST",
            "/api/v1/auth/bootstrap",
            {
                "workspace_name": "MB02 workspace",
                "workspace_slug": "mb02-workspace",
                "email": OWNER_EMAIL,
                "password": OWNER_PASSWORD,
                "mode": "PAPER",
            },
            {"X-Bootstrap-Token": (root / ".secrets/bootstrap_token").read_text()},
        )
        assert status == 201, bootstrap
        workspace_id = bootstrap["workspace_id"]
        secret = parse_qs(urlparse(bootstrap["mfa_setup_uri"]).query)["secret"][0]
        previous = pyotp.TOTP(secret).at(datetime.now(UTC) - timedelta(seconds=30))
        status, _ = api(
            "POST",
            "/api/v1/auth/bootstrap/verify",
            {"workspace_slug": "mb02-workspace", "email": OWNER_EMAIL, "code": previous},
        )
        assert status == 200
        status, login = api(
            "POST",
            "/api/v1/auth/login",
            {
                "workspace_slug": "mb02-workspace",
                "email": OWNER_EMAIL,
                "password": OWNER_PASSWORD,
                "code": pyotp.TOTP(secret).now(),
            },
        )
        assert status == 200, login
        csrf = {"X-CSRF-Token": str(login["csrf_token"])}
        status, risk_profile = api(
            "POST",
            "/api/v1/risk-profiles",
            {
                "name": "MB02 caps",
                "max_risk_fraction": "0.01",
                "settings": {"schema_version": 1, "hard_caps": HARD_CAPS},
            },
            csrf,
        )
        assert status == 201, risk_profile
        root_settings = {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 30,
            },
            "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
            "strategy": {
                "default_release": "h03-v1",
                "parameters": {},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {"profile_version_id": risk_profile["id"]},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
        status, root_draft = api(
            "POST",
            "/api/v1/settings/drafts",
            {"settings": root_settings, "expected_parent_version_id": None},
            csrf,
        )
        assert status == 201, root_draft
        status, root_version = api(
            "POST",
            f"/api/v1/settings/drafts/{root_draft['draft']['id']}/publish",
            {"expected_revision": 1},
            csrf,
        )
        assert status == 200, root_version
        root_before = api("GET", "/api/v1/settings/current")[1]["current"]

        from trading_bot.domain.strategy.registry import StrategyRegistry

        registry = StrategyRegistry()
        psql(
            "INSERT INTO strategy_release(id,workspace_id,name,version,status,recipe,content_hash) "
            f"VALUES (gen_random_uuid(),'{workspace_id}','h03-v1',1,'PUBLISHED','{{}}'::jsonb,"
            f"'{registry.get_release('h03-v1').content_hash}'),"
            f"(gen_random_uuid(),'{workspace_id}','h01-v1',1,'RETIRED','{{}}'::jsonb,"
            f"'{registry.get_release('h01-v1').content_hash}')"
        )
        state = {"workspace_id": workspace_id}
        assert run_probe("instrument", state, "admin-web") == {"instruments": 2}
        state |= run_probe("seed", state, "trading-runtime")
        bot_id = state["bot_id"]

        # --- Admin API: bot list/detail/create, CSRF, cross-scope ------------------------
        status, bots = api("GET", "/api/v1/bots")
        assert status == 200
        assert bot_id in {bot["id"] for bot in bots["bots"]}
        status, detail = api("GET", f"/api/v1/bots/{bot_id}")
        assert status == 200
        assert detail["bot"]["current_config"] is None
        assert api("GET", f"/api/v1/bots/{uuid.uuid4()}")[0] == 404
        created = api("POST", "/api/v1/bots", {"name": "mb02-new", "max_notional": "5000"}, csrf)
        assert created[0] == 201, created
        assert created[1]["bot"]["status"] == "DISABLED"
        assert created[1]["bot"]["account"]["mode"] == "PAPER"
        replay = api("POST", "/api/v1/bots", {"name": "mb02-new", "max_notional": "5000"}, csrf)
        assert replay[0] == 200
        assert replay[1]["bot"]["id"] == created[1]["bot"]["id"]
        conflict = api("POST", "/api/v1/bots", {"name": "mb02-new", "max_notional": "6000"}, csrf)
        assert conflict[0] == 409
        overlay_v1 = {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.005"}}
        no_csrf = api("POST", f"/api/v1/bots/{bot_id}/config-drafts", {"overlay": overlay_v1})
        assert no_csrf[0] == 403
        foreign = api(
            "POST", f"/api/v1/bots/{uuid.uuid4()}/config-drafts", {"overlay": overlay_v1}, csrf
        )
        assert foreign[0] == 404
        secret_draft = api(
            "POST",
            f"/api/v1/bots/{bot_id}/config-drafts",
            {"overlay": {"schema_version": 1, "risk": {"api_key": "DO_NOT_ECHO_MB02"}}},
            csrf,
        )
        assert secret_draft[0] == 422
        assert "DO_NOT_ECHO_MB02" not in json.dumps(secret_draft[1])
        venue_draft = api(
            "POST",
            f"/api/v1/bots/{bot_id}/config-drafts",
            {"overlay": {"schema_version": 1, "venue": "BINANCE_USDM"}},
            csrf,
        )
        assert venue_draft[0] == 422

        # --- Draft/validate/publish v1, invalid overlays và idempotency ------------------
        def draft(overlay, parent, key=None):
            headers = csrf | ({"Idempotency-Key": key} if key else {})
            return api(
                "POST",
                f"/api/v1/bots/{bot_id}/config-drafts",
                {"overlay": overlay, "expected_parent_version_id": parent},
                headers,
            )

        def publish(draft_id, revision, key=None):
            headers = csrf | ({"Idempotency-Key": key} if key else {})
            return api(
                "POST",
                f"/api/v1/bots/{bot_id}/config-drafts/{draft_id}/publish",
                {"expected_revision": revision},
                headers,
            )

        status, first = draft(overlay_v1, None, "mb02-draft-v1-key")
        assert status == 201, first
        status, retry = draft(overlay_v1, None, "mb02-draft-v1-key")
        assert status == 201
        assert retry["draft"]["id"] == first["draft"]["id"]
        reused = draft({"schema_version": 1}, None, "mb02-draft-v1-key")
        assert reused[0] == 422
        v1_draft = first["draft"]
        assert v1_draft["base_settings_version_id"] == root_before["id"]
        validation = api(
            "POST", f"/api/v1/bots/{bot_id}/config-drafts/{v1_draft['id']}/validate", headers=csrf
        )
        assert validation == (200, {"valid": True, "errors": [], "warnings": []})
        loose = draft({"schema_version": 1, "risk": {"max_positions": 3}}, None)[1]["draft"]
        loose_validation = api(
            "POST", f"/api/v1/bots/{bot_id}/config-drafts/{loose['id']}/validate", headers=csrf
        )[1]
        assert loose_validation["valid"] is False
        assert loose_validation["errors"][0]["field"] == "risk.max_positions"
        status, loose_publish = publish(loose["id"], 1)
        assert status == 422
        assert loose_publish["detail"]["errors"][0]["field"] == "risk.max_positions"
        retired = draft({"schema_version": 1, "strategy": {"release_id": "h01-v1"}}, None)
        status, retired_publish = publish(retired[1]["draft"]["id"], 1)
        assert status == 422
        assert retired_publish["detail"]["errors"][0]["field"] == "strategy.release_id"
        stale_revision = publish(v1_draft["id"], 2)
        assert stale_revision[0] == 409
        status, v1 = publish(v1_draft["id"], 1, "mb02-publish-v1-key")
        assert status == 200, v1
        v1 = v1["version"]
        assert v1["status"] == "PUBLISHED"
        assert v1["effective_at"]
        status, v1_retry = publish(v1_draft["id"], 1, "mb02-publish-v1-key")
        assert status == 200
        assert v1_retry["version"] == v1
        assert publish(v1_draft["id"], 2, "mb02-publish-v1-key")[0] == 422
        stale_parent = publish(loose["id"], 1)
        assert stale_parent[0] == 409

        # --- Runtime: entry đầu dùng overlay v1 và vị thế PAPER còn mở -------------------
        state["open"] = run_probe("open", state, "trading-runtime")
        opened = state["open"]
        assert opened["intent_bot_config_version_id"] == v1["id"]
        # Root cho 1% equity (100 USDT); overlay v1 siết còn 0,5% nên risk budget là 50.
        assert Decimal(opened["risk_budget"]) == Decimal("50")
        assert opened["trade_status"] == "OPEN"
        assert opened["settings_version_id"] == root_before["id"]

        # --- Publish v2 khi vị thế đang mở; root không đổi -------------------------------
        overlay_v2 = {
            "schema_version": 1,
            "universe": {"symbols": ["ETHUSDT"]},
            "risk": {"per_trade_risk_fraction": "0.004"},
        }
        status, second = draft(overlay_v2, v1["id"])
        assert status == 201, second
        status, v2 = publish(second["draft"]["id"], 1, "mb02-publish-v2-key")
        assert status == 200, v2
        v2 = v2["version"]
        assert v2["parent_version_id"] == v1["id"]
        root_after = api("GET", "/api/v1/settings/current")[1]["current"]
        assert root_after["id"] == root_before["id"]
        assert root_after["settings_hash"] == root_before["settings_hash"]

        after = run_probe("after", state, "trading-runtime")
        assert after["pinned_version_id"] == v1["id"]
        assert after["pinned_effective_hash"] == opened["intent_effective_config_hash"]
        assert after["current_version_id"] == v2["id"]
        assert after["hashes_verify"] is True
        assert after["current_symbols"] == ["ETHUSDT"]
        assert after["current_per_trade"] == "0.004"
        assert after["root_caps_enforced"] is True
        assert after["runtime_visible_statuses"] == ["PUBLISHED"]
        assert after["runtime_visible_count"] == 2
        assert after["runtime_update"] == "42501"
        assert after["unbound_runtime_count"] == 0
        assert after["new_entry_outcome"] == "NO_TRADE"
        assert after["new_entry_code"] == "BOT_MARKET_SCOPE_MISMATCH"
        assert after["exit_reason"] == "STOP_LOSS"
        assert after["trade_closed"] is True
        assert after["monitor_pins"] == [
            {"release": "h03-v1", "settings_version_id": root_before["id"], "parameters": []}
        ]
        assert after["intent_bot_config_version_id"] == v1["id"]
        assert after["trade_status"] == "CLOSED"
        assert after["trade_settings_version_id"] == root_before["id"]
        assert after["intent_count"] == 1

        # --- Rollback có lineage và idempotent ------------------------------------------
        def rollback(version, current_id, key):
            return api(
                "POST",
                f"/api/v1/bots/{bot_id}/config-versions/{version}/rollback",
                {"expected_current_version_id": current_id, "reason": "MB02 rollback"},
                csrf | {"Idempotency-Key": key},
            )

        assert rollback(v1["version"], str(uuid.uuid4()), "mb02-rollback-stale")[0] == 409
        status, rolled = rollback(v1["version"], v2["id"], "mb02-rollback-key")
        assert status == 200, rolled
        rolled = rolled["version"]
        assert rolled["source_version_id"] == v1["id"]
        assert rolled["parent_version_id"] == v2["id"]
        assert rolled["overlay_hash"] == v1["overlay_hash"]
        status, rolled_retry = rollback(v1["version"], v2["id"], "mb02-rollback-key")
        assert status == 200
        assert rolled_retry["version"]["id"] == rolled["id"]
        status, detail = api("GET", f"/api/v1/bots/{bot_id}")
        assert detail["bot"]["current_config"]["id"] == rolled["id"]

        # --- Audit/outbox, RLS admin theo role/grant, bất biến và quyền DB ---------------
        audit = psql(
            "SELECT action || ':' || count(*) FROM audit_event "
            f"WHERE workspace_id = '{workspace_id}' AND action LIKE 'bot.%' "
            "GROUP BY action ORDER BY action"
        ).stdout.split()
        # 4 draft qua API (v1, loose, retired, v2); retry/replay không ghi audit lần hai.
        assert audit == [
            "bot.config.draft_created:4",
            "bot.config.published:2",
            "bot.config.rolled_back:1",
            "bot.created:1",
        ], audit
        outbox = psql(
            "SELECT count(*) FROM outbox_event "
            f"WHERE workspace_id = '{workspace_id}' AND event_type LIKE 'bot.%'"
        ).stdout.strip()
        assert outbox == "8"
        owner_membership = psql(
            "SELECT m.id FROM user_membership m JOIN user_account u ON u.id = m.user_id "
            f"WHERE u.email_normalized = '{OWNER_EMAIL}'"
        ).stdout.strip()
        operator_user, operator_membership = uuid.uuid4(), uuid.uuid4()
        psql(
            "INSERT INTO user_account(id,email_normalized,password_hash,status) "
            f"VALUES ('{operator_user}','operator@example.test','x','ACTIVE'); "
            "INSERT INTO user_membership(id,workspace_id,user_id,status) "
            f"VALUES ('{operator_membership}','{workspace_id}','{operator_user}','ACTIVE'); "
            "INSERT INTO role_binding(id,workspace_id,membership_id,role) "
            f"VALUES (gen_random_uuid(),'{workspace_id}','{operator_membership}','operator')"
        )
        rls_state = state | {
            "owner_membership_id": owner_membership,
            "operator_membership_id": str(operator_membership),
        }
        denied = run_probe("admin_rls", rls_state, "admin-web")
        # 5 version: v1, loose, retired, v2, rollback; 4 key: draft, publish v1/v2, rollback.
        assert denied == {
            "owner_versions": 5,
            "owner_requests": 4,
            "operator_versions": 0,
            "foreign_workspace_versions": 0,
            "no_member_versions": 0,
        }, denied
        psql(
            "INSERT INTO bot_grant(id,workspace_id,bot_id,membership_id,permission,granted_by) "
            f"VALUES (gen_random_uuid(),'{workspace_id}','{bot_id}','{operator_membership}',"
            f"'bot:read','{owner_membership}')"
        )
        granted = run_probe("admin_rls", rls_state, "admin-web")
        assert granted["operator_versions"] == denied["owner_versions"]
        immutable = psql(
            "UPDATE bot_config_version SET revision = revision WHERE status = 'PUBLISHED'",
            success=False,
        )
        assert "MB02_BOT_CONFIG_VERSION_IMMUTABLE" in immutable.stderr
        append_only = psql("DELETE FROM bot_config_version", success=False)
        assert "MB02_BOT_CONFIG_VERSION_APPEND_ONLY" in append_only.stderr
        direct_publish = psql(
            "INSERT INTO bot_config_version (id, workspace_id, bot_id, account_id, version, "
            "schema_version, overlay_json, overlay_hash, base_settings_version_id, "
            "base_settings_hash, status, revision, created_by, updated_by, approved_by, "
            "published_at, effective_at) SELECT gen_random_uuid(), workspace_id, bot_id, "
            "account_id, 99, schema_version, overlay_json, overlay_hash, "
            "base_settings_version_id, base_settings_hash, 'PUBLISHED', 1, created_by, "
            "updated_by, approved_by, published_at, effective_at FROM bot_config_version "
            "WHERE status = 'PUBLISHED' LIMIT 1",
            success=False,
        )
        assert "MB02_BOT_CONFIG_INSERT_MUST_BE_DRAFT" in direct_publish.stderr
        request_guard = psql("UPDATE bot_config_request SET operation = operation", success=False)
        assert "MB02_BOT_CONFIG_REQUEST_APPEND_ONLY" in request_guard.stderr
        privileges = psql(
            "SELECT string_agg(r || ':' || t || ':' || p || '=' || "
            "has_table_privilege(r, t, p)::text, ',' ORDER BY r, t, p) FROM ("
            "VALUES ('crypidian_trading_runtime','bot_config_version','SELECT'),"
            "('crypidian_trading_runtime','bot_config_version','INSERT'),"
            "('crypidian_trading_runtime','bot_config_version','UPDATE'),"
            "('crypidian_trading_runtime','bot_config_request','SELECT'),"
            "('crypidian_admin_web','bot_config_version','SELECT'),"
            "('crypidian_admin_web','bot_config_version','INSERT'),"
            "('crypidian_admin_web','bot_config_version','UPDATE'),"
            "('crypidian_admin_web','bot_config_version','DELETE'),"
            "('crypidian_admin_web','bot_config_request','INSERT'),"
            "('crypidian_admin_web','bot_config_request','UPDATE'),"
            "('crypidian_execution_gateway','bot_config_version','SELECT'),"
            "('crypidian_ingestion_worker','bot_config_version','SELECT'),"
            "('crypidian_trainer_worker','bot_config_version','SELECT')) AS v(r, t, p)"
        ).stdout.strip()
        expected = {
            "crypidian_admin_web:bot_config_request:INSERT": "true",
            "crypidian_admin_web:bot_config_request:UPDATE": "false",
            "crypidian_admin_web:bot_config_version:DELETE": "false",
            "crypidian_admin_web:bot_config_version:INSERT": "true",
            "crypidian_admin_web:bot_config_version:SELECT": "true",
            "crypidian_admin_web:bot_config_version:UPDATE": "true",
            "crypidian_execution_gateway:bot_config_version:SELECT": "false",
            "crypidian_ingestion_worker:bot_config_version:SELECT": "false",
            "crypidian_trading_runtime:bot_config_request:SELECT": "false",
            "crypidian_trading_runtime:bot_config_version:INSERT": "false",
            "crypidian_trading_runtime:bot_config_version:SELECT": "true",
            "crypidian_trading_runtime:bot_config_version:UPDATE": "false",
            "crypidian_trainer_worker:bot_config_version:SELECT": "false",
        }
        assert dict(item.split("=") for item in privileges.split(",")) == expected
        rls_flags = psql(
            "SELECT string_agg(relname || ':' || relrowsecurity || relforcerowsecurity, ',' "
            "ORDER BY relname) FROM pg_class WHERE relname IN "
            "('bot_config_version','bot_config_request')"
        ).stdout.strip()
        assert rls_flags == "bot_config_request:truetrue,bot_config_version:truetrue"
        guard = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "c7a90d3f8e21",
            success=False,
        )
        assert "MB02_DOWNGRADE_REQUIRES_EXPORT_OF_BOT_CONFIG" in guard.stderr
    finally:
        compose("down", "--volumes", "--remove-orphans")
