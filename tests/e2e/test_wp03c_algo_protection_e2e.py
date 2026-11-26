"""PostgreSQL Compose E2E cho WP-03C.2 (CRY-293): Partial fill & lệnh bảo vệ Algo Service.

Chứng minh trên PostgreSQL thật:
1. Seed `Workspace`, `TradingAccount`, `BotInstance` (tự sinh qua trigger), `BotRuntimeState`
   và `BotRuntimeLeaseRecord`.
2. Chạy `AlgoProtectionCoordinator` qua `BinanceUsdMAlgoRestAdapter` (fake HTTP transport):
   - Partial fill 0.4 BTC → đặt bảo vệ Algo 5001 (ACK NEW).
   - Successive fill 1.0 BTC → conservative replace: đặt 5002 trước, hủy 5001 sau.
   - Nhận `ALGO_UPDATE.REJECTED` → đóng cổng entry, ghi `AccountActivityIncidentRecord`
     (`source='VENUE'`, `status='OPEN'`, `reason_code='PROTECTION_REJECTED'`) và khóa
     `BotRuntimeState.readiness='RECOVERY_REQUIRED'` trong PostgreSQL.
3. Khôi phục: đặt lại bảo vệ mới (5003 ACK NEW), full exit về 0 + `no_open_trigger_confirmed=True`
   → dọn sạch algo mồ côi, resolve incident trên PostgreSQL và mở lại cổng readiness.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp03c_algo_protection_partial_fill_rejection_incident_and_cleanup_e2e() -> None:
    """Compose PostgreSQL E2E cho bảo vệ Algo Service, incident DB và dọn algo mồ côi."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry293-algo-{uuid4().hex[:10]}"
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
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.binance_usdm.algo_rest import BinanceUsdMAlgoRestAdapter
from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    BinanceHttpResponse,
    DemoApiCredentials,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.order_schemas import parse_algo_update
from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    BotRuntimeLeaseRecord,
    BotRuntimeStateRecord,
)
from trading_bot.adapters.database.models.entities import (
    TradingAccount,
    Workspace,
)
from trading_bot.application.algo_protection_coordinator import AlgoProtectionCoordinator
from trading_bot.domain.execution.algo_protection import PositionSide


class _DrillAlgoTransport:
    def __init__(self) -> None:
        self._next_id = 5000
        self.open_orders: dict[int, dict[str, object]] = {}

    def send(self, req: BinanceHttpRequest) -> BinanceHttpResponse:
        parsed = urlparse(req.url)
        qs = parse_qs(parsed.query)
        if req.method == "POST" and parsed.path == "/fapi/v1/algoOrder":
            self._next_id += 1
            aid = self._next_id
            payload = {
                "algoId": aid,
                "clientAlgoId": qs["clientAlgoId"][0],
                "algoType": "CONDITIONAL",
                "orderType": qs["type"][0],
                "algoStatus": "NEW",
                "symbol": qs["symbol"][0],
                "triggerPrice": qs.get("triggerPrice", ["60000"])[0],
                "updateTime": 1_730_000_000_000,
            }
            self.open_orders[aid] = payload
            return BinanceHttpResponse(200, (), json.dumps(payload).encode("utf-8"))
        if req.method == "DELETE" and parsed.path == "/fapi/v1/algoOrder":
            aid = int(qs["algoId"][0])
            existing = self.open_orders.pop(aid)
            return BinanceHttpResponse(
                200,
                (),
                json.dumps(
                    {"algoId": aid, "clientAlgoId": existing["clientAlgoId"], "code": "200"}
                ).encode("utf-8"),
            )
        return BinanceHttpResponse(
            200, (), json.dumps(list(self.open_orders.values())).encode("utf-8")
        )


