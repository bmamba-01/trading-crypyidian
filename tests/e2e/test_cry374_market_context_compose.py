"""Compose E2E cho read-model bối cảnh thị trường trên PostgreSQL thật (CRY-374)."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"


def test_market_context_read_model_on_compose_postgres() -> None:
    """Kiểm chứng DB/RLS, policy DISPLAY và từ chối khi thiếu quyền trên PostgreSQL thật."""
    project_name = f"crypidian-cry374-context-{uuid.uuid4().hex[:10]}"
    env = os.environ.copy()
    env.setdefault("APP_IMAGE", build_image_reference(REPO_ROOT))
    # compose.yaml đọc CRYPIDIAN_APP_IMAGE; nếu chỉ set APP_IMAGE thì dùng image :local cũ.
    env["CRYPIDIAN_APP_IMAGE"] = env["APP_IMAGE"]
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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.models.market_entities import MarketFunding, MarketInstrument
from trading_bot.adapters.database.models.research_entities import (
    SourcePolicyObservationRecord,
    SourcePolicyRevisionRecord,
)
from trading_bot.adapters.database.models.source_license_evidence_entities import (
    SourceLicenseEvidenceRecord,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.license_scope import (
    SourceLicenseScope,
    canonical_scope_json,
    scope_sha256,
)
from trading_bot.adapters.database.repositories.market_context import (
    SqlAlchemyMarketContextRepository,
)
from trading_bot.application.market_context import MarketContextReadModelService

SOURCE = "BINANCE_USDM"
SYMBOL = "BTCUSDT"

SCOPE = SourceLicenseScope(
    source_id=SOURCE,
    provider="BINANCE",
    product="MARKET",
    venue="USDM",
    contract="FUTURES",
    symbols=(SYMBOL,),
    intervals=("1m",),
    data_start=datetime.now(UTC) - timedelta(days=30),
    data_end=datetime.now(UTC) + timedelta(days=30),
    allowed_purposes=(SourceUsePurpose.DISPLAY,),
    allowed_outputs=("aggregate_report",),
    derived_use=("aggregate_reporting",),
    attribution="Binance Market Data",
    max_retention_days=90,
)


SCOPE_JSON = json.loads(canonical_scope_json(SCOPE))
EVIDENCE_REF = "https://www.binance.com/en/terms"


def policy(ws, sequence, policy_id, state, purposes, actor, reviewed_at, evidence, scope_json):
    return SourcePolicyRevisionRecord(
        id=uuid4(), workspace_id=ws, source_id=SOURCE, sequence=sequence,
        policy_id=policy_id, state=state, allowed_purposes=purposes,
        provider=SCOPE.provider, product=SCOPE.product, venue=SCOPE.venue,
        contract=SCOPE.contract,
        license_scope=scope_json,
        license_evidence_id=evidence.id if evidence else None,
        license_digest=evidence.digest if evidence else None,
        license_scope_hash=scope_sha256(SCOPE) if evidence else None,
        evidence_ref=EVIDENCE_REF if scope_json else None,
        reviewed_at=reviewed_at,
        valid_until=datetime.now(UTC) + timedelta(days=1),
        publisher="Binance", endpoint_url="https://fapi.binance.com/fapi/v1/klines",
        access_method="GET", auth_scope="public", attribution=SCOPE.attribution,
        retention_ref="ret-90d", quota_window_seconds=60, quota_max_requests=100,
        quota_max_bytes=1048576, quota_max_cost=Decimal("0"), quota_cost_unit="USD",
        actor_id=actor, reason="CRY-374 E2E policy",
        approved_by="reviewer-cry374" if state == "verified" else None,
    )


async def run_drill() -> None:
    password = open("/run/secrets/postgres_password", encoding="utf-8").read().strip()
    dsn = f"postgresql+psycopg://crypidian_admin:{password}@postgres:5432/crypidian"
    engine = create_async_engine(dsn)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    ws, inst = uuid4(), uuid4()

    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws)}
        )
        session.add(
            Workspace(
                id=ws, name="CRY374 E2E", slug=f"cry374-{ws.hex[:8]}",
                mode="PAPER", status="ACTIVE",
            )
        )
        session.add(
            MarketInstrument(
                id=inst, workspace_id=ws, source=SOURCE, idempotency_key="inst-cry374",
                symbol=SYMBOL, base_asset="BTC", quote_asset="USDT", tick_size=Decimal("0.1"),
                quantity_step=Decimal("0.001"), min_notional=Decimal("5"), status="TRADING",
                revision=1, checksum="0" * 64,
                event_at=now - timedelta(hours=1), received_at=now - timedelta(hours=1),
                available_at=now - timedelta(hours=1),
            )
        )
        await session.flush()
        event_at = now - timedelta(minutes=5)
        session.add(
            MarketFunding(
                id=uuid4(), workspace_id=ws, instrument_id=inst, source=SOURCE,
                idempotency_key="funding-cry374", revision=1, checksum="a" * 64,
                funding_time=event_at, event_at=event_at,
                received_at=event_at + timedelta(seconds=1),
                available_at=event_at + timedelta(seconds=2),
                funding_rate=Decimal("0.0001"), expires_at=now + timedelta(days=90),
            )
        )
        await session.flush()
        session.add(
            policy(
                ws, 1, "cry374-discovered", "discovered", ["display"],
                "operator-cry374", None, None, None,
            )
        )
        await session.flush()
        session.add(
            policy(
                ws, 2, "cry374-pending", "rights_pending", ["display"],
                "operator-cry374", None, None, SCOPE_JSON,
            )
        )
        await session.flush()
        evidence = SourceLicenseEvidenceRecord(
            id=uuid4(), workspace_id=ws, source_id=SOURCE,
            policy_id="cry374-pending", idempotency_key=uuid4(),
            evidence_ref=EVIDENCE_REF,
            digest="b" * 64, byte_count=123, media_type="text/plain",
            scope_version=SCOPE.scope_version,
            license_scope=SCOPE_JSON,
            canonical_scope=canonical_scope_json(SCOPE),
            scope_hash=scope_sha256(SCOPE),
            actor_id="operator-cry374",
        )
        session.add(evidence)
        await session.flush()
        session.add(
            policy(
                ws, 3, "cry374-verified", "verified", ["display"], "operator-cry374",
                now - timedelta(minutes=30), evidence, SCOPE_JSON,
            )
        )
        await session.commit()

    # Receipt quan sát phải được ghi bởi transaction/commit khác (known_at > created_at).
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws)}
        )
        for sequence, policy_id in (
            (1, "cry374-discovered"),
            (2, "cry374-pending"),
            (3, "cry374-verified"),
        ):
            session.add(
                SourcePolicyObservationRecord(
                    id=uuid4(), workspace_id=ws, source_id=SOURCE,
                    sequence=sequence, policy_id=policy_id,
                )
            )
        await session.commit()

    async with sessions() as session:
        repository = SqlAlchemyMarketContextRepository(session)
        report = await MarketContextReadModelService(repository).get_report(
            workspace_id=ws, symbol=SYMBOL
        )
        payload = report.to_dict()
        denied = await MarketContextReadModelService(
            repository, source_id="NO_RIGHTS_SOURCE"
        ).get_report(workspace_id=ws, symbol=SYMBOL)
        denied_payload = denied.to_dict()
    await engine.dispose()

    assert payload["display_allowed"] is True, payload
    assert payload["observations"][0]["checksum"] == "a" * 64, payload
    assert Decimal(payload["observations"][0]["value"]) == Decimal("0.0001"), payload
    funding_coverage = next(
        item for item in payload["coverage"] if item["kind"] == "funding"
    )
    assert funding_coverage["count"] == 1, funding_coverage
    assert funding_coverage["stale"] is False, funding_coverage
    assert denied_payload["display_allowed"] is False, denied_payload
    assert denied_payload["observations"] == [], denied_payload
    assert denied_payload["coverage"] == [], denied_payload
    print(json.dumps({"ok": True, "count": funding_coverage["count"], "denied": True}))

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
