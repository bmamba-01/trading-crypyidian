"""FX-1 Compose E2E: Chứng minh lệnh đóng bằng CHANNEL_EXIT/FEATURE_EXIT trên PostgreSQL thật.

Kiểm tra theo đúng tiêu chí nghiệm thu của CRY-195:
1. Bản sửa `be1c3ab` (v2 kiểm đúng release của mình, không bị NOT_SELECTED) có mặt trên `dev`.
2. H03-v2 đóng lệnh bằng CHANNEL_EXIT khi nến chạm kênh Donchian 20 nến 1h, ledger và checkpoint khớp.
3. H06-v2 đóng lệnh bằng FEATURE_EXIT khi z-score 20 của nến 15m hồi về ngưỡng (>= -0.2), ledger và checkpoint khớp.
4. Đối soát recovery thành công, không xung đột số dư/vị thế sau khi đóng.
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
    # --------------------------------------------------------------------------
    # Phần 1: Seed dữ liệu PostgreSQL bằng quyền admin
    # --------------------------------------------------------------------------
    ws = uuid4()
    user_id = uuid4()
    mem_id = uuid4()
    acc_h03 = uuid4()
    acc_h06 = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)

    # Identifiers cho H03
    intent_h03 = uuid4()
    trade_h03 = uuid4()
    plan_h03 = uuid4()
    dec_h03 = uuid4()
    res_h03 = uuid4()
    set_v_h03 = uuid4()
    rel_id_h03 = uuid4()

    # Identifiers cho H06
    intent_h06 = uuid4()
    trade_h06 = uuid4()
    plan_h06 = uuid4()
    dec_h06 = uuid4()
    res_h06 = uuid4()
    set_v_h06 = uuid4()
    rel_id_h06 = uuid4()

    from trading_bot.application.strategy_catalog import strategy_registry
    reg = strategy_registry()
    hash_h03 = reg.get_release("h03-v2").content_hash
    hash_h06 = reg.get_release("h06-v2").content_hash

    recipe_h03 = ExitRecipe(
        "h03-exit-v2", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120, exit_channel_period=20
    )
    recipe_h06 = ExitRecipe(
        "h06-exit-v2",
        1.5,
        take_profit_atr_multiple=2.0,
        max_hold_bars=16,
        exit_feature_name="zscore_20",
        exit_feature_operator=">=",
        exit_feature_threshold=-0.2,
    )
    rules = ExchangeRules(
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        max_quantity=Decimal("1000"),
    )
    base_plan_h03 = TradePlan(
        workspace_id=str(ws),
        account_id=str(acc_h03),
        settings_version_id=str(set_v_h03),
        settings_hash=None,
        risk_profile_version_id="risk-v1",
        strategy_id="H03",
        strategy_release_id="h03-v2",
        release_content_hash=hash_h03,
        snapshot_id="f" * 64,
        candidate_id="c" * 64,
        symbol="BTCUSDT",
        direction="long",
        candidate_reason_code="H03_LONG_ENTRY",
        evidence_feature_ids=("f" * 64,),
        evidence_bar_keys=("entry-bar-h03",),
        macro_evidence_ids=(),
        entry_reference_price=Decimal("50000"),
        entry_price=Decimal("50000"),
        stop_price=Decimal("45000"),
        target_price=None,
        quantity=Decimal("1"),
        notional=Decimal("50000"),
        leverage=Decimal("10"),
        initial_margin=Decimal("5000"),
        risk_budget=Decimal("500"),
        estimated_loss=Decimal("500"),
        entry_fee_cost=Decimal("5"),
        exit_fee_cost=Decimal("5"),
        adverse_funding_cost=Decimal("0"),
        entry_slippage_cost=Decimal("0"),
        cost_model_version="cost-v1",
        exchange_rules=rules,
        exit_recipe=recipe_h03,
        expires_at=now + timedelta(hours=24),
        source_signal_at=now - timedelta(hours=2),
        plan_hash="",
    )
    plan_h03_obj = replace(base_plan_h03, plan_hash=calculate_plan_hash(base_plan_h03))
    plan_hash_h03 = plan_h03_obj.plan_hash

    base_plan_h06 = TradePlan(
        workspace_id=str(ws),
        account_id=str(acc_h06),
        settings_version_id=str(set_v_h06),
        settings_hash=None,
        risk_profile_version_id="risk-v1",
        strategy_id="H06",
        strategy_release_id="h06-v2",
        release_content_hash=hash_h06,
        snapshot_id="g" * 64,
        candidate_id="k" * 64,
        symbol="BTCUSDT",
        direction="long",
        candidate_reason_code="H06_LONG_ENTRY",
        evidence_feature_ids=("g" * 64,),
        evidence_bar_keys=("entry-bar-h06",),
        macro_evidence_ids=(),
        entry_reference_price=Decimal("100"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        target_price=Decimal("110"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        leverage=Decimal("10"),
        initial_margin=Decimal("10"),
        risk_budget=Decimal("10"),
        estimated_loss=Decimal("10"),
        entry_fee_cost=Decimal("0.1"),
        exit_fee_cost=Decimal("0.1"),
        adverse_funding_cost=Decimal("0"),
        entry_slippage_cost=Decimal("0"),
        cost_model_version="cost-v1",
        exchange_rules=rules,
        exit_recipe=recipe_h06,
        expires_at=now + timedelta(hours=24),
        source_signal_at=now - timedelta(minutes=30),
        plan_hash="",
    )
    plan_h06_obj = replace(base_plan_h06, plan_hash=calculate_plan_hash(base_plan_h06))
    plan_hash_h06 = plan_h06_obj.plan_hash

    admin_pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'FX01 Workspace', %s, 'PAPER', 'ACTIVE')",
            (ws, f"fx01-{ws.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, content_hash) "
            "VALUES (%s, %s, 'h03-v2', 1, 'PUBLISHED', '{}'::jsonb, %s), "
            "       (%s, %s, 'h06-v2', 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (rel_id_h03, ws, hash_h03, rel_id_h06, ws, hash_h06),
        )
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'hash', 'ACTIVE')",
            (user_id, f"fx01-{user_id.hex[:8]}@test.io"),
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
            (set_v_h03, ws, "9" * 64, mem_id),
        )
        if set_v_h06 != set_v_h03:
            conn.execute(
                "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
                "settings_json, settings_hash, status, created_by) "
                "VALUES (%s, %s, 2, 1, '{}'::jsonb, %s, 'PUBLISHED', %s)",
                (set_v_h06, ws, "8" * 64, mem_id),
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
            "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'FRESH', 1.0, '{\"label\":\"TREND\"}'::jsonb, true, '{}'), "
            "       (%s, %s, %s, %s, 'BTCUSDT', '15m', %s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, "
            "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'FRESH', 1.0, '{\"label\":\"RANGE\"}'::jsonb, true, '{}')",
            (
                uuid4(), ws, "f" * 64, "f" * 64, now - timedelta(hours=1), now - timedelta(hours=1), "e" * 64, "f" * 64, "0" * 64,
                uuid4(), ws, "g" * 64, "g" * 64, now - timedelta(minutes=15), now - timedelta(minutes=15), "e" * 64, "f" * 64, "0" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, status, max_notional) "
            "VALUES (%s, %s, 'acc-h03', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 100000), "
            "       (%s, %s, 'acc-h06', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 100000)",
            (acc_h03, ws, acc_h06, ws),
        )
        # Lấy bot_id do database trigger tự tạo cho mỗi trading_account
        bot_h03 = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws, acc_h03),
        ).fetchone()[0]
        bot_h06 = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws, acc_h06),
        ).fetchone()[0]

        conn.execute(
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, granted_by) "
            "VALUES (%s, %s, %s, %s, 'bot:read', %s), "
            "       (%s, %s, %s, %s, 'bot:read', %s)",
            (uuid4(), ws, bot_h03, mem_id, mem_id, uuid4(), ws, bot_h06, mem_id, mem_id),
        )
        # Instrument BTCUSDT
        conn.execute(
            "INSERT INTO market_instrument (id, workspace_id, source, idempotency_key, symbol, "
            "base_asset, quote_asset, tick_size, quantity_step, min_notional, status, revision, "
            "checksum, event_at, received_at, available_at) "
            "VALUES (%s, %s, 'BINANCE_USDM_REST_BACKFILL', %s, 'BTCUSDT', 'BTC', 'USDT', 0.1, 0.001, 5, "
            "'TRADING', 1, %s, %s, %s, %s)",
            (inst_id, ws, f"inst-{ws.hex[:8]}", "a" * 64, now, now, now),
        )

        # Seed 20 nến 1h cho H03 (Donchian channel: channel_low = 49000.0)
        for i in range(20):
            open_t = now - timedelta(hours=20 - i)
            close_t = open_t + timedelta(hours=1)
            low_p = Decimal("49000.0") if i == 10 else Decimal("49200.0") + Decimal(i * 10)
            high_p = Decimal("51000.0")
            open_p = Decimal("50000.0")
            close_p = Decimal("50200.0")
            conn.execute(
                "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
                "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
                "event_at, received_at, open, high, low, close, volume, is_closed, available_at) "
                "VALUES (%s, %s, %s, 'BTCUSDT', '1h', 'BINANCE_USDM_REST_BACKFILL', %s, %s, 1, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, 10, true, %s)",
                (
                    uuid4(), ws, inst_id,
                    f"lk-1h-{i}-{ws.hex[:6]}", f"ik-1h-{i}-{ws.hex[:6]}", "b" * 64,
                    open_t, close_t, close_t, close_t,
                    open_p, high_p, low_p, close_p, close_t,
                ),
            )

        # Seed 220 nến 15m cho H06 để thỏa mãn toàn bộ warmup (max ema_200 = 200 nến)
        for j in range(220):
            open_t = now - timedelta(minutes=15 * (220 - j))
            close_t = open_t + timedelta(minutes=15)
            c_p = Decimal("100.0") if j == 219 else (Decimal("99.0") if j % 2 == 0 else Decimal("101.0"))
            conn.execute(
                "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
                "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
                "event_at, received_at, open, high, low, close, volume, is_closed, available_at) "
                "VALUES (%s, %s, %s, 'BTCUSDT', '15m', 'BINANCE_USDM_REST_BACKFILL', %s, %s, 1, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, 5, true, %s)",
                (
                    uuid4(), ws, inst_id,
                    f"lk-15m-{j}-{ws.hex[:6]}", f"ik-15m-{j}-{ws.hex[:6]}", "c" * 64,
                    open_t, close_t, close_t, close_t,
                    c_p, c_p + Decimal("1.0"), c_p - Decimal("1.0"), c_p, close_t,
                ),
            )

        # Seed initial records cho H03
        conn.execute(
            "INSERT INTO trade_plan (id, workspace_id, account_id, bot_id, plan_hash, symbol, notional, payload) "
            "VALUES (%s, %s, %s, %s, %s, 'BTCUSDT', 50000, %s::jsonb)",
            (plan_h03, ws, acc_h03, bot_h03, plan_hash_h03, json.dumps(_plan_to_json(plan_h03_obj))),
        )
        conn.execute(
            "INSERT INTO risk_decision (id, workspace_id, account_id, bot_id, decision_key, plan_hash, "
            "snapshot_id, approved, reason_code, detail_vi, payload) "
            "VALUES (%s, %s, %s, %s, 'dec-h03', %s, 'snap-h03', true, 'APPROVED', 'OK', '{}'::jsonb)",
            (dec_h03, ws, acc_h03, bot_h03, plan_hash_h03),
        )
        conn.execute(
            "INSERT INTO risk_reservation (id, workspace_id, account_id, bot_id, risk_decision_id, "
            "idempotency_key, plan_hash, symbol, reserved_risk, reserved_notional, "
            "reserved_initial_margin, expires_at, status, resolution_reason) "
            "VALUES (%s, %s, %s, %s, %s, 'res-h03', %s, 'BTCUSDT', 500, 50000, 5000, "
            "now() + interval '1 hour', 'TRANSFERRED', 'OPEN_RISK_RECORDED')",
            (res_h03, ws, acc_h03, bot_h03, dec_h03, plan_hash_h03),
        )
        conn.execute(
            "INSERT INTO order_intent (id, workspace_id, account_id, bot_id, mode, idempotency_key, plan_hash, "
            "settings_version_id, strategy_release_id, strategy_release_hash, feature_snapshot_id, "
            "candidate_id, risk_decision_id, risk_reservation_id, symbol, quote_asset, side, quantity, "
            "filled_quantity, status, created_at, evidence) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "'BTCUSDT', 'USDT', 'LONG', 1, 1, 'FILLED', %s, '{}'::jsonb)",
            (intent_h03, ws, acc_h03, bot_h03, str(intent_h03), plan_hash_h03, set_v_h03, rel_id_h03, hash_h03, "f" * 64, "c" * 64, dec_h03, res_h03, now),
        )
        lineage_h03 = {
            "settings_version_id": str(set_v_h03),
            "strategy_release_id": str(rel_id_h03),
            "strategy_release_hash": hash_h03,
            "feature_snapshot_id": "f" * 64,
            "candidate_id": "c" * 64,
            "quote_asset": "USDT",
            "risk_decision_id": str(dec_h03),
            "risk_reservation_id": str(res_h03),
            "plan_hash": plan_hash_h03,
        }
        conn.execute(
            "INSERT INTO trade (id, workspace_id, account_id, bot_id, mode, intent_id, status, symbol, "
            "quote_asset, side, settings_version_id, strategy_release_id, strategy_release_hash, "
            "feature_snapshot_id, candidate_id, risk_decision_id, risk_reservation_id, gross_pnl, "
            "fee_total, funding_total, slippage_total, total_cost, net_pnl, opened_at, lineage) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, 'OPEN', 'BTCUSDT', 'USDT', 'LONG', %s, %s, %s, "
            "%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, %s, %s::jsonb)",
            (
                trade_h03, ws, acc_h03, bot_h03, intent_h03, set_v_h03, rel_id_h03, hash_h03,
                "f" * 64, "c" * 64, dec_h03, res_h03, now, json.dumps(lineage_h03),
            ),
        )
        conn.execute(
            "INSERT INTO fill (id, workspace_id, account_id, bot_id, intent_id, fill_role, "
            "quantity, price, fee_amount, slippage_amount, occurred_at, fill_key, "
            "fill_policy_version, bar_key, lineage) "
            "VALUES (gen_random_uuid(), %s, %s, %s, %s, 'ENTRY', 1, 50000, 0, 0, "
            "%s, %s, 'paper-fill-v1', 'entry-bar-h03', %s::jsonb)",
            (ws, acc_h03, bot_h03, intent_h03, now, f"paper:{intent_h03}:entry-bar-h03:1", json.dumps(lineage_h03)),
        )
        conn.execute(
            "INSERT INTO position_event (id, workspace_id, account_id, bot_id, mode, intent_id, "
            "event_key, event_type, quantity, occurred_at, payload, lineage) "
            "VALUES (gen_random_uuid(), %s, %s, %s, 'PAPER', %s, %s, 'OPENED', 1, %s, '{}'::jsonb, %s::jsonb)",
            (ws, acc_h03, bot_h03, intent_h03, f"entry-fill:paper:{intent_h03}:entry-bar-h03:1", now, json.dumps(lineage_h03)),
        )

        # Seed initial records cho H06
        conn.execute(
            "INSERT INTO trade_plan (id, workspace_id, account_id, bot_id, plan_hash, symbol, notional, payload) "
            "VALUES (%s, %s, %s, %s, %s, 'BTCUSDT', 100, %s::jsonb)",
            (plan_h06, ws, acc_h06, bot_h06, plan_hash_h06, json.dumps(_plan_to_json(plan_h06_obj))),
        )
        conn.execute(
            "INSERT INTO risk_decision (id, workspace_id, account_id, bot_id, decision_key, plan_hash, "
            "snapshot_id, approved, reason_code, detail_vi, payload) "
            "VALUES (%s, %s, %s, %s, 'dec-h06', %s, 'snap-h06', true, 'APPROVED', 'OK', '{}'::jsonb)",
            (dec_h06, ws, acc_h06, bot_h06, plan_hash_h06),
        )
        conn.execute(
            "INSERT INTO risk_reservation (id, workspace_id, account_id, bot_id, risk_decision_id, "
            "idempotency_key, plan_hash, symbol, reserved_risk, reserved_notional, "
            "reserved_initial_margin, expires_at, status, resolution_reason) "
            "VALUES (%s, %s, %s, %s, %s, 'res-h06', %s, 'BTCUSDT', 10, 100, 10, "
            "now() + interval '1 hour', 'TRANSFERRED', 'OPEN_RISK_RECORDED')",
            (res_h06, ws, acc_h06, bot_h06, dec_h06, plan_hash_h06),
        )
        conn.execute(
            "INSERT INTO order_intent (id, workspace_id, account_id, bot_id, mode, idempotency_key, plan_hash, "
            "settings_version_id, strategy_release_id, strategy_release_hash, feature_snapshot_id, "
            "candidate_id, risk_decision_id, risk_reservation_id, symbol, quote_asset, side, quantity, "
            "filled_quantity, status, created_at, evidence) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "'BTCUSDT', 'USDT', 'LONG', 1, 1, 'FILLED', %s, '{}'::jsonb)",
            (intent_h06, ws, acc_h06, bot_h06, str(intent_h06), plan_hash_h06, set_v_h06, rel_id_h06, hash_h06, "g" * 64, "k" * 64, dec_h06, res_h06, now),
        )
        lineage_h06 = {
            "settings_version_id": str(set_v_h06),
            "strategy_release_id": str(rel_id_h06),
            "strategy_release_hash": hash_h06,
            "feature_snapshot_id": "g" * 64,
            "candidate_id": "k" * 64,
            "quote_asset": "USDT",
            "risk_decision_id": str(dec_h06),
            "risk_reservation_id": str(res_h06),
            "plan_hash": plan_hash_h06,
        }
        conn.execute(
            "INSERT INTO trade (id, workspace_id, account_id, bot_id, mode, intent_id, status, symbol, "
            "quote_asset, side, settings_version_id, strategy_release_id, strategy_release_hash, "
            "feature_snapshot_id, candidate_id, risk_decision_id, risk_reservation_id, gross_pnl, "
            "fee_total, funding_total, slippage_total, total_cost, net_pnl, opened_at, lineage) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, 'OPEN', 'BTCUSDT', 'USDT', 'LONG', %s, %s, %s, "
            "%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, %s, %s::jsonb)",
            (
                trade_h06, ws, acc_h06, bot_h06, intent_h06, set_v_h06, rel_id_h06, hash_h06,
                "g" * 64, "k" * 64, dec_h06, res_h06, now, json.dumps(lineage_h06),
            ),
        )
        conn.execute(
            "INSERT INTO fill (id, workspace_id, account_id, bot_id, intent_id, fill_role, "
            "quantity, price, fee_amount, slippage_amount, occurred_at, fill_key, "
            "fill_policy_version, bar_key, lineage) "
            "VALUES (gen_random_uuid(), %s, %s, %s, %s, 'ENTRY', 1, 100, 0, 0, "
            "%s, %s, 'paper-fill-v1', 'entry-bar-h06', %s::jsonb)",
            (ws, acc_h06, bot_h06, intent_h06, now, f"paper:{intent_h06}:entry-bar-h06:1", json.dumps(lineage_h06)),
        )
        conn.execute(
            "INSERT INTO position_event (id, workspace_id, account_id, bot_id, mode, intent_id, "
            "event_key, event_type, quantity, occurred_at, payload, lineage) "
            "VALUES (gen_random_uuid(), %s, %s, %s, 'PAPER', %s, %s, 'OPENED', 1, %s, '{}'::jsonb, %s::jsonb)",
            (ws, acc_h06, bot_h06, intent_h06, f"entry-fill:paper:{intent_h06}:entry-bar-h06:1", now, json.dumps(lineage_h06)),
        )

    # --------------------------------------------------------------------------
    # Phần 2: Khởi tạo runtime application dưới role crypidian_trading_runtime
    # --------------------------------------------------------------------------
    runtime_engine = create_database_engine(_runtime_db_url())
    session_factory = create_session_factory(runtime_engine)

    checkpoints_repo = PaperCheckpointRepository()
    ledger_repo = PaperLedgerRepository()
    profile = SimpleNamespace(quote_asset="USDT")

    # --------------------------------------------------------------------------
    # Kịch bản 1: H03-v2 Donchian CHANNEL_EXIT
    # --------------------------------------------------------------------------
    gateway_h03 = PaperExecutionGateway(
        PaperExecutionPolicy("paper-fill-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )
    coordinator_h03 = PaperLifecycleCoordinator(gateway_h03, ledger_repo, checkpoints_repo)

    gateway_h03.submit(plan_h03_obj, str(intent_h03), submitted_at=now - timedelta(hours=2))
    entry_bar_h03 = PaperBar(
        bar_key="entry-bar-h03",
        symbol="BTCUSDT",
        open=Decimal("50000"),
        high=Decimal("50500"),
        low=Decimal("49500"),
        close=Decimal("50200"),
        opened_at=now - timedelta(hours=1),
        closed_at=now,
        available_at=now,
    )
    gateway_h03.apply_market_event(entry_bar_h03)
    pos_h03 = gateway_h03.position(f"paper:{intent_h03}")
    assert pos_h03 is not None

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h03) as uow:
        await checkpoints_repo.append(
            uow,
            account_id=acc_h03,
            event_key="init-h03",
            event_hash=hash_h03,
            snapshot=gateway_h03.export_state(),
            occurred_at=now,
            event_scope=f"paper:{intent_h03}:BTCUSDT",
        )

    # Nến monitor 1h có low = 48500 chạm kênh Donchian 20 nến (channel_low = 49000.0)
    monitor_bar_h03 = PaperBar(
        bar_key=f"live:BTCUSDT:1h:{now.isoformat()}",
        symbol="BTCUSDT",
        open=Decimal("49500"),
        high=Decimal("49800"),
        low=Decimal("48500"),
        close=Decimal("48800"),
        opened_at=now,
        closed_at=now + timedelta(hours=1),
        available_at=now + timedelta(hours=1),
    )

    context_provider = PostgresPaperDecisionContextProvider(
        sessions=session_factory,
        workspace_id=ws,
    )

    command_h03 = SimpleNamespace(
        account_id=acc_h03,
        bar=monitor_bar_h03,
        paper_position_id=f"paper:{intent_h03}",
        feature_snapshot_id="f" * 64,
    )
    published_settings = SimpleNamespace(workspace_id=ws)
    account_ref_h03 = SimpleNamespace(account_id=acc_h03, bot_id=bot_h03)

    # 1. Trích xuất monitor evidence từ PostgreSQL
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h03) as uow:
        evidence_h03 = await context_provider.load_monitor_evidence(
            uow=uow,
            command=command_h03,
            published_settings=published_settings,
            execution_profile=profile,
            account=account_ref_h03,
            position=pos_h03,
            pinned_strategy_release_name="h03-v2",
            pinned_strategy_release_hash=hash_h03,
            pinned_strategy_settings_version_id=str(set_v_h03),
            pinned_strategy_parameters=(),
        )

    assert evidence_h03.exit_channel_level == Decimal("49000.0"), (
        f"Kênh Donchian 20 nến 1h phải là 49000.0, got: {evidence_h03.exit_channel_level}"
    )
    assert evidence_h03.strategy_bar_closed is True

    # 2. Thực thi monitor_market_event đóng lệnh bằng CHANNEL_EXIT (MB-05 lease fencing)
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h03) as uow:
        await uow.bind_account(acc_h03)
        lease_repo = BotRuntimeLease(uow.session)
        db_now = await lease_repo.database_now()
        token_h03 = await lease_repo.acquire(
            ws, bot_h03, "fx01-e2e", db_now + timedelta(seconds=120)
        )

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h03) as uow:
        result_h03 = await coordinator_h03.monitor_market_event(
            uow,
            account_id=acc_h03,
            paper_position_id=f"paper:{intent_h03}",
            ledger_intent_id=intent_h03,
            trade_id=trade_h03,
            position=pos_h03,
            bar=monitor_bar_h03,
            opposing_signal=False,
            atr=evidence_h03.atr,
            exit_channel_level=evidence_h03.exit_channel_level,
            exit_feature_value=evidence_h03.exit_feature_value,
            strategy_bar_closed=evidence_h03.strategy_bar_closed,
            fencing_token=token_h03,
        )

    assert result_h03.position.is_closed is True
    assert result_h03.monitor_result.exit_reason == "CHANNEL_EXIT"
    assert result_h03.exit_fill is not None
    assert result_h03.exit_fill_appended is True
    assert result_h03.trade_closed is True

    # 3. Đối soát database PostgreSQL
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h03) as uow:
        fills = (
            await uow.session.scalars(
                select(FillRecord).where(
                    FillRecord.workspace_id == ws,
                    FillRecord.intent_id == intent_h03,
                    FillRecord.fill_role == "EXIT",
                )
            )
        ).all()
        assert len(fills) == 1
        assert fills[0].price == Decimal("49000.0")

        tr = await uow.session.scalar(
            select(TradeRecord).where(TradeRecord.workspace_id == ws, TradeRecord.id == trade_h03)
        )
        assert tr is not None
        assert tr.status == "CLOSED"
        assert tr.closed_at is not None

        pe = (
            await uow.session.scalars(
                select(PositionEventRecord).where(
                    PositionEventRecord.workspace_id == ws,
                    PositionEventRecord.intent_id == intent_h03,
                    PositionEventRecord.event_type == "CLOSED",
                )
            )
        ).first()
        assert pe is not None
        assert pe.payload.get("exit_reason") == "CHANNEL_EXIT"

        # Hydrate và verify ledger consistency
        await coordinator_h03.hydrate_account(uow, account_id=acc_h03)
        assert gateway_h03.position(f"paper:{intent_h03}").is_closed is True

    # --------------------------------------------------------------------------
    # Kịch bản 2: H06-v2 Z-score FEATURE_EXIT
    # --------------------------------------------------------------------------
    gateway_h06 = PaperExecutionGateway(
        PaperExecutionPolicy("paper-fill-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )
    coordinator_h06 = PaperLifecycleCoordinator(gateway_h06, ledger_repo, checkpoints_repo)

    gateway_h06.submit(plan_h06_obj, str(intent_h06), submitted_at=now - timedelta(minutes=30))
    entry_bar_h06 = PaperBar(
        bar_key="entry-bar-h06",
        symbol="BTCUSDT",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        opened_at=now - timedelta(minutes=15),
        closed_at=now,
        available_at=now,
    )
    gateway_h06.apply_market_event(entry_bar_h06)
    pos_h06 = gateway_h06.position(f"paper:{intent_h06}")
    assert pos_h06 is not None

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h06) as uow:
        await checkpoints_repo.append(
            uow,
            account_id=acc_h06,
            event_key="init-h06",
            event_hash=hash_h06,
            snapshot=gateway_h06.export_state(),
            occurred_at=now,
            event_scope=f"paper:{intent_h06}:BTCUSDT",
        )

    monitor_bar_h06 = PaperBar(
        bar_key=f"live:BTCUSDT:15m:{now.isoformat()}",
        symbol="BTCUSDT",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        opened_at=now,
        closed_at=now + timedelta(minutes=15),
        available_at=now + timedelta(minutes=15),
    )

    command_h06 = SimpleNamespace(
        account_id=acc_h06,
        bar=monitor_bar_h06,
        paper_position_id=f"paper:{intent_h06}",
        feature_snapshot_id="g" * 64,
    )
    account_ref_h06 = SimpleNamespace(account_id=acc_h06, bot_id=bot_h06)

    # 1. Trích xuất monitor evidence từ nến 15m trong DB
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h06) as uow:
        evidence_h06 = await context_provider.load_monitor_evidence(
            uow=uow,
            command=command_h06,
            published_settings=published_settings,
            execution_profile=profile,
            account=account_ref_h06,
            position=pos_h06,
            pinned_strategy_release_name="h06-v2",
            pinned_strategy_release_hash=hash_h06,
            pinned_strategy_settings_version_id=str(set_v_h06),
            pinned_strategy_parameters=(),
        )

    assert evidence_h06.exit_feature_value is not None, "exit_feature_value không được None"
    assert evidence_h06.exit_feature_value >= Decimal("-0.2"), (
        f"zscore_20 phải >= -0.2 để kích hoạt thoát, got: {evidence_h06.exit_feature_value}"
    )

    # 2. Thực thi monitor_market_event đóng lệnh bằng FEATURE_EXIT (MB-05 lease fencing)
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h06) as uow:
        await uow.bind_account(acc_h06)
        lease_repo = BotRuntimeLease(uow.session)
        db_now = await lease_repo.database_now()
        token_h06 = await lease_repo.acquire(
            ws, bot_h06, "fx01-e2e", db_now + timedelta(seconds=120)
        )

    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h06) as uow:
        result_h06 = await coordinator_h06.monitor_market_event(
            uow,
            account_id=acc_h06,
            paper_position_id=f"paper:{intent_h06}",
            ledger_intent_id=intent_h06,
            trade_id=trade_h06,
            position=pos_h06,
            bar=monitor_bar_h06,
            opposing_signal=False,
            atr=evidence_h06.atr,
            exit_channel_level=evidence_h06.exit_channel_level,
            exit_feature_value=evidence_h06.exit_feature_value,
            strategy_bar_closed=evidence_h06.strategy_bar_closed,
            fencing_token=token_h06,
        )

    assert result_h06.position.is_closed is True
    assert result_h06.monitor_result.exit_reason == "FEATURE_EXIT"
    assert result_h06.exit_fill is not None
    assert result_h06.exit_fill_appended is True
    assert result_h06.trade_closed is True

    # 3. Đối soát database PostgreSQL
    async with UnitOfWork(session_factory, workspace_id=ws, bot_id=bot_h06) as uow:
        fills_06 = (
            await uow.session.scalars(
                select(FillRecord).where(
                    FillRecord.workspace_id == ws,
                    FillRecord.intent_id == intent_h06,
                    FillRecord.fill_role == "EXIT",
                )
            )
        ).all()
        assert len(fills_06) == 1

        tr_06 = await uow.session.scalar(
            select(TradeRecord).where(TradeRecord.workspace_id == ws, TradeRecord.id == trade_h06)
        )
        assert tr_06 is not None
        assert tr_06.status == "CLOSED"
        assert tr_06.closed_at is not None

        pe_06 = (
            await uow.session.scalars(
                select(PositionEventRecord).where(
                    PositionEventRecord.workspace_id == ws,
                    PositionEventRecord.intent_id == intent_h06,
                    PositionEventRecord.event_type == "CLOSED",
                )
            )
        ).first()
        assert pe_06 is not None
        assert pe_06.payload.get("exit_reason") == "FEATURE_EXIT"

        # Hydrate và verify ledger consistency
        await coordinator_h06.hydrate_account(uow, account_id=acc_h06)
        assert gateway_h06.position(f"paper:{intent_h06}").is_closed is True

    print("FX01_PAPER_EXIT_E2E_OK")


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


def test_fx01_paper_channel_exit_and_feature_exit_e2e_postgres() -> None:
    """Xác nhận E2E Compose đóng lệnh CHANNEL_EXIT và FEATURE_EXIT dưới PostgreSQL thật."""
    # 1. Xác nhận bản sửa be1c3ab: H03-v2/H06-v2 chọn đúng release của mình trên dev
    from trading_bot.domain.strategy.contracts import StrategySelection
    from trading_bot.domain.strategy.rules_v2 import _v1_selection

    sel_h03 = _v1_selection("h03-v2", "H03", StrategySelection("s", ("h03-v2",), published=True))
    assert isinstance(sel_h03, StrategySelection)
    assert "h03-v1" in sel_h03.active_release_ids

    sel_h06 = _v1_selection("h06-v2", "H06", StrategySelection("s", ("h06-v2",), published=True))
    assert isinstance(sel_h06, StrategySelection)
    assert "h06-v1" in sel_h06.active_release_ids

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
    project = f"crypidian-fx01-exit-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
    }
    try:
        ensure_image_available(root, image)
        _compose(
            root,
            project,
            environment,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
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
        heads = _compose(
            root, project, environment, "run", "--rm", "--no-deps", "migrator", "alembic", "heads"
        )
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
        assert "FX01_PAPER_EXIT_E2E_OK" in result.stdout, result.stdout + "\n" + result.stderr
    finally:
        _compose(root, project, environment, "down", "-v", "--remove-orphans")
