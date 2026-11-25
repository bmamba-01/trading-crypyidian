"""Compose E2E cho chính sách retention 90 ngày trên PostgreSQL thật (CRY-452)."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_live_market_retention_backfill_trigger_and_rls_prune() -> None:
    root = Path(__file__).parents[2]
    with isolated_compose_resources(root) as resources:
        project = f"crypidian-cry452-retention-{resources.run_id}"
        base = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--project-directory",
            str(root),
            "-f",
            str(resources.compose_file),
        ]
        environment = os.environ | {"CRYPIDIAN_APP_IMAGE": resources.image}

        def compose(*args: str, stdin: str | None = None) -> str:
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            assert result.returncode == 0, (args[:6], result.stdout[-2000:], result.stderr[-4000:])
            return result.stdout

        def psql(sql: str, *, expect_error: bool = False) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [
                    *base,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "crypidian_admin",
                    "-d",
                    "crypidian",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-At",
                    "-c",
                    sql,
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if not expect_error:
                assert result.returncode == 0, (sql, result.stderr)
            return result

        image_created = ensure_image_available(root, resources.image)
        ws1 = str(uuid.uuid4())
        ws2 = str(uuid.uuid4())
        inst1 = str(uuid.uuid4())
        inst2 = str(uuid.uuid4())
        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            # Nâng cấp lên ngay trước CRY-452 để kiểm chứng backfill trên dữ liệu cũ.
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "ab26dde001df")

            psql(
                f"""
                INSERT INTO workspace (id, name, slug, mode, status, created_at) VALUES
                    ('{ws1}', 'ws1', 'ws1-{ws1[:8]}', 'PAPER', 'ACTIVE', now()),
                    ('{ws2}', 'ws2', 'ws2-{ws2[:8]}', 'PAPER', 'ACTIVE', now());
                INSERT INTO market_instrument (
                    id, workspace_id, source, idempotency_key, symbol, base_asset, quote_asset,
                    tick_size, quantity_step, min_notional, status, revision, checksum,
                    event_at, received_at, available_at, created_at
                ) VALUES
                    ('{inst1}', '{ws1}', 'binance_usdm_rest', 'inst-1', 'BTCUSDT', 'BTC', 'USDT',
                     0.1, 0.001, 5, 'TRADING', 1, 'c1', now(), now(), now(), now()),
                    ('{inst2}', '{ws2}', 'binance_usdm_rest', 'inst-2', 'BTCUSDT', 'BTC', 'USDT',
                     0.1, 0.001, 5, 'TRADING', 1, 'c2', now(), now(), now(), now());

                INSERT INTO market_funding (
                    id, workspace_id, instrument_id, source, idempotency_key, revision,
                    checksum, funding_time, event_at, received_at, available_at,
                    funding_rate, created_at
                ) VALUES
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_rest', 'f-old', 1,
                     'h1', now() - interval '95 days', now() - interval '95 days',
                     now() - interval '95 days', now() - interval '95 days', 0.0001, now()),
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_rest', 'f-fresh', 1,
                     'h2', now() - interval '1 day', now() - interval '1 day',
                     now() - interval '1 day', now() - interval '1 day', 0.0001, now()),
                    ('{uuid.uuid4()}', '{ws2}', '{inst2}', 'binance_usdm_rest', 'f-ws2-old', 1,
                     'h3', now() - interval '95 days', now() - interval '95 days',
                     now() - interval '95 days', now() - interval '95 days', 0.0001, now());

                INSERT INTO market_liquidation (
                    id, workspace_id, instrument_id, source, idempotency_key, checksum,
                    event_at, received_at, available_at, side, order_type, time_in_force,
                    status, price, average_price, quantity, filled_quantity, created_at
                ) VALUES
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_ws', 'l-old', 'h4',
                     now() - interval '95 days', now() - interval '95 days',
                     now() - interval '95 days', 'SELL', 'LIMIT', 'IOC', 'FILLED',
                     90000, 90000, 1, 1, now()),
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_ws', 'l-fresh', 'h5',
                     now() - interval '1 day', now() - interval '1 day',
                     now() - interval '1 day', 'BUY', 'LIMIT', 'IOC', 'FILLED',
                     90000, 90000, 1, 1, now());

                INSERT INTO market_trade_flow (
                    id, workspace_id, instrument_id, source, idempotency_key, checksum,
                    event_at, received_at, available_at, bucket_start, bucket_seconds,
                    buy_quantity, sell_quantity, buy_quote, sell_quote, trade_count,
                    first_aggregate_id, last_aggregate_id, is_complete, created_at
                ) VALUES
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_ws', 't-old', 'h6',
                     now() - interval '95 days', now() - interval '95 days',
                     now() - interval '95 days', now() - interval '95 days', 60,
                     1, 2, 90000, 180000, 3, 1, 3, true, now()),
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_ws', 't-fresh', 'h7',
                     now() - interval '1 day', now() - interval '1 day',
                     now() - interval '1 day', now() - interval '1 day', 60,
                     1, 2, 90000, 180000, 3, 4, 6, true, now());

                INSERT INTO market_mark_price (
                    id, workspace_id, instrument_id, source, idempotency_key, checksum,
                    event_at, received_at, available_at, mark_price, index_price, created_at
                ) VALUES
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_ws', 'mp-old', 'h8',
                     now() - interval '95 days', now() - interval '95 days',
                     now() - interval '95 days', 90000, 89990, now()),
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_ws', 'mp-fresh', 'h9',
                     now() - interval '1 day', now() - interval '1 day',
                     now() - interval '1 day', 90000, 89990, now());

                INSERT INTO market_open_interest (
                    id, workspace_id, instrument_id, source, idempotency_key, revision,
                    checksum, observed_at, event_at, received_at, available_at,
                    quantity, created_at
                ) VALUES
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_rest', 'oi-old', 1,
                     'h10', now() - interval '95 days', now() - interval '95 days',
                     now() - interval '95 days', now() - interval '95 days', 1000, now()),
                    ('{uuid.uuid4()}', '{ws1}', '{inst1}', 'binance_usdm_rest', 'oi-fresh', 1,
                     'h11', now() - interval '1 day', now() - interval '1 day',
                     now() - interval '1 day', now() - interval '1 day', 1000, now());
                """
            )

            # Chạy CRY-452 + CRY-453 lên head (d0e1f2a3b4c5).
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert "d0e1f2a3b4c5 (head)" in heads

            # Kiểm tra backfill: không còn NULL expires_at.
            nulls = psql(
                "SELECT (SELECT count(*) FROM market_funding WHERE expires_at IS NULL) + "
                "(SELECT count(*) FROM market_liquidation WHERE expires_at IS NULL) + "
                "(SELECT count(*) FROM market_trade_flow WHERE expires_at IS NULL) + "
                "(SELECT count(*) FROM market_mark_price WHERE expires_at IS NULL) + "
                "(SELECT count(*) FROM market_open_interest WHERE expires_at IS NULL)"
            ).stdout.strip()
            assert nulls == "0"

            # Trigger vẫn chặn UPDATE và chặn DELETE bản ghi còn hạn.
            upd = psql("UPDATE market_funding SET funding_rate = 0.0002", expect_error=True)
            assert upd.returncode != 0
            del_fresh = psql(
                "DELETE FROM market_liquidation WHERE idempotency_key = 'l-fresh'",
                expect_error=True,
            )
            assert del_fresh.returncode != 0
            del_fresh_mp = psql(
                "DELETE FROM market_mark_price WHERE idempotency_key = 'mp-fresh'",
                expect_error=True,
            )
            assert del_fresh_mp.returncode != 0

            # Role crypidian_ingestion_worker dưới RLS chỉ xóa được bản ghi hết hạn của ws1.
            psql(
                f"""
                BEGIN;
                SET LOCAL ROLE crypidian_ingestion_worker;
                SELECT set_config('app.workspace_id', '{ws1}', true);
                DELETE FROM market_funding WHERE expires_at <= now();
                DELETE FROM market_liquidation WHERE expires_at <= now();
                DELETE FROM market_trade_flow WHERE expires_at <= now();
                DELETE FROM market_mark_price WHERE expires_at <= now();
                DELETE FROM market_open_interest WHERE expires_at <= now();
                COMMIT;
                """
            )

            counts = psql(
                f"SELECT "
                f"(SELECT count(*) FROM market_funding WHERE workspace_id = '{ws1}'), "
                f"(SELECT count(*) FROM market_funding WHERE workspace_id = '{ws2}'), "
                f"(SELECT count(*) FROM market_liquidation WHERE workspace_id = '{ws1}'), "
                f"(SELECT count(*) FROM market_trade_flow WHERE workspace_id = '{ws1}'), "
                f"(SELECT count(*) FROM market_mark_price WHERE workspace_id = '{ws1}'), "
                f"(SELECT count(*) FROM market_open_interest WHERE workspace_id = '{ws1}')"
            ).stdout.strip()
            assert counts == "1|1|1|1|1|1", counts
        finally:
            compose("down", "--volumes", "--remove-orphans")
            if image_created:
                subprocess.run(["docker", "image", "rm", "-f", resources.image], check=False)
