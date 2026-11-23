"""Compose E2E test tự động cho bộ seed 10 bot và lệnh check release/hash (OPS-1 / CRY-37)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts import seed_signal_test_bots as seed_script
from scripts.docker_image import build_image_reference, ensure_image_available

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"


def test_ops1_seed_apply_genesis_and_check_e2e_postgres() -> None:
    """Chứng minh trên PostgreSQL Compose thật:
    1. `_container_apply` + `_container_genesis` tạo workspace, 10 bot và 10 checkpoint genesis.
    2. `_container_check` báo `clean=True` khi mọi release PUBLISHED khớp StrategyRegistry.
    3. Khi DB có release lệch hash và release không còn trong catalog -> `_container_check`
       báo `clean=False` và liệt kê chính xác `hash_mismatches` + `unknown_releases`.
    4. Chạy lại `_container_apply` + `_container_genesis` tự supersede bản lệch hash và giữ
       idempotent (không tạo trùng checkpoint genesis).
    """
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    image = build_image_reference(REPO_ROOT)
    ensure_image_available(REPO_ROOT, image)
    project = f"crypidian-ops1-seed-e2e-{uuid.uuid4().hex[:10]}"
    env = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base_cmd = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project,
        "-f",
        str(COMPOSE_FILE),
    ]

    symbols = [f"TEST{i:02d}USDT" for i in range(40)]
    plan = seed_script.build_plan(symbols, seed=20261123)
    workspace_id = "22222222-2222-4222-8222-222222222222"
    state = {"workspace_id": workspace_id, "plan": plan}

    probe_code = """
import asyncio
import json
import os
from decimal import Decimal
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from scripts import seed_signal_test_bots as script
from trading_bot.adapters.database.repositories.workspace_settings import (
    canonical_settings,
    settings_hash,
)
from trading_bot.config.workspace_settings import (
    FeatureSettings,
    MarketDataSettings,
    PaperExecutionSettings,
    RiskSettings,
    RuntimeSettings,
    StrategySettings,
    WorkspaceSettings,
)

state = json.loads(os.environ["OPS1_STATE"])
workspace_id = state["workspace_id"]
ws_uuid = UUID(workspace_id)
user_id = uuid4()
mem_id = uuid4()
set_v_id = uuid4()
root_settings = canonical_settings(
    WorkspaceSettings(
        schema_version=1,
        runtime=RuntimeSettings(mode="PAPER", timezone="UTC"),
        market_data=MarketDataSettings(
            venue="BINANCE_USDM",
            symbols=("BTCUSDT",),
            timeframes=("1h", "4h"),
            max_age_seconds=300,
        ),
        features=FeatureSettings(formula_version="crypidian-formulas-v1", sessions_timezone="UTC"),
        strategy=StrategySettings(
            default_release="h01-v1",
            entry_timeframe="1h",
            confirmation_timeframe="4h",
        ),
        risk=RiskSettings(
            profile_version_id=uuid4(),
            max_drawdown_fraction=Decimal("0.2"),
            loss_window_timezone="UTC",
        ),
        paper_execution=PaperExecutionSettings(
            quote_asset="USDT",
            starting_equity=Decimal("10000"),
            fee_schedule_version="paper-fees-v1",
            fill_model="next_bar_with_adverse_costs",
        ),
    )
)

# 0. Seed workspace PAPER + user/membership + root settings PUBLISHED tối thiểu
with script._connect() as conn, conn.transaction():
    conn.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace_id,))
    conn.execute(
        "INSERT INTO workspace (id, name, slug, mode, status) "
        "VALUES (%s, 'OPS1 Seed Workspace', %s, 'PAPER', 'ACTIVE')",
        (ws_uuid, f"ops1-{ws_uuid.hex[:8]}"),
    )
    conn.execute(
        "INSERT INTO user_account (id, email_normalized, password_hash, status) "
        "VALUES (%s, %s, 'hash', 'ACTIVE')",
        (user_id, f"ops1-{user_id.hex[:8]}@test.io"),
    )
    conn.execute(
        "INSERT INTO user_membership (id, workspace_id, user_id, status) "
        "VALUES (%s, %s, %s, 'ACTIVE')",
        (mem_id, ws_uuid, user_id),
    )
    conn.execute(
        "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
        "VALUES (%s, %s, %s, 'OWNER')",
        (uuid4(), ws_uuid, mem_id),
    )
    conn.execute(
        "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
        "settings_json, settings_hash, status, created_by) "
        "VALUES (%s, %s, 1, 1, %s, %s, 'PUBLISHED', %s)",
        (set_v_id, ws_uuid, Jsonb(root_settings), settings_hash(root_settings), mem_id),
    )

# 1. Apply + Genesis lần 1
applied_1 = script._container_apply(state)
assert len(applied_1["bots"]) == 10, applied_1

genesis_1 = asyncio.run(script._container_genesis({**state, **applied_1}))
assert genesis_1["genesis_created"] == 10, genesis_1

# 2. Check sạch 100%
check_1 = script._container_check({"workspace_id": workspace_id})
assert check_1["clean"] is True, check_1
assert check_1["unknown_releases"] == []
assert check_1["hash_mismatches"] == []

# 3. Cố tình chèn 1 release lệch hash (h01-v1) và 1 release không còn trong catalog
with script._connect() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace_id,))
        cur.execute(
            "UPDATE strategy_release SET content_hash = %s "
            "WHERE workspace_id = %s AND name = 'h01-v1' AND status = 'PUBLISHED'",
            ("0" * 64, UUID(workspace_id)),
        )
        cur.execute(
            "INSERT INTO strategy_release "
            "(id, workspace_id, name, version, status, recipe, content_hash) "
            "VALUES (gen_random_uuid(), %s, 'removed-legacy-v0', 1, 'PUBLISHED', '{}'::jsonb, %s)",
            (UUID(workspace_id), "a" * 64),
        )
    conn.commit()

# 4. Check phát hiện cả 2 loại sai lệch
check_2 = script._container_check({"workspace_id": workspace_id})
assert check_2["clean"] is False, check_2
assert [x["release_id"] for x in check_2["unknown_releases"]] == ["removed-legacy-v0"]
assert [x["release_id"] for x in check_2["hash_mismatches"]] == ["h01-v1"]

# 5. Chạy lại apply + genesis: bản lệch hash h01-v1 tự được supersede, không tạo trùng genesis
applied_2 = script._container_apply(state)
genesis_2 = asyncio.run(script._container_genesis({**state, **applied_2}))
assert genesis_2["genesis_created"] == 0, "Không được tạo trùng checkpoint genesis!"

check_3 = script._container_check({"workspace_id": workspace_id})
assert check_3["hash_mismatches"] == [], f"h01-v1 phải được supersede sạch: {check_3}"
assert [x["release_id"] for x in check_3["unknown_releases"]] == ["removed-legacy-v0"]
print(json.dumps({"ok": True, "bots": len(applied_1["bots"])}))
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
                "-e",
                f"OPS1_STATE={json.dumps(state)}",
                "-v",
                f"{REPO_ROOT / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{REPO_ROOT / 'src/trading_bot'}:/app/src/trading_bot:ro",
                "-v",
                f"{REPO_ROOT / 'scripts'}:/app/scripts:ro",
                "migrator",
                "python",
                "-c",
                probe_code,
            ],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert '"ok": true' in result.stdout
    finally:
        subprocess.run(
            [*base_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            cwd=REPO_ROOT,
            check=False,
        )
