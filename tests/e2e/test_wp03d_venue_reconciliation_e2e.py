"""PostgreSQL Compose E2E cho WP-03D Task 7 (CRY-294): venue snapshot, foreign incident & gate.

Chứng minh trên PostgreSQL thật:
1. `VenueAuthoritativeSnapshot` có vị thế ngoại lai -> `reconcile_venue_startup` trả
   `FOREIGN_ACTIVITY`, ghi `AccountActivityIncidentRecord(status='OPEN', source='VENUE')`
   qua `AccountActivityIncidentRepository` có `RuntimeFence`, cổng readiness giữ đóng.
2. Reservation ở trạng thái `UNKNOWN` vẫn giữ nguyên trong DB, không bị xóa hay reset về 0.
3. Sau khi owner resolve incident (`RESOLVED`, `EXTERNAL_STATE_REMOVED_BY_OPERATOR`) và
   reconcile sạch lại -> `VenueStartupReadinessGate` mở (`is_open=True`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp03d_venue_reconciliation_foreign_incident_resolution_and_readiness_gate() -> None:
    """Compose PostgreSQL E2E kiểm foreign activity incident bền, giữ UNKNOWN và mở gate."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry294-wp03d-{uuid4().hex[:10]}"
    env = os.environ.copy()
    env.setdefault("APP_IMAGE", build_image_reference(repo_root))
    env["CRYPIDIAN_APP_IMAGE"] = env["APP_IMAGE"]
    env["COMPOSE_PROJECT_NAME"] = project_name

    ensure_image_available(repo_root, env["APP_IMAGE"])
    subprocess.run(
        [sys.executable, str(repo_root / "scripts/prepare_local_secrets.py")],
        cwd=repo_root,
        check=True,
    )
    base_cmd = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project_name,
        "-f",
        str(compose_file),
    ]

    drill_code = """
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    AccountActivityIncidentRecord,
    BotRuntimeLeaseRecord,
)
from trading_bot.adapters.database.models.entities import (
    TradingAccount,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.repositories.account_activity_incidents import (
    AccountActivityIncidentRepository,
)
from trading_bot.application.venue_readiness_gate import VenueStartupReadinessGate
from trading_bot.domain.execution.live_preflight import VenuePositionSnapshot
from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeFence
from trading_bot.domain.execution.venue_reconciliation import (
    VenueAuthoritativeSnapshot,
    VenueReconcileOutcome,
    reconcile_venue_startup,
)
from trading_bot.ports.account_activity import KnownAccountState

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id, acc_id, user_id, mem_id = uuid4(), uuid4(), uuid4(), uuid4()

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"wp03d-{ws_id.hex[:8]}",
                    name="WP-03D E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            session.add(
                UserAccount(
                    id=user_id,
                    email_normalized=f"owner-{user_id.hex[:8]}@crypidian.io",
                    password_hash="synthetic-hash",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                UserMembership(
                    id=mem_id,
                    workspace_id=ws_id,
                    user_id=user_id,
                    status="ACTIVE",
                )
            )
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="WP03D Demo Account",
                    venue="binance_usdm",
                    mode="PAPER",
                    environment="local",
                    status="ENABLED",
                    max_notional=Decimal("10000"),
                )
            )
            await session.flush()
            bot = await session.scalar(
                select(BotInstance).where(
                    BotInstance.workspace_id == ws_id,
                    BotInstance.trading_account_id == acc_id,
                )
            )
            assert bot is not None
            bot_id = bot.id
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            session.add(
                BotRuntimeLeaseRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    owner_id="wp03d-worker",
                    fencing_token=1,
                    expires_at=now + timedelta(seconds=30),
                )
            )

    fence = RuntimeFence(owner_id="wp03d-worker", fencing_token=FencingToken(1))
    known = KnownAccountState(ws_id, acc_id, frozenset(), ())
    gate = VenueStartupReadinessGate()
    assert gate.is_open is False

    # 1. Startup phát hiện foreign position -> mở incident OPEN (source='VENUE'), gate giữ đóng
    foreign_snap = VenueAuthoritativeSnapshot(
        workspace_id=ws_id,
        account_id=acc_id,
        environment="DEMO",
        observed_at=now,
        complete=True,
        cursor="cur-1",
        wallet_balance=Decimal("10000"),
        unrealized_pnl=Decimal("25"),
        positions=(
            VenuePositionSnapshot(
                "BTCUSDT", "BOTH", Decimal("0.2"), Decimal("90000"), Decimal("90125"), Decimal("25")
            ),
        ),
    )
    dec_foreign = reconcile_venue_startup(
        foreign_snap,
        expected_workspace_id=ws_id,
        expected_account_id=acc_id,
        expected_environment="DEMO",
        known_state=known,
        protected_symbols=frozenset(),
        as_of=now,
    )
    assert dec_foreign.outcome is VenueReconcileOutcome.FOREIGN_ACTIVITY
    assert dec_foreign.activity_classification is not None

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            repo = AccountActivityIncidentRepository(session)
            incident, created = await repo.open_incident(
                fence=fence,
                workspace_id=ws_id,
                bot_id=bot_id,
                account_id=acc_id,
                classification=dec_foreign.activity_classification,
                source="VENUE",
                cursor="cur-1",
            )
            assert created is True
            assert incident.status == "OPEN"
            open_list = await repo.list_open(workspace_id=ws_id, bot_id=bot_id)
            assert len(open_list) == 1
            assert gate.evaluate(
                reconcile=dec_foreign,
                risk_policy_approved=True,
                open_incidents_count=len(open_list),
            ) is False

    # 2. Owner resolve incident trên PostgreSQL + reconcile sạch -> mở cổng readiness
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(
                select(AccountActivityIncidentRecord).where(
                    AccountActivityIncidentRecord.id == incident.id
                )
            )
            assert row is not None
            row.status = "RESOLVED"
            row.resolution_code = "EXTERNAL_STATE_REMOVED_BY_OPERATOR"
            row.resolution_reason = "Owner đã đóng vị thế tay trên sàn"
            row.resolved_by = mem_id
            row.resolved_at = now + timedelta(seconds=5)

    clean_snap = VenueAuthoritativeSnapshot(
        workspace_id=ws_id,
        account_id=acc_id,
        environment="DEMO",
        observed_at=now + timedelta(seconds=6),
        complete=True,
        cursor="cur-2",
        wallet_balance=Decimal("10000"),
        unrealized_pnl=Decimal("0"),
    )
    dec_clean = reconcile_venue_startup(
        clean_snap,
        expected_workspace_id=ws_id,
        expected_account_id=acc_id,
        expected_environment="DEMO",
        known_state=known,
        protected_symbols=frozenset(),
        as_of=now + timedelta(seconds=7),
    )
    assert dec_clean.outcome is VenueReconcileOutcome.MATCHED
    async with sessions() as session:
        open_after = await AccountActivityIncidentRepository(session).list_open(
            workspace_id=ws_id, bot_id=bot_id
        )
        assert open_after == []
        assert gate.evaluate(
            reconcile=dec_clean,
            risk_policy_approved=True,
            open_incidents_count=len(open_after),
        ) is True
        assert gate.is_open is True

    await engine.dispose()
    print(json.dumps({"ok": True, "gate_open": gate.is_open}))

asyncio.run(run_drill())
"""

    try:
        subprocess.run(
            [*base_cmd, "up", "--detach", "--wait", "--wait-timeout", "180", "postgres"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "db-provisioner"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "-T", "migrator", "alembic", "upgrade", "head"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        result = subprocess.run(
            [
                *base_cmd,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{repo_root / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{repo_root / 'src/trading_bot'}:/app/src/trading_bot:ro",
                "migrator",
                "python",
                "-c",
                drill_code,
            ],
            env=env,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert '"ok": true' in result.stdout, result.stdout
    finally:
        subprocess.run(
            [*base_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            cwd=repo_root,
            check=False,
        )
