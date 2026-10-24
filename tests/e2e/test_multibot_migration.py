"""Migration lịch sử và RLS bot trên PostgreSQL cô lập."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

# Bootstrap ngắn đọc gói JSON từ stdin rồi chạy probe với phase/snapshot trong env.
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB01_PHASE=p['phase'],MB01_SNAPSHOT=p['snapshot']);"
    "exec(compile(p['code'],'multibot_migration.py','exec'),{'__name__':'__main__'})"
)


def test_multibot_backfill_constraints_rls_and_downgrade_guard() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-mb01-e2e-{uuid.uuid4().hex[:10]}"
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

    def compose(*args, success=True, stdin=None):
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )
        assert (result.returncode == 0) is success, (
            args,
            result.stdout[-5000:],
            result.stderr[-5000:],
        )
        return result

    probe = (root / "tests/e2e/probes/multibot_migration.py").read_text(encoding="utf-8")

    def run_probe(phase, snapshot="", service="migrator"):
        # Mã probe và snapshot đi qua stdin: Windows giới hạn dòng lệnh 32.767 ký tự.
        payload = json.dumps({"phase": phase, "snapshot": snapshot, "code": probe})
        return compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            service,
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "b2e7a9d4c631")
        snapshot = json.loads(run_probe("seed").stdout.strip().splitlines()[-1])
        run_probe("unsupported_account")
        unsupported = compose(
            "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head", success=False
        )
        assert "MB01_UNSUPPORTED_ACCOUNT_MODE_OR_STATUS" in unsupported.stderr
        run_probe("restore_account")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        run_probe("verify", json.dumps(snapshot))
        run_probe("actual_runtime", service="trading-runtime")
        run_probe("actual_admin", service="admin-web")
        guard = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "b2e7a9d4c631",
            success=False,
        )
        assert "MB01_DOWNGRADE_REQUIRES_EXPORT_OF_BOT_STATE" in guard.stderr
        run_probe("prepare_downgrade")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "b2e7a9d4c631")
        run_probe("verify_legacy", json.dumps(snapshot))
        # Role cấp cluster có thể đã tồn tại do database khác; upgrade phải không lỗi.
        run_probe("precreate_provisioner_role")
        unsafe = compose(
            "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head", success=False
        )
        assert "MB01_PROVISIONER_ROLE_UNSAFE" in unsafe.stderr
        run_probe("harden_provisioner_role")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        run_probe("verify_reupgrade")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
    finally:
        compose("down", "--volumes", "--remove-orphans")
