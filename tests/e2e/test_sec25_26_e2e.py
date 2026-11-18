"""E2E PostgreSQL + Docker cho CRY-141: SEC-25 (BotGrant hạn, lý do, audit, rà soát) và SEC-26."""
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from trading_bot.adapters.database.engine import create_database_engine, create_session_factory
from trading_bot.adapters.database.models import AuditEvent, BotGrant
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.admin.bot_grant_service import BotGrantAdminService
from trading_bot.app import create_app
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings


def _db_url(user: str, password_file: str) -> str:
    pw = Path(password_file).read_text(encoding="utf-8").strip()
    return f"postgresql+psycopg://{user}:{pw}@postgres:5432/crypidian"


def _expect_pg_error(conn: psycopg.Connection, sql: str, params: tuple[object, ...], match: str) -> None:
    with conn.transaction():
        try:
            conn.execute(sql, params)
        except psycopg.Error as exc:
            assert match in str(exc), f"Expected {match!r} in {exc!r}"
            return
    raise AssertionError(f"Query unexpectedly succeeded: {sql}")


async def main() -> None:
    admin_pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    ws_id = uuid4()
    acc_id = uuid4()
    owner_user = uuid4()
    owner_member = uuid4()
    op_user = uuid4()
    op_member = uuid4()
    now = datetime.now(UTC)

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) VALUES (%s, %s, %s, 'PAPER', 'ACTIVE')",
            (ws_id, "SEC25 WS", f"sec25-{ws_id.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'hash', 'ACTIVE'), (%s, %s, 'hash', 'ACTIVE')",
            (owner_user, f"owner-{owner_user.hex[:8]}@test.io", op_user, f"op-{op_user.hex[:8]}@test.io"),
        )
        conn.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE'), (%s, %s, %s, 'ACTIVE')",
            (owner_member, ws_id, owner_user, op_member, ws_id, op_user),
        )
        conn.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (%s, %s, %s, 'owner'), (%s, %s, %s, 'operator')",
            (uuid4(), ws_id, owner_member, uuid4(), ws_id, op_member),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, status, max_notional) "
            "VALUES (%s, %s, 'acc-sec25', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 10000)",
            (acc_id, ws_id),
        )
        bot_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws_id, acc_id),
        ).fetchone()[0]

        # Seed 2 grant đã hết hạn sẵn (READ để test cấp lại, REPORT_READ để nằm trong bucket expired).
        expired_read_id = uuid4()
        expired_report_id = uuid4()
        conn.execute(
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, granted_by, expires_at, reason) "
            "VALUES (%s, %s, %s, %s, 'bot:read', %s, %s, %s), (%s, %s, %s, %s, 'bot:report:read', %s, %s, %s)",
            (
                expired_read_id,
                ws_id,
                bot_id,
                op_member,
                owner_member,
                now - timedelta(minutes=15),
                "Quyền đọc cũ đã hết hạn",
                expired_report_id,
                ws_id,
                bot_id,
                op_member,
                owner_member,
                now - timedelta(hours=1),
                "Quyền báo cáo cũ đã hết hạn",
            ),
        )
        # Kiểm tra ràng buộc DB và trigger bất biến lịch sử.
        _expect_pg_error(
            conn,
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, granted_by, revoke_reason) "
            "VALUES (%s, %s, %s, %s, 'bot:runtime:resume', %s, 'thiếu revoked_at')",
            (uuid4(), ws_id, bot_id, op_member, owner_member),
            "bot_grant_revoke_reason_valid",
        )
        _expect_pg_error(
            conn,
            "UPDATE bot_grant SET expires_at = %s WHERE id = %s",
            (now + timedelta(days=1), expired_read_id),
            "MB01_BOT_GRANT_HISTORY_IMMUTABLE",
        )
        _expect_pg_error(
            conn,
            "UPDATE bot_grant SET reason = 'sửa lý do' WHERE id = %s",
            (expired_read_id,),
            "MB01_BOT_GRANT_HISTORY_IMMUTABLE",
        )
        _expect_pg_error(
            conn,
            "DELETE FROM bot_grant WHERE id = %s",
            (expired_read_id,),
            "MB01_BOT_GRANT_HISTORY_IMMUTABLE",
        )

    engine = create_database_engine(
        _db_url("crypidian_admin_web", "/run/secrets/database_admin_web_password")
    )
    sessions = create_session_factory(engine)
    try:
        owner = Principal(
            user_id=owner_user,
            membership_id=owner_member,
            workspace_id=ws_id,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        )
        operator = Principal(
            user_id=op_user,
            membership_id=op_member,
            workspace_id=ws_id,
            roles=frozenset({Role.OPERATOR}),
            mfa_verified_at=datetime.now(UTC),
        )
        service = BotGrantAdminService(sessions)

        # Grant hết hạn ở DB không cấp quyền.
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.workspace_id', :w, true), "
                    "set_config('app.membership_id', :m, true)"
                ),
                {"w": str(ws_id), "m": str(op_member)},
            )
            try:
                await BotGrantRepository(session).require(
                    operator, bot_id=bot_id, permission=BotPermission.READ
                )
            except PermissionError:
                pass
            else:
                raise AssertionError("Grant đã hết hạn vẫn cấp quyền")

        # Cấp quyền mới có hạn gần (sắp hết hạn trong cửa sổ 7 ngày) và cấp lại quyền READ đã hết hạn.
        soon_expiry = datetime.now(UTC) + timedelta(days=2)
        pause_grant = await service.grant(
            owner,
            bot_id=bot_id,
            membership_id=op_member,
            permission=BotPermission.RUNTIME_PAUSE,
            expires_at=soon_expiry,
            reason="Trực ca phiên Á BTCUSDT",
        )
        reissued_read = await service.grant(
            owner,
            bot_id=bot_id,
            membership_id=op_member,
            permission=BotPermission.READ,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            reason="Cấp lại quyền đọc sau khi hết hạn",
        )
        assert reissued_read["id"] != str(expired_read_id)

        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.workspace_id', :w, true), "
                    "set_config('app.membership_id', :m, true)"
                ),
                {"w": str(ws_id), "m": str(op_member)},
            )
            await BotGrantRepository(session).require(
                operator, bot_id=bot_id, permission=BotPermission.RUNTIME_PAUSE
            )
            await BotGrantRepository(session).require(
                operator, bot_id=bot_id, permission=BotPermission.READ
            )

        # Rà soát quyền định kỳ (có ghi audit).
        review = await service.review_grants(
            owner,
            expiring_within_days=7,
            include_revoked=True,
            record_audit=True,
            reason="Rà soát định kỳ SEC-25",
        )
        assert review["summary"] == {"active": 2, "expiring_soon": 1, "expired": 1, "revoked": 1}
        assert [item["id"] for item in review["expiring_soon"]] == [pause_grant["id"]]
        assert [item["id"] for item in review["expired"]] == [str(expired_report_id)]

        # Thu hồi grant kèm lý do -> mất quyền ngay và ghi audit.
        revoke_result = await service.revoke(
            owner,
            bot_id=bot_id,
            membership_id=op_member,
            permission=BotPermission.RUNTIME_PAUSE,
            reason="Kết thúc ca trực an toàn",
        )
        assert revoke_result["revoked"] is True

        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.workspace_id', :w, true), "
                    "set_config('app.membership_id', :m, true)"
                ),
                {"w": str(ws_id), "m": str(owner_member)},
            )
            audits = (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.workspace_id == ws_id)
                    .order_by(AuditEvent.occurred_at)
                )
            ).all()
            actions = [a.action for a in audits]
            assert "bot.grant.granted" in actions
            assert "bot.grant.revoked" in actions
            assert "bot.grant.access_reviewed" in actions
            revoked_audit = next(
                a for a in audits if a.action == "bot.grant.revoked" and str(a.target_id) == pause_grant["id"]
            )
            assert revoked_audit.reason is not None
            assert "Kết thúc ca trực an toàn" in revoked_audit.reason
            assert soon_expiry.astimezone(UTC).isoformat() in revoked_audit.reason

            grant_row = await session.scalar(
                select(BotGrant).where(BotGrant.id == pause_grant["id"])
            )
            assert grant_row is not None
            assert grant_row.revoked_at is not None
            assert grant_row.revoke_reason == "Kết thúc ca trực an toàn"

        # Grant đã thu hồi không được phép sửa lại ở DB.
        with psycopg.connect(
            host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
        ) as conn:
            _expect_pg_error(
                conn,
                "UPDATE bot_grant SET revoke_reason = 'tamper' WHERE id = %s",
                (pause_grant["id"],),
                "MB01_BOT_GRANT_HISTORY_IMMUTABLE",
            )
            _expect_pg_error(
                conn,
                "UPDATE bot_grant SET revoked_at = NULL WHERE id = %s",
                (pause_grant["id"],),
                "MB01_BOT_GRANT_HISTORY_IMMUTABLE",
            )

        # SEC-26 + HTTP API thật trên app production nối PostgreSQL.
        admin_kwargs: dict[str, object] = {"app_env": "production", "process_role": "admin-web"}
        if "allowed_hosts" in AppSettings.model_fields:
            admin_kwargs["allowed_hosts"] = "testserver"
        prod_admin = create_app(
            AppSettings(**admin_kwargs),  # type: ignore[arg-type]
            session_factory=sessions,
            secret_provider=object(),  # type: ignore[arg-type]
        )
        with TestClient(prod_admin) as admin_client:
            assert admin_client.get("/openapi.json").status_code == 404
            assert admin_client.get("/docs").status_code == 404
            assert admin_client.get("/redoc").status_code == 404
            assert admin_client.get("/api/v1/bot-grants/review").status_code == 401

            prod_admin.dependency_overrides[current_identity] = lambda: AuthIdentity(
                principal=owner, email="owner@example.test"
            )
            prod_admin.dependency_overrides[require_csrf] = lambda: AuthIdentity(
                principal=owner, email="owner@example.test"
            )
            http_grant = admin_client.post(
                "/api/v1/bot-grants",
                json={
                    "bot_id": str(bot_id),
                    "membership_id": str(op_member),
                    "permission": BotPermission.RUNTIME_RESUME.value,
                    "reason": "Cấp qua HTTP E2E",
                    "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                },
            )
            assert http_grant.status_code == 201, http_grant.text
            http_review = admin_client.get("/api/v1/bot-grants/review?expiring_within_days=7")
            assert http_review.status_code == 200
            assert http_review.json()["review"]["summary"]["active"] == 2
            http_revoke = admin_client.post(
                "/api/v1/bot-grants/revoke",
                json={
                    "bot_id": str(bot_id),
                    "membership_id": str(op_member),
                    "permission": BotPermission.RUNTIME_RESUME.value,
                    "reason": "Thu hồi qua HTTP E2E",
                },
            )
            assert http_revoke.status_code == 200
            assert http_revoke.json()["revoked"] is True
    finally:
        await engine.dispose()

    prod_runtime = create_app(
        AppSettings(
            app_env="production",
            process_role="trading-runtime",
            runtime_workspace_id=ws_id,
        )
    )
    runtime_client = TestClient(prod_runtime)
    assert runtime_client.get("/openapi.json").status_code == 404
    assert runtime_client.get("/api/v1/auth/login").status_code == 404
    assert runtime_client.get("/api/v1/bot-grants/review").status_code == 404
    assert runtime_client.get("/admin").status_code == 404
    assert runtime_client.get("/health/live").status_code == 200
    print("SEC25_26_POSTGRES_E2E_OK")


asyncio.run(main())
"""


def test_sec25_26_postgres_and_app_surface_e2e() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    image = build_image_reference(root)
    ensure_image_available(root, image)
    project = f"crypidian-sec2526-e2e-{uuid.uuid4().hex[:10]}"
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
        assert "e9a1c3f5b7d2 (head)" in alembic_heads.stdout

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
        assert "SEC25_26_POSTGRES_E2E_OK" in result.stdout, result.stdout + "\n" + result.stderr

        # Kiểm tra rollback/re-upgrade migration trên DB thật không lỗi.
        downgrade = subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "downgrade", "e1f3a5b7c9d1"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert downgrade.returncode == 0, downgrade.stdout + "\n" + downgrade.stderr
        reupgrade = subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "upgrade", "head"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert reupgrade.returncode == 0, reupgrade.stdout + "\n" + reupgrade.stderr
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
        )
