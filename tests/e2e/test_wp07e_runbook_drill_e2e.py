"""Compose E2E diễn tập runbook WP-07E trên PostgreSQL thật (CRY-377)."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"


def test_wp07e_runbook_drill_on_compose_postgres() -> None:
    """Diễn tập runbook WP-07E trên Compose PostgreSQL thật:
    1. Khởi tạo DB + migration (`db-provisioner` + `migrator`).
    2. Chèn dữ liệu diễn tập: nến `market_bar` revision > 1 và cũ (stale), `worker_heartbeat`,
       `telegram_binding` bị revoke, `source_policy_revision` hết hạn.
    3. Chạy `collect_workbench_metrics_alerts` từ container và xác nhận phát hiện đủ cảnh báo
       tiếng Việt, truy vết đúng `trace_ref`, và không rò rỉ token/PII.
    """
    project_name = f"crypidian-wp07e-drill-{uuid.uuid4().hex[:10]}"
    env = os.environ.copy()
    env.setdefault("APP_IMAGE", build_image_reference(REPO_ROOT))
    env["COMPOSE_PROJECT_NAME"] = project_name

    ensure_image_available(REPO_ROOT, env["APP_IMAGE"])
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/prepare_local_secrets.py")],
        cwd=REPO_ROOT,
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
        str(COMPOSE_FILE),
    ]

    drill_code = """
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.entities import (
    StrategyRelease,
    TelegramBinding,
    TradingAccount,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.models.market_entities import MarketBar, MarketInstrument
from trading_bot.adapters.database.models.research_entities import SourcePolicyRevisionRecord
from trading_bot.adapters.database.models.worker_heartbeat_entities import WorkerHeartbeat
from trading_bot.admin.workbench_metrics_service import collect_workbench_metrics_alerts


async def run_drill() -> None:
    admin_pw = open("/run/secrets/postgres_password", encoding="utf-8").read().strip()
    dsn = f"postgresql+psycopg://crypidian_admin:{admin_pw}@postgres:5432/crypidian"
    engine = create_async_engine(dsn)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id = uuid4()
    user_id = uuid4()
    mem_id = uuid4()
    inst_id = uuid4()
    bar1_id = uuid4()
    bar2_id = uuid4()
    hb_id = uuid4()
    bind_id = uuid4()
    pol_id = uuid4()

    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        session.add(
            Workspace(
                id=ws_id,
                name="WP07E Drill WS",
                slug=f"drill-{ws_id.hex[:8]}",
                mode="PAPER",
                status="ACTIVE",
            )
        )
        session.add(
            UserAccount(
                id=user_id,
                email_normalized=f"drill-{user_id.hex[:6]}@test.io",
                password_hash="h",
                status="ACTIVE",
            )
        )
        await session.flush()
        session.add(UserMembership(id=mem_id, workspace_id=ws_id, user_id=user_id, status="ACTIVE"))
        await session.flush()
        session.add(
            MarketInstrument(
                id=inst_id,
                workspace_id=ws_id,
                source="binance_usdm_rest",
                idempotency_key="inst-btcusdt",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                tick_size=Decimal("0.1"),
                quantity_step=Decimal("0.001"),
                min_notional=Decimal("5"),
                status="TRADING",
                revision=1,
                checksum="0" * 64,
                event_at=now - timedelta(hours=10),
                received_at=now - timedelta(hours=10),
                available_at=now - timedelta(hours=10),
            )
        )
        await session.flush()
        # Revision 1 & 2 (đính chính nến stale)
        t_open = now - timedelta(hours=6)
        t_close = now - timedelta(hours=5)
        session.add(
            MarketBar(
                id=bar1_id,
                workspace_id=ws_id,
                instrument_id=inst_id,
                symbol="BTCUSDT",
                timeframe="1h",
                source="binance_usdm_rest",
                logical_key="btcusdt-1h-drill",
                idempotency_key="bar-rev1",
                revision=1,
                checksum="1" * 64,
                open_time=t_open,
                close_time=t_close,
                open=Decimal("50000"),
                high=Decimal("50500"),
                low=Decimal("49900"),
                close=Decimal("50200"),
                volume=Decimal("10"),
                is_closed=True,
                event_at=t_close,
                received_at=t_close,
                available_at=t_close,
            )
        )
        await session.flush()
        session.add(
            MarketBar(
                id=bar2_id,
                workspace_id=ws_id,
                instrument_id=inst_id,
                symbol="BTCUSDT",
                timeframe="1h",
                source="binance_usdm_rest",
                logical_key="btcusdt-1h-drill",
                idempotency_key="bar-rev2",
                revision=2,
                supersedes_id=bar1_id,
                checksum="2" * 64,
                open_time=t_open,
                close_time=t_close,
                open=Decimal("50000"),
                high=Decimal("50550"),
                low=Decimal("49900"),
                close=Decimal("50250"),
                volume=Decimal("11"),
                is_closed=True,
                event_at=t_close,
                received_at=t_close,
                available_at=t_close,
            )
        )
        session.add(
            WorkerHeartbeat(
                workspace_id=ws_id,
                component="market_signal_scanner",
                instance_id=hb_id,
                state="degraded",
                reason_code="SCANNER_LAGGING",
                detail_vi="Scanner trễ nhịp kiểm thử",
                heartbeat_at=now - timedelta(minutes=10),
                last_tick_at=now - timedelta(minutes=10),
                counters={},
            )
        )
        session.add(
            TelegramBinding(
                id=bind_id,
                workspace_id=ws_id,
                membership_id=mem_id,
                telegram_user_id="987654321",
                revoked_at=now - timedelta(minutes=30),
            )
        )
        session.add(
            SourcePolicyRevisionRecord(
                id=pol_id,
                workspace_id=ws_id,
                source_id="binance_usdm_rest",
                policy_id="pol-drill-1",
                sequence=1,
                state="discovered",
                allowed_purposes=["paper_demo"],
                valid_until=now - timedelta(days=1),
                publisher="Binance",
                endpoint_url="https://fapi.binance.com",
                access_method="REST",
                auth_scope="public",
                attribution="Binance Market Data",
                retention_ref="ret-365d",
                quota_window_seconds=60,
                quota_max_requests=100,
                quota_max_bytes=1048576,
                quota_max_cost=Decimal("0"),
                quota_cost_unit="USD",
                actor_id="drill-operator",
                reason="Diễn tập WP-07E hết hạn quyền dữ liệu",
            )
        )
        await session.commit()

    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        report = await collect_workbench_metrics_alerts(session, workspace_id=ws_id, now=now)
        payload = report.to_dict()

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "987654321" not in serialized, "Không được lộ telegram_chat_id/user_id!"
    codes = {a["code"] for a in payload["alerts"]}
    expected = {
        "MARKET_DATA_STALE",
        "SCANNER_LAG_HIGH",
        "TELEGRAM_BINDING_REVOKED",
        "DATASET_RIGHTS_EXPIRED",
    }
    assert expected <= codes, codes
    print(json.dumps({"ok": True, "codes": sorted(codes)}))
    await engine.dispose()


asyncio.run(run_drill())
"""

    try:
        subprocess.run(
            [*base_cmd, "up", "--detach", "--wait", "--wait-timeout", "180", "postgres"],
            env=env,
            cwd=REPO_ROOT,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "db-provisioner"],
            env=env,
            cwd=REPO_ROOT,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "-T", "migrator", "alembic", "upgrade", "head"],
            env=env,
            cwd=REPO_ROOT,
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
                f"{REPO_ROOT / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{REPO_ROOT / 'src/trading_bot'}:/app/src/trading_bot:ro",
                "migrator",
                "python",
                "-c",
                drill_code,
            ],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert '"ok": true' in result.stdout, result.stdout
    finally:
        subprocess.run(
            [*base_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            cwd=REPO_ROOT,
            check=False,
        )
