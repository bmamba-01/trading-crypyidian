from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from textwrap import dedent

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available


def _compose(root: Path, project: str, environment: dict[str, str], *args: str):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--project-name",
            project,
            "-f",
            "compose.yaml",
            *args,
        ],
        check=False,
        capture_output=True,
        cwd=root,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    if result.returncode:
        pytest.fail(
            f"Compose command failed: {' '.join(args)}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def test_postgres_expiry_releases_only_due_reserved_risk() -> None:
    """Kiểm tra expiry đã migrate, chạy trong Postgres thật và tuân RLS."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    project = f"crypidian-expiry-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        web_port = listener.getsockname()[1]
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "TRADING_BOT_WEB_PORT": str(web_port),
    }
    workspace_id, account_id, plan_id, decision_id = (str(uuid.uuid4()) for _ in range(4))
    due_id, unknown_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        ensure_image_available(root, image)
        _compose(root, project, environment, "up", "--detach", "--wait", "--wait-timeout", "180")
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "check",
        )
        seed = dedent(
            """
            import json
            import os
            from pathlib import Path
            import psycopg
            workspace, account, plan, decision, due, unknown = (
                os.environ[key] for key in (
                    'E2E_WORKSPACE', 'E2E_ACCOUNT', 'E2E_PLAN', 'E2E_DECISION',
                    'E2E_DUE', 'E2E_UNKNOWN',
                )
            )
            password = Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']).read_text().strip()
            with psycopg.connect(
                host='postgres', dbname='crypidian', user='crypidian_admin', password=password,
            ) as connection:
                connection.execute(
                    "INSERT INTO workspace (id, name, slug, mode, status) "
                    "VALUES (%s, 'Expiry E2E', %s, 'PAPER', 'ACTIVE')",
                    (workspace, 'expiry-' + workspace[:8]),
                )
                connection.execute(
                    "INSERT INTO trading_account "
                    "(id, workspace_id, name, venue, mode, environment, status, max_notional) "
                    "VALUES (%s, %s, 'expiry-e2e', 'binance_usdm', 'PAPER', 'local', "
                    "'ENABLED', 1000)", (account, workspace),
                )
                connection.execute(
                    "SELECT set_config('app.bot_id', (SELECT id::text FROM bot_instance "
                    "WHERE trading_account_id=%s), true)", (account,),
                )
                connection.execute(
                    "INSERT INTO trade_plan "
                    "(id, workspace_id, account_id, plan_hash, symbol, notional, payload) "
                    "VALUES (%s, %s, %s, %s, 'BTCUSDT', 100, '{}'::jsonb)",
                    (plan, workspace, account, 'a' * 64),
                )
                connection.execute(
                    "INSERT INTO risk_decision "
                    "(id, workspace_id, account_id, decision_key, plan_hash, snapshot_id, "
                    "approved, reason_code, detail_vi, payload) "
                    "VALUES (%s, %s, %s, %s, %s, 'expiry-e2e', true, 'APPROVED', 'OK', "
                    "jsonb_build_object('candidate_expires_at', "
                    "(now() + interval '30 seconds')::text))",
                    (decision, workspace, account, 'b' * 64, 'a' * 64),
                )
                connection.execute(
                    "INSERT INTO risk_reservation "
                    "(id, workspace_id, account_id, risk_decision_id, idempotency_key, "
                    "plan_hash, symbol, reserved_risk, reserved_notional, "
                    "reserved_initial_margin, expires_at, status) "
                    "VALUES (%s, %s, %s, %s, 'due', %s, 'BTCUSDT', 10, 100, 20, "
                    "now() + interval '30 seconds', 'RESERVED'), "
                    "(%s, %s, %s, %s, 'unknown', %s, 'BTCUSDT', 10, 100, 20, "
                    "now() + interval '30 seconds', 'UNKNOWN')",
                    (due, workspace, account, decision, 'a' * 64,
                     unknown, workspace, account, decision, 'a' * 64),
                )
            print(json.dumps({'seeded': True}))
            """
        ).strip()
        common_env = environment | {
            "E2E_WORKSPACE": workspace_id,
            "E2E_ACCOUNT": account_id,
            "E2E_PLAN": plan_id,
            "E2E_DECISION": decision_id,
            "E2E_DUE": due_id,
            "E2E_UNKNOWN": unknown_id,
        }
        _compose(
            root,
            project,
            common_env,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"E2E_WORKSPACE={workspace_id}",
            "-e",
            f"E2E_ACCOUNT={account_id}",
            "-e",
            f"E2E_PLAN={plan_id}",
            "-e",
            f"E2E_DECISION={decision_id}",
            "-e",
            f"E2E_DUE={due_id}",
            "-e",
            f"E2E_UNKNOWN={unknown_id}",
            "migrator",
            "python",
            "-c",
            seed,
        )
        probe = dedent(
            """
            import asyncio, json, os
            from datetime import UTC, datetime, timedelta
            from pathlib import Path
            from uuid import UUID
            from sqlalchemy import select, text
            from trading_bot.adapters.database.engine import (
                create_database_engine, create_session_factory, database_url_from_secret,
            )
            from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
            from trading_bot.adapters.database.repositories.risk_reservations import (
                RiskReservationRepository,
            )

            async def main():
                workspace = UUID(os.environ['E2E_WORKSPACE'])
                account = UUID(os.environ['E2E_ACCOUNT'])
                due = UUID(os.environ['E2E_DUE'])
                unknown = UUID(os.environ['E2E_UNKNOWN'])
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host='postgres', database='crypidian',
                )
                engine = create_database_engine(url)
                sessions = create_session_factory(engine)
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("SELECT set_config('app.workspace_id', :workspace, true)"),
                        {'workspace': str(workspace)},
                    )
                    count = await RiskReservationRepository(
                        session, clock=lambda: datetime.now(UTC) + timedelta(minutes=1),
                    ).expire_due(workspace_id=workspace, account_id=account)
                    rows = (await session.scalars(select(RiskReservationRecord))).all()
                    status = {str(row.id): (row.status, row.resolution_reason) for row in rows}
                await engine.dispose()
                print(json.dumps({
                    'released': count,
                    'due': status[str(due)],
                    'unknown': status[str(unknown)],
                }))
            asyncio.run(main())
            """
        ).strip()
        result = _compose(
            root,
            project,
            common_env,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"E2E_WORKSPACE={workspace_id}",
            "-e",
            f"E2E_ACCOUNT={account_id}",
            "-e",
            f"E2E_DUE={due_id}",
            "-e",
            f"E2E_UNKNOWN={unknown_id}",
            # MB-01: chỉ runtime có RLS/quyền trên reservation theo bot scope.
            "trading-runtime",
            "python",
            "-c",
            probe,
        )
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        assert observed == {
            "released": 1,
            "due": ["RELEASED", "EXPIRED_BEFORE_SUBMIT"],
            "unknown": ["UNKNOWN", None],
        }
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "9a7c2d5e1b40",
        )
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "upgrade",
            "head",
        )
    finally:
        _compose(root, project, environment, "down", "--volumes", "--remove-orphans")
