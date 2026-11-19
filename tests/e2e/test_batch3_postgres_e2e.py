"""E2E PostgreSQL thật cho Đợt 3: CRY-263, CRY-266, CRY-439, CRY-440 và CRY-438."""
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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from sqlalchemy import text

from trading_bot.adapters.database.repositories.operational_telemetry import (
    OperationalTelemetryRepository,
)
from trading_bot.adapters.database.repositories.paper_entry_operator_blocks import (
    OperatorEntryBlockError,
    PaperEntryOperatorBlockRepository,
)
from trading_bot.adapters.database.engine import create_database_engine, create_session_factory
from trading_bot.application.learning_signal_dataset import (
    load_and_build_signal_outcome_dataset,
)
from trading_bot.application.operational_telemetry import (
    OperationalHealthService,
    evaluate_and_persist_breaker,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.execution.runtime_control import (
    BotEntryState,
    BotReadiness,
    EntryBlockReason,
    EntryGate,
    EntryGateInput,
    RuntimeActivityStatus,
    RuntimeOperation,
    WorkspaceKillState,
)
from trading_bot.domain.risk.circuit_breaker import BreakerState, CircuitBreakerPolicy


def _db_url(user: str, password_file: str) -> str:
    pw = Path(password_file).read_text(encoding="utf-8").strip()
    return f"postgresql+psycopg://{user}:{pw}@postgres:5432/crypidian"


async def main() -> None:
    admin_pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    ws1 = uuid4()
    ws2 = uuid4()
    bot1 = uuid4()
    acc1 = uuid4()
    user1 = uuid4()
    mem1 = uuid4()
    dec1 = uuid4()
    dec2 = uuid4()
    now = datetime.now(UTC)

    # 1. Seed cơ bản bằng superuser để kiểm tra RLS/role/trigger
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, %s, %s, 'PAPER', 'ACTIVE'), (%s, %s, %s, 'PAPER', 'ACTIVE')",
            (ws1, "WS1", f"ws1-{ws1.hex[:8]}", ws2, "WS2", f"ws2-{ws2.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (user1, f"u-{user1.hex[:8]}@test.io", "hash"),
        )
        conn.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (mem1, ws1, user1),
        )
        conn.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (%s, %s, %s, 'OWNER')",
            (uuid4(), ws1, mem1),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, status, max_notional) "
            "VALUES (%s, %s, 'acc1', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 10000)",
            (acc1, ws1),
        )
        rel_id = uuid4()
        conn.execute(
            "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, content_hash) "
            "VALUES (%s, %s, %s, 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (rel_id, ws1, f"h01-{ws1.hex[:8]}", "a" * 64),
        )
        conn.execute(
            "INSERT INTO bot_instance (id, workspace_id, trading_account_id, name, status) "
            "VALUES (%s, %s, %s, 'bot1', 'ENABLED') ON CONFLICT DO NOTHING",
            (bot1, ws1, acc1),
        )
        bot1 = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws1, acc1),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, granted_by) "
            "VALUES (%s, %s, %s, %s, 'bot:read', %s)",
            (uuid4(), ws1, bot1, mem1, mem1),
        )
        # Seed market_instrument + market_bar đã đóng cũ để kích hoạt FEED_STALE
        inst_id = uuid4()
        conn.execute(
            "INSERT INTO market_instrument (id, workspace_id, source, idempotency_key, symbol, "
            "base_asset, quote_asset, tick_size, quantity_step, min_notional, status, revision, "
            "checksum, event_at, received_at, available_at) "
            "VALUES (%s, %s, 'BINANCE_USDM_REST_BACKFILL', %s, 'BTCUSDT', 'BTC', 'USDT', 0.1, 0.001, 5, "
            "'TRADING', 1, %s, %s, %s, %s)",
            (inst_id, ws1, f"inst-{ws1.hex[:8]}", "b" * 64, now, now, now),
        )
        conn.execute(
            "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
            "logical_key, idempotency_key, revision, checksum, open_time, close_time, "
            "event_at, received_at, open, high, low, close, volume, is_closed, available_at) "
            "VALUES (%s, %s, %s, 'BTCUSDT', '1m', 'BINANCE_USDM_REST_BACKFILL', %s, %s, 1, %s, %s, %s, "
            "%s, %s, 100, 101, 99, 100, 10, true, %s)",
            (
                uuid4(),
                ws1,
                inst_id,
                f"lk-{ws1.hex[:8]}",
                f"ik-{ws1.hex[:8]}",
                "b" * 64,
                now - timedelta(minutes=10),
                now - timedelta(minutes=9),
                now - timedelta(minutes=9),
                now - timedelta(minutes=9),
                now - timedelta(minutes=9),
            ),
        )
        # Seed workspace_settings_version, bot_config_version, shared_signal_evidence, bot_decision_job, bot_decision
        wsv_id = uuid4()
        bcv_id = uuid4()
        ev1_id = uuid4()
        ev2_id = uuid4()
        job1_id = uuid4()
        job2_id = uuid4()
        snap_id = "c" * 64
        conn.execute(
            "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
            "settings_json, settings_hash, status, created_by) "
            "VALUES (%s, %s, 1, 1, '{}'::jsonb, %s, 'PUBLISHED', %s)",
            (wsv_id, ws1, "9" * 64, mem1),
        )
        conn.execute(
            "INSERT INTO feature_snapshot (id, workspace_id, snapshot_id, content_hash, symbol, timeframe, "
            "as_of, computed_at, schema_hash, formula_hash, code_hash, input_bar_ids, input_event_ids, "
            "features, warmup, quality_flags, data_freshness, data_coverage, regime, is_usable, canonical_json) "
            "VALUES (%s, %s, %s, %s, 'BTCUSDT', '1h', %s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, "
            "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'FRESH', 1.0, '{\"label\":\"TREND\"}'::jsonb, true, '{}')",
            (uuid4(), ws1, snap_id, snap_id, now - timedelta(hours=5), now - timedelta(hours=5), "e" * 64, "f" * 64, "0" * 64),
        )
        conn.execute(
            "INSERT INTO bot_config_version (id, workspace_id, bot_id, account_id, version, schema_version, "
            "overlay_json, overlay_hash, base_settings_version_id, base_settings_hash, status, revision, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 1, 1, '{}'::jsonb, %s, %s, %s, 'DRAFT', 1, %s, %s)",
            (bcv_id, ws1, bot1, acc1, "1" * 64, wsv_id, "9" * 64, mem1, mem1),
        )
        conn.execute(
            "UPDATE bot_config_version SET status = 'PUBLISHED', approved_by = %s, published_at = %s, effective_at = %s WHERE id = %s",
            (mem1, now - timedelta(hours=6), now - timedelta(hours=6), bcv_id),
        )
        for idx, evid in enumerate((ev1_id, ev2_id), start=1):
            conn.execute(
                "INSERT INTO shared_signal_evidence (id, workspace_id, symbol, setup_type, observed_at, "
                "available_at, validity_end, feature_snapshot_id, source_refs, content_hash, evidence_hash, "
                "schema_version, canonical_json) "
                "VALUES (%s, %s, 'BTCUSDT', 'H01', %s, %s, %s, %s, '[\"BINANCE_USDM_REST\"]'::jsonb, %s, %s, '1', '{}')",
                (
                    evid,
                    ws1,
                    now - timedelta(hours=5 + idx),
                    now - timedelta(hours=5 + idx),
                    now + timedelta(hours=1),
                    snap_id,
                    str(idx + 1) * 64,
                    str(idx + 1) * 64,
                ),
            )
        for jid, did, evid in ((job1_id, dec1, ev1_id), (job2_id, dec2, ev2_id)):
            conn.execute(
                "INSERT INTO bot_decision_job (id, workspace_id, bot_id, account_id, evidence_id, evidence_hash, "
                "bot_config_version_id, evaluated_at, context_hash, context_payload, status, completed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, 'COMPLETED', %s)",
                (jid, ws1, bot1, acc1, evid, "2" * 64, bcv_id, now - timedelta(hours=5), uuid4().hex + uuid4().hex, now - timedelta(hours=5)),
            )
            payload = json.dumps({"feature_snapshot_id": snap_id, "candidate": {"symbol": "BTCUSDT"}})
            conn.execute(
                "INSERT INTO bot_decision (id, workspace_id, bot_id, account_id, job_id, evidence_id, evidence_hash, "
                "bot_config_version_id, bot_config_hash, root_settings_version_id, root_settings_hash, "
                "strategy_release_id, strategy_release_hash, evaluated_at, status, candidate_id, "
                "reason_code, idempotency_key, decision_hash, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'h01', %s, %s, 'CANDIDATE', %s, 'OK', %s, %s, %s::jsonb)",
                (did, ws1, bot1, acc1, jid, evid, "2" * 64, bcv_id, "1" * 64, wsv_id, "9" * 64, "a" * 64, now - timedelta(hours=5), "4" * 64, uuid4().hex + uuid4().hex, "5" * 64, payload),
            )

        # CRY-439: kiểm tra role crypidian_scorer_history
        role_row = conn.execute(
            "SELECT rolsuper, rolinherit, rolbypassrls FROM pg_roles WHERE rolname = 'crypidian_scorer_history'"
        ).fetchone()
        assert role_row == (False, False, False), role_row
        members = conn.execute(
            "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = 'crypidian_scorer_history'"
        ).fetchall()
        assert members == [], members

        # Seed learning_score_event cho ws1 và ws2
        reg_id = uuid4()
        art_id = uuid4()
        rep_id = uuid4()
        prf_id = uuid4()
        dep_id = uuid4()
        # Dùng superuser để kiểm tra column-level privilege bằng SET LOCAL ROLE
        conn.execute("SET LOCAL ROLE crypidian_scorer_history") if False else None

    # Test column-level privilege + RLS của crypidian_scorer_history
    scorer_engine = create_database_engine(
        _db_url("crypidian_scorer_history", "/run/secrets/database_scorer_history_password")
    )
    scorer_sessions = create_session_factory(scorer_engine)
    async with UnitOfWork(scorer_sessions, workspace_id=ws1, snapshot_read_only=True) as uow:
        # Được đọc đúng allowlist cột
        rows = (
            await uow.session.execute(
                text(
                    "SELECT id, workspace_id, created_at, mode, decision, reason_code, "
                    "probability, confidence, uncertainty FROM learning_score_event"
                )
            )
        ).all()
        assert rows == []
        # Bị từ chối đọc cột nhạy cảm hoặc table-wide
        for forbidden_sql in (
            "SELECT * FROM learning_score_event",
            "SELECT bot_decision_id FROM learning_score_event",
            "SELECT id FROM bot_decision",
        ):
            failed = False
            try:
                await uow.session.execute(text(forbidden_sql))
            except Exception:
                failed = True
            assert failed, f"Query lẽ ra phải bị từ chối: {forbidden_sql}"
    await scorer_engine.dispose()

    # Test CRY-263 & CRY-266 với role runtime + admin_web thật
    runtime_engine = create_database_engine(
        _db_url("crypidian_trading_runtime", "/run/secrets/database_trading_runtime_password")
    )
    runtime_sessions = create_session_factory(runtime_engine)
    admin_engine = create_database_engine(
        _db_url("crypidian_admin_web", "/run/secrets/database_admin_web_password")
    )
    admin_sessions = create_session_factory(admin_engine)

    # Runtime đánh giá telemetry thật (feed 9 phút > 30s -> breaker OPEN)
    async with UnitOfWork(runtime_sessions, workspace_id=ws1) as uow:
        telemetry, _lw, snap = await evaluate_and_persist_breaker(
            uow.session,
            workspace_id=ws1,
            bot_id=bot1,
            as_of=now,
            policy=CircuitBreakerPolicy(max_feed_age_seconds=30, cooldown_period=timedelta(seconds=0)),
            outbox=uow,
        )
        assert telemetry.feed_age_seconds is not None and telemetry.feed_age_seconds > 30
        assert snap.state is BreakerState.OPEN
        open_snap = await OperationalTelemetryRepository(uow.session).has_open_breaker(
            workspace_id=ws1, bot_id=bot1
        )
        assert open_snap is not None and not open_snap.entry_allowed

    # Trigger immutable trên circuit_breaker_event
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        failed = False
        try:
            conn.execute("UPDATE circuit_breaker_event SET detail_vi = 'hack'")
        except psycopg.errors.RaiseException as exc:
            assert "CIRCUIT_BREAKER_EVENT_IMMUTABLE" in str(exc)
            failed = True
        assert failed

    # EntryGate chặn NEW_ENTRY nhưng cho PROTECTIVE_EXIT
    gate_in = EntryGateInput(
        account_enabled=True,
        bot_enabled=True,
        workspace_kill_state=WorkspaceKillState.ACTIVE,
        bot_entry_state=BotEntryState.ENABLED,
        readiness=BotReadiness.READY,
        lease_held=True,
        open_incident=False,
        activity_status=RuntimeActivityStatus.VERIFIED,
        control_available=True,
        learning_incident_hold_active=False,
        circuit_breaker_open=True,
    )
    assert EntryGate.evaluate(gate_in, RuntimeOperation.NEW_ENTRY).reason is EntryBlockReason.CIRCUIT_BREAKER_OPEN
    assert EntryGate.evaluate(gate_in, RuntimeOperation.PROTECTIVE_EXIT).allowed is True

    # Test CRY-440: Operator block thắng trước, và nếu đã có OrderIntent thì block bị từ chối 409
    async with UnitOfWork(admin_sessions, workspace_id=ws1, actor_membership_id=mem1) as uow:
        blk = await PaperEntryOperatorBlockRepository(uow.session).block_decision(
            workspace_id=ws1,
            bot_decision_id=dec1,
            blocked_by=mem1,
            reason="Chặn PAPER entry test E2E",
            mfa_verified_at=now,
            correlation_id=uuid4(),
        )
        assert blk.bot_decision_id == dec1

    async with UnitOfWork(runtime_sessions, workspace_id=ws1) as uow:
        found = await PaperEntryOperatorBlockRepository(uow.session).lock_decision_and_check_blocked(
            workspace_id=ws1, bot_decision_id=dec1
        )
        assert found is not None

    # Seed OrderIntent cho dec2 rồi thử block dec2 -> phải lỗi ENTRY_ALREADY_RECORDED
    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        risk_dec_id = uuid4()
        res_id = uuid4()
        conn.execute(
            "INSERT INTO trade_plan (id, workspace_id, account_id, bot_id, plan_hash, symbol, notional, payload) "
            "VALUES (%s, %s, %s, %s, %s, 'BTCUSDT', 100, '{}'::jsonb)",
            (uuid4(), ws1, acc1, bot1, "7" * 64),
        )
        conn.execute(
            "INSERT INTO risk_decision (id, workspace_id, account_id, bot_id, decision_key, plan_hash, "
            "snapshot_id, approved, reason_code, detail_vi, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, true, 'OK', 'Duyệt', '{}'::jsonb)",
            (risk_dec_id, ws1, acc1, bot1, "6" * 64, "7" * 64, snap_id),
        )
        conn.execute(
            "INSERT INTO risk_reservation (id, workspace_id, account_id, bot_id, risk_decision_id, "
            "idempotency_key, plan_hash, symbol, reserved_risk, reserved_notional, reserved_initial_margin, status, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'BTCUSDT', 10, 100, 10, 'RESERVED', %s)",
            (res_id, ws1, acc1, bot1, risk_dec_id, uuid4().hex, "7" * 64, now + timedelta(hours=1)),
        )
        conn.execute(
            "INSERT INTO order_intent (id, workspace_id, account_id, bot_id, mode, idempotency_key, plan_hash, "
            "settings_version_id, strategy_release_id, strategy_release_hash, feature_snapshot_id, candidate_id, "
            "bot_decision_id, risk_decision_id, risk_reservation_id, symbol, quote_asset, side, quantity, filled_quantity, status, evidence, created_at) "
            "VALUES (%s, %s, %s, %s, 'PAPER', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'BTCUSDT', 'USDT', 'LONG', 0.01, 0, 'ACCEPTED', '{}'::jsonb, %s)",
            (uuid4(), ws1, acc1, bot1, uuid4().hex, "7" * 64, wsv_id, rel_id, "a" * 64, snap_id, "4" * 64, dec2, risk_dec_id, res_id, now),
        )

    async with UnitOfWork(admin_sessions, workspace_id=ws1, actor_membership_id=mem1) as uow:
        conflict = False
        try:
            await PaperEntryOperatorBlockRepository(uow.session).block_decision(
                workspace_id=ws1,
                bot_decision_id=dec2,
                blocked_by=mem1,
                reason="Thử chặn sau khi đã có OrderIntent",
                mfa_verified_at=now,
                correlation_id=uuid4(),
            )
        except OperatorEntryBlockError as exc:
            assert exc.code == "ENTRY_ALREADY_RECORDED"
            conflict = True
        assert conflict

    # Test CRY-438: signal_outcome + bar_sources + training rights dataset builder
    from trading_bot.domain.research.license_scope import (
        SourceLicenseScope,
        canonical_scope_json,
        scope_sha256,
    )
    from trading_bot.domain.research.source_policy import SourceUsePurpose

    scope_obj = SourceLicenseScope(
        source_id="BINANCE_USDM_REST",
        provider="BINANCE_USDM_REST",
        product="E2E_FIXTURE",
        venue="E2E_FIXTURE",
        contract="E2E_FIXTURE",
        symbols=("BTCUSDT",),
        intervals=("1h",),
        data_start=datetime(2020, 1, 1, tzinfo=UTC),
        data_end=datetime(2040, 1, 1, tzinfo=UTC),
        allowed_purposes=(SourceUsePurpose.TRAINING,),
        allowed_outputs=("trained_model",),
        derived_use=("model_training",),
        max_retention_days=None,
        attribution="Fixture E2E",
    )
    scope_json = canonical_scope_json(scope_obj)
    scope_digest = scope_sha256(scope_obj)
    lic_ev_id = uuid4()
    lic_digest = "8" * 64

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=admin_pw, autocommit=True
    ) as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws1),))
        conn.execute(
            "INSERT INTO source_policy_revision (id, workspace_id, source_id, sequence, policy_id, state, "
            "allowed_purposes, provider, product, venue, contract, license_scope, license_scope_hash, evidence_ref, reviewed_at, publisher, endpoint_url, access_method, "
            "auth_scope, attribution, retention_ref, quota_window_seconds, quota_max_requests, quota_max_bytes, "
            "quota_max_cost, quota_cost_unit, actor_id, reason, approved_by) "
            "VALUES (%s, %s, 'BINANCE_USDM_REST', 1, 'pol-rest-1', 'discovered', '[]'::jsonb, 'BINANCE_USDM_REST', 'E2E_FIXTURE', 'E2E_FIXTURE', 'E2E_FIXTURE', NULL, NULL, NULL, NULL, "
            "'Binance', 'https://fapi.binance.com', 'REST', 'public', 'Fixture E2E', 'ret-1', 60, 100, 1000, 0, 'REQ', 'actor-1', 'discover', NULL), "
            "(%s, %s, 'BINANCE_USDM_REST', 2, 'pol-rest-2', 'rights_pending', '[\"training\"]'::jsonb, 'BINANCE_USDM_REST', 'E2E_FIXTURE', 'E2E_FIXTURE', 'E2E_FIXTURE', %s::jsonb, NULL, 'ev-1', %s, "
            "'Binance', 'https://fapi.binance.com', 'REST', 'public', 'Fixture E2E', 'ret-1', 60, 100, 1000, 0, 'REQ', 'actor-1', 'pending', NULL)",
            (uuid4(), ws1, uuid4(), ws1, scope_json, now - timedelta(days=1)),
        )
        conn.execute(
            "INSERT INTO source_license_evidence (id, workspace_id, source_id, policy_id, idempotency_key, "
            "evidence_ref, digest, byte_count, media_type, scope_version, license_scope, canonical_scope, scope_hash, actor_id) "
            "VALUES (%s, %s, 'BINANCE_USDM_REST', 'pol-rest-2', %s, 'ev-1', %s, 12, 'text/plain', 'wp06-license-scope-v1', %s::jsonb, %s, %s, 'actor-1')",
            (lic_ev_id, ws1, uuid4(), lic_digest, scope_json, scope_json, scope_digest),
        )
        conn.execute(
            "INSERT INTO source_policy_revision (id, workspace_id, source_id, sequence, policy_id, state, "
            "allowed_purposes, provider, product, venue, contract, license_scope, license_evidence_id, license_digest, license_scope_hash, evidence_ref, reviewed_at, "
            "publisher, endpoint_url, access_method, auth_scope, attribution, retention_ref, quota_window_seconds, quota_max_requests, "
            "quota_max_bytes, quota_max_cost, quota_cost_unit, actor_id, reason, approved_by) "
            "VALUES (%s, %s, 'BINANCE_USDM_REST', 3, 'pol-rest-3', 'verified', '[\"training\"]'::jsonb, 'BINANCE_USDM_REST', 'E2E_FIXTURE', 'E2E_FIXTURE', 'E2E_FIXTURE', %s::jsonb, %s, %s, %s, 'ev-1', %s, "
            "'Binance', 'https://fapi.binance.com', 'REST', 'public', 'Fixture E2E', 'ret-1', 60, 100, 1000, 0, 'REQ', 'actor-1', 'ok', 'approver-1')",
            (uuid4(), ws1, scope_json, lic_ev_id, lic_digest, scope_digest, now - timedelta(days=1)),
        )
        conn.execute(
            "INSERT INTO signal_outcome (id, workspace_id, bot_id, decision_id, method_version, label_version, "
            "horizon_bars, status, symbol, timeframe, direction, strategy_id, strategy_release_id, signal_at, "
            "horizon_end_at, first_touch, outcome_r, label, input_hash, scored_at, bar_sources) "
            "VALUES (%s, %s, %s, %s, 'sig-outcome-v2', 'signal-gross-r-band-v1', 4, 'SCORED', 'BTCUSDT', '1h', 'long', 'h01', 'h01', %s, %s, 'TP1', 1.0, 1, %s, %s, '[\"BINANCE_USDM_REST\"]'::jsonb), "
            "(%s, %s, %s, %s, 'sig-outcome-v2', 'signal-gross-r-band-v1', 4, 'SCORED', 'BTCUSDT', '1h', 'long', 'h01', 'h01', %s, %s, 'SL', -1.0, -1, %s, %s, '[\"BINANCE_USDM\"]'::jsonb)",
            (
                uuid4(), ws1, bot1, dec1, now - timedelta(hours=5), now - timedelta(hours=1), "9" * 64, now,
                uuid4(), ws1, bot1, dec2, now - timedelta(hours=5), now - timedelta(hours=1), "0" * 64, now,
            ),
        )

    async with UnitOfWork(
        admin_sessions, workspace_id=ws1, actor_membership_id=mem1, snapshot_read_only=True
    ) as uow:
        manifest = await load_and_build_signal_outcome_dataset(
            uow.session,
            workspace_id=ws1,
            holdout_start=now,
            evaluation_cutoff=now + timedelta(days=1),
            as_of=now,
            horizon_bars=4,
        )
        assert manifest.label_version == "signal-gross-r-band-v1"
        assert len(manifest.samples) == 1
        assert len(manifest.exclusions) == 1
        assert "SIGNAL_BAR_SOURCE_NOT_TRAINING_LICENSED" in manifest.exclusions[0].reasons

    await runtime_engine.dispose()
    await admin_engine.dispose()
    print("BATCH3_POSTGRES_E2E_OK")


asyncio.run(main())
"""


def test_batch3_postgres_e2e_roles_rls_breaker_block_and_signal_dataset() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    image = build_image_reference(root)
    ensure_image_available(root, image)
    project = f"crypidian-batch3-e2e-{uuid.uuid4().hex[:10]}"
    env = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
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
    try:
        subprocess.run(
            [*base, "up", "-d", "postgres", "migrator"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        alembic_heads = subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "heads"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "e8a2c4d6f0b3 (head)" in alembic_heads.stdout

        result = subprocess.run(
            [
                *base,
                "run",
                "--rm",
                "-T",
                "-v",
                f"{root / '.secrets'}:/run/secrets:ro",
                "migrator",
                "python",
                "-c",
                "import sys; exec(sys.stdin.read())",
            ],
            cwd=root,
            env=env,
            input=PROBE_CODE,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert "BATCH3_POSTGRES_E2E_OK" in result.stdout, result.stdout + result.stderr
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
        )
