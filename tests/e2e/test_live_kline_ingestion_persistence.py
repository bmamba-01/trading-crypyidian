"""Compose E2E cho ingest nến live: stream + REST giả lập → PostgreSQL bằng role ingestion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ['LIVE_INGEST_STATE']=json.dumps(p['state']);"
    "exec(compile(p['code'],'live_kline_ingestion.py','exec'),{'__name__':'__main__'})"
)


def run_live_ingestion_compose_probe() -> dict[str, Any]:
    """Chạy probe trong PostgreSQL Compose tạm thời; không gọi mạng Binance thật."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-live-ingest-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = ["docker", "compose", "--project-name", project, "-f", "compose.yaml"]

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
        error_tail = "\n".join(result.stderr.splitlines()[-30:])
        assert result.returncode == 0, (args, result.stdout[-5000:], error_tail)
        return result

    payload = json.dumps(
        {
            "state": {
                "workspace_id": str(uuid.uuid4()),
                "other_workspace_id": str(uuid.uuid4()),
                "now": datetime.now(UTC).isoformat(),
                "exchange_info": (
                    root / "tests/fixtures/binance_usdm/exchange_info.json"
                ).read_text(encoding="utf-8"),
            },
            "code": (root / "tests/e2e/probes/live_kline_ingestion.py").read_text(encoding="utf-8"),
        }
    )
    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrator",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        lines = result.stdout.strip().splitlines()
        assert lines, result.stderr[-3000:]
        return cast(dict[str, Any], json.loads(lines[-1]))
    finally:
        cleanup = subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert cleanup.returncode == 0, cleanup.stderr[-3000:]
        if image_created:
            image_removal = subprocess.run(
                ["docker", "image", "rm", image],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=120,
                check=False,
            )
            assert image_removal.returncode == 0, image_removal.stderr[-3000:]


def test_live_klines_reach_market_store_through_rights_gate_and_ingestion_role() -> None:
    result = run_live_ingestion_compose_probe()

    assert result == {
        "denied_without_policy": True,
        "warmup_bars": 200,
        "bars_after_stream_close": 201,
        "bars_after_gap_fill": 205,
        "bars_after_replay": 205,
        "session_outcomes": ["DISCONNECTED", "DISCONNECTED", "DISCONNECTED", "DISCONNECTED"],
        "single_cursor_and_instrument": True,
        "contiguous_closed_live_bars": True,
        "cursor_points_to_next_bar": True,
        "scanner_reads_live_bars": True,
        "other_workspace_sees_nothing": True,
        "stream_uses_market_route": True,
        "public_requests_only": True,
        "exchange_info_requests": 1,
    }
