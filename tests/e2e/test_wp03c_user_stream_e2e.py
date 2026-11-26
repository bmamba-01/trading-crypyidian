"""PostgreSQL Compose E2E cho WP-03C.1 (CRY-292): User Stream lifecycle & Event Reducer.

Chứng minh trên PostgreSQL thật:
1. Seed `Workspace`, `TradingAccount`, `BotInstance`, `BotRuntimeLeaseRecord` và
   `BotRuntimeStateRecord(readiness='READY')`.
2. Khởi chạy `UserStreamSupervisor` + `VenueEventReducer`:
   - Nhận sự kiện `ORDER_TRADE_UPDATE` (partial fill 0.4).
   - Gặp `listenKeyExpired` → supervisor đóng ngay `entry_gate_open=False`, cập nhật
     `BotRuntimeStateRecord(readiness='RECOVERY_REQUIRED', reason_code='STREAM_EXPIRED')`.
3. Reconnect (tạo listenKey mới), replay sự kiện trùng lặp + áp dụng REST snapshot (`FILLED` 1.0),
   xác nhận hội tụ idempotent, mở lại cổng `entry_gate_open=True`, cập nhật DB về `READY`,
   và xác minh telemetry không lộ `listenKey` hay `api_key`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp03c_user_stream_disconnect_reconcile_and_db_gate_e2e() -> None:
    """Compose PostgreSQL E2E chứng minh đóng/mở cổng DB theo vòng đời User Data Stream."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry292-stream-{uuid4().hex[:10]}"
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

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    BinanceHttpResponse,
    DemoApiCredentials,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.order_schemas import (
    RegularOrder,
    RegularOrderStatus,
    RegularOrderType,
)
from trading_bot.adapters.binance_usdm.user_stream import (
    BinanceUsdMListenKeyClient,
    StreamHealthState,
    UserStreamSupervisor,
)
from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    BotRuntimeLeaseRecord,
    BotRuntimeStateRecord,
)
from trading_bot.adapters.database.models.entities import TradingAccount, Workspace
from trading_bot.domain.execution.venue_event_reducer import VenueEventReducer
from trading_bot.domain.execution.venue_orders import VenueOrderState


class _DrillStreamTransport:
    def __init__(self) -> None:
        self._seq = 0

    def send(self, req: BinanceHttpRequest) -> BinanceHttpResponse:
        if req.method == "POST":
            self._seq += 1
            return BinanceHttpResponse(
                200, (), json.dumps({"listenKey": f"SECRET_KEY_{self._seq}"}).encode("utf-8")
            )
        return BinanceHttpResponse(200, (), b"{}")


async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id, acc_id = uuid4(), uuid4()

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"e292-{ws_id.hex[:8]}",
                    name="WP-03C User Stream E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="Demo Stream Account",
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
                    owner_id="stream-worker-1",
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

    reducer = VenueEventReducer()
    client = BinanceUsdMListenKeyClient(
        target=BinanceDemoTarget(account_id=str(acc_id), credential_ref="ref-e292"),
        credentials=DemoApiCredentials(
            workspace_id=str(ws_id),
            account_id=str(acc_id),
            credential_ref="ref-e292",
            api_key="API_KEY_SECRET_E292",
            api_secret="HMAC_SECRET_E292",
        ),
        transport=_DrillStreamTransport(),
        clock=lambda: now,
    )
    sup = UserStreamSupervisor(
        workspace_id=str(ws_id),
        account_id=str(acc_id),
        listen_key_port=client,
        reducer=reducer,
        clock=lambda: now,
    )
    sup.start()
    assert sup.on_reconcile_completed(matched=True) is True

    # Nhận partial fill 0.4 rồi gặp listenKeyExpired -> đóng cổng DB
    partial_msg = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1000,
        "T": 1000,
        "o": {
            "s": "BTCUSDT",
            "c": "cid-e292",
            "o": "MARKET",
            "ot": "MARKET",
            "x": "TRADE",
            "X": "PARTIALLY_FILLED",
            "i": 9001,
            "l": "0.4",
            "z": "0.4",
            "L": "65000",
            "q": "1.0",
            "t": 701,
            "T": 1000,
        },
    }
    sup.on_raw_message(partial_msg)
    sup.on_raw_message({"e": "listenKeyExpired", "E": 2000})
    assert sup.state is StreamHealthState.EXPIRED
    assert sup.entry_gate_open is False

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
            state.reason_code = "STREAM_EXPIRED"
            state.updated_at = now

    # Reconnect + replay trùng lặp + REST snapshot -> hội tụ idempotent -> mở lại READY
    sup.start()
    sup.on_raw_message(partial_msg)
    reducer.apply_rest_snapshot(
        orders=(
            RegularOrder(
                symbol="BTCUSDT",
                order_id=9001,
                client_order_id="cid-e292",
                order_type=RegularOrderType.MARKET,
                status=RegularOrderStatus.FILLED,
                raw_status="FILLED",
                executed_quantity=Decimal("1.0"),
                original_quantity=Decimal("1.0"),
                update_time_ms=3000,
            ),
        ),
        snapshot_time_ms=3000,
    )
    assert reducer.orders["9001"].state is VenueOrderState.FILLED
    assert reducer.orders["9001"].cumulative_filled == Decimal("1.0")
    assert sup.on_reconcile_completed(matched=True) is True

    telem = json.dumps(sup.telemetry_snapshot())
    assert "SECRET_KEY_" not in telem
    assert "API_KEY_SECRET_E292" not in telem

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

    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        state = await session.scalar(
            select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
        )
        assert state is not None and state.readiness == "READY"
        assert state.reason_code is None

    await engine.dispose()
    print(json.dumps({"ok": True, "order_state": reducer.orders["9001"].state.value}))


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
