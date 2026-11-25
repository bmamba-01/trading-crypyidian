"""PostgreSQL Compose E2E cho atomic hard limits Live (OPS07F / Task 6 / CRY-464).

Chứng minh trên PostgreSQL thật (disposable Compose, không gọi sàn):
1. Hai yêu cầu giữ reservation đồng thời (concurrent) với tổng notional > cap: khóa hàng
   `trading_account` bảo đảm đúng 1 yêu cầu được duyệt và tổng active notional không vượt cap.
2. Khi transaction gặp lỗi và rollback, reservation `UNKNOWN` có sẵn vẫn được giữ nguyên
   trong DB và tiếp tục chiếm capacity cho yêu cầu kế tiếp.
3. Giảm cap hoặc bật pause có hiệu lực ngay lập tức trên yêu cầu tiếp theo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_live_risk_atomic_concurrent_reservation_unknown_rollback_and_immediate_pause() -> None:
    """Compose PostgreSQL E2E kiểm concurrent reservation, UNKNOWN rollback và giảm cap/pause."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry464-risk-{uuid4().hex[:10]}"
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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.entities import TradingAccount, Workspace
from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
from trading_bot.adapters.database.repositories.live_risk_enforcement import (
    AtomicLiveRiskEnforcementRepository,
)
from trading_bot.domain.risk.live_risk_profile import (
    LiveAccountMode,
    LiveOrderRiskRequest,
    LiveRiskReasonCode,
    SignedLiveRiskProfile,
    sign_live_risk_profile_digest,
)

SIGNING_KEY = "e2e-live-risk-signing-key"

