"""Compose E2E: migration ``7707d82de246`` bù ba bảng live market cho DB đi theo nhánh ``main``.

Giả lập DB đã nâng cấp theo ``main`` trước khi hợp nhất (CRY-88): Alembic ghi đã qua revision gộp
``c1fec3af85ea`` nhưng ba bảng của ``b4d6f8a0c2e5`` chưa từng được tạo. ``upgrade head`` phải tạo
lại đủ bảng, RLS, trigger append-only và quyền như ``b4d6``; thiếu một phần thì phải dừng.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

TABLES = ("market_mark_price", "market_liquidation", "market_trade_flow")


def test_backfill_recreates_live_market_tables_for_main_lineage_database() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-b4d6-backfill-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
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

    def compose(*args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if ok and result.returncode != 0:
            raise AssertionError(f"Compose {args[:6]!r} thất bại:\n{result.stderr[-6000:]}")
        return result

    def sql(statement: str) -> str:
        return compose(
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
            statement,
        ).stdout.strip()

    def alembic(*args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, ok=ok)

    def drop(tables: tuple[str, ...]) -> None:
        sql("; ".join(f'DROP TABLE "{table}"' for table in tables))

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        alembic("upgrade", "head")
        assert "c9e1a3b5d7f9 (head)" in alembic("heads").stdout

        # DB theo main: đã qua revision gộp nhưng chưa từng có ba bảng của b4d6. Hạ các revision
        # sau 7707d82de246 trước (vd cột analyst_run.context của CRY-384) để stamp lùi khớp schema.
        alembic("downgrade", "7707d82de246")
        drop(TABLES)
        alembic("stamp", "c1fec3af85ea")
        assert sql("SELECT count(*) FROM pg_class WHERE relname IN " + str(TABLES)) == "0"
        alembic("upgrade", "head")
        assert "c9e1a3b5d7f9 (head)" in alembic("current").stdout
        assert (
            sql(
                "SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity "
                f"AND relname IN {TABLES}"
            )
            == "3"
        )
        assert (
            sql(
                f"SELECT count(*) FROM pg_trigger WHERE tgname IN "
                f"{tuple(f'{table}_immutable' for table in TABLES)}"
            )
            == "3"
        )
        assert (
            sql(
                "SELECT bool_and(has_table_privilege('crypidian_ingestion_worker', t, 'INSERT')) "
                "AND bool_and(has_table_privilege('crypidian_trading_runtime', t, 'SELECT')) "
                "AND NOT bool_or(has_table_privilege('crypidian_trading_runtime', t, 'INSERT')) "
                f"FROM unnest(ARRAY{list(TABLES)}) AS t"
            )
            == "t"
        )
        check = alembic("check")
        assert "No new upgrade operations detected" in check.stdout + check.stderr

        # Chỉ thiếu một phần: không tự đoán, migration phải dừng và giữ nguyên revision.
        drop(TABLES[:1])
        alembic("stamp", "c1fec3af85ea")
        partial = alembic("upgrade", "head", ok=False)
        assert partial.returncode != 0
        assert "Chỉ có một phần bảng live market" in partial.stdout + partial.stderr
        assert "c1fec3af85ea" in alembic("current").stdout
    finally:
        compose("down", "--volumes", "--remove-orphans", ok=False)
