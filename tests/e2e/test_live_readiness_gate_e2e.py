"""PostgreSQL Compose E2E cho server-side release gate G0–G5 (OPS07F / Task 10 / CRY-468).

Chứng minh trên PostgreSQL disposable thật (synthetic identity/evidence, không bật dispatch):
1. Dossier thiếu bằng chứng / self-approval bị từ chối server-side, ghi `RiskDecisionRecord`
   bất biến kèm `dossier_hash` và khóa `bot_runtime_state.readiness = 'RECOVERY_REQUIRED'`.
2. Dossier đầy đủ G0–G5 với 2 approver độc lập (`CODE_RELEASE` != `RISK_CAPITAL`) đạt G0–G5,
   lưu `dossier_hash` bất biến vào PostgreSQL, nhưng `live_dispatch_allowed` vẫn là `False`
   chờ quyết định G6 riêng.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_live_readiness_gate_postgres_immutable_hashes_and_entry_denial() -> None:
    """Compose PostgreSQL E2E kiểm immutable dossier hash, independent approver và entry denial."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry468-gate-{uuid4().hex[:10]}"
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

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    BotRuntimeLeaseRecord,
    BotRuntimeStateRecord,
)
from trading_bot.adapters.database.models.entities import TradingAccount, Workspace
from trading_bot.adapters.database.models.risk_entities import RiskDecisionRecord
from trading_bot.adapters.database.repositories.live_readiness_gate import (
    SqlAlchemyLiveReadinessGateRepository,
)
from trading_bot.domain.execution.live_readiness_dossier import (
    SUPPORTED_DOSSIER_SCHEMA_VERSION,
    ActorKind,
    ApprovalScope,
    DossierReasonCode,
    EvidenceStatus,
    GateEvidenceItem,
    LiveReadinessDossier,
    LiveReadinessGateRequest,
    ReadinessGateId,
    SignedApprovalRecord,
    sign_dossier_approval,
)

SIGNING_KEY = "cry468-e2e-signing-key"

