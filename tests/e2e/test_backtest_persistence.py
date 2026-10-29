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


def _compose(repository_root: Path, project_name: str, environment: dict[str, str], *args: str):
    command = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project_name,
        "-f",
        "compose.yaml",
        *args,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        cwd=repository_root,
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


def test_backtest_run_trial_report_persist_once_and_are_tenant_isolated() -> None:
    repository_root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    project_name = f"crypidian-backtest-e2e-{uuid.uuid4().hex[:10]}"
    image_reference = build_image_reference(repository_root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        web_port = listener.getsockname()[1]
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image_reference,
        "TRADING_BOT_WEB_PORT": str(web_port),
    }
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        ensure_image_available(repository_root, image_reference)
        _compose(
            repository_root,
            project_name,
            environment,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
        )
        _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "check",
        )

        workspace_id = str(uuid.uuid4())
        workspace_seed = dedent(
            """
            import os
            from pathlib import Path
            import psycopg
            workspace_id = os.environ['CRYPIDIAN_E2E_WORKSPACE_ID']
            password = Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']).read_text().strip()
            with psycopg.connect(
                host='postgres',
                dbname='crypidian',
                user='crypidian_admin',
                password=password,
            ) as connection:
                connection.execute(
                    "INSERT INTO workspace (id, name, slug, mode, status) "
                    "VALUES (%s, 'Backtest E2E', %s, 'PAPER', 'ACTIVE')",
                    (workspace_id, 'backtest-' + workspace_id[:8]),
                )
            print(workspace_id)
            """
        ).strip()
        seeded = _compose(
            repository_root,
            project_name,
            environment | {"CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id},
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "migrator",
            "python",
            "-c",
            workspace_seed,
        )
        assert seeded.stdout.strip().endswith(workspace_id)

        probe_code = dedent(
            """
            import asyncio
            import json
            import os
            from pathlib import Path
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from sqlalchemy import func, select, text
            from sqlalchemy.exc import DBAPIError
            from trading_bot.adapters.database.engine import (
                create_database_engine, create_session_factory, database_url_from_secret,
            )
            from trading_bot.adapters.database.models.backtest_entities import (
                BacktestReportRecord, BacktestRunRecord, BacktestTrialRecord,
            )
            from trading_bot.adapters.database.repositories.backtests import BacktestRepository
            from trading_bot.application.backtest import (
                BacktestRunner, ReplayManifest, ReplayObservation,
            )
            from trading_bot.domain.learning.evaluation import EvaluationTrade
            from trading_bot.domain.market.models import OHLCVBar
            from trading_bot.domain.strategy.contracts import (
                DecisionSnapshot, MarketSeries, SeriesQuality,
            )
            from uuid import UUID, uuid4
            import hashlib

            async def main():
                workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host=os.environ.get('TRADING_BOT_DATABASE_HOST', 'postgres'),
                    database=os.environ.get('TRADING_BOT_DATABASE_NAME', 'crypidian'),
                )
                engine = create_database_engine(url)
                sessions = create_session_factory(engine)
                start = datetime(2026, 10, 20, tzinfo=UTC)
                manifest = ReplayManifest(
                    dataset_hash='a' * 64,
                    range_start=start,
                    range_end=start + timedelta(days=1),
                    settings_version_id='settings-e2e-v1',
                    risk_profile_version_id='risk-e2e-v1',
                    strategy_release_id='strategy-e2e-v1',
                    strategy_content_hash='b' * 64,
                    feature_schema_hash='c' * 64,
                    fill_model_version='paper-fill-e2e-v1',
                    pipeline_content_hash='d' * 64,
                    cost_assumptions=(('fee', '0.001'),),
                    parameter_family_hash='e' * 64,
                    trial_count=1,
                    trial_index=0,
                    seed=7,
                )
                observations = []
                for index in range(2):
                    decision_time = start + timedelta(minutes=10 * (index + 1))
                    bars = tuple(OHLCVBar(
                        symbol='BTCUSDT', timeframe='5m',
                        open_time=decision_time - timedelta(minutes=10 - 5 * offset),
                        close_time=decision_time - timedelta(minutes=5 - 5 * offset),
                        event_at=decision_time - timedelta(minutes=5 - 5 * offset),
                        received_at=decision_time - timedelta(minutes=5 - 5 * offset),
                        available_at=decision_time - timedelta(minutes=5 - 5 * offset),
                        open=Decimal('100'), high=Decimal('102'), low=Decimal('99'),
                        close=Decimal('101'), volume=Decimal('10'), is_closed=True,
                    ) for offset in range(2))
                    snapshot = DecisionSnapshot(
                        symbol='BTCUSDT', decision_time=decision_time,
                        snapshot_id=f'fixture-snapshot-{index}',
                        series=(MarketSeries(
                            timeframe='5m', bars=bars, quality=SeriesQuality.READY,
                            expected_interval=timedelta(minutes=5),
                            max_event_age=timedelta(minutes=10), backfill_complete=True,
                        ),), features=(), position_is_flat=True,
                    )
                    observations.append(ReplayObservation(
                        event_id=f'fixture-event-{index}', event_at=bars[-1].event_at,
                        available_at=bars[-1].available_at, snapshot=snapshot,
                    ))
                def evaluate(snapshot):
                    return EvaluationTrade(
                        trade_id=f'trade:{snapshot.snapshot_id}',
                        opened_at=snapshot.decision_time + timedelta(minutes=1),
                        closed_at=snapshot.decision_time + timedelta(minutes=2),
                        gross_pnl=Decimal('12'), fees=Decimal('1'),
                        funding=Decimal('-0.25'), slippage=Decimal('0.5'),
                        exposure=Decimal('100'),
                    )
                runner = BacktestRunner()
                result = runner.run(
                    tuple(observations), manifest=manifest, evaluator=evaluate
                )
                replay = runner.run(
                    tuple(reversed(observations)), manifest=manifest, evaluator=evaluate
                )
                assert result.report_hash == replay.report_hash
                assert result.trade_count == replay.trade_count == 2
                trade_hash = hashlib.sha256(json.dumps([
                    [trade.trade_id, trade.opened_at.isoformat(), trade.closed_at.isoformat(),
                     str(trade.gross_pnl), str(trade.fees), str(trade.funding),
                     str(trade.slippage), str(trade.exposure)]
                    for trade in sorted(
                        [evaluate(item.snapshot) for item in observations],
                        key=lambda trade: (trade.closed_at, trade.trade_id),
                    )
                ], separators=(',', ':')).encode()).hexdigest()
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                        {'workspace_id': str(workspace_id)},
                    )
                    repository = BacktestRepository(session)
                    first = await repository.append_result(
                        workspace_id=workspace_id, manifest=manifest, result=result
                    )
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                        {'workspace_id': str(workspace_id)},
                    )
                    retry = await BacktestRepository(session).append_result(
                        workspace_id=workspace_id, manifest=manifest, result=result
                    )
                    run_count = await session.scalar(
                        select(func.count()).select_from(BacktestRunRecord)
                    )
                    trial_count = await session.scalar(
                        select(func.count()).select_from(BacktestTrialRecord)
                    )
                    report_count = await session.scalar(
                        select(func.count()).select_from(BacktestReportRecord)
                    )
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                        {'workspace_id': str(uuid4())},
                    )
                    cross_workspace_count = await session.scalar(
                        select(func.count()).select_from(BacktestRunRecord)
                    )
                immutable = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(
                            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                            {'workspace_id': str(workspace_id)},
                        )
                        await session.execute(
                            text("UPDATE backtest_run SET manifest_hash = :hash WHERE id = :id"),
                            {'hash': 'f' * 64, 'id': first.run_id},
                        )
                except DBAPIError:
                    immutable = True
                await engine.dispose()
                print(json.dumps({
                    'run_id': str(first.run_id),
                    'trial_record_id': str(first.trial_record_id),
                    'report_id': str(first.report_id),
                    'retry_duplicate': retry.duplicate,
                    'run_count': run_count,
                    'trial_count': trial_count,
                    'report_count': report_count,
                    'cross_workspace_count': cross_workspace_count,
                    'immutable': immutable,
                    'manifest_hash': result.manifest_hash,
                    'replay_manifest_hash': replay.manifest_hash,
                    'report_hash': result.report_hash,
                    'replay_report_hash': replay.report_hash,
                    'trade_hash': trade_hash,
                    'net_pnl': str(result.net_pnl),
                }))

            asyncio.run(main())
            """
        ).strip()
        probe = _compose(
            repository_root,
            project_name,
            environment | {"CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id},
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "trainer-worker",
            "python",
            "-c",
            probe_code,
        )
        result = json.loads(probe.stdout.strip().splitlines()[-1])
        assert result["retry_duplicate"] is True
        assert result["run_count"] == 1
        assert result["trial_count"] == 1
        assert result["report_count"] == 1
        assert result["cross_workspace_count"] == 0
        assert result["immutable"] is True
        assert result["manifest_hash"] == result["replay_manifest_hash"]
        assert result["report_hash"] == result["replay_report_hash"]
        assert len(result["trade_hash"]) == 64
        assert result["net_pnl"] == "21.50"
        _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "c42e8f1a7d30",
        )
        _compose(
            repository_root,
            project_name,
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
        _compose(
            repository_root, project_name, environment, "down", "--volumes", "--remove-orphans"
        )
