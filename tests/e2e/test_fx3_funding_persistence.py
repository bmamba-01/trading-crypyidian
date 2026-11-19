"""Compose E2E FX-3 lát 1: funding đã chốt kèm mark price trên PostgreSQL thật (CRY-198)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(FX3_PHASE=p['phase'],FX3_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'fx3_funding_persistence.py','exec'),{'__name__':'__main__'})"
)
SERVICES = {
    "seed": "migrator",
    "write": "ingestion-worker",
    "read": "trading-runtime",
    "bundle_store": "admin-web",
    "bundle_load": "backtest-worker",
}


def test_settled_funding_with_mark_price_is_idempotent_constrained_and_rls_scoped() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-fx3-funding-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--profile",
        "backtest",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]

    def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        assert result.returncode == 0, (args[:6], result.stderr[-5000:])
        return result

    probe = (root / "tests/e2e/probes/fx3_funding_persistence.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
        "instrument_id": str(uuid.uuid4()),
    }

    def run(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            SERVICES[phase],
            "python",
            "-c",
            BOOTSTRAP,
            stdin=payload,
        )
        output = cast(dict[str, Any], json.loads(result.stdout.strip().splitlines()[-1]))
        state.update(output.pop("_state", {}))
        return output

    def psql(statement: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            base
            + ["exec", "-T", "postgres", "psql", "-U", "crypidian_admin", "-d", "crypidian"]
            + ["-v", "ON_ERROR_STOP=1", "-At", "-c", statement],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        assert (
            "a6c8e0b2d4f7 (head)"
            in compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads").stdout
        )
        check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        assert "No new upgrade operations detected" in check.stdout + check.stderr

        assert run("seed") == {"seeded": True}
        assert run("write") == {
            "role": "crypidian_ingestion_worker",
            "first": "ACCEPT",
            "again": "DUPLICATE",
            "changed_mark": "conflict",
        }
        assert run("read") == {
            "role": "crypidian_trading_runtime",
            "own": [["0.000100000000000000", "90500.120000000000000000", "Regular"]],
            "other_workspace_rows": 0,
        }
        # Bản ghi nguồn append-only (trigger chung của kho market) và mark price phải dương.
        immutable = psql("UPDATE market_funding SET mark_price = 1")
        assert immutable.returncode != 0
        assert "append-only" in immutable.stderr
        bad_mark = psql(
            "INSERT INTO market_funding (id,workspace_id,instrument_id,source,idempotency_key,"
            "revision,checksum,funding_time,event_at,received_at,available_at,funding_rate,"
            "mark_price,rate_type) SELECT gen_random_uuid(),workspace_id,instrument_id,source,"
            "'fx3-bad-mark',1,checksum,funding_time + interval '8 hours',"
            "event_at + interval '8 hours',received_at + interval '8 hours',"
            "available_at + interval '8 hours',funding_rate,0,'Regular' FROM market_funding"
        )
        assert bad_mark.returncode != 0
        assert "market_funding_mark_price_positive" in bad_mark.stderr
        # Runtime đọc được cột mới, ingestion-worker ghi được. Lưu ý: mọi role service kế thừa
        # ``crypidian_app`` (CRUD bảng tenant, từ migration nền tảng 48a80bae456d) nên runtime
        # cũng INSERT được về mặt quyền — lỗ hổng least-privilege có sẵn, ghi ở memory/tracker,
        # không thuộc lát FX-3; trigger append-only vẫn chặn UPDATE/DELETE.
        privileges = psql(
            "SELECT has_column_privilege('crypidian_trading_runtime','market_funding',"
            "'mark_price','SELECT'),"
            "has_table_privilege('crypidian_ingestion_worker','market_funding','INSERT')"
        )
        assert privileges.stdout.strip() == "t|t"

        # FX-3 lát 3b: bundle replay schema 2 mang funding đã chốt (point-in-time theo as_of),
        # lưu idempotent bằng role admin-web; backtest-worker đọc lại funding từ DB và khớp hash.
        stored = run("bundle_store")
        assert stored == {
            "role": "crypidian_admin_web",
            "settlements": [["0.000100000000000000", "90500.120000000000000000"]],
            "before_available": 0,
            "stored": [False, True],
            "schema_version": 2,
            "bundle_hash": stored["bundle_hash"],
        }, stored
        loaded = run("bundle_load")
        assert loaded == {
            "role": "crypidian_backtest_worker",
            "hash_matches": True,
            "funding_event_ids": ["funding:BTCUSDT:2026-11-16T08:00:00+00:00"],
        }, loaded
    finally:
        subprocess.run(
            base + ["down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
        )
