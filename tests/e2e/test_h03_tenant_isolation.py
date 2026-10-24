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


def _compose(
    repository_root: Path,
    project_name: str,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--project-name",
            project_name,
            "-f",
            "compose.yaml",
            *arguments,
        ],
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
            f"Compose command failed: {' '.join(arguments)}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def test_h03_decision_rejects_workspace_a_inputs_when_scoped_to_workspace_b() -> None:
    """Kiểm tra RLS, config, profile, reservation và ledger qua PostgreSQL Compose thật."""
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
    project_name = f"crypidian-h03-tenant-e2e-{uuid.uuid4().hex[:10]}"
    workspace_a = str(uuid.uuid4())
    workspace_b = str(uuid.uuid4())
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
            ["docker", "info", "--format", "{{.ServerVersion}}"],
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

        seed_code = dedent(
            """
            import asyncio
            import hashlib
            import json
            import os
            from dataclasses import replace
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine, database_url_from_secret,
            )
            from trading_bot.adapters.database.models import (
                RiskProfileVersion, TradingAccount, UserAccount, UserMembership,
                Workspace, WorkspaceSettingsVersion,
            )
            from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
            from trading_bot.adapters.database.models.risk_entities import (
                RiskDecisionRecord, RiskReservationRecord, TradePlanRecord,
            )
            from trading_bot.adapters.database.repositories.workspace_settings import (
                canonical_settings, settings_hash,
            )
            from trading_bot.config.workspace_settings import WorkspaceSettings
            from trading_bot.domain.market.feature_snapshot import (
                DataFreshness, FeatureSnapshot, MarketRegime, RegimeAssessment,
            )

            workspace_a = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_A'])
            workspace_b = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_B'])
            now = datetime.now(UTC)
            profile_a = uuid4()
            settings_a = uuid4()
            member_a = uuid4()
            member_b = uuid4()
            user_a = uuid4()
            user_b = uuid4()
            account_a = uuid4()
            account_b = uuid4()
            plan_id = uuid4()
            decision_id = uuid4()
            reservation_id = uuid4()
            plan_hash = hashlib.sha256(b'h03-tenant-plan-a').hexdigest()
            feature_values = {
                'atr_14': 2.0,
                'ema_20': 100.0,
                'ema_80': 99.0,
            }
            provisional_feature = FeatureSnapshot(
                snapshot_id='0' * 64,
                content_hash='0' * 64,
                workspace_id=workspace_a,
                symbol='BTCUSDT',
                timeframe='1h',
                as_of=now - timedelta(minutes=1),
                computed_at=now,
                schema_hash='a' * 64,
                formula_hash='b' * 64,
                code_hash='c' * 64,
                input_bar_ids=('fixture-bar-a',),
                input_event_ids=('fixture-event-a',),
                features=feature_values,
                warmup={'atr_14': True, 'ema_20': True, 'ema_80': True},
                quality_flags=(),
                data_freshness=DataFreshness.FRESH,
                data_coverage=1.0,
                regime=RegimeAssessment(
                    MarketRegime.TREND, 0.8, 1.0, 'FIXTURE_TREND', 'Dữ liệu kiểm thử.'
                ),
                is_usable=True,
            )
            feature_id = hashlib.sha256(provisional_feature.canonical_json().encode()).hexdigest()
            feature = replace(
                provisional_feature,
                snapshot_id=feature_id,
                content_hash=feature_id,
            )
            profile_caps = {
                'schema_version': 1,
                'hard_caps': {
                    'max_aggregate_open_risk': '250',
                    'max_notional': '10000',
                    'max_leverage': '5',
                    'max_positions': 2,
                    'max_symbol_notional': '2000',
                    'max_concentration_fraction': '0.2',
                    'max_daily_loss': '200',
                    'max_rolling_loss': '500',
                    'max_account_age_seconds': 30,
                    'max_data_age_seconds': 300,
                },
            }
            settings_payload = {
                'schema_version': 1,
                'runtime': {'mode': 'PAPER', 'timezone': 'UTC'},
                'market_data': {
                    'venue': 'BINANCE_USDM', 'symbols': ['BTCUSDT'],
                    'timeframes': ['1h', '4h'], 'max_age_seconds': 30,
                },
                'features': {
                    'formula_version': 'crypidian-features-v1', 'sessions_timezone': 'UTC',
                },
                'strategy': {
                    'default_release': 'h03-v1', 'parameters': {},
                    'entry_timeframe': '1h', 'confirmation_timeframe': '4h',
                },
                'risk': {'profile_version_id': str(profile_a)},
                'paper_execution': {
                    'quote_asset': 'USDT', 'starting_equity': '10000',
                    'fee_schedule_version': 'paper-fees-v1',
                    'fill_model': 'next_bar_with_adverse_costs',
                },
            }
            settings_json = canonical_settings(WorkspaceSettings.model_validate(settings_payload))
            settings_digest = settings_hash(settings_json)
            database_url = database_url_from_secret(
                username=os.environ['TRADING_BOT_DATABASE_USER'],
                password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                host='postgres', database='crypidian',
            )
            engine = create_database_engine(database_url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async def main():
                async with sessions() as session, session.begin():
                    session.add_all([
                        Workspace(id=workspace_a, name='Tenant A', slug='tenant-a-' + str(workspace_a)[:8], mode='PAPER', status='ACTIVE'),
                        Workspace(id=workspace_b, name='Tenant B', slug='tenant-b-' + str(workspace_b)[:8], mode='PAPER', status='ACTIVE'),
                    ])
                    await session.flush()
                    session.add_all([
                        UserAccount(id=user_a, email_normalized='tenant-a-' + str(workspace_a)[:8] + '@example.test', password_hash='seed-only', status='ACTIVE'),
                        UserAccount(id=user_b, email_normalized='tenant-b-' + str(workspace_b)[:8] + '@example.test', password_hash='seed-only', status='ACTIVE'),
                        UserMembership(id=member_a, workspace_id=workspace_a, user_id=user_a, status='ACTIVE'),
                        UserMembership(id=member_b, workspace_id=workspace_b, user_id=user_b, status='ACTIVE'),
                    ])
                    await session.flush()
                    session.add_all([
                        TradingAccount(id=account_a, workspace_id=workspace_a, name='tenant-a-paper', venue='binance_usdm', mode='PAPER', environment='local', status='DISABLED', max_notional=Decimal('10000')),
                        TradingAccount(id=account_b, workspace_id=workspace_b, name='tenant-b-paper', venue='binance_usdm', mode='PAPER', environment='local', status='DISABLED', max_notional=Decimal('10000')),
                        RiskProfileVersion(id=profile_a, workspace_id=workspace_a, name='A conservative', version=1, max_risk_fraction=Decimal('0.01'), settings=profile_caps, is_active=True),
                    ])
                    await session.flush()
                    session.add_all([
                        WorkspaceSettingsVersion(id=settings_a, workspace_id=workspace_a, version=1, parent_version_id=None, schema_version=1, settings_json=settings_json, settings_hash=settings_digest, status='PUBLISHED', revision=1, created_by=member_a, approved_by=member_a, published_at=now, effective_at=now),
                        FeatureSnapshotRecord(id=uuid4(), workspace_id=workspace_a, snapshot_id=feature.snapshot_id, content_hash=feature.content_hash, symbol=feature.symbol, timeframe=feature.timeframe, as_of=feature.as_of, computed_at=feature.computed_at, schema_hash=feature.schema_hash, formula_hash=feature.formula_hash, code_hash=feature.code_hash, input_bar_ids=list(feature.input_bar_ids), input_event_ids=list(feature.input_event_ids), features=dict(feature.features), warmup=dict(feature.warmup), quality_flags=list(feature.quality_flags), data_freshness=feature.data_freshness.value, data_coverage=feature.data_coverage, regime={'label': feature.regime.label.value, 'confidence': feature.regime.confidence, 'coverage': feature.regime.coverage, 'reason_code': feature.regime.reason_code, 'reason_vi': feature.regime.reason_vi}, is_usable=True, canonical_json=feature.canonical_json()),
                    ])
                    await session.flush()
                    # Trigger đã tạo bot cho account A; fixture ghi lineage qua scope bot đó.
                    bot_a = await session.scalar(
                        text('SELECT id FROM bot_instance WHERE workspace_id = :w AND trading_account_id = :a'),
                        {'w': workspace_a, 'a': account_a},
                    )
                    await session.execute(
                        text("SELECT set_config('app.bot_id', :bot, true)"), {'bot': str(bot_a)}
                    )
                    session.add(TradePlanRecord(
                        id=plan_id, workspace_id=workspace_a, account_id=account_a,
                        plan_hash=plan_hash, symbol='BTCUSDT', notional=Decimal('100'),
                        payload={'plan_hash': plan_hash},
                    ))
                    await session.flush()
                    session.add(RiskDecisionRecord(
                        id=decision_id, workspace_id=workspace_a, account_id=account_a,
                        decision_key=hashlib.sha256(b'h03-tenant-decision-a').hexdigest(),
                        plan_hash=plan_hash, snapshot_id=feature.snapshot_id, approved=True,
                        reason_code='APPROVED', detail_vi='Fixture tenant A.',
                        payload={'candidate_expires_at': (now + timedelta(hours=1)).isoformat()},
                    ))
                    await session.flush()
                    session.add(RiskReservationRecord(
                        id=reservation_id, workspace_id=workspace_a, account_id=account_a,
                        risk_decision_id=decision_id,
                        idempotency_key='h03-tenant-a-reservation', plan_hash=plan_hash,
                        symbol='BTCUSDT', reserved_risk=Decimal('1'),
                        reserved_notional=Decimal('100'),
                        reserved_initial_margin=Decimal('50'),
                        expires_at=now + timedelta(hours=1), status='RESERVED',
                    ))
            asyncio.run(main())
            asyncio.run(engine.dispose())
            print(json.dumps({
                'workspace_a': str(workspace_a), 'workspace_b': str(workspace_b),
                'profile_a': str(profile_a), 'settings_a': str(settings_a),
                'account_a': str(account_a), 'account_b': str(account_b),
                'plan_hash': plan_hash, 'decision_a': str(decision_id),
                'reservation_a': str(reservation_id), 'feature_a': feature.snapshot_id,
                'settings_hash_a': settings_digest,
            }))
            """
        ).strip()
        seeded = _compose(
            repository_root,
            project_name,
            environment
            | {
                "CRYPIDIAN_E2E_WORKSPACE_A": workspace_a,
                "CRYPIDIAN_E2E_WORKSPACE_B": workspace_b,
            },
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_A={workspace_a}",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_B={workspace_b}",
            "migrator",
            "python",
            "-c",
            seed_code,
        )
        seed = json.loads(seeded.stdout.strip().splitlines()[-1])

        probe_code = dedent(
            """
            import asyncio
            import json
            import os
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID
            from sqlalchemy import func, select
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine, database_url_from_secret,
            )
            from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord
            from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
            from trading_bot.adapters.database.repositories.feature_snapshots import FeatureSnapshotRepository
            from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerConflict, PaperLedgerRepository
            from trading_bot.adapters.database.repositories.risk_reservations import ReservationConflict, RiskReservationRepository
            from trading_bot.adapters.database.repositories.workspace_settings import WorkspaceSettingsRepository
            from trading_bot.application.outbox import UnitOfWork
            from trading_bot.application.paper_decision_cycle import DecisionCycleOutcome, PaperDecisionCycle
            from trading_bot.application.runtime_risk_profile import (
                RiskProfileRuntimeError, load_risk_profile,
            )
            from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
            from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
            from trading_bot.domain.risk.engine import RiskEngine, RiskState
            from trading_bot.domain.risk.planner import AccountSnapshot, CostModel, ExchangeRules, OpenExposure, PlannerSettings, RiskLimits, TradePlanner
            from trading_bot.domain.strategy.contracts import DecisionSnapshot
            from trading_bot.domain.strategy.registry import StrategyRegistry

            workspace_a = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_A'])
            workspace_b = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_B'])
            profile_a = UUID(os.environ['CRYPIDIAN_E2E_PROFILE_A'])
            settings_a = UUID(os.environ['CRYPIDIAN_E2E_SETTINGS_A'])
            account_a = UUID(os.environ['CRYPIDIAN_E2E_ACCOUNT_A'])
            account_b = UUID(os.environ['CRYPIDIAN_E2E_ACCOUNT_B'])
            reservation_a = UUID(os.environ['CRYPIDIAN_E2E_RESERVATION_A'])
            feature_a = os.environ['CRYPIDIAN_E2E_FEATURE_A']
            now = datetime.now(UTC)
            url = database_url_from_secret(
                username=os.environ['TRADING_BOT_DATABASE_USER'],
                password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                host='postgres', database='crypidian',
            )
            engine = create_database_engine(url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            settings_payload = {
                'schema_version': 1,
                'runtime': {'mode': 'PAPER', 'timezone': 'UTC'},
                'market_data': {'venue': 'BINANCE_USDM', 'symbols': ['BTCUSDT'], 'timeframes': ['1h', '4h'], 'max_age_seconds': 30},
                'features': {'formula_version': 'crypidian-features-v1', 'sessions_timezone': 'UTC'},
                'strategy': {'default_release': 'h03-v1', 'parameters': {}, 'entry_timeframe': '1h', 'confirmation_timeframe': '4h'},
                'risk': {'profile_version_id': str(profile_a)},
                'paper_execution': {'quote_asset': 'USDT', 'starting_equity': '10000', 'fee_schedule_version': 'paper-fees-v1', 'fill_model': 'next_bar_with_adverse_costs'},
            }
            published_a = PublishedWorkspaceSettings(
                workspace_id=workspace_a, settings_version_id=settings_a,
                settings_hash=os.environ['CRYPIDIAN_E2E_SETTINGS_HASH_A'],
                settings=WorkspaceSettings.model_validate(settings_payload),
            )
            planner_settings_b = PlannerSettings(
                workspace_id=str(workspace_b), settings_version_id=str(settings_a),
                risk_profile_version_id=str(profile_a), published=True,
                active_release_ids=('h03-v1',), leverage=Decimal('2'),
                exchange=ExchangeRules(Decimal('0.1'), Decimal('0.01'), Decimal('1'), Decimal('5'), Decimal('10000'), Decimal('100')),
                limits=RiskLimits(Decimal('0.01'), Decimal('250'), Decimal('10000'), Decimal('5'), 2, Decimal('2000'), Decimal('0.2'), Decimal('200'), Decimal('500'), timedelta(seconds=30), timedelta(minutes=5)),
                release_content_hashes=(('h03-v1', 'a' * 64),),
            )
            account_b_snapshot = AccountSnapshot(
                workspace_id=str(workspace_b), account_id=str(account_b),
                settings_version_id=str(settings_a), captured_at=now,
                equity=Decimal('10000'), available_margin=Decimal('10000'), is_known=True,
            )
            risk_state_b = RiskState(
                workspace_id=str(workspace_b), account_id=str(account_b),
                settings_version_id=str(settings_a), risk_profile_version_id=str(profile_a),
                evaluated_at=now, account_captured_at=now, market_data_as_of=now,
                equity=Decimal('10000'), available_margin=Decimal('10000'),
                open_exposures=(), realized_loss_today=Decimal('0'),
                realized_loss_rolling=Decimal('0'), open_position_count=0,
                is_known=True, pending_reservation_initial_margin=Decimal('0'),
            )
            cycle = PaperDecisionCycle(
                StrategyRegistry(), TradePlanner(), RiskEngine(),
                PaperExecutionGateway(PaperExecutionPolicy('paper-e2e-v1', timedelta(0), Decimal(0), Decimal(0), Decimal('0.001'))),
            )
            snapshot = DecisionSnapshot(
                symbol='BTCUSDT', decision_time=now, snapshot_id=feature_a,
                series=(), features=(), position_is_flat=True,
            )
            async def main():
                observed = {}
                async with UnitOfWork(sessions, workspace_id=workspace_b) as uow:
                    scoped_settings = await WorkspaceSettingsRepository(uow.session).load_effective_config(workspace_id=workspace_a)
                    observed['foreign_settings_hidden'] = scoped_settings is None
                    scoped_feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                        workspace_id=workspace_a, symbol='BTCUSDT', timeframe='1h', as_of=now,
                    )
                    observed['foreign_feature_hidden'] = scoped_feature is None
                    try:
                        await cycle.prepare_published_with_risk_profile(
                            session=uow.session, published_settings=published_a,
                            snapshot=snapshot, evaluated_at=now,
                            planner_settings=planner_settings_b,
                            account=account_b_snapshot, risk_state=risk_state_b,
                            open_exposures=(), cost_model=CostModel('tenant-test', Decimal('0.001'), Decimal('0.001'), Decimal('0.05'), Decimal(0)),
                        )
                    except RiskProfileRuntimeError:
                        observed['foreign_profile_rejected'] = True
                    else:
                        observed['foreign_profile_rejected'] = False
                    early = cycle.prepare_published(
                        published_settings=published_a, snapshot=snapshot,
                        evaluated_at=now, planner_settings=planner_settings_b,
                        account=account_b_snapshot, risk_state=risk_state_b,
                        open_exposures=(), cost_model=CostModel('tenant-test', Decimal('0.001'), Decimal('0.001'), Decimal('0.05'), Decimal(0)),
                    )
                    observed['coordinator_rejected'] = (
                        early.outcome is DecisionCycleOutcome.NO_TRADE
                        and early.strategy_result.code == 'PUBLISHED_SETTINGS_SCOPE_MISMATCH'
                    )
                    try:
                        await RiskReservationRepository(uow.session).mark_unknown(
                            workspace_id=workspace_a, account_id=account_a,
                            reservation_id=reservation_a,
                        )
                    except ReservationConflict:
                        observed['foreign_reservation_rejected'] = True
                    else:
                        observed['foreign_reservation_rejected'] = False
                    try:
                        await PaperLedgerRepository().create_intent(
                            uow, account_id=account_a,
                            idempotency_key='h03-tenant-cross-scope-attempt',
                            plan_hash=os.environ['CRYPIDIAN_E2E_PLAN_HASH_A'],
                            settings_version_id=settings_a,
                            strategy_release_id=UUID('20000000-0000-4000-8000-000000000002'),
                            strategy_release_hash='a' * 64,
                            feature_snapshot_id=feature_a,
                            candidate_id='e' * 64,
                            risk_decision_id=UUID(os.environ['CRYPIDIAN_E2E_DECISION_A']),
                            risk_reservation_id=reservation_a,
                            symbol='BTCUSDT', side='LONG', quote_asset='USDT',
                            quantity=Decimal('1'), created_at=now,
                            evidence={'feature_snapshot_id': feature_a},
                        )
                    except PaperLedgerConflict:
                        observed['foreign_intent_rejected'] = True
                    else:
                        observed['foreign_intent_rejected'] = False
                    observed['workspace_b_intents'] = await uow.session.scalar(
                        select(func.count()).select_from(OrderIntentRecord)
                    )
                    observed['workspace_b_reservations'] = await uow.session.scalar(
                        select(func.count()).select_from(RiskReservationRecord)
                    )
                async with UnitOfWork(sessions, workspace_id=workspace_a) as uow:
                    await uow.bind_account(account_a)
                    visible_settings = await WorkspaceSettingsRepository(
                        uow.session
                    ).load_effective_config(workspace_id=workspace_a)
                    visible_profile = await load_risk_profile(
                        uow.session, workspace_id=workspace_a, profile_id=profile_a,
                    )
                    visible_feature = await FeatureSnapshotRepository(
                        uow.session
                    ).get_as_of(
                        workspace_id=workspace_a, symbol='BTCUSDT',
                        timeframe='1h', as_of=now,
                    )
                    reservation = await uow.session.scalar(
                        select(RiskReservationRecord).where(
                            RiskReservationRecord.workspace_id == workspace_a,
                            RiskReservationRecord.id == reservation_a,
                        )
                    )
                    observed['workspace_a_settings_visible'] = (
                        visible_settings is not None
                        and visible_settings.settings_version_id == settings_a
                    )
                    observed['workspace_a_profile_visible'] = (
                        visible_profile.profile_id == profile_a
                    )
                    observed['workspace_a_feature_visible'] = (
                        visible_feature is not None
                        and visible_feature.snapshot_id == feature_a
                    )
                    observed['workspace_a_reservation_status'] = reservation.status if reservation else None
                await engine.dispose()
                print(json.dumps(observed))
            asyncio.run(main())
            """
        ).strip()
        probed = _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_A={workspace_a}",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_B={workspace_b}",
            "-e",
            f"CRYPIDIAN_E2E_PROFILE_A={seed['profile_a']}",
            "-e",
            f"CRYPIDIAN_E2E_SETTINGS_A={seed['settings_a']}",
            "-e",
            f"CRYPIDIAN_E2E_SETTINGS_HASH_A={seed['settings_hash_a']}",
            "-e",
            f"CRYPIDIAN_E2E_ACCOUNT_A={seed['account_a']}",
            "-e",
            f"CRYPIDIAN_E2E_ACCOUNT_B={seed['account_b']}",
            "-e",
            f"CRYPIDIAN_E2E_RESERVATION_A={seed['reservation_a']}",
            "-e",
            f"CRYPIDIAN_E2E_FEATURE_A={seed['feature_a']}",
            "-e",
            f"CRYPIDIAN_E2E_PLAN_HASH_A={seed['plan_hash']}",
            "-e",
            f"CRYPIDIAN_E2E_DECISION_A={seed['decision_a']}",
            "trading-runtime",
            "python",
            "-c",
            probe_code,
        )
        result = json.loads(probed.stdout.strip().splitlines()[-1])
        assert result == {
            "foreign_settings_hidden": True,
            "foreign_feature_hidden": True,
            "foreign_profile_rejected": True,
            "coordinator_rejected": True,
            "foreign_reservation_rejected": True,
            "foreign_intent_rejected": True,
            "workspace_b_intents": 0,
            "workspace_b_reservations": 0,
            "workspace_a_settings_visible": True,
            "workspace_a_profile_visible": True,
            "workspace_a_feature_visible": True,
            "workspace_a_reservation_status": "RESERVED",
        }
    finally:
        _compose(
            repository_root,
            project_name,
            environment,
            "down",
            "--volumes",
            "--remove-orphans",
        )
