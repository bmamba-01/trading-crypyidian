"""PostgreSQL E2E cho capability persist, quyền tối thiểu và downgrade bảo toàn dữ liệu."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available

PERSIST_FENCE_PARENT = "f7a9c1e3d5b7"
PERSIST_FENCE_REVISION = "a8c0e2f4b6d9"
BOOTSTRAP = (
    "import json,os,sys,types;p=json.load(sys.stdin);"
    "os.environ.update(WP07_PERSIST_STATE=json.dumps(p['state']),WP07_PERSIST_PHASE=p['phase']);"
    "m=types.ModuleType('wp07_runtime_replay_fixture');sys.modules[m.__name__]=m;"
    "exec(compile(p['runtime_fixture'],'runtime_replay_fixture.py','exec'),m.__dict__);"
    "exec(compile(p['code'],'wp07_backtest_persist_fence.py','exec'),{'__name__':'__main__'})"
)


def test_backtest_persist_capability_roles_transactions_and_downgrade() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    project = f"crypidian-persist-fence-{uuid4().hex[:10]}"
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
            timeout=300,
        )
        assert result.returncode == 0, (args, result.stdout[-1000:], result.stderr[-25000:])
        return result

    probe = (root / "tests/e2e/probes/wp07_backtest_persist_fence.py").read_text()
    runtime_fixture = (root / "tests/fixtures/runtime_replay.py").read_text()
    state: dict[str, Any] = {
        key: str(uuid4())
        for key in (
            "workspace_id",
            "other_workspace_id",
            "job_id",
            "run_id",
        )
    }

    def run_probe(phase: str) -> dict[str, Any]:
        # Service provisioner đã có secrets các process; kết nối trong probe dùng login thật.
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "db-provisioner",
            "python",
            "-c",
            BOOTSTRAP,
            stdin=json.dumps(
                {
                    "state": state,
                    "phase": phase,
                    "code": probe,
                    "runtime_fixture": runtime_fixture,
                }
            ),
        )
        return cast(dict[str, Any], json.loads(result.stdout.strip().splitlines()[-1]))

    ensure_image_available(root, image)
    try:
        compose("config", "--quiet")
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        result = run_probe("main")
        assert result["role_boundaries"] is True
        assert result["transactions"] is True
        assert result["deterministic_races"] is True
        # Chỉ kiểm tra downgrade/re-upgrade của migration fence, không đi qua
        # các migration WP06 khác vốn thêm cột lineage vào cùng bảng outcome.
        compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            PERSIST_FENCE_REVISION,
        )
        state["snapshot"] = run_probe("fence_snapshot")["snapshot"]
        compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            PERSIST_FENCE_PARENT,
        )
        assert run_probe("down") == {"preserved": True}
        compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "upgrade",
            PERSIST_FENCE_REVISION,
        )
        assert run_probe("up") == {"preserved": True}
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
