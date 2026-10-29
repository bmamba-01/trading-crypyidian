# ruff: noqa: E501

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


def test_sensitivity_scenario_and_replay_outcomes_are_persisted_and_reproducible() -> None:
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
    project_name = f"crypidian-outcome-e2e-{uuid.uuid4().hex[:10]}"
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
        _compose(repository_root, project_name, environment, "up", "--detach", "--wait")
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
        seed = dedent(
            """
            import os
            from pathlib import Path
            import psycopg
            workspace_id = os.environ['CRYPIDIAN_E2E_WORKSPACE_ID']
            password = Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']).read_text().strip()
            with psycopg.connect(host='postgres', dbname='crypidian', user='crypidian_admin', password=password) as connection:
                connection.execute(
                    "INSERT INTO workspace (id, name, slug, mode, status) VALUES (%s, 'Outcome E2E', %s, 'PAPER', 'ACTIVE')",
                    (workspace_id, 'outcome-' + workspace_id[:8]),
                )
            print(workspace_id)
            """
        ).strip()
        _compose(
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
            seed,
        )
        probe_code = dedent(
            """
            import asyncio
            import json
            import os
            from dataclasses import replace
            from decimal import Decimal
            from pathlib import Path
            import sys
            from uuid import UUID
            from sqlalchemy import func, select, text
            from sqlalchemy.exc import DBAPIError
            from trading_bot.adapters.database.engine import create_database_engine, create_session_factory, database_url_from_secret
            from trading_bot.adapters.database.models.backtest_entities import BacktestReportRecord
            from trading_bot.adapters.database.models.backtest_outcome_entities import BacktestScenarioRecord, BacktestTradeOutcomeRecord
            from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
            from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict
            from trading_bot.application.backtest_sensitivity import SensitivityScenario
            sys.path.insert(0, '/e2e-tests')
            from fixtures.runtime_replay import build_h03_runtime_sensitivity

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
                scenarios = (
                    SensitivityScenario(scenario_id='base', kind='BASE'),
                    SensitivityScenario(
                        scenario_id='fee-stress', kind='COST',
                        cost_overrides=(('entry_fee_rate', '0.002'), ('exit_fee_rate', '0.002')),
                    ),
                    SensitivityScenario(
                        scenario_id='slippage-stress', kind='COST',
                        cost_overrides=(('entry_slippage_rate', '0.002'), ('exit_slippage_rate', '0.002')),
                    ),
                    SensitivityScenario(
                        scenario_id='shorter-channel', kind='PARAMETER',
                        parameter_overrides=(('donchian_lookback', '20'),),
                    ),
                )
                comparison = build_h03_runtime_sensitivity(workspace_id, scenarios)
                repeated = build_h03_runtime_sensitivity(workspace_id, tuple(reversed(scenarios)))
                if not comparison.is_complete or not repeated.is_complete:
                    raise AssertionError('Runtime PAPER sensitivity replay chưa hoàn tất.')
                multi_comparison = build_h03_runtime_sensitivity(
                    workspace_id, (SensitivityScenario(scenario_id='base', kind='BASE'),),
                    include_second_trade=True,
                )
                multi_repeated = build_h03_runtime_sensitivity(
                    workspace_id, (SensitivityScenario(scenario_id='base', kind='BASE'),),
                    include_second_trade=True,
                )
                if not multi_comparison.is_complete or not multi_repeated.is_complete:
                    raise AssertionError('Replay hai giao dịch chưa hoàn tất.')
                multi_run = multi_comparison.runs[0]
                repeated_multi_run = multi_repeated.runs[0]
                if len(multi_run.trade_outcomes) != 2:
                    raise AssertionError('Replay hai giao dịch phải tạo đúng hai outcome.')
                if multi_run.trade_outcomes[0].net_pnl >= 0:
                    raise AssertionError('Giao dịch đầu cần lỗ để kiểm tra equity giảm.')
                if multi_run.trade_outcomes[1].exposure >= multi_run.trade_outcomes[0].exposure:
                    raise AssertionError('Entry thứ hai chưa phản ánh equity sau giao dịch đầu.')
                if multi_run.sensitivity_run.result.net_pnl != sum(
                    (trade.net_pnl for trade in multi_run.trade_outcomes), start=Decimal('0')
                ):
                    raise AssertionError('Report PnL không khớp tổng outcome của hai giao dịch.')
                first_receipts = {}
                for run in comparison.runs:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        first_receipts[run.sensitivity_run.scenario.scenario_id] = await BacktestOutcomeRepository(session).append_sensitivity_run(
                            workspace_id=workspace_id, sensitivity_run=run.sensitivity_run,
                            trade_outcomes=run.trade_outcomes,
                        )
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                    multi_receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
                        workspace_id=workspace_id, sensitivity_run=multi_run.sensitivity_run,
                        trade_outcomes=multi_run.trade_outcomes,
                    )
                parameter_run = next(run for run in comparison.runs if run.sensitivity_run.scenario.kind == 'PARAMETER')
                repeated_parameter_run = next(run for run in repeated.runs if run.sensitivity_run.scenario.kind == 'PARAMETER')
                slippage_run = next(run for run in comparison.runs if run.sensitivity_run.scenario.scenario_id == 'slippage-stress')
                repeated_slippage_run = next(run for run in repeated.runs if run.sensitivity_run.scenario.scenario_id == 'slippage-stress')
                first = first_receipts[parameter_run.sensitivity_run.scenario.scenario_id]
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                    repository = BacktestOutcomeRepository(session)
                    second = await repository.append_sensitivity_run(
                        workspace_id=workspace_id, sensitivity_run=repeated_parameter_run.sensitivity_run,
                        trade_outcomes=repeated_parameter_run.trade_outcomes,
                    )
                    slippage_retry = await repository.append_sensitivity_run(
                        workspace_id=workspace_id, sensitivity_run=repeated_slippage_run.sensitivity_run,
                        trade_outcomes=repeated_slippage_run.trade_outcomes,
                    )
                    restored = await repository.load_trade_outcomes(
                        workspace_id=workspace_id, trial_record_id=first.trial_record_id,
                    )
                    multi_retry = await repository.append_sensitivity_run(
                        workspace_id=workspace_id,
                        sensitivity_run=repeated_multi_run.sensitivity_run,
                        trade_outcomes=repeated_multi_run.trade_outcomes,
                    )
                    multi_restored = await repository.load_trade_outcomes(
                        workspace_id=workspace_id, trial_record_id=multi_receipt.trial_record_id,
                    )
                    multi_report = await session.scalar(select(BacktestReportRecord).where(
                        BacktestReportRecord.workspace_id == workspace_id,
                        BacktestReportRecord.trial_record_id == multi_receipt.trial_record_id,
                    ))
                    scenario_row = await session.scalar(select(BacktestScenarioRecord).where(
                        BacktestScenarioRecord.workspace_id == workspace_id,
                        BacktestScenarioRecord.trial_record_id == first.trial_record_id,
                    ))
                    scenario_count = await session.scalar(select(func.count()).select_from(BacktestScenarioRecord))
                    outcome_count = await session.scalar(select(func.count()).select_from(BacktestTradeOutcomeRecord))
                    slippage_receipt = first_receipts['slippage-stress']
                    slippage_scenario = await session.scalar(select(BacktestScenarioRecord).where(
                        BacktestScenarioRecord.workspace_id == workspace_id,
                        BacktestScenarioRecord.trial_record_id == slippage_receipt.trial_record_id,
                    ))
                    slippage_report = await session.scalar(select(BacktestReportRecord).where(
                        BacktestReportRecord.workspace_id == workspace_id,
                        BacktestReportRecord.trial_record_id == slippage_receipt.trial_record_id,
                    ))
                    slippage_outcomes = await repository.load_trade_outcomes(
                        workspace_id=workspace_id, trial_record_id=slippage_receipt.trial_record_id,
                    )
                conflict_detected = False
                # Kiểm tra xung đột bằng cách đổi tham số dưới cùng khóa trial đã lưu.
                changed_scenario_run = replace(parameter_run.sensitivity_run, scenario=SensitivityScenario(
                    scenario_id='shorter-channel', kind='PARAMETER',
                    parameter_overrides=(('donchian_lookback', '21'),),
                ))
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                    try:
                        await BacktestOutcomeRepository(session).append_sensitivity_run(
                            workspace_id=workspace_id, sensitivity_run=changed_scenario_run,
                            trade_outcomes=parameter_run.trade_outcomes,
                        )
                    except BacktestRepositoryConflict:
                        conflict_detected = True
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(UUID(int=workspace_id.int + 1))})
                    cross_scenario_count = await session.scalar(select(func.count()).select_from(BacktestScenarioRecord))
                    cross_outcome_count = await session.scalar(select(func.count()).select_from(BacktestTradeOutcomeRecord))
                immutable = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await session.execute(text("UPDATE backtest_trade_outcome SET trade_id = 'tampered' WHERE trial_record_id = :id"), {'id': first.trial_record_id})
                except DBAPIError:
                    immutable = True
                await engine.dispose()
                report_hashes = {
                    run.sensitivity_run.scenario.scenario_id: run.sensitivity_run.result.report_hash
                    for run in comparison.runs
                }
                repeated_report_hashes = {
                    run.sensitivity_run.scenario.scenario_id: run.sensitivity_run.result.report_hash
                    for run in repeated.runs
                }
                print(json.dumps({
                    'scenario_id': scenario_row.scenario_id,
                    'scenario_kind': scenario_row.kind,
                    'scenario_parameters': scenario_row.scenario['parameter_overrides'],
                    'scenario_hash': first.scenario_hash,
                    'report_hash_first': report_hashes,
                    'outcome_hash_first': first.outcomes_hash,
                    'outcome_hash_retry': second.outcomes_hash,
                    'outcome_hash_reloaded': restored.outcomes_hash,
                    'source_trade_id': parameter_run.trade_outcomes[0].trade_id,
                    'slippage_scenario': slippage_scenario.scenario,
                    'slippage_manifest_hash': slippage_scenario.manifest_hash,
                    'slippage_outcome_hash_first': first_receipts['slippage-stress'].outcomes_hash,
                    'slippage_outcome_hash_retry': slippage_retry.outcomes_hash,
                    'slippage_retry_duplicate': slippage_retry.duplicate,
                    'slippage_report': slippage_report.report,
                    'slippage_report_hash': slippage_report.report_hash,
                    'slippage_outcome': {
                        'slippage': str(slippage_outcomes.trades[0].slippage),
                        'net_pnl': str(slippage_outcomes.trades[0].net_pnl),
                        'hash': slippage_outcomes.outcomes_hash,
                    },
                    'replay_report_hash': repeated_report_hashes,
                    'reloaded_trade_id': restored.trades[0].trade_id,
                    'retry_duplicate': second.duplicate,
                    'scenario_count': scenario_count,
                    'outcome_count': outcome_count,
                    'multi_trade_count': len(multi_restored.trades),
                    'multi_trade_ids': [trade.trade_id for trade in multi_restored.trades],
                    'multi_outcome_hash': multi_receipt.outcomes_hash,
                    'multi_outcome_hash_retry': multi_retry.outcomes_hash,
                    'multi_outcome_hash_reloaded': multi_restored.outcomes_hash,
                    'multi_retry_duplicate': multi_retry.duplicate,
                    'multi_report_hash': multi_report.report_hash,
                    'multi_report_net_pnl': multi_report.report['net_pnl'],
                    'multi_trade_net_pnl': str(sum(
                        (trade.net_pnl for trade in multi_restored.trades),
                        Decimal('0'),
                    )),
                    'multi_first_exposure': str(multi_run.trade_outcomes[0].exposure),
                    'multi_second_exposure': str(multi_run.trade_outcomes[1].exposure),
                    'cross_scenario_count': cross_scenario_count,
                    'cross_outcome_count': cross_outcome_count,
                    'conflict_detected': conflict_detected,
                    'immutable': immutable,
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
            "--volume",
            f"{repository_root / 'tests'}:/e2e-tests:ro",
            "trainer-worker",
            "python",
            "-c",
            probe_code,
        )
        result = json.loads(probe.stdout.strip().splitlines()[-1])
        assert result["scenario_id"] == "shorter-channel"
        assert result["scenario_kind"] == "PARAMETER"
        assert result["scenario_parameters"] == [["donchian_lookback", "20"]]
        assert result["report_hash_first"] == result["replay_report_hash"]
        assert result["report_hash_first"]["base"] != result["report_hash_first"]["slippage-stress"]
        assert len(result["scenario_hash"]) == 64
        assert result["outcome_hash_first"] == result["outcome_hash_retry"]
        assert result["outcome_hash_first"] == result["outcome_hash_reloaded"]
        assert result["reloaded_trade_id"] == result["source_trade_id"]
        assert result["retry_duplicate"] is True
        assert result["scenario_count"] == 5
        assert result["outcome_count"] == 6
        assert result["multi_trade_count"] == 2
        assert len(result["multi_trade_ids"]) == 2
        assert result["multi_outcome_hash"] == result["multi_outcome_hash_retry"]
        assert result["multi_outcome_hash"] == result["multi_outcome_hash_reloaded"]
        assert result["multi_retry_duplicate"] is True
        assert result["multi_report_net_pnl"] == result["multi_trade_net_pnl"]
        assert len(result["multi_report_hash"]) == 64
        assert float(result["multi_second_exposure"]) < float(result["multi_first_exposure"])
        assert result["slippage_scenario"]["cost_overrides"] == [
            ["entry_slippage_rate", "0.002"],
            ["exit_slippage_rate", "0.002"],
        ]
        assert len(result["slippage_manifest_hash"]) == 64
        assert result["slippage_report"]["slippage"] == result["slippage_outcome"]["slippage"]
        assert result["slippage_report"]["net_pnl"] == result["slippage_outcome"]["net_pnl"]
        assert len(result["slippage_report_hash"]) == 64
        assert len(result["slippage_outcome"]["hash"]) == 64
        assert result["slippage_outcome_hash_first"] == result["slippage_outcome_hash_retry"]
        assert result["slippage_outcome_hash_first"] == result["slippage_outcome"]["hash"]
        assert result["slippage_retry_duplicate"] is True
        assert result["cross_scenario_count"] == result["cross_outcome_count"] == 0
        assert result["conflict_detected"] is True
        assert result["immutable"] is True
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
            "7a1d9e4c2b63",
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