def make_profile(acc_id: str, now: datetime, max_account: Decimal) -> SignedLiveRiskProfile:
    base = SignedLiveRiskProfile(
        profile_version="live-risk-e2e-v1",
        account_id=acc_id,
        environment="LIVE",
        build_revision="rev-e2e",
        account_mode=LiveAccountMode.SINGLE_ASSET_USDT,
        timezone_name="Asia/Ho_Chi_Minh",
        daily_reset_hour=0,
        rolling_window_hours=24,
        include_unrealized_loss=True,
        max_account_notional=max_account,
        max_symbol_notional=max_account,
        max_order_notional=Decimal("700"),
        max_leverage=Decimal("3"),
        max_aggregate_exposure_notional=max_account,
        max_daily_loss=Decimal("200"),
        max_rolling_loss=Decimal("300"),
        owner_id="owner-1",
        approver_id="risk-2",
        signed_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=12),
        signature_digest="placeholder",
    )
    sig = sign_live_risk_profile_digest(base.compute_hash(), signing_key=SIGNING_KEY)
    return replace(base, signature_digest=sig)

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url, pool_size=6)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    # 1. Seed workspace + trading_account (trigger tự tạo bot_instance)
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"risk-{ws_id.hex[:8]}",
                    name="Live Risk E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="Live Risk Account",
                    venue="binance_usdm",
                    mode="PAPER",
                    environment="local",
                    status="ENABLED",
                    max_notional=Decimal("1000"),
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

    profile = make_profile(str(acc_id), now, Decimal("1000"))

    async def attempt_reserve(
        idem_key: str,
        notional: Decimal,
        prof: SignedLiveRiskProfile,
        symbol: str = "BTCUSDT",
        paused: bool = False,
    ):
        async with sessions() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
                )
                await session.execute(
                    text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
                )
                repo = AtomicLiveRiskEnforcementRepository(session)
                req = LiveOrderRiskRequest(
                    symbol=symbol,
                    order_notional=notional,
                    leverage=Decimal("2"),
                    build_revision="rev-e2e",
                    expected_profile_hash=prof.compute_hash(),
                    paused_or_killed=paused,
                )
                decision, reservation = await repo.evaluate_and_reserve(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    profile=prof,
                    wallet_balance=Decimal("5000"),
                    unrealized_pnl=Decimal("0"),
                    available_balance=Decimal("5000"),
                    initial_margin_used=Decimal("0"),
                    fee_and_funding_today=Decimal("0"),
                    fee_and_funding_rolling=Decimal("0"),
                    open_exposure_notional=Decimal("0"),
                    symbol_open_exposure_notional=Decimal("0"),
                    mark_price=Decimal("90000"),
                    mark_observed_at=now - timedelta(seconds=1),
                    request=req,
                    realized_events=[],
                    idempotency_key=idem_key,
                    reserved_risk=Decimal("20"),
                    reserved_initial_margin=Decimal("150"),
                    reservation_ttl=timedelta(minutes=15),
                    as_of=now,
                    signing_key=SIGNING_KEY,
                )
                return decision, (reservation.id if reservation else None)

    # 2. Concurrent reservation: 2 request x 600 USDT (2 symbol khác nhau) với cap account 1000
    (dec_a, res_a), (dec_b, res_b) = await asyncio.gather(
        attempt_reserve("conc-a", Decimal("600"), profile, symbol="BTCUSDT"),
        attempt_reserve("conc-b", Decimal("600"), profile, symbol="ETHUSDT"),
    )
    approved_count = sum(1 for d in (dec_a, dec_b) if d.approved)
    denied = [d for d in (dec_a, dec_b) if not d.approved]
    assert approved_count == 1, (dec_a, dec_b)
    assert len(denied) == 1
    assert denied[0].reason_code == LiveRiskReasonCode.ACCOUNT_CAP_EXCEEDED
    winning_res_id = res_a or res_b
    assert winning_res_id is not None

    # 3. Chuyển reservation thắng sang UNKNOWN và giả lập rollback ở transaction khác
    async with sessions() as session:
        async with session.begin():
            res = await session.scalar(
                select(RiskReservationRecord).where(RiskReservationRecord.id == winning_res_id)
            )
            assert res is not None
            res.status = "UNKNOWN"

    try:
        async with sessions() as session:
            async with session.begin():
                res = await session.scalar(
                    select(RiskReservationRecord).where(RiskReservationRecord.id == winning_res_id)
                )
                assert res is not None
                raise RuntimeError("simulated-downstream-failure")
    except RuntimeError:
        pass

    # UNKNOWN reservation giữ 600 -> lệnh 500 trên symbol khác bị từ chối (600+500 > 1000)
    dec_after_unknown, res_after_unknown = await attempt_reserve(
        "after-unknown", Decimal("500"), profile, symbol="SOLUSDT"
    )
    assert dec_after_unknown.approved is False
    assert dec_after_unknown.reason_code == LiveRiskReasonCode.ACCOUNT_CAP_EXCEEDED
    assert res_after_unknown is None

    # 4. Giảm cap xuống 500 và bật pause -> hiệu lực ngay lập tức
    tighter_profile = make_profile(str(acc_id), now, Decimal("500"))
    dec_tighter, _ = await attempt_reserve(
        "tighter", Decimal("100"), tighter_profile, symbol="SOLUSDT"
    )
    assert dec_tighter.approved is False
    assert dec_tighter.reason_code == LiveRiskReasonCode.ACCOUNT_CAP_EXCEEDED

    dec_paused, _ = await attempt_reserve("paused", Decimal("100"), profile, paused=True)
    assert dec_paused.approved is False
    assert dec_paused.reason_code == LiveRiskReasonCode.PAUSED_OR_KILLED

    async with sessions() as session:
        sum_notional = func.coalesce(
            func.sum(RiskReservationRecord.reserved_notional), Decimal("0")
        )
        total_active = await session.scalar(
            select(sum_notional).where(
                RiskReservationRecord.account_id == acc_id,
                RiskReservationRecord.status.in_(["RESERVED", "UNKNOWN"]),
            )
        )
        assert total_active == Decimal("600"), total_active

    await engine.dispose()
    print(json.dumps({"ok": True, "active_notional": str(total_active)}))

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