def make_dossier(acc_id: str, now: datetime, *, code_actor: str, risk_actor: str):
    evidences = tuple(
        GateEvidenceItem(
            gate=g,
            evidence_id=f"ev-{g.value}",
            source_ref=f"e2e/{g.value}",
            command_or_check="pytest",
            status=EvidenceStatus.VERIFIED,
            verified_by="verifier-1",
            verified_at=now - timedelta(hours=1),
            expires_at=now + timedelta(days=7),
            evidence_hash="b" * 64,
            demo_days_observed=14 if g is ReadinessGateId.G4 else None,
            demo_samples_observed=150 if g is ReadinessGateId.G4 else None,
        )
        for g in ReadinessGateId
    )
    base = LiveReadinessDossier(
        schema_version=SUPPORTED_DOSSIER_SCHEMA_VERSION,
        build_hash="build-e2e-468",
        config_hash="cfg-e2e-468",
        risk_profile_hash="risk-e2e-468",
        venue="binance_usdm",
        environment="LIVE",
        account_id=acc_id,
        symbols=("BTCUSDT",),
        strategy_release_ids=("h01-v1",),
        required_demo_days=14,
        required_demo_samples=100,
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=3),
        evidences=evidences,
        approvals=(),
    )
    h = base.compute_dossier_hash()
    approvals = (
        SignedApprovalRecord(
            approval_scope=ApprovalScope.CODE_RELEASE,
            actor_id=code_actor,
            actor_kind=ActorKind.HUMAN_OWNER,
            membership_id="mem-code",
            role="owner",
            role_revoked=False,
            approved_dossier_hash=h,
            signed_at=now - timedelta(minutes=20),
            expires_at=now + timedelta(days=3),
            signature_digest=sign_dossier_approval(
                dossier_hash=h,
                approval_scope=ApprovalScope.CODE_RELEASE,
                actor_id=code_actor,
                signing_key=SIGNING_KEY,
            ),
        ),
        SignedApprovalRecord(
            approval_scope=ApprovalScope.RISK_CAPITAL,
            actor_id=risk_actor,
            actor_kind=ActorKind.HUMAN_RISK_APPROVER,
            membership_id="mem-risk",
            role="risk_admin",
            role_revoked=False,
            approved_dossier_hash=h,
            signed_at=now - timedelta(minutes=15),
            expires_at=now + timedelta(days=3),
            signature_digest=sign_dossier_approval(
                dossier_hash=h,
                approval_scope=ApprovalScope.RISK_CAPITAL,
                actor_id=risk_actor,
                signing_key=SIGNING_KEY,
            ),
        ),
    )
    return replace(base, approvals=approvals)

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
                    slug=f"g468-{ws_id.hex[:8]}",
                    name="Gate G0-G5 E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="Gate Account",
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
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            session.add(
                BotRuntimeLeaseRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    owner_id="gate-e2e-worker",
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
                    readiness="STARTING",
                    reason_code=None,
                    heartbeat_at=now,
                    fencing_token=1,
                )
            )

    # 1. Self-approval denial -> khóa bot_runtime_state và lưu RiskDecisionRecord(approved=False)
    bad_dossier = make_dossier(str(acc_id), now, code_actor="same-actor", risk_actor="same-actor")
    bad_req = LiveReadinessGateRequest(
        build_hash=bad_dossier.build_hash,
        config_hash=bad_dossier.config_hash,
        risk_profile_hash=bad_dossier.risk_profile_hash,
        venue=bad_dossier.venue,
        environment=bad_dossier.environment,
        account_id=bad_dossier.account_id,
        symbols=bad_dossier.symbols,
        strategy_release_ids=bad_dossier.strategy_release_ids,
        expected_dossier_hash=bad_dossier.compute_dossier_hash(),
        executor_actor_id="deploy-op",
    )
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            repo = SqlAlchemyLiveReadinessGateRepository(session)
            v_bad, _ = await repo.evaluate_and_record(
                workspace_id=ws_id,
                bot_id=bot_id,
                account_id=acc_id,
                dossier=bad_dossier,
                request=bad_req,
                signing_key=SIGNING_KEY,
                as_of=now,
                evaluation_key="self-approval",
            )
            assert v_bad.all_g0_g5_passed is False
            assert v_bad.live_dispatch_allowed is False
            assert v_bad.reason_code == DossierReasonCode.SELF_APPROVAL_FORBIDDEN

    # 2. Independent approvers -> đạt G0-G5, lưu immutable hash, Live vẫn khóa (chờ G6)
    good_dossier = make_dossier(str(acc_id), now, code_actor="owner-1", risk_actor="risk-2")
    good_req = LiveReadinessGateRequest(
        build_hash=good_dossier.build_hash,
        config_hash=good_dossier.config_hash,
        risk_profile_hash=good_dossier.risk_profile_hash,
        venue=good_dossier.venue,
        environment=good_dossier.environment,
        account_id=good_dossier.account_id,
        symbols=good_dossier.symbols,
        strategy_release_ids=good_dossier.strategy_release_ids,
        expected_dossier_hash=good_dossier.compute_dossier_hash(),
        executor_actor_id="deploy-op",
    )
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            repo = SqlAlchemyLiveReadinessGateRepository(session)
            v_good, _ = await repo.evaluate_and_record(
                workspace_id=ws_id,
                bot_id=bot_id,
                account_id=acc_id,
                dossier=good_dossier,
                request=good_req,
                signing_key=SIGNING_KEY,
                as_of=now,
                evaluation_key="independent-ok",
            )
            assert v_good.all_g0_g5_passed is True
            assert v_good.live_dispatch_allowed is False
            assert v_good.reason_code == DossierReasonCode.G0_G5_PASSED_LIVE_STILL_LOCKED

    # 3. Xác minh bền vững trên PostgreSQL
    async with sessions() as session:
        state = await session.scalar(
            select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
        )
        assert state is not None
        assert state.readiness == "RECOVERY_REQUIRED"
        assert state.reason_code == DossierReasonCode.SELF_APPROVAL_FORBIDDEN.value

        decisions = (
            await session.scalars(
                select(RiskDecisionRecord).where(RiskDecisionRecord.account_id == acc_id)
            )
        ).all()
        by_code = {d.reason_code: d for d in decisions}
        assert DossierReasonCode.SELF_APPROVAL_FORBIDDEN.value in by_code
        assert DossierReasonCode.G0_G5_PASSED_LIVE_STILL_LOCKED.value in by_code
        passed_dec = by_code[DossierReasonCode.G0_G5_PASSED_LIVE_STILL_LOCKED.value]
        assert passed_dec.plan_hash == good_dossier.compute_dossier_hash()
        assert passed_dec.payload["live_dispatch_allowed"] is False

    await engine.dispose()
    print(json.dumps({"ok": True, "decisions": len(decisions)}))

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
