"""RT-1 Compose E2E cho CRY-205: ghim nguồn nến, ưu tiên nguồn thống nhất, fail-closed và receipt.

Kiểm chứng trên PostgreSQL thật với role ``crypidian_trading_runtime`` (RLS đầy đủ):
1. Fail-closed ``PAPER_EQUITY_UNKNOWN`` khi thiếu ``paper_account_seed`` + ghi receipt từ chối
   terminal vào ``paper_candidate_outcome`` để tick sau không đánh giá lại candidate kẹt.
2. Fail-closed ``PAPER_INSTRUMENT_UNAVAILABLE`` khi thiếu ``market_instrument`` + ghi receipt
   terminal; đồng thời xác nhận lỗi tạm thời (``DATABASE_UNAVAILABLE``) không bị chặn retry.
3. Vị thế PAPER ghim nguồn ``CRYPIDIAN_PIPELINE_FIXTURE`` trong ``paper_execution_checkpoint``
   không bị nến nguồn ``BINANCE_USDM`` / ``BINANCE_USDM_REST_BACKFILL`` kích hoạt SL/TP/đóng;
   nến đúng nguồn đã ghim vẫn đóng vị thế (``STOP_LOSS``) và đối soát ledger nhất quán 100%.
4. Quy tắc ưu tiên nguồn thống nhất giữa runtime và chart khi cả ``BINANCE_USDM`` (WS) lẫn
   ``BINANCE_USDM_REST_BACKFILL`` (REST) cùng có nến ở một mốc thời gian.
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
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.types.json import Jsonb
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
)
from trading_bot.adapters.database.models.analyst_entities import PaperCandidateOutcomeRecord
from trading_bot.adapters.database.models.paper_entities import (
    PaperExecutionCheckpointRecord,
    TradeRecord,
)
from trading_bot.adapters.database.repositories.bot_decisions import (
    BotDecisionRepository,
    PostgresSignalDecisionStore,
)
from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
from trading_bot.adapters.database.repositories.paper_checkpoints import PaperCheckpointRepository
from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerRepository
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
    canonical_settings,
    settings_hash,
)
from trading_bot.app import _LOGGED_CANDIDATE_REJECTIONS, _submit_bot_candidates
from trading_bot.application.bot_dispatcher import (
    BOT_DECISION_NAMESPACE,
    BOT_DECISION_SCHEMA_VERSION,
    BotDecision,
    BotDecisionContext,
    BotDecisionJob,
    BotDecisionStatus,
    _hash,
    _utc_iso,
)
from trading_bot.application.bot_reporting import preferred_market_source
from trading_bot.application.market_signal_composition import load_closed_market_bars
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_lifecycle import PaperLifecycleCoordinator
from trading_bot.application.paper_market_context import PostgresPaperDecisionContextProvider
from trading_bot.application.paper_market_event_loop import closed_bars_after, paper_bar_key
from trading_bot.application.paper_runtime_application import (
    PaperDecisionCommand,
    PaperRuntimeApplicationError,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.config.workspace_settings import WorkspaceSettings
from trading_bot.domain.execution.paper import (
    IntentConflict,
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.execution.paper_state import _plan_to_json
from trading_bot.domain.market.bar_source_policy import (
    LIVE_BAR_SOURCE,
    REST_BACKFILL_BAR_SOURCE,
)
from trading_bot.domain.risk.planner import ExchangeRules, TradePlan, calculate_plan_hash
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe


def _runtime_db_url() -> str:
    user = os.environ.get("TRADING_BOT_DATABASE_USER", "crypidian_trading_runtime")
    pw_file = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
    pw = pw_file.read_text(encoding="utf-8").strip()
    return f"postgresql+psycopg://{user}:{pw}@postgres:5432/crypidian"


def _seed_bars(
    conn: psycopg.Connection,
    *,
    ws: UUID,
    inst_id: UUID | None,
    symbol: str,
    timeframe: str,
    source: str,
    start: datetime,
    step: timedelta,
    count: int,
    base_price: Decimal,
    tag: str,
) -> datetime:
    last_close = start
    for i in range(count):
        open_t = start + i * step
        close_t = open_t + step
        avail_t = close_t + timedelta(seconds=1)
        price = base_price + Decimal(i % 5) * Decimal("0.5")
        conn.execute(
            "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
            "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
            "event_at, received_at, available_at, open, high, low, close, volume, is_closed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, 100, true)",
            (
                uuid4(),
                ws,
                inst_id,
                symbol,
                timeframe,
                source,
                f"lk:{tag}:{symbol}:{timeframe}:{i}",
                f"ik:{tag}:{symbol}:{timeframe}:{i}",
                f"{(i + 1):064x}",
                open_t,
                close_t,
                close_t,
                close_t,
                avail_t,
                price,
                price + Decimal("1"),
                price - Decimal("1"),
                price,
            ),
        )
        last_close = close_t
    return last_close


def _insert_candidate_decision(
    conn: psycopg.Connection,
    *,
    ws: UUID,
    bot_id: UUID,
    acc_id: UUID,
    bot_config_id: UUID,
    bot_config_hash: str,
    root_settings_id: UUID,
    root_settings_hash: str,
    release_hash: str,
    symbol: str,
    observed_at: datetime,
    evaluated_at: datetime,
    feature_snapshot_id: str,
) -> BotDecision:
    ev_id = uuid4()
    ev_hash = f"{ev_id.int:064x}"[:64]
    content_hash = f"{(ev_id.int ^ 1):064x}"[:64]
    conn.execute(
        "INSERT INTO shared_signal_evidence (id, workspace_id, symbol, setup_type, "
        "observed_at, available_at, validity_end, feature_snapshot_id, source_refs, "
        "content_hash, evidence_hash, schema_version, canonical_json) "
        "VALUES (%s, %s, %s, 'market.bar.closed:1h', %s, %s, %s, %s, %s, %s, %s, 'crypidian-signal-evidence-v1', %s)",
        (
            ev_id,
            ws,
            symbol,
            observed_at,
            observed_at,
            evaluated_at + timedelta(hours=2),
            feature_snapshot_id,
            Jsonb([f"bar:{symbol}:1h"]),
            content_hash,
            ev_hash,
            json.dumps({"symbol": symbol, "timeframe": "1h"}),
        ),
    )
    context = BotDecisionContext(
        workspace_id=ws,
        bot_id=bot_id,
        account_id=acc_id,
        bot_config_version_id=bot_config_id,
        bot_config_hash=bot_config_hash,
        root_settings_version_id=root_settings_id,
        root_settings_hash=root_settings_hash,
        strategy_release_id="h03-v1",
        symbols=(symbol,),
        effective_at=evaluated_at - timedelta(hours=1),
    )
    evidence_stub = SimpleNamespace(
        workspace_id=ws,
        evidence_id=ev_id,
        evidence_hash=ev_hash,
    )
    job = BotDecisionJob.create(
        evidence=evidence_stub,
        context=context,
        evaluated_at=evaluated_at,
    )
    conn.execute(
        "INSERT INTO bot_decision_job (id, workspace_id, bot_id, account_id, evidence_id, "
        "evidence_hash, bot_config_version_id, evaluated_at, context_hash, context_payload, "
        "status, completed_at, attempts) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'COMPLETED', %s, 1)",
        (
            job.job_id,
            ws,
            bot_id,
            acc_id,
            ev_id,
            ev_hash,
            bot_config_id,
            evaluated_at,
            context.context_hash,
            Jsonb(context.canonical_payload()),
            evaluated_at,
        ),
    )
    cand_id = f"cand-{symbol}-{ev_id.hex[:8]}"
    decision_id = uuid5(BOT_DECISION_NAMESPACE, "decision:" + job.idempotency_key)
    decision_payload = {
        "account_id": str(acc_id),
        "bot_config_hash": bot_config_hash,
        "bot_config_version_id": str(bot_config_id),
        "bot_id": str(bot_id),
        "candidate": {
            "candidate_id": cand_id,
            "symbol": symbol,
            "direction": "long",
            "expires_at": (evaluated_at + timedelta(hours=2)).isoformat(),
        },
        "decision_id": str(decision_id),
        "decision_snapshot_id": feature_snapshot_id,
        "detail_vi": "Tín hiệu thử nghiệm CRY-205",
        "evaluated_at": _utc_iso(evaluated_at),
        "evidence_hash": ev_hash,
        "evidence_id": str(ev_id),
        "feature_snapshot_id": feature_snapshot_id,
        "gate_details": [],
        "idempotency_key": job.idempotency_key,
        "job_id": str(job.job_id),
        "model_release_id": None,
        "reason_code": "H03_LONG_ENTRY",
        "root_settings_hash": root_settings_hash,
        "root_settings_version_id": str(root_settings_id),
        "schema_version": BOT_DECISION_SCHEMA_VERSION,
        "status": BotDecisionStatus.CANDIDATE.value,
        "strategy_parameters": [],
        "strategy_release_hash": release_hash,
        "strategy_release_id": "h03-v1",
        "workspace_id": str(ws),
    }
    decision = BotDecision.from_payload(decision_payload, _hash(decision_payload))
    conn.execute(
        "INSERT INTO bot_decision (id, workspace_id, bot_id, account_id, job_id, evidence_id, "
        "evidence_hash, bot_config_version_id, bot_config_hash, root_settings_version_id, "
        "root_settings_hash, strategy_release_id, strategy_release_hash, evaluated_at, status, "
        "reason_code, candidate_id, idempotency_key, decision_hash, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'CANDIDATE', %s, %s, %s, %s, %s)",
        (
            decision.decision_id,
            ws,
            bot_id,
            acc_id,
            job.job_id,
            ev_id,
            ev_hash,
            bot_config_id,
            bot_config_hash,
            root_settings_id,
            root_settings_hash,
            "h03-v1",
            release_hash,
            evaluated_at,
            decision.reason_code,
            cand_id,
            decision.idempotency_key,
            decision.decision_hash,
            Jsonb(decision.canonical_payload()),
        ),
    )
    return decision


async def main() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    ws = uuid4()
    acc_id = uuid4()
    user_id = uuid4()
    mem_id = uuid4()
    root_settings_id = uuid4()
    bot_config_id = uuid4()
    risk_profile_id = uuid4()
    btc_inst_id = uuid4()
    rel_id = uuid4()
    feature_snap_btc = "1" * 64
    feature_snap_eth = "2" * 64

    release_hash = strategy_registry().get_release("h03-v1").content_hash
    settings_doc = WorkspaceSettings.model_validate(
        {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "timeframes": ["1m", "15m", "1h", "4h"],
                "max_age_seconds": 7200,
            },
            "features": {
                "formula_version": "crypidian-features-v1",
                "sessions_timezone": "UTC",
            },
            "strategy": {
                "default_release": "h03-v1",
                "parameters": {},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {
                "profile_version_id": str(risk_profile_id),
                "max_equity_age_seconds": 3600,
                "max_mark_age_seconds": 3600,
            },
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
    )
    settings_payload = canonical_settings(settings_doc)
    root_hash = settings_hash(settings_payload)
    effective_at = now - timedelta(days=1)

    admin_pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'CRY205 E2E', %s, 'PAPER', 'ACTIVE')",
            (ws, f"cry205-{ws.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'hash', 'ACTIVE')",
            (user_id, f"cry205-{user_id.hex[:8]}@test.io"),
        )
        conn.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (mem_id, ws, user_id),
        )
        conn.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (%s, %s, %s, 'OWNER')",
            (uuid4(), ws, mem_id),
        )
        conn.execute(
            "INSERT INTO risk_profile_version (id, workspace_id, name, version, "
            "max_risk_fraction, max_open_risk_fraction, settings, is_active) "
            "VALUES (%s, %s, 'cry205-risk', 1, 0.01, 1.0, %s, true)",
            (
                risk_profile_id,
                ws,
                Jsonb(
                    {
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
                            "max_account_age_seconds": 86400,
                            "max_data_age_seconds": 7200,
                        },
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
            "settings_json, settings_hash, status, revision, created_by, approved_by, "
            "published_at, effective_at) "
            "VALUES (%s, %s, 1, 1, %s, %s, 'PUBLISHED', 1, %s, %s, %s, %s)",
            (
                root_settings_id,
                ws,
                Jsonb(settings_payload),
                root_hash,
                mem_id,
                mem_id,
                effective_at,
                effective_at,
            ),
        )
        conn.execute(
            "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, content_hash) "
            "VALUES (%s, %s, 'h03-v1', 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (rel_id, ws, release_hash),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, status, max_notional) "
            "VALUES (%s, %s, 'acc-cry205', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 100000)",
            (acc_id, ws),
        )
        bot_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws, acc_id),
        ).fetchone()[0]
        overlay = canonical_overlay(
            BotConfigOverlay.model_validate(
                {
                    "schema_version": 1,
                    "strategy": {"release_id": "h03-v1"},
                    "universe": {"symbols": ["BTCUSDT", "ETHUSDT"]},
                }
            )
        )
        ov_hash = overlay_hash(overlay)
        conn.execute(
            "INSERT INTO bot_config_version (id, workspace_id, bot_id, account_id, version, "
            "schema_version, overlay_json, overlay_hash, base_settings_version_id, "
            "base_settings_hash, status, revision, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 1, 1, %s, %s, %s, %s, 'DRAFT', 1, %s, %s)",
            (
                bot_config_id,
                ws,
                bot_id,
                acc_id,
                Jsonb(overlay),
                ov_hash,
                root_settings_id,
                root_hash,
                mem_id,
                mem_id,
            ),
        )
        conn.execute(
            "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
            "published_at=%s, effective_at=%s WHERE workspace_id=%s AND id=%s",
            (mem_id, effective_at, effective_at, ws, bot_config_id),
        )
        for snap_id, sym in ((feature_snap_btc, "BTCUSDT"), (feature_snap_eth, "ETHUSDT")):
            conn.execute(
                "INSERT INTO feature_snapshot (id, workspace_id, snapshot_id, content_hash, symbol, "
                "timeframe, as_of, computed_at, schema_hash, formula_hash, code_hash, input_bar_ids, "
                "input_event_ids, features, warmup, quality_flags, data_freshness, data_coverage, "
                "regime, is_usable, canonical_json) "
                "VALUES (%s, %s, %s, %s, %s, '1h', %s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, "
                "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'FRESH', 1.0, '{\"label\":\"TREND\"}'::jsonb, true, '{}')",
                (uuid4(), ws, snap_id, snap_id, sym, now - timedelta(hours=1), now - timedelta(hours=1), "e" * 64, "f" * 64, "0" * 64),
            )
        # Seed market_instrument cho BTCUSDT (BINANCE_USDM, sẵn sàng).
        # ETHUSDT chỉ có instrument với available_at trong tương lai -> chưa khả dụng tại evaluated_at
        # => kích hoạt đường fail-closed PAPER_INSTRUMENT_UNAVAILABLE.
        eth_inst_id = uuid4()
        conn.execute(
            "INSERT INTO market_instrument (id, workspace_id, source, idempotency_key, symbol, "
            "base_asset, quote_asset, tick_size, quantity_step, min_notional, status, revision, "
            "checksum, event_at, received_at, available_at) "
            "VALUES (%s, %s, 'BINANCE_USDM', %s, 'BTCUSDT', 'BTC', 'USDT', 0.1, 0.001, 5, "
            "'TRADING', 1, %s, %s, %s, %s)",
            (btc_inst_id, ws, f"inst-btc-{ws.hex[:8]}", "a" * 64, effective_at, effective_at, effective_at),
        )
        future_inst_at = now + timedelta(days=2)
        conn.execute(
            "INSERT INTO market_instrument (id, workspace_id, source, idempotency_key, symbol, "
            "base_asset, quote_asset, tick_size, quantity_step, min_notional, status, revision, "
            "checksum, event_at, received_at, available_at) "
            "VALUES (%s, %s, 'BINANCE_USDM', %s, 'ETHUSDT', 'ETH', 'USDT', 0.01, 0.001, 5, "
            "'TRADING', 1, %s, %s, %s, %s)",
            (eth_inst_id, ws, f"inst-eth-{ws.hex[:8]}", "b" * 64, future_inst_at, future_inst_at, future_inst_at),
        )
        # Seed đủ nến 1h và 4h cho BTCUSDT và ETHUSDT (dùng nguồn BINANCE_USDM_REST_BACKFILL).
        btc_1h_close = _seed_bars(
            conn,
            ws=ws,
            inst_id=btc_inst_id,
            symbol="BTCUSDT",
            timeframe="1h",
            source=REST_BACKFILL_BAR_SOURCE,
            start=now - timedelta(hours=220),
            step=timedelta(hours=1),
            count=210,
            base_price=Decimal("100"),
            tag="btc1h",
        )
        _seed_bars(
            conn,
            ws=ws,
            inst_id=btc_inst_id,
            symbol="BTCUSDT",
            timeframe="4h",
            source=REST_BACKFILL_BAR_SOURCE,
            start=now - timedelta(hours=4 * 220),
            step=timedelta(hours=4),
            count=210,
            base_price=Decimal("100"),
            tag="btc4h",
        )
        eth_1h_close = _seed_bars(
            conn,
            ws=ws,
            inst_id=eth_inst_id,
            symbol="ETHUSDT",
            timeframe="1h",
            source=REST_BACKFILL_BAR_SOURCE,
            start=now - timedelta(hours=220),
            step=timedelta(hours=1),
            count=210,
            base_price=Decimal("200"),
            tag="eth1h",
        )
        _seed_bars(
            conn,
            ws=ws,
            inst_id=eth_inst_id,
            symbol="ETHUSDT",
            timeframe="4h",
            source=REST_BACKFILL_BAR_SOURCE,
            start=now - timedelta(hours=4 * 220),
            step=timedelta(hours=4),
            count=210,
            base_price=Decimal("200"),
            tag="eth4h",
        )
        eval_btc = btc_1h_close + timedelta(seconds=10)
        btc_decision = _insert_candidate_decision(
            conn,
            ws=ws,
            bot_id=bot_id,
            acc_id=acc_id,
            bot_config_id=bot_config_id,
            bot_config_hash=ov_hash,
            root_settings_id=root_settings_id,
            root_settings_hash=root_hash,
            release_hash=release_hash,
            symbol="BTCUSDT",
            observed_at=btc_1h_close + timedelta(seconds=1),
            evaluated_at=eval_btc,
            feature_snapshot_id=feature_snap_btc,
        )

    engine = create_database_engine(_runtime_db_url())
    sessions = create_session_factory(engine)
    provider = PostgresPaperDecisionContextProvider(sessions=sessions, workspace_id=ws)
    store = PostgresSignalDecisionStore(sessions)

    async def record_outcome(decision: BotDecision, stage: str, reason_code: str) -> None:
        async with UnitOfWork(sessions, workspace_id=ws, bot_id=decision.bot_id) as uow:
            await uow.session.execute(
                pg_insert(PaperCandidateOutcomeRecord)
                .values(
                    id=uuid4(),
                    workspace_id=ws,
                    bot_id=decision.bot_id,
                    account_id=decision.account_id,
                    decision_id=decision.decision_id,
                    stage=stage,
                    reason_code=reason_code[:120],
                )
                .on_conflict_do_nothing(constraint="uq_paper_candidate_outcome")
            )

    # =========================================================================
    # Kịch bản (a): Thiếu paper_account_seed -> PAPER_EQUITY_UNKNOWN + receipt
    # =========================================================================
    pending_before = await store.pending_candidate_decisions(
        workspace_id=ws, account_id=acc_id, now=eval_btc + timedelta(seconds=5)
    )
    assert [d.decision_id for d in pending_before] == [btc_decision.decision_id], pending_before

    account_ref = SimpleNamespace(account_id=acc_id, bot_id=bot_id)
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
            workspace_id=ws
        )
        assert published is not None
        cmd_btc = PaperDecisionCommand(
            account_id=acc_id,
            feature_snapshot_id=feature_snap_btc,
            evaluated_at=eval_btc,
            bot_decision_id=btc_decision.decision_id,
        )
        try:
            await provider.load_context(
                uow=uow,
                command=cmd_btc,
                published_settings=published,
                execution_profile=None,
                account=account_ref,
            )
            raise AssertionError("Phải ném PAPER_EQUITY_UNKNOWN khi thiếu paper_account_seed.")
        except PaperRuntimeApplicationError as err:
            assert err.reason_code == "PAPER_EQUITY_UNKNOWN", err.reason_code

    # Ghi nhận từ chối qua _submit_bot_candidates (như runtime production).
    _LOGGED_CANDIDATE_REJECTIONS.clear()

    class _EquityFailApp:
        async def evaluate_and_submit(self, command: object) -> object:
            raise PaperRuntimeApplicationError("PAPER_EQUITY_UNKNOWN")

    await _submit_bot_candidates(_EquityFailApp(), pending_before, record_outcome)
    # Tick 2: candidate đã có receipt terminal -> KHÔNG còn xuất hiện trong pending!
    pending_after_equity = await store.pending_candidate_decisions(
        workspace_id=ws, account_id=acc_id, now=eval_btc + timedelta(seconds=30)
    )
    assert pending_after_equity == (), pending_after_equity

    # =========================================================================
    # Kịch bản (b): Có seed equity nhưng thiếu market_instrument (ETHUSDT)
    # =========================================================================
    eval_eth = eth_1h_close + timedelta(seconds=10)
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO paper_account_seed (id, bot_id, workspace_id, trading_account_id, "
            "quote_asset, initial_equity, source_settings_version_id, source_settings_hash, "
            "idempotency_key) VALUES (%s, %s, %s, %s, 'USDT', 10000, %s, %s, %s)",
            (uuid4(), bot_id, ws, acc_id, root_settings_id, root_hash, f"seed-{acc_id}"),
        )
        eth_decision = _insert_candidate_decision(
            conn,
            ws=ws,
            bot_id=bot_id,
            acc_id=acc_id,
            bot_config_id=bot_config_id,
            bot_config_hash=ov_hash,
            root_settings_id=root_settings_id,
            root_settings_hash=root_hash,
            release_hash=release_hash,
            symbol="ETHUSDT",
            observed_at=eth_1h_close + timedelta(seconds=1),
            evaluated_at=eval_eth,
            feature_snapshot_id=feature_snap_eth,
        )

    pending_eth = await store.pending_candidate_decisions(
        workspace_id=ws, account_id=acc_id, now=eval_eth + timedelta(seconds=5)
    )
    assert [d.decision_id for d in pending_eth] == [eth_decision.decision_id], pending_eth

    # Kiểm chứng lỗi tạm thời (DATABASE_UNAVAILABLE) KHÔNG ghi receipt terminal -> vẫn retry được!
    _LOGGED_CANDIDATE_REJECTIONS.clear()

    class _TransientFailApp:
        async def evaluate_and_submit(self, command: object) -> object:
            raise PaperRuntimeApplicationError("DATABASE_UNAVAILABLE")

    await _submit_bot_candidates(_TransientFailApp(), pending_eth, record_outcome)
    pending_still_retryable = await store.pending_candidate_decisions(
        workspace_id=ws, account_id=acc_id, now=eval_eth + timedelta(seconds=10)
    )
    assert [d.decision_id for d in pending_still_retryable] == [eth_decision.decision_id]

    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
            workspace_id=ws
        )
        cmd_eth = PaperDecisionCommand(
            account_id=acc_id,
            feature_snapshot_id=feature_snap_eth,
            evaluated_at=eval_eth,
            bot_decision_id=eth_decision.decision_id,
        )
        try:
            await provider.load_context(
                uow=uow,
                command=cmd_eth,
                published_settings=published,
                execution_profile=None,
                account=account_ref,
            )
            raise AssertionError("Phải ném PAPER_INSTRUMENT_UNAVAILABLE khi thiếu market_instrument.")
        except PaperRuntimeApplicationError as err:
            assert err.reason_code == "PAPER_INSTRUMENT_UNAVAILABLE", err.reason_code

    _LOGGED_CANDIDATE_REJECTIONS.clear()

    class _InstrumentFailApp:
        async def evaluate_and_submit(self, command: object) -> object:
            raise PaperRuntimeApplicationError("PAPER_INSTRUMENT_UNAVAILABLE")

    await _submit_bot_candidates(_InstrumentFailApp(), pending_eth, record_outcome)
    pending_after_instrument = await store.pending_candidate_decisions(
        workspace_id=ws, account_id=acc_id, now=eval_eth + timedelta(seconds=30)
    )
    assert pending_after_instrument == (), pending_after_instrument

    # =========================================================================
    # Kịch bản (c): Ghim nguồn nến CRYPIDIAN_PIPELINE_FIXTURE & chặn nguồn khác
    # =========================================================================
    policy = PaperExecutionPolicy(
        version_id="paper-policy-v1",
        latency=timedelta(seconds=5),
        spread_rate=Decimal("0.0001"),
        slippage_rate=Decimal("0.0002"),
        fee_rate=Decimal("0.0005"),
    )
    rules = ExchangeRules(
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        min_notional=Decimal("5"),
        max_notional=Decimal("100000"),
        max_quantity=Decimal("1000"),
    )
    future_exp = (datetime.now(UTC) + timedelta(hours=24)).replace(microsecond=0)
    base_plan = TradePlan(
        workspace_id=str(ws),
        account_id=str(acc_id),
        settings_version_id=str(root_settings_id),
        settings_hash=None,
        risk_profile_version_id=str(risk_profile_id),
        strategy_id="H03",
        strategy_release_id="h03-v1",
        release_content_hash=release_hash,
        snapshot_id=feature_snap_btc,
        candidate_id="c" * 64,
        symbol="BTCUSDT",
        direction="long",
        candidate_reason_code="H03_LONG_ENTRY",
        evidence_feature_ids=(feature_snap_btc,),
        evidence_bar_keys=("fixture-entry-1",),
        macro_evidence_ids=(),
        entry_reference_price=Decimal("100"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        leverage=Decimal("2"),
        initial_margin=Decimal("50"),
        risk_budget=Decimal("5"),
        estimated_loss=Decimal("5"),
        entry_fee_cost=Decimal("0.05"),
        exit_fee_cost=Decimal("0.05"),
        adverse_funding_cost=Decimal("0"),
        entry_slippage_cost=Decimal("0.02"),
        cost_model_version="cost-v1",
        exchange_rules=rules,
        exit_recipe=ExitRecipe("h03-exit-v1", 2.0, take_profit_atr_multiple=4.0, max_hold_bars=24),
        expires_at=future_exp,
        source_signal_at=now - timedelta(minutes=5),
        plan_hash="",
    )
    plan_obj = replace(base_plan, plan_hash=calculate_plan_hash(base_plan))
    plan_id = uuid4()
    risk_dec_id = uuid4()
    res_id = uuid4()
    idem_key = f"cry205-pin-{uuid4().hex[:8]}"

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO trade_plan (id, workspace_id, account_id, bot_id, plan_hash, symbol, "
            "notional, payload) VALUES (%s, %s, %s, %s, %s, 'BTCUSDT', 100, %s::jsonb)",
            (plan_id, ws, acc_id, bot_id, plan_obj.plan_hash, json.dumps(_plan_to_json(plan_obj))),
        )
        conn.execute(
            "INSERT INTO risk_decision (id, workspace_id, account_id, bot_id, decision_key, "
            "plan_hash, snapshot_id, approved, reason_code, detail_vi, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, true, 'APPROVED', 'OK', %s::jsonb)",
            (
                risk_dec_id,
                ws,
                acc_id,
                bot_id,
                f"dec-{idem_key}",
                plan_obj.plan_hash,
                feature_snap_btc,
                json.dumps({"candidate_expires_at": future_exp.isoformat()}),
            ),
        )
        conn.execute(
            "INSERT INTO risk_reservation (id, workspace_id, account_id, bot_id, risk_decision_id, "
            "idempotency_key, plan_hash, symbol, reserved_risk, reserved_notional, "
            "reserved_initial_margin, expires_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'BTCUSDT', 5, 100, 50, %s, 'RESERVED')",
            (
                res_id,
                ws,
                acc_id,
                bot_id,
                risk_dec_id,
                f"res-{idem_key}",
                plan_obj.plan_hash,
                future_exp,
            ),
        )
    # Khởi tạo gateway + submit qua coordinator.submit_approved thật (tạo intent + exchange_order + checkpoint)
    gateway = PaperExecutionGateway(policy)
    ledger_repo = PaperLedgerRepository()
    checkpoint_repo = PaperCheckpointRepository()
    coordinator = PaperLifecycleCoordinator(gateway, ledger_repo, checkpoint_repo)

    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        await uow.bind_account(acc_id)
        lease_repo = BotRuntimeLease(uow.session)
        db_now = await lease_repo.database_now()
        fencing = await lease_repo.acquire(
            ws, bot_id, "cry205-worker-1", db_now + timedelta(seconds=120)
        )
        submitted = await coordinator.submit_approved(
            uow,
            account_id=acc_id,
            result=SimpleNamespace(
                outcome=SimpleNamespace(value="RISK_APPROVED"),
                plan=plan_obj,
                strategy_result=SimpleNamespace(
                    candidate_id="c" * 64,
                    eligible_after=now + timedelta(seconds=1),
                ),
                risk_decision=SimpleNamespace(approved=True),
            ),
            evaluated_at=now,
            idempotency_key=idem_key,
            strategy_release_id=rel_id,
            feature_snapshot_id=feature_snap_btc,
            risk_decision_id=risk_dec_id,
            risk_reservation_id=res_id,
            quote_asset="USDT",
            evidence={"source": "CRYPIDIAN_PIPELINE_FIXTURE"},
            fencing_token=fencing,
        )
        intent_id = submitted.ledger_intent_id
        paper_intent_id = submitted.order.intent_id

    entry_open = now + timedelta(minutes=1)
    entry_bar = PaperBar(
        bar_key=paper_bar_key(
            source="CRYPIDIAN_PIPELINE_FIXTURE",
            symbol="BTCUSDT",
            timeframe="1m",
            open_time=entry_open,
        ),
        symbol="BTCUSDT",
        opened_at=entry_open,
        closed_at=entry_open + timedelta(minutes=1),
        available_at=entry_open + timedelta(minutes=1, seconds=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        bar_source="CRYPIDIAN_PIPELINE_FIXTURE",
    )
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        entry_res = await coordinator.apply_entry_market_event(
            uow,
            account_id=acc_id,
            ledger_intent_id=intent_id,
            paper_intent_id=paper_intent_id,
            bar=entry_bar,
            fencing_token=fencing,
        )
        assert entry_res.trade_id is not None
        assert entry_res.position is not None
        assert entry_res.position.bar_source == "CRYPIDIAN_PIPELINE_FIXTURE"
        trade_id = entry_res.trade_id

    # Kiểm chứng checkpoint đã lưu bar_source trong DB và hydrate lại qua coordinator mới!
    hydrated_gateway = PaperExecutionGateway(policy)
    hydrated_coord = PaperLifecycleCoordinator(hydrated_gateway, ledger_repo, checkpoint_repo)
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        latest_cp = await uow.session.scalar(
            select(PaperExecutionCheckpointRecord)
            .where(
                PaperExecutionCheckpointRecord.workspace_id == ws,
                PaperExecutionCheckpointRecord.account_id == acc_id,
            )
            .order_by(PaperExecutionCheckpointRecord.revision.desc())
            .limit(1)
        )
        assert latest_cp is not None
        cp_body = json.loads(latest_cp.snapshot_payload)
        assert cp_body["positions"][0]["bar_source"] == "CRYPIDIAN_PIPELINE_FIXTURE"
        await hydrated_coord.hydrate_account(uow, account_id=acc_id)

    hydrated_pos = hydrated_gateway.position(paper_intent_id)
    assert hydrated_pos is not None
    assert hydrated_pos.bar_source == "CRYPIDIAN_PIPELINE_FIXTURE"

    # Seed nến 1m từ BINANCE_USDM và BINANCE_USDM_REST_BACKFILL (giá 65.000, low 50 — xuyên SL/TP!)
    monitor_open = entry_open + timedelta(minutes=1)
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        for foreign_src, close_p in (
            (LIVE_BAR_SOURCE, Decimal("61000")),
            (REST_BACKFILL_BAR_SOURCE, Decimal("60000")),
        ):
            conn.execute(
                "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
                "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
                "event_at, received_at, available_at, open, high, low, close, volume, is_closed) "
                "VALUES (%s, %s, %s, 'BTCUSDT', '1m', %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, "
                "65000, 65500, 50, %s, 500, true)",
                (
                    uuid4(),
                    ws,
                    btc_inst_id,
                    foreign_src,
                    f"lk-foreign:{foreign_src}",
                    f"ik-foreign:{foreign_src}",
                    "9" * 64,
                    monitor_open,
                    monitor_open + timedelta(minutes=1),
                    monitor_open + timedelta(minutes=1),
                    monitor_open + timedelta(minutes=1),
                    monitor_open + timedelta(minutes=1, seconds=1),
                    close_p,
                ),
            )

    check_now = monitor_open + timedelta(minutes=2)
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        # 1) Vị thế ghim CRYPIDIAN_PIPELINE_FIXTURE KHÔNG đọc được nến BINANCE_USDM / REST!
        pinned_bars = await closed_bars_after(
            uow.session,
            workspace_id=ws,
            symbol="BTCUSDT",
            timeframe="1m",
            now=check_now,
            after=entry_open,
            at_or_after=None,
            limit=50,
            pinned_source=hydrated_pos.bar_source,
        )
        assert pinned_bars == (), f"Không được lấy nến khác nguồn: {pinned_bars}"

        # 2) Khi KHÔNG ghim nguồn (ưu tiên thống nhất WS > REST), cả runtime lẫn chart chọn BINANCE_USDM (61000)!
        unpinned_bars = await closed_bars_after(
            uow.session,
            workspace_id=ws,
            symbol="BTCUSDT",
            timeframe="1m",
            now=check_now,
            after=entry_open,
            at_or_after=None,
            limit=50,
        )
        assert len(unpinned_bars) == 1
        assert unpinned_bars[0].bar_source == LIVE_BAR_SOURCE
        assert unpinned_bars[0].close == Decimal("61000")

        signal_bars = await load_closed_market_bars(
            uow.session,
            workspace_id=ws,
            symbol="BTCUSDT",
            timeframe="1m",
            as_of=check_now,
        )
        assert len(signal_bars) == 1
        assert signal_bars[0].source == LIVE_BAR_SOURCE
        assert signal_bars[0].close == Decimal("61000")
        assert (
            preferred_market_source([REST_BACKFILL_BAR_SOURCE, LIVE_BAR_SOURCE, "CRYPIDIAN_PIPELINE_FIXTURE"])
            == LIVE_BAR_SOURCE
        )

    # 3) Phòng thủ lớp domain: ép đưa nến BINANCE_USDM vào monitor_position -> IntentConflict, vị thế vẫn OPEN!
    foreign_bar = unpinned_bars[0]
    try:
        hydrated_gateway.monitor_position(hydrated_pos, foreign_bar)
        raise AssertionError("monitor_position phải từ chối nến khác nguồn đã ghim!")
    except IntentConflict:
        pass

    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        trade_still_open = await uow.session.scalar(
            select(TradeRecord).where(TradeRecord.workspace_id == ws, TradeRecord.id == trade_id)
        )
        assert trade_still_open is not None and trade_still_open.status == "OPEN"

    # 4) Thêm nến 1m đúng nguồn CRYPIDIAN_PIPELINE_FIXTURE chạm SL (low=94 <= 95) -> đóng STOP_LOSS!
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
            "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
            "event_at, received_at, available_at, open, high, low, close, volume, is_closed) "
            "VALUES (%s, %s, %s, 'BTCUSDT', '1m', 'CRYPIDIAN_PIPELINE_FIXTURE', %s, %s, 1, %s, "
            "%s, %s, %s, %s, %s, 99, 99.5, 94, 94.5, 100, true)",
            (
                uuid4(),
                ws,
                btc_inst_id,
                f"lk-fixture-stop-{ws.hex[:8]}",
                f"ik-fixture-stop-{ws.hex[:8]}",
                "8" * 64,
                monitor_open,
                monitor_open + timedelta(minutes=1),
                monitor_open + timedelta(minutes=1),
                monitor_open + timedelta(minutes=1),
                monitor_open + timedelta(minutes=1, seconds=1),
            ),
        )

    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        same_source_bars = await closed_bars_after(
            uow.session,
            workspace_id=ws,
            symbol="BTCUSDT",
            timeframe="1m",
            now=check_now,
            after=entry_open,
            at_or_after=None,
            limit=50,
            pinned_source=hydrated_pos.bar_source,
        )
        assert len(same_source_bars) == 1
        assert same_source_bars[0].bar_source == "CRYPIDIAN_PIPELINE_FIXTURE"
        monitored = await hydrated_coord.monitor_market_event(
            uow,
            account_id=acc_id,
            ledger_intent_id=intent_id,
            trade_id=trade_id,
            paper_position_id=paper_intent_id,
            position=hydrated_pos,
            bar=same_source_bars[0],
            fencing_token=fencing,
        )
        assert monitored.trade_closed is True
        assert monitored.monitor_result.exit_reason == "STOP_LOSS"

    # Đối soát lại toàn bộ checkpoint ↔ ledger trên coordinator mới.
    final_coord = PaperLifecycleCoordinator(
        PaperExecutionGateway(policy), ledger_repo, checkpoint_repo
    )
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot_id) as uow:
        await final_coord.hydrate_account(uow, account_id=acc_id)
        closed_trade = await uow.session.scalar(
            select(TradeRecord).where(TradeRecord.workspace_id == ws, TradeRecord.id == trade_id)
        )
        assert closed_trade is not None and closed_trade.status == "CLOSED"

    await engine.dispose()
    print("CRY205_RT1_E2E_OK")


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


def test_cry205_rt1_bar_source_pinning_and_fail_closed_e2e_postgres() -> None:
    """Xác nhận E2E Compose CRY-205 trên PostgreSQL thật với role trading-runtime."""
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
    project = f"crypidian-cry205-rt1-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
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
        assert "CRY205_RT1_E2E_OK" in result.stdout, result.stdout + "\n" + result.stderr
    finally:
        _compose(root, project, environment, "down", "-v", "--remove-orphans")
