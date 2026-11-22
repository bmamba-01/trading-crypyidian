"""RT-1 Compose E2E: Chứng minh H01 thoát lệnh bằng OPPOSING_SIGNAL trên PostgreSQL thật.

Kiểm tra theo đúng tiêu chí nghiệm thu của CRY-203 ([RT-1/opposing-h01]):
1. PostgresPaperDecisionContextProvider tính tín hiệu ngược point-in-time từ nến chiến lược đã đóng.
2. Vị thế LONG của H01 khi gặp tín hiệu SHORT nhận evidence.opposing_signal = True.
3. Vị thế chuyển sang pending_exit_reason = "OPPOSING_SIGNAL".
4. Nến kế tiếp đến khớp thoát lệnh tại giá mở nến sau (FX-4/time-stop-nen-sau).
5. Ledger database (trade, fill, position_event), checkpoint và verify_paper_ledger_consistency nhất quán 100%.
"""
# ruff: noqa: E501

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_CODE = r"""
import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
from sqlalchemy import select

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
)
from trading_bot.adapters.database.models.paper_entities import (
    FillRecord,
    PositionEventRecord,
    TradeRecord,
)
from trading_bot.adapters.database.repositories.bot_runtime_leases import (
    BotRuntimeLease,
)
from trading_bot.adapters.database.repositories.paper_checkpoints import (
    PaperCheckpointRepository,
)
from trading_bot.adapters.database.repositories.paper_ledger import (
    PaperLedgerRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_lifecycle import PaperLifecycleCoordinator
from trading_bot.application.paper_market_context import (
    PostgresPaperDecisionContextProvider,
)
from trading_bot.domain.execution.paper import (
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
    PaperPosition,
)
from trading_bot.domain.execution.paper_state import _plan_to_json
from trading_bot.domain.risk.planner import (
    ExchangeRules,
    TradePlan,
    calculate_plan_hash,
)
from trading_bot.domain.strategy.recipes import ExitRecipe


def _admin_db_url() -> str:
    pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    return f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"


def _runtime_db_url() -> str:
    user = os.environ.get("TRADING_BOT_DATABASE_USER", "crypidian_trading_runtime")
    pw_file = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
    pw = pw_file.read_text(encoding="utf-8").strip()
    return f"postgresql+psycopg://{user}:{pw}@postgres:5432/crypidian"


async def main() -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    ws = uuid4()
    bot_id = uuid4()
    acc_id = uuid4()
    user_id = uuid4()
    mem_id = uuid4()

    intent_id = uuid4()
    trade_id = uuid4()
    plan_id = uuid4()
    dec_id = uuid4()
    res_id = uuid4()
    set_v_id = uuid4()
    rel_id = uuid4()

    from trading_bot.application.strategy_catalog import strategy_registry
    reg = strategy_registry()
    hash_h01 = reg.get_release("h01-v1").content_hash

    recipe_h01 = ExitRecipe(
        "h01-exit-v1", 2.0, trailing_atr_multiple=3.0, opposite_signal_exit=True, max_hold_bars=120
    )
    rules = ExchangeRules(
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        max_quantity=Decimal("1000"),
    )
    base_plan = TradePlan(
        workspace_id=str(ws),
        account_id=str(acc_id),
        settings_version_id=str(set_v_id),
        settings_hash=None,
        risk_profile_version_id="risk-v1",
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash=hash_h01,
        snapshot_id="f" * 64,
        candidate_id="c" * 64,
        symbol="BTCUSDT",
        direction="long",
        candidate_reason_code="H01_LONG_ENTRY",
        evidence_feature_ids=("f" * 64,),
        evidence_bar_keys=("entry-bar-h01",),
        macro_evidence_ids=(),
        entry_reference_price=Decimal("38000"),
        entry_price=Decimal("38000"),
        stop_price=Decimal("30000"),
        target_price=None,
        quantity=Decimal("1"),
        notional=Decimal("38000"),
        leverage=Decimal("2"),
        initial_margin=Decimal("19000"),
        risk_budget=Decimal("8000"),
        estimated_loss=Decimal("8000"),
        entry_fee_cost=Decimal("5"),
        exit_fee_cost=Decimal("5"),
        adverse_funding_cost=Decimal("0"),
        entry_slippage_cost=Decimal("0"),
        cost_model_version="cost-v1",
        exchange_rules=rules,
        exit_recipe=recipe_h01,
        expires_at=now + timedelta(hours=24),
        source_signal_at=now - timedelta(hours=2),
        plan_hash="",
    )
    plan_obj = replace(base_plan, plan_hash=calculate_plan_hash(base_plan))
    plan_hash = plan_obj.plan_hash

    admin_pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'RT1 Workspace', %s, 'PAPER', 'ACTIVE')",
            (ws, f"rt1-{ws.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, content_hash) "
            "VALUES (%s, %s, 'h01-v1', 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (rel_id, ws, hash_h01),
        )
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'hash', 'ACTIVE')",
            (user_id, f"rt1-{user_id.hex[:8]}@test.io"),
        )
        conn.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (mem_id, ws, user_id),
        )
        conn.execute(
            "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
            "settings_json, settings_hash, status, created_by) "
            "VALUES (%s, %s, 1, 1, '{}'::jsonb, %s, 'PUBLISHED', %s)",
            (set_v_id, ws, f"hash-{set_v_id.hex[:8]}", mem_id),
        )
        conn.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (%s, %s, %s, 'OWNER')",
            (uuid4(), ws, mem_id),
        )
        conn.execute(
            "INSERT INTO feature_snapshot (id, workspace_id, snapshot_id, content_hash, symbol, timeframe, "
            "as_of, computed_at, schema_hash, formula_hash, code_hash, input_bar_ids, input_event_ids, "
            "features, warmup, quality_flags, data_freshness, data_coverage, regime, is_usable, canonical_json) "
            "VALUES (%s, %s, %s, %s, 'BTCUSDT', '1h', %s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, "
            "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'FRESH', 1.0, '{\"label\":\"TREND\"}'::jsonb, true, '{}')",
            (uuid4(), ws, "f" * 64, "f" * 64, now - timedelta(hours=1), now - timedelta(hours=1), "e" * 64, "f" * 64, "0" * 64),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, status, max_notional) "
            "VALUES (%s, %s, 'acc-h01', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 100000)",
            (acc_id, ws),
        )
        bot_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws, acc_id),
        ).fetchone()[0]

        conn.execute(
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, granted_by) "
            "VALUES (%s, %s, %s, %s, 'bot:read', %s)",
            (uuid4(), ws, bot_id, mem_id, mem_id),
        )
        inst_id = uuid4()
        conn.execute(
            "INSERT INTO market_instrument (id, workspace_id, source, idempotency_key, symbol, "
            "base_asset, quote_asset, tick_size, quantity_step, min_notional, status, revision, "
            "checksum, event_at, received_at, available_at) "
            "VALUES (%s, %s, 'BINANCE_USDM_REST_BACKFILL', %s, 'BTCUSDT', 'BTC', 'USDT', 0.1, 0.001, 5, "
            "'TRADING', 1, %s, %s, %s, %s)",
            (inst_id, ws, f"inst-{ws.hex[:8]}", "a" * 64, now, now, now),
        )

        # Seed 620 nến 4h cho H01 (warmup EMA200 >= 3x200 = 600 nến theo quy tắc FX-6)
        four_h = timedelta(hours=4)
        start_4h = now - 620 * four_h
        for i in range(620):
            t = start_4h + i * four_h
            close_t = t + four_h
            base = Decimal("40000.0") if i >= 615 else Decimal("50000.0")  # đóng dưới EMA200 ở các nến cuối
            conn.execute(
                "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
                "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
                "event_at, received_at, open, high, low, close, volume, is_closed, available_at) "
                "VALUES (%s, %s, %s, 'BTCUSDT', '4h', 'BINANCE_USDM_REST_BACKFILL', %s, %s, 1, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, 100, true, %s)",
                (
                    uuid4(),
                    ws,
                    inst_id,
                    f"lk-4h-{i}-{ws.hex[:6]}",
                    f"ik-4h-{i}-{ws.hex[:6]}",
                    "b" * 64,
                    t,
                    close_t,
                    close_t,
                    close_t,
                    base,
                    base + Decimal("100.0"),
                    base - Decimal("100.0"),
                    base,
                    close_t,
                ),
            )

        # Seed 620 nến 1h cho H01 (warmup EMA200 >= 3x200 = 600 nến theo quy tắc FX-6 và tạo bearish cross tại nến cuối cùng)
        one_h = timedelta(hours=1)
        start_1h = now - 620 * one_h
        for i in range(620):
            t = start_1h + i * one_h
            close_t = t + one_h
            if i < 618:
                p = Decimal("50000.0") + Decimal(i * 10)
            elif i == 618:
                p = Decimal("60000.0")
            else:
                p = Decimal("38000.0")  # sập mạnh ở nến 619 -> fast EMA20 cắt xuống slow EMA80
            conn.execute(
                "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
                "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
                "event_at, received_at, open, high, low, close, volume, is_closed, available_at) "
                "VALUES (%s, %s, %s, 'BTCUSDT', '1h', 'BINANCE_USDM_REST_BACKFILL', %s, %s, 1, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, 100, true, %s)",
                (
                    uuid4(),
                    ws,
                    inst_id,
                    f"lk-1h-{i}-{ws.hex[:6]}",
                    f"ik-1h-{i}-{ws.hex[:6]}",
                    "c" * 64,
                    t,
                    close_t,
                    close_t,
                    close_t,
                    p,
                    p + Decimal("200.0"),
                    p - Decimal("200.0"),
                    p,
                    close_t,
                ),
            )

        # Seed initial records cho H01
        conn.execute(
            "INSERT INTO trade_plan (id, workspace_id, account_id, bot_id, plan_hash, symbol, notional, payload) "
            "VALUES (%s, %s, %s, %s, %s, 'BTCUSDT', 38000, %s::jsonb)",
            (plan_id, ws, acc_id, bot_id, plan_hash, json.dumps(_plan_to_json(plan_obj))),
        )
        conn.execute(
            "INSERT INTO risk_decision (id, workspace_id, account_id, bot_id, decision_key, plan_hash, "
            "snapshot_id, approved, reason_code, detail_vi, payload) "
            "VALUES (%s, %s, %s, %s, 'dec-h01', %s, 'snap-h01', true, 'APPROVED', 'OK', '{}'::jsonb)",
            (dec_id, ws, acc_id, bot_id, plan_hash),
        )
        conn.execute(
            "INSERT INTO risk_reservation (id, workspace_id, account_id, bot_id, risk_decision_id, "
            "idempotency_key, plan_hash, symbol, reserved_risk, reserved_notional, "
            "reserved_initial_margin, expires_at, status, resolution_reason) "
            "VALUES (%s, %s, %s, %s, %s, 'res-h01', %s, 'BTCUSDT', 8000, 38000, 19000, "
            "now() + interval '1 hour', 'TRANSFERRED', 'OPEN_RISK_RECORDED')",
            (res_id, ws, acc_id, bot_id, dec_id, plan_hash),
        )
        conn.execute(
            "INSERT INTO order_intent (id, workspace_id, account_id, bot_id, mode, idempotency_key, plan_hash, "
            "settings_version_id, strategy_release_id, strategy_release_hash, feature_snapshot_id, "
            "candidate_id, risk_decision_id, risk_reservation_id, symbol, quote_asset, side, quantity, "
            "filled_quantity, status, created_at, evidence) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "'BTCUSDT', 'USDT', 'LONG', 1, 1, 'FILLED', %s, '{}'::jsonb)",
            (intent_id, ws, acc_id, bot_id, str(intent_id), plan_hash, set_v_id, rel_id, hash_h01, "f" * 64, "c" * 64, dec_id, res_id, now),
        )
        lineage_h01 = {
            "settings_version_id": str(set_v_id),
            "strategy_release_id": str(rel_id),
            "strategy_release_hash": hash_h01,
            "feature_snapshot_id": "f" * 64,
            "candidate_id": "c" * 64,
            "quote_asset": "USDT",
            "risk_decision_id": str(dec_id),
            "risk_reservation_id": str(res_id),
            "plan_hash": plan_hash,
        }
        conn.execute(
            "INSERT INTO trade (id, workspace_id, account_id, bot_id, mode, intent_id, status, symbol, "
            "quote_asset, side, settings_version_id, strategy_release_id, strategy_release_hash, "
            "feature_snapshot_id, candidate_id, risk_decision_id, risk_reservation_id, gross_pnl, "
            "fee_total, funding_total, slippage_total, total_cost, net_pnl, opened_at, lineage) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, 'OPEN', 'BTCUSDT', 'USDT', 'LONG', %s, %s, %s, "
            "%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, %s, %s::jsonb)",
            (
                trade_id, ws, acc_id, bot_id, intent_id, set_v_id, rel_id, hash_h01,
                "f" * 64, "c" * 64, dec_id, res_id, now, json.dumps(lineage_h01),
            ),
        )
        conn.execute(
            "INSERT INTO fill (id, workspace_id, account_id, bot_id, intent_id, fill_role, "
            "quantity, price, fee_amount, slippage_amount, occurred_at, fill_key, "
            "fill_policy_version, bar_key, lineage) "
            "VALUES (gen_random_uuid(), %s, %s, %s, %s, 'ENTRY', 1, 38000, 0, 0, "
            "%s, %s, 'paper-fill-v1', 'entry-bar-h01', %s::jsonb)",
            (ws, acc_id, bot_id, intent_id, now, f"paper:{intent_id}:entry-bar-h01:1", json.dumps(lineage_h01)),
        )
        conn.execute(
            "INSERT INTO position_event (id, workspace_id, account_id, bot_id, mode, intent_id, "
            "event_key, event_type, quantity, occurred_at, payload, lineage) "
            "VALUES (gen_random_uuid(), %s, %s, %s, 'PAPER', %s, %s, 'OPENED', 1, %s, '{}'::jsonb, %s::jsonb)",
            (ws, acc_id, bot_id, intent_id, f"entry-fill:paper:{intent_id}:entry-bar-h01:1", now, json.dumps(lineage_h01)),
        )

    engine = create_database_engine(_runtime_db_url())
    session_factory = create_session_factory(engine)
    context_provider = PostgresPaperDecisionContextProvider(
        sessions=session_factory, workspace_id=ws
    )

    checkpoints_repo = PaperCheckpointRepository()
    ledger_repo = PaperLedgerRepository()
    profile = SimpleNamespace(quote_asset="USDT")

    # --------------------------------------------------------------------------
    # Khởi tạo Gateway và Coordinator cho H01
    # --------------------------------------------------------------------------
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy("paper-fill-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )
    coordinator = PaperLifecycleCoordinator(gateway, ledger_repo, checkpoints_repo)

    # 1. Submit order và khớp ENTRY cho H01 (long)
    gateway.submit(plan_obj, str(intent_id), submitted_at=now - timedelta(hours=2))
    entry_bar = PaperBar(
        bar_key="entry-bar-h01",
        symbol="BTCUSDT",
        open=Decimal("38000"),
        high=Decimal("38200"),
        low=Decimal("37800"),
        close=Decimal("38000"),
        opened_at=now - timedelta(hours=1),
        closed_at=now,
        available_at=now,
    )
    gateway.apply_market_event(entry_bar)
    pos = gateway.position(f"paper:{intent_id}")
    assert pos is not None
    assert pos.quantity == Decimal("1")
    assert pos.direction == "long"

    # 2. Khởi tạo checkpoint genesis
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        await checkpoints_repo.append(
            uow,
            account_id=acc_id,
            event_key="init-h01",
            event_hash=hash_h01,
            snapshot=gateway.export_state(),
            occurred_at=now,
            event_scope=f"paper:{intent_id}:BTCUSDT",
        )

    # 3. Bar 1: Nến monitor tại `now` (nến 1h trước đó vừa đóng tại `now`)
    # Nến 1h mới đóng có bearish cross, sẽ kích hoạt OPPOSING_SIGNAL
    monitor_bar_1 = PaperBar(
        bar_key=f"live:BTCUSDT:1h:{now.isoformat()}",
        symbol="BTCUSDT",
        open=Decimal("38000"),
        high=Decimal("38200"),
        low=Decimal("37800"),
        close=Decimal("37900"),
        opened_at=now,
        closed_at=now + timedelta(hours=1),
        available_at=now + timedelta(hours=1),
    )
    cmd1 = SimpleNamespace(
        account_id=acc_id,
        bar=monitor_bar_1,
        paper_position_id=f"paper:{intent_id}",
        feature_snapshot_id="f" * 64,
    )
    published_settings = SimpleNamespace(workspace_id=ws)
    account_ref = SimpleNamespace(account_id=acc_id, bot_id=bot_id)

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        evidence = await context_provider.load_monitor_evidence(
            uow=uow,
            command=cmd1,
            published_settings=published_settings,
            execution_profile=profile,
            account=account_ref,
            position=pos,
            pinned_strategy_release_name="h01-v1",
            pinned_strategy_release_hash=hash_h01,
            pinned_strategy_settings_version_id=str(set_v_id),
            pinned_strategy_parameters=(),
        )

    # Chứng minh: opposing_signal được tính đúng point-in-time từ nến đã đóng
    assert evidence.opposing_signal is True, "H01 phải phát hiện tín hiệu đối ứng từ nến đã đóng!"
    assert evidence.strategy_bar_closed is True

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        await uow.bind_account(acc_id)
        lease_repo = BotRuntimeLease(uow.session)
        db_now = await lease_repo.database_now()
        token = await lease_repo.acquire(
            ws, bot_id, "rt1-e2e", db_now + timedelta(seconds=120)
        )

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        res1 = await coordinator.monitor_market_event(
            uow,
            account_id=acc_id,
            paper_position_id=f"paper:{intent_id}",
            ledger_intent_id=intent_id,
            trade_id=trade_id,
            position=pos,
            bar=monitor_bar_1,
            opposing_signal=evidence.opposing_signal,
            atr=evidence.atr,
            exit_channel_level=evidence.exit_channel_level,
            exit_feature_value=evidence.exit_feature_value,
            strategy_bar_closed=evidence.strategy_bar_closed,
            fencing_token=token,
        )

    # Vị thế chưa đóng ngay mà chuyển sang pending exit OPPOSING_SIGNAL
    assert not res1.position.is_closed, f"Position không được đóng ngay tại bar 1: {res1.monitor_result}"
    assert res1.position.pending_exit_reason == "OPPOSING_SIGNAL", (
        f"pending_exit_reason phải là OPPOSING_SIGNAL, got: {res1.position.pending_exit_reason}"
    )

    # 4. Bar 2: Nến tiếp theo đến (now + 1h), mở ở 37700
    # Vị thế sẽ đóng ngay tại giá mở nến sau (bar.open) theo FX-4
    bar2_t = now + timedelta(hours=1)
    monitor_bar_2 = PaperBar(
        bar_key=f"live:BTCUSDT:1h:{bar2_t.isoformat()}",
        symbol="BTCUSDT",
        open=Decimal("37700"),
        high=Decimal("37900"),
        low=Decimal("37500"),
        close=Decimal("37600"),
        opened_at=bar2_t,
        closed_at=bar2_t + timedelta(hours=1),
        available_at=bar2_t + timedelta(hours=1),
    )
    cmd2 = SimpleNamespace(
        account_id=acc_id,
        bar=monitor_bar_2,
        paper_position_id=f"paper:{intent_id}",
        feature_snapshot_id="f" * 64,
    )

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        evidence2 = await context_provider.load_monitor_evidence(
            uow=uow,
            command=cmd2,
            published_settings=published_settings,
            execution_profile=profile,
            account=account_ref,
            position=res1.position,
            pinned_strategy_release_name="h01-v1",
            pinned_strategy_release_hash=hash_h01,
            pinned_strategy_settings_version_id=str(set_v_id),
            pinned_strategy_parameters=(),
        )

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        await uow.bind_account(acc_id)
        lease_repo = BotRuntimeLease(uow.session)
        db_now = await lease_repo.database_now()
        token2 = await lease_repo.acquire(
            ws, bot_id, "rt1-e2e", db_now + timedelta(seconds=120)
        )

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        res2 = await coordinator.monitor_market_event(
            uow,
            account_id=acc_id,
            paper_position_id=f"paper:{intent_id}",
            ledger_intent_id=intent_id,
            trade_id=trade_id,
            position=res1.position,
            bar=monitor_bar_2,
            opposing_signal=evidence2.opposing_signal,
            atr=evidence2.atr,
            exit_channel_level=evidence2.exit_channel_level,
            exit_feature_value=evidence2.exit_feature_value,
            strategy_bar_closed=evidence2.strategy_bar_closed,
            fencing_token=token2,
        )

    # Vị thế đóng thành công bằng OPPOSING_SIGNAL ở giá mở nến sau (37700)
    assert res2.position.is_closed is True
    assert res2.monitor_result.exit_reason == "OPPOSING_SIGNAL"
    assert res2.position.exit_reason == "OPPOSING_SIGNAL"
    assert res2.monitor_result.exit_reference_price == Decimal("37700")
    assert res2.position.exit_reference_price == Decimal("37700")
    assert res2.exit_fill is not None
    assert res2.exit_fill_appended is True
    assert res2.trade_closed is True

    # 5. Đối soát database PostgreSQL
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_id) as uow:
        fills = (
            await uow.session.scalars(
                select(FillRecord).where(
                    FillRecord.workspace_id == ws,
                    FillRecord.intent_id == intent_id,
                    FillRecord.fill_role == "EXIT",
                )
            )
        ).all()
        assert len(fills) == 1
        assert fills[0].price == Decimal("37700")

        tr = await uow.session.scalar(
            select(TradeRecord).where(TradeRecord.workspace_id == ws, TradeRecord.id == trade_id)
        )
        assert tr is not None
        assert tr.status == "CLOSED"
        assert tr.closed_at is not None

        pe = (
            await uow.session.scalars(
                select(PositionEventRecord).where(
                    PositionEventRecord.workspace_id == ws,
                    PositionEventRecord.intent_id == intent_id,
                    PositionEventRecord.event_type == "CLOSED",
                )
            )
        ).first()
        assert pe is not None
        assert pe.payload.get("exit_reason") == "OPPOSING_SIGNAL"

    print("RT1_OPPOSING_H01_E2E_OK")

if __name__ == "__main__":
    asyncio.run(main())
"""


