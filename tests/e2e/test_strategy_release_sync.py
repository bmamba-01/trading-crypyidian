"""Compose E2E cho đồng bộ strategy_release khi content_hash đổi (CRY-437)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from scripts.docker_image import build_image_reference, ensure_image_available

CARD_RELEASE_ID = "sc-binance-usdm-vol-breakout-001-v1.0.0"
OLD_CARD_HASH = "9055c001" + "0" * 56

PROBE_CODE = """
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.models import (
    BotConfigVersion,
    RiskProfileVersion,
    StrategyRelease,
    TradingAccount,
    UserAccount,
    Workspace,
    UserMembership,
    WorkspaceSettingsVersion,
)
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    load_effective_bot_config,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.adapters.database.repositories.workspace_settings import (
    canonical_settings,
    settings_hash,
)
from trading_bot.config.workspace_settings import WorkspaceSettings

CARD_RELEASE_ID = "sc-binance-usdm-vol-breakout-001-v1.0.0"
OLD_CARD_HASH = "9055c001" + "0" * 56


async def _run() -> dict:
    phase = os.environ["CRY437_PHASE"]
    state = json.loads(os.environ["CRY437_STATE"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    sessions = create_session_factory(engine)
    now = datetime.now(UTC)
    try:
        if phase == "seed":
            workspace_id = UUID(state["workspace_id"])
            user_id = uuid4()
            membership_id = uuid4()
            profile_id = uuid4()
            settings_id = uuid4()
            account_id = uuid4()
            release_id = uuid4()
            bot_version_id = uuid4()

            risk_settings = {
                "schema_version": 1,
                "hard_caps": {
                    "max_aggregate_open_risk": "250",
                    "max_notional": "10000",
                    "max_leverage": "5",
                    "max_positions": 2,
                    "max_symbol_notional": "2000",
                    "max_concentration_fraction": "0.2",
                    "max_daily_loss": "200",
                    "max_rolling_loss": "500",
                    "max_account_age_seconds": 3600,
                    "max_data_age_seconds": 300,
                },
            }
            ws_settings = WorkspaceSettings.model_validate(
                {
                    "schema_version": 1,
                    "runtime": {"mode": "PAPER", "timezone": "UTC"},
                    "market_data": {
                        "venue": "BINANCE_USDM",
                        "symbols": ["BTCUSDT"],
                        "timeframes": ["1h", "4h"],
                        "max_age_seconds": 300,
                    },
                    "features": {
                        "formula_version": "crypidian-features-v1",
                        "sessions_timezone": "UTC",
                    },
                    "strategy": {
                        "default_release": "h03-v1",
                        "parameters": {"donchian_lookback": 55},
                        "entry_timeframe": "1h",
                        "confirmation_timeframe": "4h",
                    },
                    "risk": {"profile_version_id": str(profile_id)},
                    "paper_execution": {
                        "quote_asset": "USDT",
                        "starting_equity": "10000",
                        "fee_schedule_version": "paper-fees-v1",
                        "fill_model": "next_bar_with_adverse_costs",
                    },
                }
            )
            ws_payload = ws_settings.model_dump(mode="json")
            overlay = BotConfigOverlay.model_validate(
                {
                    "schema_version": 1,
                    "strategy": {"release_id": CARD_RELEASE_ID, "parameters": {}},
                }
            )
            ov_payload = canonical_overlay(overlay)

            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        text("SELECT set_config('app.workspace_id', :wid, true)"),
                        {"wid": str(workspace_id)},
                    )
                    session.add(
                        Workspace(
                            id=workspace_id,
                            name="CRY-437 Workspace",
                            slug=state["workspace_slug"],
                            mode="PAPER",
                            status="ACTIVE",
                        )
                    )
                    session.add(
                        UserAccount(
                            id=user_id,
                            email_normalized=f"cry437-{workspace_id.hex[:8]}@example.test",
                            password_hash="not-used",
                            status="ACTIVE",
                        )
                    )
                    await session.flush()
                    session.add(
                        UserMembership(
                            id=membership_id,
                            workspace_id=workspace_id,
                            user_id=user_id,
                            status="ACTIVE",
                        )
                    )
                    await session.flush()
                    session.add(
                        RiskProfileVersion(
                            id=profile_id,
                            workspace_id=workspace_id,
                            name="Conservative",
                            version=1,
                            max_risk_fraction=Decimal("0.01"),
                            settings=risk_settings,
                        )
                    )
                    await session.flush()
                    ws_payload = canonical_settings(ws_settings)
                    ws_hash = settings_hash(ws_payload)
                    session.add(
                        WorkspaceSettingsVersion(
                            id=settings_id,
                            workspace_id=workspace_id,
                            version=1,
                            schema_version=1,
                            status="PUBLISHED",
                            settings_json=ws_payload,
                            settings_hash=ws_hash,
                            created_by=membership_id,
                            approved_by=membership_id,
                            published_at=now - timedelta(hours=2),
                            effective_at=now - timedelta(hours=2),
                        )
                    )
                    session.add(
                        TradingAccount(
                            id=account_id,
                            workspace_id=workspace_id,
                            name="T03 CARD 1h",
                            venue="binance_usdm",
                            mode="PAPER",
                            environment="local",
                            status="ENABLED",
                            max_notional=Decimal("10000"),
                        )
                    )
                    await session.flush()
                    bot_id = await session.scalar(
                        text("SELECT id FROM bot_instance WHERE trading_account_id = :aid"),
                        {"aid": account_id},
                    )
                    session.add(
                        StrategyRelease(
                            id=release_id,
                            workspace_id=workspace_id,
                            name=CARD_RELEASE_ID,
                            version=1,
                            status="PUBLISHED",
                            recipe={},
                            content_hash=OLD_CARD_HASH,
                        )
                    )
                    bot_cfg = BotConfigVersion(
                        id=bot_version_id,
                        workspace_id=workspace_id,
                        bot_id=bot_id,
                        account_id=account_id,
                        version=1,
                        schema_version=1,
                        status="DRAFT",
                        overlay_json=ov_payload,
                        overlay_hash=overlay_hash(ov_payload),
                        base_settings_version_id=settings_id,
                        base_settings_hash=ws_hash,
                        created_by=membership_id,
                        updated_by=membership_id,
                        approved_by=None,
                        published_at=None,
                        effective_at=None,
                    )
                    session.add(bot_cfg)
                    await session.flush()
                    bot_cfg.status = "PUBLISHED"
                    bot_cfg.approved_by = membership_id
                    bot_cfg.published_at = now - timedelta(hours=1)
                    bot_cfg.effective_at = now - timedelta(hours=1)
            return {"account_id": str(account_id)}

        if phase == "verify_before":
            workspace_id = UUID(state["workspace_id"])
            account_id = UUID(state["account_id"])
            async with sessions() as session:
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :wid, true)"),
                    {"wid": str(workspace_id)},
                )
                try:
                    await load_effective_bot_config(
                        session,
                        workspace_id=workspace_id,
                        account_id=account_id,
                        as_of=now,
                        registry=strategy_registry(),
                    )
                except BotConfigResolutionError as err:
                    return {"error_code": err.code}
            return {"error_code": None}

        if phase == "verify_after":
            workspace_id = UUID(state["workspace_id"])
            account_id = UUID(state["account_id"])
            expected_hash = strategy_registry().get_release(CARD_RELEASE_ID).content_hash
            async with sessions() as session:
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :wid, true)"),
                    {"wid": str(workspace_id)},
                )
                effective = await load_effective_bot_config(
                    session,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    as_of=now,
                    registry=strategy_registry(),
                )
                rows = (
                    await session.scalars(
                        select(StrategyRelease)
                        .where(
                            StrategyRelease.workspace_id == workspace_id,
                            StrategyRelease.name == CARD_RELEASE_ID,
                        )
                        .order_by(StrategyRelease.version.asc())
                    )
                ).all()
                return {
                    "resolved_release_id": effective.strategy.release_id if effective else None,
                    "resolved_hash": (
                        effective.strategy.release_content_hash if effective else None
                    ),
                    "expected_hash": expected_hash,
                    "versions": [
                        {
                            "version": r.version,
                            "status": r.status,
                            "content_hash": r.content_hash,
                        }
                        for r in rows
                    ],
                }
        raise ValueError(f"Unknown phase: {phase}")
    finally:
        await engine.dispose()


