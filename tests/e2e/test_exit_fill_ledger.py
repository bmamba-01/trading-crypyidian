from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from textwrap import dedent

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available


def _compose(root: Path, project: str, environment: dict[str, str], *args: str):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--project-name",
            project,
            "-f",
            "compose.yaml",
            *args,
        ],
        check=False,
        capture_output=True,
        cwd=root,
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


def test_exit_fills_are_idempotent_and_do_not_change_entry_order_fill() -> None:
    """Xác nhận exit fill/cost/position trên PostgreSQL thật và dưới RLS tenant."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    project = f"crypidian-exit-fill-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        web_port = listener.getsockname()[1]
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "TRADING_BOT_WEB_PORT": str(web_port),
    }
    ids = {name: str(uuid.uuid4()) for name in ("workspace", "account", "intent", "trade")}
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
        ensure_image_available(root, image)
        _compose(root, project, environment, "up", "--detach", "--wait", "--wait-timeout", "180")
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "check",
        )
        seed = dedent(
            """
            import os
            import json
            import uuid
            from pathlib import Path
            import psycopg
            workspace = os.environ['E2E_WORKSPACE']
            account = os.environ['E2E_ACCOUNT']
            intent = os.environ['E2E_INTENT']
            trade = os.environ['E2E_TRADE']
            entry_fill_id = str(uuid.uuid4())
            entry_fee_id = str(uuid.uuid4())
            entry_slippage_id = str(uuid.uuid4())
            password = Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']).read_text().strip()
            profile_hash = 'e' * 64
            evidence = {'paper_execution_profile_hash': profile_hash}
            lineage = {
                'settings_version_id': '10000000-0000-0000-0000-000000000001',
                'strategy_release_id': '20000000-0000-0000-0000-000000000001',
                'strategy_release_hash': 'a' * 64,
                'feature_snapshot_id': 'f' * 64,
                'candidate_id': 'd' * 64,
                'quote_asset': 'USDT',
                'risk_decision_id': '30000000-0000-0000-0000-000000000001',
                'risk_reservation_id': '40000000-0000-0000-0000-000000000001',
                'plan_hash': 'b' * 64,
                'paper_execution_profile_hash': profile_hash,
            }
            with psycopg.connect(
                host='postgres', dbname='crypidian', user='crypidian_admin', password=password,
            ) as connection:
                connection.execute(
                    "INSERT INTO workspace (id, name, slug, mode, status) "
                    "VALUES (%s, 'Exit fill E2E', %s, 'PAPER', 'ACTIVE')",
                    (workspace, 'exit-' + workspace[:8]),
                )
                connection.execute(
                    "INSERT INTO trading_account "
                    "(id, workspace_id, name, venue, mode, environment, status, max_notional) "
                    "VALUES (%s, %s, 'exit-fill-e2e', 'binance_usdm', 'PAPER', 'local', "
                    "'ENABLED', 1000)", (account, workspace),
                )
                connection.execute(
                    "SELECT set_config('app.bot_id', (SELECT id::text FROM bot_instance "
                    "WHERE trading_account_id=%s), true)", (account,),
                )
                connection.execute('SET session_replication_role = replica')
                # Chuỗi plan→decision→reservation thật để FK ghép MB-01 hợp lệ khi re-upgrade.
                connection.execute(
                    "INSERT INTO trade_plan "
                    "(id, workspace_id, account_id, plan_hash, symbol, notional, payload) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, 'BTCUSDT', 100, '{}'::jsonb)",
                    (workspace, account, 'b' * 64),
                )
                connection.execute(
                    "INSERT INTO risk_decision "
                    "(id, workspace_id, account_id, decision_key, plan_hash, snapshot_id, "
                    "approved, reason_code, detail_vi, payload) "
                    "VALUES (%s, %s, %s, %s, %s, 'exit-fill-e2e', true, 'APPROVED', 'OK', "
                    "'{}'::jsonb)",
                    ('30000000-0000-0000-0000-000000000001', workspace, account, 'c' * 64,
                     'b' * 64),
                )
                connection.execute(
                    "INSERT INTO risk_reservation "
                    "(id, workspace_id, account_id, risk_decision_id, idempotency_key, "
                    "plan_hash, symbol, reserved_risk, reserved_notional, "
                    "reserved_initial_margin, expires_at, status, resolution_reason) "
                    "VALUES (%s, %s, %s, %s, 'exit-fill', %s, 'BTCUSDT', 10, 100, 20, "
                    "now() + interval '1 hour', 'TRANSFERRED', 'OPEN_RISK_RECORDED')",
                    ('40000000-0000-0000-0000-000000000001', workspace, account,
                     '30000000-0000-0000-0000-000000000001', 'b' * 64),
                )
                connection.execute(
                    "INSERT INTO order_intent "
                    "(id, workspace_id, account_id, mode, idempotency_key, plan_hash, "
                    "settings_version_id, strategy_release_id, strategy_release_hash, "
                    "feature_snapshot_id, candidate_id, risk_decision_id, risk_reservation_id, "
                    "symbol, quote_asset, side, quantity, filled_quantity, status, created_at, "
                    "evidence) "
                    "VALUES (%s, %s, %s, 'PAPER', 'exit-intent', %s, %s, %s, %s, %s, %s, "
                    "%s, %s, 'BTCUSDT', 'USDT', 'LONG', 1, 1, 'FILLED', now(), %s::jsonb)",
                    (intent, workspace, account, 'b' * 64,
                     '10000000-0000-0000-0000-000000000001',
                     '20000000-0000-0000-0000-000000000001', 'a' * 64, 'f' * 64, 'd' * 64,
                     '30000000-0000-0000-0000-000000000001',
                     '40000000-0000-0000-0000-000000000001', json.dumps(evidence)),
                )
                connection.execute(
                    "INSERT INTO trade "
                    "(id, workspace_id, account_id, mode, intent_id, status, symbol, quote_asset, "
                    "side, settings_version_id, strategy_release_id, strategy_release_hash, "
                    "feature_snapshot_id, candidate_id, risk_decision_id, risk_reservation_id, "
                    "gross_pnl, fee_total, funding_total, slippage_total, total_cost, net_pnl, "
                    "opened_at, closed_at, lineage) "
                    "VALUES (%s, %s, %s, 'PAPER', %s, 'OPEN', 'BTCUSDT', 'USDT', 'LONG', "
                    "%s, %s, %s, %s, %s, %s, %s, 0, 0, 0, 0, 0, 0, now(), NULL, %s::jsonb)",
                    (trade, workspace, account, intent,
                     '10000000-0000-0000-0000-000000000001',
                     '20000000-0000-0000-0000-000000000001', 'a' * 64, 'f' * 64, 'd' * 64,
                     '30000000-0000-0000-0000-000000000001',
                     '40000000-0000-0000-0000-000000000001', json.dumps(lineage)),
                )
                connection.execute(
                    "INSERT INTO fill "
                    "(id, workspace_id, account_id, mode, intent_id, fill_role, fill_key, bar_key, "
                    "fill_policy_version, quantity, price, fee_amount, slippage_amount, "
                    "occurred_at, lineage) "
                    "VALUES (%s, %s, %s, 'PAPER', %s, 'ENTRY', 'entry-fill', 'entry-bar', "
                    "'paper-v1', 1, 100, 0.1, 0.01, now(), %s::jsonb)",
                    (entry_fill_id, workspace, account, intent, json.dumps(lineage)),
                )
                connection.execute(
                    "INSERT INTO trade_cost "
                    "(id, workspace_id, account_id, mode, trade_id, cost_key, cost_type, "
                    "amount, currency, occurred_at, source_event_id, lineage) "
                    "VALUES (%s, %s, %s, 'PAPER', %s, 'fill:entry-fill:fee', 'FEE', "
                    "0.1, 'USDT', now(), 'entry-fill', %s::jsonb), "
                    "(%s, %s, %s, 'PAPER', %s, 'fill:entry-fill:slippage', 'SLIPPAGE', "
                    "0.01, 'USDT', now(), 'entry-fill', %s::jsonb)",
                    (entry_fee_id, workspace, account, trade, json.dumps(lineage),
                     entry_slippage_id, workspace, account, trade, json.dumps(lineage)),
                )
                connection.execute('SET session_replication_role = origin')
            """
        ).strip()
        common_env = environment | {f"E2E_{key.upper()}": value for key, value in ids.items()}
        _compose(
            root,
            project,
            common_env,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"E2E_WORKSPACE={ids['workspace']}",
            "-e",
            f"E2E_ACCOUNT={ids['account']}",
            "-e",
            f"E2E_INTENT={ids['intent']}",
            "-e",
            f"E2E_TRADE={ids['trade']}",
            "migrator",
            "python",
            "-c",
            seed,
        )
        probe = dedent(
            """
            import asyncio, json, os
            from datetime import UTC, datetime, timedelta
            from pathlib import Path
            from uuid import UUID
            from sqlalchemy import select
            from trading_bot.adapters.database.engine import (
                create_database_engine, create_session_factory, database_url_from_secret,
            )
            from trading_bot.adapters.database.models.paper_entities import (
                FillRecord, OrderIntentRecord, PositionEventRecord, TradeCostRecord, TradeRecord,
            )
            from trading_bot.adapters.database.repositories.paper_ledger import (
                PaperLedgerConflict, PaperLedgerRepository,
            )
            from trading_bot.application.outbox import UnitOfWork

            workspace = UUID(os.environ['E2E_WORKSPACE'])
            account = UUID(os.environ['E2E_ACCOUNT'])
            intent_id = UUID(os.environ['E2E_INTENT'])
            trade_id = UUID(os.environ['E2E_TRADE'])
            url = database_url_from_secret(
                username=os.environ['TRADING_BOT_DATABASE_USER'],
                password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                host='postgres', database='crypidian',
            )
            engine = create_database_engine(url)
            sessions = create_session_factory(engine)
            repository = PaperLedgerRepository()
            now = datetime.now(UTC)

            async def main():
                async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                    await uow.bind_account(account)
                    first = {
                        'intent_id': intent_id, 'fill_key': 'exit-one', 'bar_key': 'bar-exit-1',
                        'fill_policy_version': 'paper-v1', 'quantity': Decimal('0.4'),
                        'price': Decimal('110'), 'fee_amount': Decimal('0.11'),
                        'slippage_amount': Decimal('0.03'), 'occurred_at': now,
                    }
                    created = await repository.append_exit_fill(uow, **first)
                    duplicate = await repository.append_exit_fill(uow, **first)
                    conflict = False
                    try:
                        await repository.append_exit_fill(
                            uow, **{**first, 'quantity': Decimal('0.3')},
                        )
                    except PaperLedgerConflict:
                        conflict = True
                    second = await repository.append_exit_fill(
                        uow,
                        **{**first, 'fill_key': 'exit-two', 'bar_key': 'bar-exit-2',
                           'quantity': Decimal('0.6'), 'price': Decimal('105'),
                           'fee_amount': Decimal('0.12'), 'slippage_amount': Decimal('0.04'),
                           'occurred_at': now + timedelta(minutes=1)},
                    )
                    intent = await uow.session.scalar(select(OrderIntentRecord).where(
                        OrderIntentRecord.workspace_id == workspace,
                        OrderIntentRecord.id == intent_id,
                    ))
                    trade = await repository.close_trade(
                        uow, trade_id=trade_id, gross_pnl=Decimal('15'),
                        closed_at=now + timedelta(minutes=2),
                    )
                    fills = (await uow.session.scalars(select(FillRecord).where(
                        FillRecord.workspace_id == workspace, FillRecord.intent_id == intent_id,
                    ))).all()
                    costs = (await uow.session.scalars(select(TradeCostRecord).where(
                        TradeCostRecord.workspace_id == workspace,
                        TradeCostRecord.trade_id == trade_id,
                    ))).all()
                    events = (await uow.session.scalars(select(PositionEventRecord).where(
                        PositionEventRecord.workspace_id == workspace,
                        PositionEventRecord.intent_id == intent_id,
                    ))).all()
                    print(json.dumps({
                        'created': created, 'duplicate': duplicate, 'conflict': conflict,
                        'second_exit': second, 'intent_filled': str(intent.filled_quantity),
                        'intent_status': intent.status, 'trade_status': trade.status,
                        'net_pnl': str(trade.net_pnl),
                        'fill_roles': sorted(f.fill_role for f in fills),
                        'profile_hashes': sorted(
                            {f.lineage.get('paper_execution_profile_hash') for f in fills}
                        ),
                        'cost_count': len(costs), 'cost_keys': sorted(c.cost_key for c in costs),
                        'position_events': sorted((e.event_type, str(e.quantity)) for e in events),
                    }))
                await engine.dispose()
            from decimal import Decimal
            asyncio.run(main())
            """
        ).strip()
        result = _compose(
            root,
            project,
            common_env,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"E2E_WORKSPACE={ids['workspace']}",
            "-e",
            f"E2E_ACCOUNT={ids['account']}",
            "-e",
            f"E2E_INTENT={ids['intent']}",
            "-e",
            f"E2E_TRADE={ids['trade']}",
            "trading-runtime",
            "python",
            "-c",
            probe,
        )
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        assert observed["created"] is True
        assert observed["duplicate"] is False
        assert observed["conflict"] is True
        assert observed["second_exit"] is True
        assert observed["intent_filled"] == "1.000000000000000000"
        assert observed["intent_status"] == "FILLED"
        assert observed["trade_status"] == "CLOSED"
        assert Decimal(observed["net_pnl"]) == Decimal("14.59")
        assert observed["fill_roles"] == ["ENTRY", "EXIT", "EXIT"]
        assert observed["profile_hashes"] == ["e" * 64]
        assert observed["cost_count"] == 6
        assert observed["position_events"] == [
            ["CLOSED", "0E-18"],
            ["REDUCED", "0.600000000000000000"],
        ]
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "5b8c3f4a1d92",
        )
        _compose(
            root,
            project,
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
        _compose(root, project, environment, "down", "--volumes", "--remove-orphans")
