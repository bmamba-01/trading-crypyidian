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

from scripts.docker_image import build_image_reference, ensure_image_available


def _compose(
    repository_root: Path,
    project_name: str,
    environment: dict[str, str],
    *args: str,
    success: bool = True,
):
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
    if (result.returncode == 0) is not success:
        raise AssertionError(
            f"Compose command failed: {args}\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-8000:]}"
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
            import hashlib
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
            from trading_bot.adapters.database.models.backtest_outcome_entities import BacktestRunDatasetRightsRecord
            from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
            from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict
            from trading_bot.application.backtest_sensitivity import SensitivityScenario
            sys.path.insert(0, '/e2e-tests')
            from fixtures.runtime_replay import (
                build_h01_dataset_runtime_replay,
                build_h03_runtime_sensitivity,
            )

            async def main():
                workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
                url = database_url_from_secret(
                    username='crypidian_replay_writer',
                    password_file=Path('/run/secrets/database_replay_writer_password'),
                    host=os.environ.get('TRADING_BOT_DATABASE_HOST', 'postgres'),
                    database=os.environ.get('TRADING_BOT_DATABASE_NAME', 'crypidian'),
                )
                engine = create_database_engine(url)
                sessions = create_session_factory(engine)
                trainer_engine = create_database_engine(database_url_from_secret(
                    username='crypidian_trainer_worker',
                    password_file=Path('/run/secrets/database_trainer_worker_password'),
                    host=os.environ.get('TRADING_BOT_DATABASE_HOST', 'postgres'),
                    database=os.environ.get('TRADING_BOT_DATABASE_NAME', 'crypidian'),
                ))
                trainer_sessions = create_session_factory(trainer_engine)
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
                dataset_bundle, dataset_comparison = build_h01_dataset_runtime_replay(workspace_id)
                if not dataset_comparison.is_complete or len(dataset_comparison.runs) != 1:
                    raise AssertionError('Replay từ bundle chưa hoàn tất qua BacktestRunner.')
                dataset_run = dataset_comparison.runs[0]
                dataset_report = dataset_run.sensitivity_run.result
                if dataset_report.observation_count != 4:
                    raise AssertionError('Runner phải xử lý đủ observation của evaluation window.')
                if dataset_run.trade_outcomes != () or dataset_report.trade_count != 0:
                    raise AssertionError('Fixture bundle H01 phải giữ nguyên mẫu số trade bằng 0.')
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                    dataset_repository = BacktestOutcomeRepository(session)
                    dataset_receipt = await dataset_repository.append_sensitivity_run(
                        workspace_id=workspace_id,
                        sensitivity_run=dataset_run.sensitivity_run,
                        trade_outcomes=dataset_run.trade_outcomes,
                        dataset_bundle=dataset_bundle,
                    )
                    first_rights_rows = tuple((await session.scalars(
                        select(BacktestRunDatasetRightsRecord)
                        .where(
                            BacktestRunDatasetRightsRecord.workspace_id == workspace_id,
                            BacktestRunDatasetRightsRecord.run_id == dataset_receipt.run_id,
                        )
                        .order_by(BacktestRunDatasetRightsRecord.interval)
                    )).all())
                    if len(first_rights_rows) != len(dataset_bundle.components):
                        raise AssertionError('Phải có đúng một rights receipt cho mỗi child manifest.')
                    expected_rights_rows = tuple(sorted((
                        BacktestRunDatasetRightsRecord.from_bundle_component(
                            workspace_id=workspace_id,
                            run_id=dataset_receipt.run_id,
                            bundle_hash=dataset_bundle.dataset_hash,
                            component=component,
                        )
                        for component in dataset_bundle.components
                    ), key=lambda item: item.interval))
                    if tuple(
                        (row.interval, row.child_dataset_hash, row.canonical_evidence, row.evidence_hash)
                        for row in first_rights_rows
                    ) != tuple(
                        (row.interval, row.child_dataset_hash, row.canonical_evidence, row.evidence_hash)
                        for row in expected_rights_rows
                    ):
                        raise AssertionError('Rights receipt đã lưu không khớp canonical evidence dự kiến.')
                    dataset_retry = await dataset_repository.append_sensitivity_run(
                        workspace_id=workspace_id,
                        sensitivity_run=dataset_run.sensitivity_run,
                        trade_outcomes=dataset_run.trade_outcomes,
                        dataset_bundle=dataset_bundle,
                    )
                    retry_rights_rows = tuple((await session.scalars(
                        select(BacktestRunDatasetRightsRecord)
                        .where(
                            BacktestRunDatasetRightsRecord.workspace_id == workspace_id,
                            BacktestRunDatasetRightsRecord.run_id == dataset_receipt.run_id,
                        )
                        .order_by(BacktestRunDatasetRightsRecord.interval)
                    )).all())
                    if tuple(
                        (row.canonical_evidence, row.evidence_hash) for row in retry_rights_rows
                    ) != tuple(
                        (row.canonical_evidence, row.evidence_hash) for row in first_rights_rows
                    ):
                        raise AssertionError('Retry đã thay đổi bằng chứng quyền đã lưu.')
                    dataset_restored = await dataset_repository.load_trade_outcomes(
                        workspace_id=workspace_id,
                        trial_record_id=dataset_receipt.trial_record_id,
                    )
                    dataset_report_row = await session.scalar(select(BacktestReportRecord).where(
                        BacktestReportRecord.workspace_id == workspace_id,
                        BacktestReportRecord.trial_record_id == dataset_receipt.trial_record_id,
                    ))
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
                for source, stored in zip(parameter_run.trade_outcomes, restored.trades, strict=True):
                    if not stored.has_entry_provenance or stored != source:
                        raise AssertionError('Provenance entry không được giữ qua PostgreSQL round trip.')
                    if ':runtime:' not in stored.entry_decision_snapshot_id:
                        raise AssertionError('Provenance phải gắn snapshot hiệu lực lúc submit.')
                    entry_payload = {
                        'entry_decision_snapshot_id': stored.entry_decision_snapshot_id,
                        'entry_decision_at': stored.entry_decision_at.isoformat(),
                        'entry_feature_schema_hash': stored.entry_feature_schema_hash,
                        'entry_features': stored.entry_features,
                    }
                    expected_entry_hash = hashlib.sha256(json.dumps(
                        entry_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                    ).encode()).hexdigest()
                    if stored.entry_features_hash != expected_entry_hash:
                        raise AssertionError('Hash vector entry không khớp canonical độc lập.')
                if multi_restored.trades != multi_run.trade_outcomes:
                    raise AssertionError('Hai outcome không giữ đúng provenance của từng intent.')
                if len({trade.entry_decision_snapshot_id for trade in multi_restored.trades}) != 2:
                    raise AssertionError('Hai entry khác nhau bị gộp snapshot provenance.')
                from trading_bot.domain.learning.evaluation import calculate_entry_features_hash
                original = parameter_run.trade_outcomes[0]
                changed_snapshot_id = original.entry_decision_snapshot_id + ':changed'
                changed_trade = replace(original,
                    entry_decision_snapshot_id=changed_snapshot_id,
                    entry_features_hash=calculate_entry_features_hash(
                        changed_snapshot_id, original.entry_decision_at,
                        original.entry_feature_schema_hash, original.entry_features,
                    ),
                )
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await BacktestOutcomeRepository(session).append_sensitivity_run(
                            workspace_id=workspace_id, sensitivity_run=parameter_run.sensitivity_run,
                            trade_outcomes=(changed_trade,),
                        )
                except BacktestRepositoryConflict:
                    pass
                else:
                    raise AssertionError('Cùng khóa nhưng khác entry provenance phải conflict.')
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
                    cross_rights_count = await session.scalar(select(func.count()).select_from(BacktestRunDatasetRightsRecord))
                async with trainer_sessions() as trainer_session:
                    trainer_can_read_bundle = await trainer_session.scalar(text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.backtest_dataset_replay_bundle', 'SELECT')"
                    ))
                immutable = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await session.execute(text("UPDATE backtest_trade_outcome SET trade_id = 'tampered' WHERE trial_record_id = :id"), {'id': first.trial_record_id})
                except DBAPIError:
                    immutable = True
                rights_update_rejected = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await session.execute(text(
                            "UPDATE backtest_run_dataset_rights SET evidence_hash = evidence_hash "
                            "WHERE workspace_id = :workspace_id AND run_id = :run_id"
                        ), {'workspace_id': str(workspace_id), 'run_id': dataset_receipt.run_id})
                except DBAPIError:
                    rights_update_rejected = True
                rights_delete_rejected = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await session.execute(text(
                            "DELETE FROM backtest_run_dataset_rights "
                            "WHERE workspace_id = :workspace_id AND run_id = :run_id"
                        ), {'workspace_id': str(workspace_id), 'run_id': dataset_receipt.run_id})
                except DBAPIError:
                    rights_delete_rejected = True
                rollback_source = dataset_run.sensitivity_run
                rollback_manifest = replace(rollback_source.manifest, seed=rollback_source.manifest.seed + 1000)
                rollback_trial = replace(
                    rollback_source.result.trial_record,
                    manifest_hash=rollback_manifest.content_hash,
                    trial_id=hashlib.sha256(f'trial:{rollback_manifest.content_hash}'.encode()).hexdigest(),
                )
                rollback_run = replace(
                    rollback_source,
                    manifest=rollback_manifest,
                    result=replace(rollback_source.result, manifest_hash=rollback_manifest.content_hash,
                        report_hash='0' * 64, trial_record=rollback_trial),
                )
                outcome_tables = ('backtest_run', 'backtest_trial', 'backtest_report',
                    'backtest_scenario', 'backtest_trade_outcome', 'backtest_run_dataset_rights')
                async def counts():
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        return tuple([await session.scalar(text(f'SELECT count(*) FROM {table}')) for table in outcome_tables])
                before_failed_insert = await counts()
                receipt_insert_denied = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        rollback_receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
                            workspace_id=workspace_id, sensitivity_run=rollback_run,
                            trade_outcomes=dataset_run.trade_outcomes,
                            dataset_bundle=dataset_bundle,
                        )
                        if rollback_receipt.duplicate:
                            raise AssertionError('Probe rollback phải tạo outcome mới.')
                        staged_rights = await session.scalar(select(func.count()).select_from(BacktestRunDatasetRightsRecord).where(
                            BacktestRunDatasetRightsRecord.run_id == rollback_receipt.run_id,
                        ))
                        if staged_rights != len(dataset_bundle.components):
                            raise AssertionError('Phải flush đủ child receipt trước INSERT gây lỗi.')
                        # INSERT lỗi trong cùng transaction caller phải rollback cả outcome vừa flush.
                        await session.execute(text(
                            'INSERT INTO backtest_run_dataset_rights '
                            'SELECT (jsonb_populate_record(NULL::backtest_run_dataset_rights, '
                            "to_jsonb(r) || jsonb_build_object('id', gen_random_uuid(), 'workspace_id', CAST(:other AS uuid), 'run_id', CAST(:new_run AS uuid)))).* "
                            'FROM backtest_run_dataset_rights r WHERE workspace_id=:workspace AND run_id=:source LIMIT 1'
                        ), {'other': str(UUID(int=workspace_id.int + 1)),
                            'new_run': str(rollback_receipt.run_id), 'workspace': workspace_id,
                            'source': dataset_receipt.run_id})
                except DBAPIError as error:
                    if error.orig.sqlstate != '42501' or 'row-level security policy' not in str(error):
                        raise AssertionError('Receipt INSERT phải thất bại do RLS WITH CHECK.') from error
                    receipt_insert_denied = True
                receipt_failure_rolled_back = receipt_insert_denied and await counts() == before_failed_insert
                await engine.dispose()
                await trainer_engine.dispose()
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
                    'dataset_bundle_hash': dataset_bundle.dataset_hash,
                    'dataset_manifest_hash': dataset_run.sensitivity_run.manifest.content_hash,
                    'dataset_observation_count': dataset_report.observation_count,
                    'dataset_report_hash': dataset_report_row.report_hash,
                    'dataset_outcome_count': dataset_receipt.trade_count,
                    'dataset_outcome_hash': dataset_receipt.outcomes_hash,
                    'dataset_outcome_hash_retry': dataset_retry.outcomes_hash,
                    'dataset_outcome_hash_reloaded': dataset_restored.outcomes_hash,
                    'dataset_retry_duplicate': dataset_retry.duplicate,
                    'dataset_run_id': str(dataset_receipt.run_id),
                    'dataset_rights_receipts': [
                        {
                            'interval': row.interval,
                            'child_dataset_hash': row.child_dataset_hash,
                            'canonical_evidence': row.canonical_evidence,
                            'evidence_hash': row.evidence_hash,
                        }
                        for row in first_rights_rows
                    ],
                    'dataset_rights_retry_unchanged': tuple(
                        (row.canonical_evidence, row.evidence_hash) for row in retry_rights_rows
                    ) == tuple(
                        (row.canonical_evidence, row.evidence_hash) for row in first_rights_rows
                    ),
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
                    'cross_rights_count': cross_rights_count,
                    'trainer_can_read_bundle': trainer_can_read_bundle,
                    'conflict_detected': conflict_detected,
                    'immutable': immutable,
                    'rights_update_rejected': rights_update_rejected,
                    'rights_delete_rejected': rights_delete_rejected,
                    'receipt_failure_rolled_back': receipt_failure_rolled_back,
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
            "db-provisioner",
            "python",
            "-c",
            probe_code,
        )
        result = json.loads(probe.stdout.strip().splitlines()[-1])
        privileged_probe_code = dedent(
            """
            import json
            import os
            from pathlib import Path
            from uuid import UUID
            import psycopg

            expected = json.loads(os.environ['WP07C_EXPECTED_RECEIPTS'])
            workspace_id = UUID(expected['workspace_id'])
            run_id = UUID(expected['run_id'])
            password = Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']).read_text().strip()
            mutations = (
                'UPDATE backtest_run_dataset_rights SET evidence_hash=evidence_hash ',
                'DELETE FROM backtest_run_dataset_rights ',
            )
            for mutation in mutations:
                try:
                    # Role migrator có quyền DML: lỗi phải đến từ trigger bất biến.
                    with psycopg.connect(host='postgres', dbname='crypidian',
                            user=os.environ['TRADING_BOT_DATABASE_USER'], password=password) as connection:
                        count = connection.execute(
                            'SELECT count(*) FROM backtest_run_dataset_rights WHERE workspace_id=%s AND run_id=%s',
                            (workspace_id, run_id),
                        ).fetchone()[0]
                        if count != len(expected['receipts']):
                            raise AssertionError('Thiếu receipt để kiểm tra trigger bất biến.')
                        connection.execute(mutation + 'WHERE workspace_id=%s AND run_id=%s', (workspace_id, run_id))
                except psycopg.Error as error:
                    if 'WP07C_REPLAY_BUNDLE_OR_RIGHTS_IMMUTABLE' not in str(error):
                        raise AssertionError('Mutation không bị chặn bởi trigger bất biến.') from error
                else:
                    raise AssertionError('Trigger đã cho phép sửa/xóa rights receipt.')

            try:
                with psycopg.connect(host='postgres', dbname='crypidian',
                        user=os.environ['TRADING_BOT_DATABASE_USER'], password=password) as connection:
                    connection.execute('SET LOCAL ROLE crypidian_replay_writer')
                    connection.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),))
                    # Sao chép row có thật, chỉ đổi UUID và workspace để chạm WITH CHECK.
                    connection.execute(
                        'INSERT INTO backtest_run_dataset_rights '
                        'SELECT (jsonb_populate_record(NULL::backtest_run_dataset_rights, '
                        "to_jsonb(r) || jsonb_build_object('id', gen_random_uuid(), 'workspace_id', %s::uuid))).* "
                        'FROM backtest_run_dataset_rights r WHERE workspace_id=%s AND run_id=%s LIMIT 1',
                        (UUID(int=workspace_id.int + 1), workspace_id, run_id),
                    )
            except psycopg.Error as error:
                if error.sqlstate != '42501' or 'row-level security policy' not in str(error):
                    raise AssertionError('INSERT khác workspace không bị RLS WITH CHECK chặn.') from error
            else:
                raise AssertionError('RLS đã cho phép INSERT receipt khác workspace.')
            print(json.dumps({'rights_trigger_update_delete': True, 'rights_insert_with_check': True}))
            """
        ).strip()
        admin_probe_code = dedent(
            """
            import asyncio
            import json
            import os
            from pathlib import Path
            from uuid import UUID
            from sqlalchemy import select, text
            from sqlalchemy.exc import DBAPIError
            from trading_bot.adapters.database.engine import create_database_engine, create_session_factory, database_url_from_secret
            from trading_bot.adapters.database.models.backtest_outcome_entities import BacktestRunDatasetRightsRecord

            async def main():
                expected = json.loads(os.environ['WP07C_EXPECTED_RECEIPTS'])
                workspace_id = UUID(expected['workspace_id'])
                run_id = UUID(expected['run_id'])
                engine = create_database_engine(database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host=os.environ.get('TRADING_BOT_DATABASE_HOST', 'postgres'),
                    database=os.environ.get('TRADING_BOT_DATABASE_NAME', 'crypidian'),
                ))
                sessions = create_session_factory(engine)
                async with sessions() as session, session.begin():
                    await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                    rows = tuple((await session.scalars(
                        select(BacktestRunDatasetRightsRecord)
                        .where(
                            BacktestRunDatasetRightsRecord.workspace_id == workspace_id,
                            BacktestRunDatasetRightsRecord.run_id == run_id,
                        )
                        .order_by(BacktestRunDatasetRightsRecord.interval)
                    )).all())
                    other_workspace_id = UUID(int=workspace_id.int + 1)
                    await session.execute(
                        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                        {'workspace_id': str(other_workspace_id)},
                    )
                    cross_rows = await session.scalar(text(
                        "SELECT count(*) FROM backtest_run_dataset_rights "
                        "WHERE workspace_id = :workspace_id AND run_id = :run_id"
                    ), {'workspace_id': str(workspace_id), 'run_id': run_id})
                    privileges = await session.execute(text(
                        "SELECT has_table_privilege(current_user, 'public.backtest_run_dataset_rights', 'SELECT'), "
                        "has_table_privilege(current_user, 'public.backtest_run_dataset_rights', 'INSERT'), "
                        "has_table_privilege(current_user, 'public.backtest_run_dataset_rights', 'UPDATE'), "
                        "has_table_privilege(current_user, 'public.backtest_run_dataset_rights', 'DELETE')"
                    ))
                    can_select, can_insert, can_update, can_delete = privileges.one()
                actual = [
                    {
                        'interval': row.interval,
                        'child_dataset_hash': row.child_dataset_hash,
                        'canonical_evidence': row.canonical_evidence,
                        'evidence_hash': row.evidence_hash,
                    }
                    for row in rows
                ]
                if actual != expected['receipts']:
                    raise AssertionError('admin-web đọc lại receipts không khớp bằng chứng worker đã ghi.')
                if cross_rows != 0:
                    raise AssertionError('admin-web nhìn thấy receipt ở workspace khác.')
                if not can_select or can_insert or can_update or can_delete:
                    raise AssertionError('Quyền admin-web trên rights receipt không đúng least privilege.')
                update_rejected = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await session.execute(text(
                            "UPDATE backtest_run_dataset_rights SET evidence_hash = evidence_hash "
                            "WHERE workspace_id = :workspace_id AND run_id = :run_id"
                        ), {'workspace_id': str(workspace_id), 'run_id': run_id})
                except DBAPIError:
                    update_rejected = True
                delete_rejected = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text("SELECT set_config('app.workspace_id', :workspace_id, true)"), {'workspace_id': str(workspace_id)})
                        await session.execute(text(
                            "DELETE FROM backtest_run_dataset_rights "
                            "WHERE workspace_id = :workspace_id AND run_id = :run_id"
                        ), {'workspace_id': str(workspace_id), 'run_id': run_id})
                except DBAPIError:
                    delete_rejected = True
                await engine.dispose()
                print(json.dumps({
                    'admin_readback': len(actual) == len(expected['receipts']),
                    'cross_workspace_hidden': cross_rows == 0,
                    'least_privilege': can_select and not can_insert and not can_update and not can_delete,
                    'update_rejected': update_rejected,
                    'delete_rejected': delete_rejected,
                }))

            asyncio.run(main())
            """
        ).strip()
        admin_environment = environment | {
            "WP07C_EXPECTED_RECEIPTS": json.dumps(
                {
                    "workspace_id": workspace_id,
                    "run_id": result["dataset_run_id"],
                    "receipts": result["dataset_rights_receipts"],
                }
            )
        }
        privileged_probe = _compose(
            repository_root,
            project_name,
            admin_environment,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            "WP07C_EXPECTED_RECEIPTS",
            "migrator",
            "python",
            "-c",
            privileged_probe_code,
        )
        assert json.loads(privileged_probe.stdout.strip().splitlines()[-1]) == {
            "rights_trigger_update_delete": True,
            "rights_insert_with_check": True,
        }
        admin_probe = _compose(
            repository_root,
            project_name,
            admin_environment,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            "WP07C_EXPECTED_RECEIPTS",
            "admin-web",
            "python",
            "-c",
            admin_probe_code,
        )
        admin_result = json.loads(admin_probe.stdout.strip().splitlines()[-1])
        assert result["scenario_id"] == "shorter-channel"
        assert len(result["dataset_bundle_hash"]) == 64
        assert len(result["dataset_manifest_hash"]) == 64
        assert result["dataset_observation_count"] == 4
        assert len(result["dataset_report_hash"]) == 64
        assert result["dataset_outcome_count"] == 0
        assert result["dataset_outcome_hash"] == result["dataset_outcome_hash_retry"]
        assert result["dataset_outcome_hash"] == result["dataset_outcome_hash_reloaded"]
        assert result["dataset_retry_duplicate"] is True
        assert len(result["dataset_rights_receipts"]) == 2
        assert {item["interval"] for item in result["dataset_rights_receipts"]} == {"1h", "4h"}
        assert all(len(item["evidence_hash"]) == 64 for item in result["dataset_rights_receipts"])
        assert result["dataset_rights_retry_unchanged"] is True
        assert result["scenario_kind"] == "PARAMETER"
        assert result["scenario_parameters"] == [["donchian_lookback", "20"]]
        assert result["report_hash_first"] == result["replay_report_hash"]
        assert result["report_hash_first"]["base"] != result["report_hash_first"]["slippage-stress"]
        assert len(result["scenario_hash"]) == 64
        assert result["outcome_hash_first"] == result["outcome_hash_retry"]
        assert result["outcome_hash_first"] == result["outcome_hash_reloaded"]
        assert result["reloaded_trade_id"] == result["source_trade_id"]
        assert result["retry_duplicate"] is True
        assert result["scenario_count"] == 6
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
        assert result["cross_rights_count"] == 0
        assert result["trainer_can_read_bundle"] is False
        assert result["conflict_detected"] is True
        assert result["immutable"] is True
        assert result["rights_update_rejected"] is True
        assert result["rights_delete_rejected"] is True
        assert result["receipt_failure_rolled_back"] is True
        assert admin_result == {
            "admin_readback": True,
            "cross_workspace_hidden": True,
            "least_privilege": True,
            "update_rejected": True,
            "delete_rejected": True,
        }
        lineage_probe = (repository_root / "tests/e2e/probes/wp06_entry_lineage.py").read_text(
            encoding="utf-8"
        )
        _compose(
            repository_root,
            project_name,
            environment | {"CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id},
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            "CRYPIDIAN_E2E_WORKSPACE_ID",
            "migrator",
            "python",
            "-c",
            lineage_probe,
        )
        downgrade = _compose(
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
            success=False,
        )
        assert "WP06_ENTRY_PROVENANCE_DOWNGRADE_REQUIRES_NO_PROVENANCE" in (
            downgrade.stdout + downgrade.stderr
        )
    finally:
        _compose(
            repository_root, project_name, environment, "down", "--volumes", "--remove-orphans"
        )