print(json.dumps(asyncio.run(_run())))
"""


def test_strategy_release_sync_cli_unblocks_stale_card_bot() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-sc-sync-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
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

    def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Compose lệnh {args[:6]!r} thất bại:\n{result.stdout}\n{result.stderr}"
            )
        return result

    workspace_id = str(uuid.uuid4())
    workspace_slug = f"cry437-{uuid.uuid4().hex[:8]}"
    state: dict[str, Any] = {
        "workspace_id": workspace_id,
        "workspace_slug": workspace_slug,
    }

    def run_probe(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": json.dumps(state), "code": PROBE_CODE})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrator",
            "python",
            "-c",
            (
                "import json,os,sys;p=json.load(sys.stdin);"
                "os.environ.update(CRY437_PHASE=p['phase'],CRY437_STATE=p['state']);"
                "exec(compile(p['code'],'cry437_probe.py','exec'),{'__name__':'__main__'})"
            ),
            stdin=payload,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

        state.update(run_probe("seed"))
        before = run_probe("verify_before")
        assert before["error_code"] == "BOT_CONFIG_ROOT_MISMATCH"

        sync_out = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrator",
            "python",
            "-m",
            "trading_bot.cli.strategy_releases",
            "sync",
            "--workspace-slug",
            workspace_slug,
        )
        first_report = json.loads(sync_out.stdout)
        assert first_report["workspaces"][0]["items"][0]["action"] == "published_new_version"

        after = run_probe("verify_after")
        assert after["resolved_release_id"] == CARD_RELEASE_ID
        assert after["resolved_hash"] == after["expected_hash"]
        assert after["versions"] == [
            {"version": 1, "status": "SUPERSEDED", "content_hash": OLD_CARD_HASH},
            {"version": 2, "status": "PUBLISHED", "content_hash": after["expected_hash"]},
        ]

        # Chạy lại lần 2 phải idempotent (unchanged, không sinh version 3).
        sync_again = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrator",
            "python",
            "-m",
            "trading_bot.cli.strategy_releases",
            "sync",
            "--workspace-id",
            workspace_id,
        )
        second_report = json.loads(sync_again.stdout)
        assert second_report["workspaces"][0]["items"][0]["action"] == "unchanged"
    finally:
        subprocess.run(
            base + ["down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=120,
        )