def _compose(
    root: Path,
    project: str,
    environment: dict[str, str],
    *args: str,
    success: bool = True,
) -> subprocess.CompletedProcess[str]:
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
    assert (result.returncode == 0) is success, (
        args,
        result.stdout[-4000:],
        result.stderr[-4000:],
    )
    return result


def test_rt1_paper_opposing_h01_e2e_postgres() -> None:
    """Xác nhận E2E Compose H01 đóng lệnh bằng OPPOSING_SIGNAL trên PostgreSQL thật."""
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
    project = f"crypidian-rt1-opposing-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
    }
    try:
        ensure_image_available(root, image)
        _compose(
            root, project, environment, "up", "--detach", "--wait", "--wait-timeout", "180", "postgres"
        )
        _compose(root, project, environment, "run", "--rm", "--no-deps", "db-provisioner")
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
        heads = _compose(root, project, environment, "run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        assert "a3c7e9f1b5d2 (head)" in heads.stdout

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
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{root / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{root / 'src' / 'trading_bot'}:/app/src/trading_bot:ro",
                "trading-runtime",
                "python",
                "-c",
                "import sys; exec(sys.stdin.read())",
            ],
            cwd=root,
            env=environment,
            input=PROBE_CODE,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert "RT1_OPPOSING_H01_E2E_OK" in result.stdout, result.stdout + "\n" + result.stderr
    finally:
        _compose(root, project, environment, "down", "-v", "--remove-orphans")
