"""Compose E2E fault injection cho ma trận retry/dead-letter Telegram Bot API (CRY-348)."""
# ruff: noqa: E501

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"


def test_cry348_telegram_retry_matrix_fault_injection_e2e_postgres() -> None:
    """Chạy fault injection trên PostgreSQL Compose thật với TelegramBotApiSender + Worker:
    - 403 Forbidden -> DEAD ngay sau attempt 1
    - 400 Bad Request -> DEAD ngay sau attempt 1
    - 429 retry_after=3 -> PENDING (available_at >= now+3s) -> SENT ở lần sau
    - 502 Bad Gateway x4 -> PENDING rồi DEAD ở attempt 4
    - httpx2.ReadTimeout x4 -> PENDING (timeout mơ hồ) rồi DEAD ở attempt 4
    """
    project_name = f"crypidian-cry348-e2e-{uuid.uuid4().hex[:10]}"
    env = os.environ.copy()
    env.setdefault("APP_IMAGE", build_image_reference(REPO_ROOT))
    env["COMPOSE_PROJECT_NAME"] = project_name

    ensure_image_available(REPO_ROOT, env["APP_IMAGE"])
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/prepare_local_secrets.py")],
        cwd=REPO_ROOT,
        check=True,
    )

    base_cmd = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project_name,
        "-f",
        str(COMPOSE_FILE),
    ]

    probe_code = """
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4, uuid5

import httpx2
import psycopg
from psycopg.types.json import Jsonb

from trading_bot.adapters.database.engine import create_database_engine, create_session_factory
from trading_bot.adapters.telegram.bot_api_sender import TelegramBotApiSender
from trading_bot.application.bot_dispatcher import (
    BOT_DECISION_NAMESPACE,
    BotDecision,
    BotDecisionStatus,
    _canonical_json,
    _decision_payload,
    _hash,
)
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker


async def main() -> None:
    pw = Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    ws_id = uuid4()
    user_id = uuid4()
    mem_id = uuid4()
    acc_id = uuid4()
    set_id = uuid4()
    snap_id = uuid4()
    ev_id = uuid4()
    cfg_id = uuid4()
    bind_id = uuid4()
    sub_id = uuid4()

    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    clock_box = {"now": now}

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=pw, autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'CRY348 E2E', %s, 'PAPER', 'ACTIVE')",
            (ws_id, f"cry348-{ws_id.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'hash', 'ACTIVE')",
            (user_id, f"cry348-{user_id.hex[:8]}@test.io"),
        )
        conn.execute(
            "INSERT INTO user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (mem_id, ws_id, user_id),
        )
        conn.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, "
            "status, max_notional) VALUES (%s, %s, 'acc-348', 'binance_usdm', 'PAPER', 'local', 'ENABLED', 1000)",
            (acc_id, ws_id),
        )
        bot_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
            (ws_id, acc_id),
        ).fetchone()[0]
        set_hash = "a" * 64
        conn.execute(
            "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
            "settings_json, settings_hash, status, created_by) "
            "VALUES (%s, %s, 1, 1, '{}'::jsonb, %s, 'PUBLISHED', %s)",
            (set_id, ws_id, set_hash, mem_id),
        )
        conn.execute(
            "INSERT INTO feature_snapshot (id, workspace_id, snapshot_id, content_hash, symbol, "
            "timeframe, as_of, computed_at, schema_hash, formula_hash, code_hash, input_bar_ids, "
            "input_event_ids, features, warmup, quality_flags, data_freshness, data_coverage, "
            "regime, is_usable, canonical_json) VALUES "
            "(%s, %s, %s, %s, 'BTCUSDT', '1h', %s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, "
            "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'FRESH', 1.0, '{}'::jsonb, true, '{}')",
            (snap_id, ws_id, "f" * 64, "f" * 64, now, now, "e" * 64, "d" * 64, "c" * 64),
        )
        conn.execute(
            "INSERT INTO shared_signal_evidence (id, workspace_id, symbol, setup_type, observed_at, "
            "available_at, validity_end, feature_snapshot_id, source_refs, content_hash, "
            "evidence_hash, schema_version, canonical_json) VALUES "
            "(%s, %s, 'BTCUSDT', 'H01_BREAKOUT', %s, %s, %s, %s, %s, %s, %s, 'v1', '{}')",
            (ev_id, ws_id, now, now, now + timedelta(hours=1), "f" * 64, Jsonb(["bar-1"]), "b" * 64, "b" * 64),
        )
        conn.execute(
            "INSERT INTO bot_config_version (id, workspace_id, bot_id, account_id, version, revision, "
            "schema_version, base_settings_version_id, base_settings_hash, overlay_json, overlay_hash, "
            "status, created_by, updated_by) VALUES "
            "(%s, %s, %s, %s, 1, 1, 1, %s, %s, '{}'::jsonb, %s, 'DRAFT', %s, %s)",
            (cfg_id, ws_id, bot_id, acc_id, set_id, set_hash, "1" * 64, mem_id, mem_id),
        )
        conn.execute(
            "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
            "published_at=%s, effective_at=%s WHERE id=%s",
            (mem_id, now, now, cfg_id),
        )
        conn.execute(
            "INSERT INTO telegram_binding (id, workspace_id, membership_id, telegram_user_id, "
            "verified_at, revoked_at) VALUES (%s, %s, %s, '987654321', %s, NULL)",
            (bind_id, ws_id, mem_id, now),
        )
        conn.execute(
            "INSERT INTO telegram_signal_subscription (id, workspace_id, membership_id, bot_id, "
            "telegram_binding_id, telegram_chat_id, enabled, symbols, timeframes, directions, "
            "strategy_ids, setup_codes) VALUES "
            "(%s, %s, %s, %s, %s, 987654321, true, %s, %s, %s, %s, %s)",
            (
                sub_id,
                ws_id,
                mem_id,
                bot_id,
                bind_id,
                Jsonb(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]),
                Jsonb(["1h"]),
                Jsonb(["LONG"]),
                Jsonb(["H01"]),
                Jsonb(["H01_ENTRY"]),
            ),
        )

        # Tạo 5 delivery tương ứng 5 kịch bản fault injection (mỗi case 1 symbol để không bị dedup throttle)
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
        delivery_ids = []
        for idx, sym in enumerate(symbols):
            ev_i = uuid4()
            ev_hash = f"evhash-{idx:057d}"
            conn.execute(
                "INSERT INTO shared_signal_evidence (id, workspace_id, symbol, setup_type, observed_at, "
                "available_at, validity_end, feature_snapshot_id, source_refs, content_hash, "
                "evidence_hash, schema_version, canonical_json) VALUES "
                "(%s, %s, %s, 'H01_BREAKOUT', %s, %s, %s, %s, %s, %s, %s, 'v1', '{}')",
                (ev_i, ws_id, sym, now, now, now + timedelta(hours=1), "f" * 64, Jsonb([f"bar-{idx}"]), ev_hash, ev_hash),
            )
            job_id = uuid4()
            idem_key = f"idem-{idx:059d}"
            dec_id = uuid5(BOT_DECISION_NAMESPACE, "decision:" + idem_key)
            cand_id = f"cand-{idx:059d}"
            del_id = uuid4()
            delivery_ids.append(del_id)
            conn.execute(
                "INSERT INTO bot_decision_job (id, workspace_id, bot_id, account_id, evidence_id, "
                "evidence_hash, bot_config_version_id, evaluated_at, context_hash, context_payload, "
                "status, completed_at, attempts) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, 'COMPLETED', %s, 1)",
                (job_id, ws_id, bot_id, acc_id, ev_i, ev_hash, cfg_id, now, f"{idx:064d}", now),
            )
            cand_obj = {
                "candidate_id": cand_id,
                "symbol": sym,
                "direction": "LONG",
                "timeframe": "1h",
                "strategy_id": "H01",
                "strategy_release_id": "h01-v1",
                "reference_price": "50000",
                "signal_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=2)).isoformat(),
                "reason_code": "H01_ENTRY",
                "detail_vi": f"Fault injection case {idx}",
            }
            payload = _decision_payload(
                {
                    "decision_id": dec_id,
                    "job_id": job_id,
                    "workspace_id": ws_id,
                    "bot_id": bot_id,
                    "account_id": acc_id,
                    "evidence_id": ev_i,
                    "evidence_hash": ev_hash,
                    "feature_snapshot_id": "f" * 64,
                    "bot_config_version_id": cfg_id,
                    "bot_config_hash": "1" * 64,
                    "root_settings_version_id": set_id,
                    "root_settings_hash": set_hash,
                    "strategy_release_id": "h01-v1",
                    "strategy_release_hash": "2" * 64,
                    "strategy_parameters": (),
                    "model_release_id": None,
                    "evaluated_at": now,
                    "status": BotDecisionStatus.CANDIDATE,
                    "reason_code": "H01_ENTRY",
                    "detail_vi": f"Fault injection case {idx}",
                    "decision_snapshot_id": "3" * 64,
                    "candidate_json": _canonical_json(cand_obj),
                    "gate_details": (),
                    "idempotency_key": idem_key,
                    "checklist_json": None,
                }
            )
            dec_hash = _hash(payload)
            # Kiểm tra trước bằng BotDecision.from_payload để chắc chắn hợp lệ 100%
            BotDecision.from_payload(payload, dec_hash)
            conn.execute(
                "INSERT INTO bot_decision (id, workspace_id, bot_id, account_id, job_id, evidence_id, "
                "evidence_hash, bot_config_version_id, bot_config_hash, root_settings_version_id, "
                "root_settings_hash, strategy_release_id, evaluated_at, status, candidate_id, "
                "reason_code, idempotency_key, decision_hash, payload) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'h01-v1', %s, 'CANDIDATE', %s, "
                "'H01_ENTRY', %s, %s, %s)",
                (
                    dec_id, ws_id, bot_id, acc_id, job_id, ev_i, ev_hash, cfg_id,
                    "1" * 64, set_id, set_hash, now, cand_id, idem_key, dec_hash, Jsonb(payload),
                ),
            )
            conn.execute(
                "INSERT INTO telegram_signal_delivery (id, workspace_id, bot_id, subscription_id, "
                "decision_id, status, attempts, available_at) VALUES "
                "(%s, %s, %s, %s, %s, 'PENDING', 0, %s)",
                (del_id, ws_id, bot_id, sub_id, dec_id, now + timedelta(milliseconds=idx)),
            )

    # Fault injection handler đọc chỉ số case từ text của request
    case3_calls = {"count": 0}

    def transport_handler(request: httpx2.Request) -> httpx2.Response:
        body = request.content.decode("utf-8", errors="ignore")
        if "Fault injection case 0" in body:
            return httpx2.Response(403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"})
        if "Fault injection case 1" in body:
            return httpx2.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
        if "Fault injection case 2" in body:
            case3_calls["count"] += 1
            if case3_calls["count"] == 1:
                return httpx2.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}})
            return httpx2.Response(200, json={"ok": True, "result": {"message_id": 9001}})
        if "Fault injection case 3" in body:
            return httpx2.Response(502, json={"ok": False, "error_code": 502, "description": "Bad Gateway"})
        if "Fault injection case 4" in body:
            raise httpx2.ReadTimeout("simulated ambiguous read timeout", request=request)
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 9999}})

    sender = TelegramBotApiSender(
        "123456:TESTTOKEN_CRY348_SAMPLE",
        transport=httpx2.MockTransport(transport_handler),
    )

    engine = create_database_engine(
        f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    )
    sessions = create_session_factory(engine)

    class _NoopPacer:
        async def wait(self, _chat_id: int) -> None:
            return None

        def mark_sent(self, _chat_id: int) -> None:
            return None

    worker = TelegramSignalDeliveryWorker(
        session_factory=sessions,
        workspace_id=ws_id,
        sender=sender,
        worker_id="cry348-worker",
        max_candidate_age=timedelta(hours=1),
        max_attempts=5,
        batch_size=20,
        now=lambda: clock_box["now"],
        pacer=_NoopPacer(),
    )

    # Vòng 1: chạy cả 5 delivery
    await worker.tick()
    # Vòng 2, 3, 4: tiến đồng hồ qua backoff/retry_after để kết thúc 429->SENT và 502/Timeout->DEAD
    for step_sec in (5, 20, 40, 60):
        clock_box["now"] = clock_box["now"] + timedelta(seconds=step_sec)
        await worker.tick()

    await sender.aclose()
    await engine.dispose()

    with psycopg.connect(
        host="postgres", dbname="crypidian", user="crypidian_admin", password=pw, autocommit=True
    ) as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws_id),))
        rows = {}
        for idx, del_id in enumerate(delivery_ids):
            st, att, err = conn.execute(
                "SELECT status, attempts, last_error_code FROM telegram_signal_delivery WHERE id=%s",
                (del_id,),
            ).fetchone()
            rows[idx] = {"status": st, "attempts": att, "error": err}

    assert rows[0] == {"status": "DEAD", "attempts": 1, "error": "TELEGRAM_HTTP_403"}, rows
    assert rows[1] == {"status": "DEAD", "attempts": 1, "error": "TELEGRAM_HTTP_400"}, rows
    assert rows[2]["status"] == "SENT" and rows[2]["attempts"] == 2, rows
    assert rows[3] == {"status": "DEAD", "attempts": 4, "error": "TELEGRAM_HTTP_502"}, rows
    assert rows[4]["status"] == "DEAD" and rows[4]["attempts"] == 4, rows
    print(json.dumps({"ok": True, "rows": rows}))


asyncio.run(main())
"""

    try:
        subprocess.run(
            [*base_cmd, "up", "--detach", "--wait", "--wait-timeout", "180", "postgres"],
            env=env,
            cwd=REPO_ROOT,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "db-provisioner"],
            env=env,
            cwd=REPO_ROOT,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "-T", "migrator", "alembic", "upgrade", "head"],
            env=env,
            cwd=REPO_ROOT,
            check=True,
        )
        result = subprocess.run(
            [
                *base_cmd,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{REPO_ROOT / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{REPO_ROOT / 'src/trading_bot'}:/app/src/trading_bot:ro",
                "migrator",
                "python",
                "-c",
                probe_code,
            ],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert '"ok": true' in result.stdout, result.stdout
    finally:
        subprocess.run(
            [*base_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            cwd=REPO_ROOT,
            check=False,
        )
