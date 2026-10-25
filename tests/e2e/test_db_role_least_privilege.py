"""SEC-01/SEC-02: quyền DB tối thiểu theo process và PostgreSQL đã vá, trên Compose cô lập."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

# Bootstrap ngắn đọc gói JSON từ stdin: Windows giới hạn dòng lệnh 32.767 ký tự.
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(SEC01_PHASE=p['phase'],SEC01_SNAPSHOT=p['snapshot']);"
    "exec(compile(p['code'],'db_role_least_privilege.py','exec'),{'__name__':'__main__'})"
)
# Head trước SEC-01 sau tích hợp: MB-03 (bảng MB-02/MB-03 đã có grant trực tiếp riêng).
PREVIOUS_HEAD = "e3c5a7b9d2f4"
WORKER_SERVICES = ("execution-gateway", "ingestion-worker", "trainer-worker")


def test_process_roles_cannot_write_identity_or_config_and_downgrade_restores_acl() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-sec01-e2e-{uuid.uuid4().hex[:10]}"
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
        assert result.returncode == 0, (args, result.stdout[-5000:], result.stderr[-5000:])
        return result

    probe = (root / "tests/e2e/probes/db_role_least_privilege.py").read_text(encoding="utf-8")

    def run_probe(phase: str, service: str = "migrator", snapshot: str = "") -> dict[str, object]:
        payload = json.dumps({"phase": phase, "snapshot": snapshot, "code": probe})
        result = compose(
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
        parsed: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
        return parsed

    def migrate(*args: str) -> None:
        compose("run", "--rm", "--no-deps", "migrator", "alembic", *args)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres", "db-provisioner")
        migrate("upgrade", PREVIOUS_HEAD)
        before = run_probe("acl_snapshot")
        migrate("upgrade", "head")
        migrate("check")
        seeded = run_probe("seed")
        assert int(str(seeded["server_version_num"])) >= 180006
        run_probe("matrix")
        for service in WORKER_SERVICES:
            assert run_probe("worker", service=service)["denied"]
        assert run_probe("runtime", service="trading-runtime")["denied"]
        assert run_probe("admin", service="admin-web")["denied"]
        # Downgrade phải trả đúng ACL/policy/membership trước SEC-01; re-upgrade lặp lại được.
        migrate("downgrade", PREVIOUS_HEAD)
        assert run_probe("acl_compare", snapshot=json.dumps(before))["acl_restored"] is True
        migrate("upgrade", "head")
        run_probe("matrix")
        migrate("check")
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
