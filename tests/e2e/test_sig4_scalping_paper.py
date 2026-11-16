"""SIG-4 Compose E2E: H18 → thang chốt lời → ledger PAPER chốt từng phần + replay lưu outcome.

Thời gian theo dữ liệu tổng hợp (02/11/2026, phiên London) và đồng hồ tiêm vào repository, nên kết
quả không phụ thuộc giờ chạy. Đi qua PostgreSQL thật (RLS, trigger ledger, role tối thiểu); không có
Binance — socket Python bị chặn trong pha lifecycle/replay.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys,types;p=json.load(sys.stdin);"
    "os.environ.update(SIG4_PHASE=p['phase'],SIG4_STATE=json.dumps(p['state']));"
    "m=types.ModuleType('sig4_runtime_replay');sys.modules[m.__name__]=m;"
    "exec(compile(p['fixture'],'sig4_runtime_replay.py','exec'),m.__dict__);"
    "exec(compile(p['code'],'sig4_scalping_paper.py','exec'),{'__name__':'__main__'})"
)
SERVICES = {"seed": "migrator", "lifecycle": "trading-runtime", "replay": "db-provisioner"}


def test_h18_laddered_paper_trade_persists_partial_exits_and_replay_outcome() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-sig4-e2e-{uuid.uuid4().hex[:10]}"
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
        # Dòng lỗi gốc (psycopg/SQLAlchemy/Conflict) thường nằm giữa traceback dài: lọc riêng.
        errors = [
            line
            for line in result.stderr.splitlines()
            if any(marker in line for marker in ("Error", "Conflict", "DETAIL", "assert"))
        ]
        assert result.returncode == 0, (args[:6], errors[-8:], result.stderr[-3000:])
        return result

    probe = (root / "tests/e2e/probes/sig4_scalping_paper.py").read_text(encoding="utf-8")
    fixture = (root / "tests/fixtures/sig4_runtime_replay.py").read_text(encoding="utf-8")
    state: dict[str, Any] = {
        "workspace_id": str(uuid.uuid4()),
        "now": datetime.now(UTC).isoformat(),
    }

    def run(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe, "fixture": fixture})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            SERVICES[phase],
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        lines = result.stdout.strip().splitlines()
        assert lines, (phase, result.stderr[-3000:])
        return cast(dict[str, Any], json.loads(lines[-1]))

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        state.update(run("seed"))

        lifecycle = run("lifecycle")
        assert lifecycle["network_attempts"] == []
        assert len(lifecycle["plan_targets"]) == 2
        assert lifecycle["partial_first"] is True
        assert lifecycle["second_exit_reason"] == "TAKE_PROFIT"
        assert lifecycle["fill_roles"] == ["ENTRY", "EXIT", "EXIT"]
        quantity = Decimal(lifecycle["plan_quantity"])
        assert sum(Decimal(value) for value in lifecycle["fill_quantities"][1:]) == quantity
        assert lifecycle["exit_reasons"] == ["TAKE_PROFIT_1", "TAKE_PROFIT"]
        assert lifecycle["exit_fill_count"] == 2
        event_types = [event for event, _ in lifecycle["events"]]
        assert "REDUCED" in event_types
        assert ["STOP_UPDATED", "BREAKEVEN_AFTER_TP1"] in lifecycle["events"]
        assert event_types[-1] == "CLOSED"
        assert lifecycle["trade_status"] == "CLOSED"
        assert Decimal(lifecycle["trade_net_pnl"]) == Decimal(lifecycle["gateway_net_pnl"])
        assert Decimal(lifecycle["trade_net_pnl"]) > 0
        assert lifecycle["targets_hit"] == 2
        assert lifecycle["restored_matches"] is True

        assert run("replay") == {
            "network_attempts": [],
            "run_count": 1,
            "report_hash_matches": True,
            "stored_trade_count": [1],
            "stored_net_pnl_matches": True,
            "trade_outcomes": 1,
            "net_pnl_positive": True,
        }
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
