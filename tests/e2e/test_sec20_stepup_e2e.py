"""E2E PostgreSQL thật cho CRY-139 (SEC-20: Step-up theo hành động, TTL ≤ 5p, WYSIWYS)."""
# ruff: noqa: E501

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_CODE = r"""
import asyncio
import base64
import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import psycopg
import pyotp
from fastapi.testclient import TestClient

from trading_bot.adapters.database.engine import create_database_engine, create_session_factory
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.app import create_app
from trading_bot.auth.action_challenge import (
    ACTION_BOT_RUNTIME_RESUME,
    ACTION_RISK_PROFILE_CREATE,
    ACTION_SETTINGS_PUBLISH,
    ACTION_TELEGRAM_LINK_CREATE,
    ACTION_WORKSPACE_FLATTEN_CONFIRM,
    ACTION_WORKSPACE_RUNTIME_RELEASE,
    ActionChallengeError,
    ActionChallengeService,
    bot_runtime_resume_payload,
    flatten_confirm_payload,
    risk_profile_create_payload,
    settings_publish_payload,
    telegram_link_payload,
    workspace_release_payload,
)
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.auth.security import PasswordManager, TotpManager
from trading_bot.auth.service import AuthService
from trading_bot.config.settings import AppSettings
from trading_bot.domain.strategy.registry import StrategyRegistry


def _db_url(user: str, password_file: str) -> str:
    pw = Path(password_file).read_text(encoding="utf-8").strip()
    return f"postgresql+psycopg://{user}:{pw}@postgres:5432/crypidian"


class VirtualClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


async def setup_workspace_and_check_rls(
    *, key_file: Path, secret_dir: Path
) -> dict[str, object]:
    admin_pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    runtime_pw = Path("/run/secrets/database_trading_runtime_password").read_text(encoding="utf-8").strip()

    secret_provider = LocalMfaSecretProvider(key_file=key_file, storage_directory=secret_dir)
    admin_engine = create_database_engine(
        _db_url("crypidian_admin_web", "/run/secrets/database_admin_web_password")
    )
    admin_sessions = create_session_factory(admin_engine)

    auth_service = AuthService(admin_sessions, secret_provider)
    slug = f"sec20-{uuid4().hex[:8]}"
    owner_email = f"owner-{uuid4().hex[:8]}@example.test"
    owner_pw = "Correct Horse Battery Staple 2026!"
    boot = await auth_service.bootstrap(
        workspace_name="SEC20 E2E Workspace",
        workspace_slug=slug,
        email=owner_email,
        password=owner_pw,
        mode="PAPER",
    )
    ws_id = boot.workspace_id
    owner_totp_secret = parse_qs(urlparse(boot.mfa_setup_uri).query)["secret"][0]
    t0 = datetime.now(UTC)
    await auth_service.confirm_bootstrap(
        workspace_slug=slug,
        email=owner_email,
        code=pyotp.TOTP(owner_totp_secret).at(t0 - timedelta(seconds=30)),
    )

    login_a = await auth_service.login(
        workspace_slug=slug,
        email=owner_email,
        password=owner_pw,
        code=pyotp.TOTP(owner_totp_secret).at(t0),
    )
    login_b = await auth_service.login(
        workspace_slug=slug,
        email=owner_email,
        password=owner_pw,
        code=pyotp.TOTP(owner_totp_secret).at(t0 + timedelta(seconds=30)),
    )

    strat_user = uuid4()
    strat_mem = uuid4()
    strat_secret = TotpManager().generate_secret()
    strat_ref = secret_provider.store_mfa_secret(ws_id, strat_secret)
    strat_email = f"strat-{uuid4().hex[:8]}@example.test"
    strat_pw = "Strategy Admin Battery Staple 2026!"
    strat_pw_hash = PasswordManager().hash(strat_pw)
    acc_id = uuid4()
    bot_id = uuid4()

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (strat_user, strat_email, strat_pw_hash),
        )
        conn.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (strat_mem, ws_id, strat_user),
        )
        conn.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (%s, %s, %s, 'strategy_admin')",
            (uuid4(), ws_id, strat_mem),
        )
        conn.execute(
            "INSERT INTO secret_reference (id, workspace_id, provider, reference_key, status) "
            "VALUES (%s, %s, 'local_mfa', %s, 'ACTIVE')",
            (uuid4(), ws_id, strat_ref),
        )
        conn.execute(
            "INSERT INTO mfa_factor (id, workspace_id, membership_id, factor_type, secret_reference, verified_at) "
            "VALUES (%s, %s, %s, 'TOTP', %s, %s)",
            (uuid4(), ws_id, strat_mem, strat_ref, t0 - timedelta(days=1)),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, status, max_notional) "
            "VALUES (%s, %s, 'sec20-acc', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 10000)",
            (acc_id, ws_id),
        )
        conn.execute(
            "INSERT INTO bot_instance (id, workspace_id, trading_account_id, name, status) "
            "VALUES (%s, %s, %s, 'sec20-bot', 'ENABLED') ON CONFLICT DO NOTHING",
            (bot_id, ws_id, acc_id),
        )
        bot_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws_id, acc_id),
        ).fetchone()[0]
        registry = StrategyRegistry()
        conn.execute(
            "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, content_hash) "
            "VALUES (%s, %s, 'h03-v1', 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (uuid4(), ws_id, registry.get_release("h03-v1").content_hash),
        )

    login_strat = await auth_service.login(
        workspace_slug=slug,
        email=strat_email,
        password=strat_pw,
        code=pyotp.TOTP(strat_secret).at(t0 - timedelta(seconds=30)),
    )

    # Kiểm tra runtime bị chặn và DB CHECK constraint từ chối TTL > 300s
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_trading_runtime", password=runtime_pw, autocommit=True
    ) as rt_conn:
        rt_conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws_id),))
        denied = False
        try:
            rt_conn.execute("SELECT count(*) FROM action_challenge")
        except psycopg.errors.InsufficientPrivilege:
            denied = True
        assert denied, "crypidian_trading_runtime must NOT access action_challenge"

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as su_conn:
        ttl_rejected = False
        try:
            su_conn.execute(
                "INSERT INTO action_challenge (id, created_at, workspace_id, membership_id, session_id, action, payload_hash, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, 'settings.publish', %s, %s)",
                (uuid4(), t0, ws_id, login_a.principal.membership_id, login_a.principal.session_id, "a" * 64, t0 + timedelta(seconds=301)),
            )
        except psycopg.errors.CheckViolation:
            ttl_rejected = True
        assert ttl_rejected

    await admin_engine.dispose()
    return {
        "ws_id": ws_id,
        "bot_id": bot_id,
        "t0": t0,
        "owner_totp_secret": owner_totp_secret,
        "strat_secret": strat_secret,
        "strat_mem": strat_mem,
        "login_a": login_a,
        "login_b": login_b,
        "login_strat": login_strat,
    }


async def test_postgres_atomic_race(
    *,
    key_file: Path,
    secret_dir: Path,
    principal,
    totp_secret: str,
    clock: VirtualClock,
    rp_payload: dict[str, object],
) -> None:
    secret_provider = LocalMfaSecretProvider(key_file=key_file, storage_directory=secret_dir)
    admin_engine = create_database_engine(
        _db_url("crypidian_admin_web", "/run/secrets/database_admin_web_password")
    )
    admin_sessions = create_session_factory(admin_engine)
    challenge_svc = ActionChallengeService(admin_sessions, secret_provider, clock=clock)
    created = await challenge_svc.create_challenge(
        principal,
        action=ACTION_RISK_PROFILE_CREATE,
        payload=rp_payload,
        ttl_seconds=300,
    )
    race_code = pyotp.TOTP(totp_secret).at(clock())
    race_results = await asyncio.gather(
        challenge_svc.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_RISK_PROFILE_CREATE,
            payload=rp_payload,
            code=race_code,
        ),
        challenge_svc.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_RISK_PROFILE_CREATE,
            payload=rp_payload,
            code=race_code,
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in race_results) == 1
    assert sum(isinstance(r, ActionChallengeError) and r.status_code == 403 for r in race_results) == 1
    await admin_engine.dispose()


def main() -> None:
    admin_web_pw = Path("/run/secrets/database_admin_web_password").read_text(encoding="utf-8").strip()
    tmpdir = Path(tempfile.mkdtemp(prefix="sec20-e2e-"))
    key_file = tmpdir / "mfa.key"
    key_file.write_text(base64.b64encode(secrets.token_bytes(32)).decode("ascii"), encoding="utf-8")
    secret_dir = tmpdir / "secrets"
    secret_dir.mkdir()

    seeded = asyncio.run(setup_workspace_and_check_rls(key_file=key_file, secret_dir=secret_dir))
    ws_id: UUID = seeded["ws_id"]  # type: ignore[assignment]
    bot_id: UUID = seeded["bot_id"]  # type: ignore[assignment]
    t0: datetime = seeded["t0"]  # type: ignore[assignment]
    owner_totp_secret: str = seeded["owner_totp_secret"]  # type: ignore[assignment]
    strat_secret: str = seeded["strat_secret"]  # type: ignore[assignment]
    strat_mem: UUID = seeded["strat_mem"]  # type: ignore[assignment]
    login_a = seeded["login_a"]
    login_b = seeded["login_b"]
    login_strat = seeded["login_strat"]

    clock = VirtualClock(t0 + timedelta(seconds=65))
    settings = AppSettings(
        app_env="test",
        action_step_up_required=True,
        database_user="crypidian_admin_web",
        database_password_file=Path("/run/secrets/database_admin_web_password"),
        database_host="postgres",
        database_name="crypidian",
        mfa_encryption_key_file=key_file,
        mfa_secret_directory=secret_dir,
        telegram_bot_username="crypidian_test_bot",
    )
    app = create_app(settings)
    cookie_name = session_cookie_name(settings)

    def _client_for(login_res) -> TestClient:
        c = TestClient(app)
        c.cookies.set(cookie_name, login_res.cookie_value)
        c.headers.update({"X-CSRF-Token": login_res.csrf_token})
        return c

    rp_payload = risk_profile_create_payload(
        name="SEC20 Caps",
        max_risk_fraction=Decimal("0.01"),
        settings={
            "schema_version": 1,
            "hard_caps": {
                "max_leverage": "5",
                "max_notional": "10000",
                "max_symbol_notional": "5000",
                "max_aggregate_open_risk": "250",
                "max_concentration_fraction": "0.5",
                "max_positions": 4,
                "max_daily_loss": "500",
                "max_rolling_loss": "1000",
                "max_account_age_seconds": 3600,
                "max_data_age_seconds": 300,
            },
        },
    )

    with TestClient(app) as base_client:
        del base_client
        # Mọi service trong lifespan dùng chung instance ActionChallengeService này
        app.state.action_challenge_service._clock = clock

        c_owner_a = _client_for(login_a)
        c_owner_b = _client_for(login_b)
        c_strat = _client_for(login_strat)

        # 3a. SECT-18: Thiếu challenge -> 403
        no_chal = c_owner_a.post("/api/v1/risk-profiles", json=rp_payload)
        assert no_chal.status_code == 403 and no_chal.json()["detail"]["code"] == "STEP_UP_REQUIRED"

        # Tạo challenge từ Session A -> Session B không dùng được
        ch1 = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={"action": ACTION_RISK_PROFILE_CREATE, "payload": rp_payload, "ttl_seconds": 300},
        )
        assert ch1.status_code == 201, ch1.text
        cid1 = ch1.json()["challenge"]["id"]
        b_try = c_owner_b.post(
            f"/api/v1/auth/challenges/{cid1}/verify",
            json={"code": pyotp.TOTP(owner_totp_secret).at(clock())},
        )
        assert b_try.status_code == 403 and b_try.json()["detail"]["code"] == "STEP_UP_SESSION_MISMATCH"

        # Kiểm tra column-level GRANT và RLS trên action_challenge với crypidian_admin_web
        with psycopg.connect(
            host="postgres", dbname="crypidian", user="crypidian_admin_web", password=admin_web_pw, autocommit=True
        ) as web_conn:
            web_conn.execute(
                "SELECT set_config('app.workspace_id', %s, false), set_config('app.membership_id', %s, false)",
                (str(ws_id), str(login_a.principal.membership_id)),
            )
            assert web_conn.execute("SELECT count(*) FROM action_challenge WHERE id = %s", (UUID(cid1),)).fetchone()[0] == 1
            col_blocked = False
            try:
                web_conn.execute("UPDATE action_challenge SET payload_hash = %s WHERE id = %s", ("b" * 64, UUID(cid1)))
            except psycopg.errors.InsufficientPrivilege:
                col_blocked = True
            assert col_blocked, "crypidian_admin_web must NOT update immutable payload_hash column"
            web_conn.execute("SELECT set_config('app.membership_id', %s, false)", (str(strat_mem),))
            assert web_conn.execute("SELECT count(*) FROM action_challenge WHERE id = %s", (UUID(cid1),)).fetchone()[0] == 0

        # Sai 5 lần -> khóa/hủy challenge trong PostgreSQL
        for _ in range(4):
            bad = c_owner_a.post(f"/api/v1/auth/challenges/{cid1}/verify", json={"code": "918273"})
            assert bad.status_code == 403 and bad.json()["detail"]["code"] == "STEP_UP_INVALID_CODE"
        fifth = c_owner_a.post(f"/api/v1/auth/challenges/{cid1}/verify", json={"code": "918273"})
        assert fifth.status_code == 403 and fifth.json()["detail"]["code"] == "STEP_UP_CHALLENGE_LOCKED"
        sixth = c_owner_a.post(
            f"/api/v1/auth/challenges/{cid1}/verify",
            json={"code": pyotp.TOTP(owner_totp_secret).at(clock())},
        )
        assert sixth.status_code == 403 and sixth.json()["detail"]["code"] == "STEP_UP_CHALLENGE_LOCKED"

        # Tạo challenge mới, kiểm tra TTL hết hạn (> 5 phút)
        ch_exp = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={"action": ACTION_RISK_PROFILE_CREATE, "payload": rp_payload, "ttl_seconds": 60},
        )
        cid_exp = ch_exp.json()["challenge"]["id"]
        clock.advance(61)
        exp_res = c_owner_a.post(
            f"/api/v1/auth/challenges/{cid_exp}/verify",
            json={"code": pyotp.TOTP(owner_totp_secret).at(clock())},
        )
        assert exp_res.status_code == 403 and exp_res.json()["detail"]["code"] == "STEP_UP_CHALLENGE_EXPIRED"

        # Tạo risk profile thật qua challenge hợp lệ, kiểm tra payload tamper + replay bị chặn
        clock.advance(35)
        ch_rp = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={"action": ACTION_RISK_PROFILE_CREATE, "payload": rp_payload, "ttl_seconds": 300},
        )
        cid_rp = ch_rp.json()["challenge"]["id"]
        ver_rp = c_owner_a.post(
            f"/api/v1/auth/challenges/{cid_rp}/verify",
            json={"code": pyotp.TOTP(owner_totp_secret).at(clock())},
        )
        assert ver_rp.status_code == 200
        tok_rp = ver_rp.json()["authorization"]["step_up_token"]

        tampered_rp = c_owner_a.post(
            "/api/v1/risk-profiles",
            json={**rp_payload, "max_risk_fraction": "0.50"},
            headers={"X-Step-Up-Challenge-Id": cid_rp, "X-Step-Up-Token": tok_rp},
        )
        assert tampered_rp.status_code == 403 and tampered_rp.json()["detail"]["code"] == "STEP_UP_PAYLOAD_MISMATCH"

        created_rp = c_owner_a.post(
            "/api/v1/risk-profiles",
            json=rp_payload,
            headers={"X-Step-Up-Challenge-Id": cid_rp, "X-Step-Up-Token": tok_rp},
        )
        assert created_rp.status_code == 201, created_rp.text
        rp_id = created_rp.json()["id"]

        replay_rp = c_owner_a.post(
            "/api/v1/risk-profiles",
            json=rp_payload,
            headers={"X-Step-Up-Challenge-Id": cid_rp, "X-Step-Up-Token": tok_rp},
        )
        assert replay_rp.status_code == 403 and replay_rp.json()["detail"]["code"] == "STEP_UP_CHALLENGE_ALREADY_USED"

        # 3b. SECT-19: Publish root settings v1 (owner) -> strategy_admin đổi strategy.parameters + symbols
        root_settings = {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 30,
            },
            "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
            "strategy": {
                "default_release": "h03-v1",
                "parameters": {"ema_fast": 12},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {"profile_version_id": rp_id},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
        d1 = c_owner_a.post(
            "/api/v1/settings/drafts",
            json={"settings": root_settings, "expected_parent_version_id": None},
        )
        assert d1.status_code == 201, d1.text
        d1_obj = d1.json()["draft"]
        clock.advance(35)
        p1_payload = settings_publish_payload(
            draft_id=UUID(d1_obj["id"]),
            expected_revision=1,
            settings_hash=d1_obj["settings_hash"],
        )
        ch_s1 = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={"action": ACTION_SETTINGS_PUBLISH, "payload": p1_payload, "ttl_seconds": 300},
        )
        s1_res = c_owner_a.post(
            f"/api/v1/settings/drafts/{d1_obj['id']}/publish",
            json={"expected_revision": 1},
            headers={
                "X-Step-Up-Challenge-Id": ch_s1.json()["challenge"]["id"],
                "X-Step-Up-Code": pyotp.TOTP(owner_totp_secret).at(clock()),
            },
        )
        assert s1_res.status_code == 200, s1_res.text
        v1_id = s1_res.json()["version"]["id"]

        v2_settings = {
            **root_settings,
            "market_data": {**root_settings["market_data"], "symbols": ["BTCUSDT", "ETHUSDT"]},
            "strategy": {**root_settings["strategy"], "parameters": {"ema_fast": 21}},
        }
        d2 = c_strat.post(
            "/api/v1/settings/drafts",
            json={"settings": v2_settings, "expected_parent_version_id": v1_id},
        )
        assert d2.status_code == 201, d2.text
        d2_obj = d2.json()["draft"]
        no_step_s2 = c_strat.post(
            f"/api/v1/settings/drafts/{d2_obj['id']}/publish",
            json={"expected_revision": 1},
        )
        assert no_step_s2.status_code == 403 and no_step_s2.json()["detail"]["code"] == "STEP_UP_REQUIRED"

        p2_payload = settings_publish_payload(
            draft_id=UUID(d2_obj["id"]),
            expected_revision=1,
            settings_hash=d2_obj["settings_hash"],
        )
        ch_s2 = c_strat.post(
            "/api/v1/auth/challenges",
            json={"action": ACTION_SETTINGS_PUBLISH, "payload": p2_payload, "ttl_seconds": 300},
        )
        assert ch_s2.status_code == 201
        ok_s2 = c_strat.post(
            f"/api/v1/settings/drafts/{d2_obj['id']}/publish",
            json={"expected_revision": 1},
            headers={
                "X-Step-Up-Challenge-Id": ch_s2.json()["challenge"]["id"],
                "X-Step-Up-Code": pyotp.TOTP(strat_secret).at(clock()),
            },
        )
        assert ok_s2.status_code == 200, ok_s2.text

        # 3c. Runtime resume / workspace release / flatten confirm / telegram link qua PostgreSQL thật
        assert c_owner_a.post(f"/api/v1/bots/{bot_id}/runtime/pause", json={"reason": "Bảo trì"}).status_code == 200
        assert (
            c_owner_a.post(
                f"/api/v1/bots/{bot_id}/runtime/resume",
                json={"reason": "Mở lại", "expected_revision": 1},
            ).status_code
            == 403
        )
        clock.advance(35)
        ch_res = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={
                "action": ACTION_BOT_RUNTIME_RESUME,
                "payload": bot_runtime_resume_payload(bot_id=bot_id, expected_revision=1, reason="Mở lại"),
                "ttl_seconds": 300,
            },
        )
        assert (
            c_owner_a.post(
                f"/api/v1/bots/{bot_id}/runtime/resume",
                json={"reason": "Mở lại", "expected_revision": 1},
                headers={
                    "X-Step-Up-Challenge-Id": ch_res.json()["challenge"]["id"],
                    "X-Step-Up-Code": pyotp.TOTP(owner_totp_secret).at(clock()),
                },
            ).status_code
            == 200
        )

        assert c_owner_a.post("/api/v1/workspace/runtime/kill", json={"reason": "Sự cố"}).status_code == 200
        assert (
            c_owner_a.post(
                "/api/v1/workspace/runtime/release",
                json={"reason": "Ổn định", "expected_revision": 1},
            ).status_code
            == 403
        )
        clock.advance(35)
        ch_rel = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={
                "action": ACTION_WORKSPACE_RUNTIME_RELEASE,
                "payload": workspace_release_payload(expected_revision=1, reason="Ổn định"),
                "ttl_seconds": 300,
            },
        )
        assert (
            c_owner_a.post(
                "/api/v1/workspace/runtime/release",
                json={"reason": "Ổn định", "expected_revision": 1},
                headers={
                    "X-Step-Up-Challenge-Id": ch_rel.json()["challenge"]["id"],
                    "X-Step-Up-Code": pyotp.TOTP(owner_totp_secret).at(clock()),
                },
            ).status_code
            == 200
        )

        preview_res = c_owner_a.post("/api/v1/workspace/flatten/preview", json={"reason": "Đóng toàn bộ"})
        assert preview_res.status_code == 200, preview_res.text
        preview = preview_res.json()["preview"]
        assert (
            c_owner_a.post(
                "/api/v1/workspace/flatten/confirm",
                json={
                    "preview_id": preview["id"],
                    "preview_hash": preview["preview_hash"],
                    "idempotency_key": f"sec20-flat-{uuid4().hex}",
                    "reason": "Đóng toàn bộ",
                },
            ).status_code
            == 403
        )
        clock.advance(35)
        ch_flat = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={
                "action": ACTION_WORKSPACE_FLATTEN_CONFIRM,
                "payload": flatten_confirm_payload(
                    preview_id=UUID(preview["id"]),
                    preview_hash=preview["preview_hash"],
                    reason="Đóng toàn bộ",
                ),
                "ttl_seconds": 300,
            },
        )
        assert (
            c_owner_a.post(
                "/api/v1/workspace/flatten/confirm",
                json={
                    "preview_id": preview["id"],
                    "preview_hash": preview["preview_hash"],
                    "idempotency_key": f"sec20-flat-{uuid4().hex}",
                    "reason": "Đóng toàn bộ",
                },
                headers={
                    "X-Step-Up-Challenge-Id": ch_flat.json()["challenge"]["id"],
                    "X-Step-Up-Code": pyotp.TOTP(owner_totp_secret).at(clock()),
                },
            ).status_code
            == 200
        )

        assert c_owner_a.post("/api/v1/telegram-binding/link", json={"reason": "Liên kết Telegram"}).status_code == 403
        clock.advance(35)
        ch_tg = c_owner_a.post(
            "/api/v1/auth/challenges",
            json={
                "action": ACTION_TELEGRAM_LINK_CREATE,
                "payload": telegram_link_payload(reason="Liên kết Telegram"),
                "ttl_seconds": 300,
            },
        )
        assert (
            c_owner_a.post(
                "/api/v1/telegram-binding/link",
                json={"reason": "Liên kết Telegram"},
                headers={
                    "X-Step-Up-Challenge-Id": ch_tg.json()["challenge"]["id"],
                    "X-Step-Up-Code": pyotp.TOTP(owner_totp_secret).at(clock()),
                },
            ).status_code
            == 200
        )

    # Race condition thật qua PostgreSQL `SELECT ... FOR UPDATE`
    clock.advance(35)
    asyncio.run(
        test_postgres_atomic_race(
            key_file=key_file,
            secret_dir=secret_dir,
            principal=login_a.principal,
            totp_secret=owner_totp_secret,
            clock=clock,
            rp_payload=rp_payload,
        )
    )
    print("SEC20_POSTGRES_E2E_OK")


main()
"""


def test_sec20_stepup_postgres_migration_rls_and_http_e2e() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    image = build_image_reference(root)
    ensure_image_available(root, image)
    project = f"crypidian-sec20-e2e-{uuid.uuid4().hex[:10]}"
    env = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]
    try:
        subprocess.run(
            [*base, "up", "-d", "postgres", "migrator"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        alembic_heads = subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "heads"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "c8d28da699c6 (head)" in alembic_heads.stdout

        # Kiểm tra downgrade -> upgrade hoạt động sạch trên PostgreSQL thật
        subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "downgrade", "d8e2f4a6b8c0"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "upgrade", "e9a1c3f5b7d2"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        result = subprocess.run(
            [
                *base,
                "run",
                "--rm",
                "-T",
                "-v",
                f"{root / '.secrets'}:/run/secrets:ro",
                "migrator",
                "python",
                "-c",
                "import sys; exec(sys.stdin.read())",
            ],
            cwd=root,
            env=env,
            input=PROBE_CODE,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert "SEC20_POSTGRES_E2E_OK" in result.stdout, result.stdout + result.stderr
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
        )