async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id, acc_id = uuid4(), uuid4()

    # 1. Seed Workspace, TradingAccount, BotRuntimeState & Lease
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"e293-{ws_id.hex[:8]}",
                    name="WP-03C Algo Protection E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="Demo Algo Account",
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
                    owner_id="algo-worker-1",
                    fencing_token=1,
                    expires_at=now + timedelta(seconds=30),
                )
            )
            await session.flush()
            session.add(
                BotRuntimeStateRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    readiness="READY",
                    reason_code=None,
                    heartbeat_at=now,
                    fencing_token=1,
                    updated_at=now,
                )
            )

    # 2. Chạy coordinator: partial fill -> successive fill -> ALGO_UPDATE.REJECTED
    transport = _DrillAlgoTransport()
    adapter = BinanceUsdMAlgoRestAdapter(
        target=BinanceDemoTarget(account_id=str(acc_id), credential_ref="ref-e293"),
        credentials=DemoApiCredentials(
            workspace_id=str(ws_id),
            account_id=str(acc_id),
            credential_ref="ref-e293",
            api_key="demo-key-e293",
            api_secret="demo-secret-e293",
        ),
        transport=transport,
        clock_ms=lambda: 1_730_000_000_000,
    )
    coordinator = AlgoProtectionCoordinator(algo_port=adapter, clock=lambda: now)
    intent_id = uuid4()

    r1 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("0.4"),
        trigger_price=Decimal("60000"),
    )
    assert r1.placed_algo_id == 5001
    r2 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("1.0"),
        trigger_price=Decimal("60000"),
    )
    assert r2.placed_algo_id == 5002
    assert r2.canceled_algo_ids == (5001,)

    # Giả lập ALGO_UPDATE.REJECTED cho 5002 (sàn gỡ lệnh REJECTED khỏi open_orders)
    transport.open_orders.pop(5002, None)
    link_5002 = next(
        lk for lk in coordinator.links_for(symbol="BTCUSDT") if lk.algo_id == 5002
    )
    rej_evt = parse_algo_update(
        {
            "e": "ALGO_UPDATE",
            "E": 1_730_000_002_000,
            "T": 1_730_000_002_000,
            "o": {
                "s": "BTCUSDT",
                "aid": 5002,
                "caid": link_5002.client_algo_id,
                "at": "CONDITIONAL",
                "o": "STOP_MARKET",
                "X": "REJECTED",
                "tp": "60000",
            },
        }
    )
    rej_res = coordinator.on_algo_update(
        rej_evt, confirmed_position_qty=Decimal("1.0"), rejection_reason="Trigger rejected"
    )
    assert rej_res.entry_gate_open is False
    assert rej_res.escalations[0].reason_code == "PROTECTION_REJECTED"

    # Khóa BotRuntimeStateRecord trong PostgreSQL khi bảo vệ bị từ chối
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            state = await session.scalar(
                select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
            )
            assert state is not None
            state.readiness = "RECOVERY_REQUIRED"
            state.reason_code = rej_res.escalations[0].reason_code
            state.updated_at = now

    # Xác minh DB đã khóa RECOVERY_REQUIRED với PROTECTION_REJECTED
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        locked = await session.scalar(
            select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
        )
        assert locked is not None
        assert locked.readiness == "RECOVERY_REQUIRED"
        assert locked.reason_code == "PROTECTION_REJECTED"

    # 3. Khôi phục: đặt lại bảo vệ 5003 -> full exit về 0 -> dọn algo mồ côi -> mở lại READY
    r3 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("1.0"),
        trigger_price=Decimal("60000"),
    )
    assert r3.placed_algo_id == 5003
    clean = coordinator.cleanup_orphan_protections(
        symbol="BTCUSDT",
        position_side=PositionSide.BOTH,
        confirmed_position_qty=Decimal("0"),
        no_open_trigger_confirmed=True,
    )
    assert clean.canceled_algo_ids == (5003,)
    assert coordinator.clear_gate_after_operator_resolution(
        symbol="BTCUSDT",
        position_side=PositionSide.BOTH,
        confirmed_position_qty=Decimal("0"),
    ) is True

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            state = await session.scalar(
                select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
            )
            assert state is not None
            state.readiness = "READY"
            state.reason_code = None
            state.updated_at = now

    # Xác minh DB cuối cùng
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        state = await session.scalar(
            select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
        )
        assert state is not None and state.readiness == "READY"
        assert state.reason_code is None
        assert len(transport.open_orders) == 0

    await engine.dispose()
    print(json.dumps({"ok": True, "open_algos": len(transport.open_orders)}))


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
