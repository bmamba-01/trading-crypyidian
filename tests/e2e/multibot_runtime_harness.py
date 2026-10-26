"""Harness Compose dùng chung cho E2E runtime nhiều bot MB-05 Part B.

Mỗi test tạo một Compose project ngẫu nhiên `crypidian-mb05b-e2e-<hex>`, dùng image app
content-addressed chung, chạy probe qua stdin (giới hạn dòng lệnh Windows) và chỉ dọn đúng
project của mình (`down --volumes --remove-orphans`).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

ROOT = Path(__file__).parents[2]
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB05B_PHASE=p['phase'],MB05B_STATE=json.dumps(p.get('state',{})));"
    "exec(compile(p['code'],'multibot_runtime.py','exec'),{'__name__':'__main__'})"
)
HEALTH_PROBE = (
    "import json,sys,urllib.error,urllib.request\n"
    "try:\n"
    "    r=urllib.request.urlopen('http://127.0.0.1:8000'+sys.argv[1],timeout=3)\n"
    "    s,b=r.status,r.read()\n"
    "except urllib.error.HTTPError as e:\n"
    "    s,b=e.code,e.read()\n"
    "print(json.dumps({'status':s,'body':json.loads(b or b'{}')}))\n"
)


# Mô phỏng bước "export trạng thái runtime" bắt buộc trước downgrade qua MB-05 (ruling 24):
# runtime thật để lại lease/state và checkpoint có fencing token; superuser của project test xóa
# hàng runtime tạm thời và gỡ token (tắt trigger bất biến trong đúng transaction) để các guard
# downgrade cũ hơn được kiểm như trước. Chỉ dùng trong Compose project của test.
MB05_EXPORT_SQL = (
    "BEGIN; "
    "ALTER TABLE bot_runtime_lease DISABLE TRIGGER bot_runtime_lease_guard; "
    "DELETE FROM bot_runtime_lease; "
    "ALTER TABLE bot_runtime_lease ENABLE TRIGGER bot_runtime_lease_guard; "
    "ALTER TABLE bot_runtime_state DISABLE TRIGGER bot_runtime_state_identity_guard; "
    "DELETE FROM bot_runtime_state; "
    "ALTER TABLE bot_runtime_state ENABLE TRIGGER bot_runtime_state_identity_guard; "
    "ALTER TABLE paper_execution_checkpoint "
    "DISABLE TRIGGER paper_execution_checkpoint_immutable; "
    "UPDATE paper_execution_checkpoint SET fencing_token = NULL "
    "WHERE fencing_token IS NOT NULL; "
    "ALTER TABLE paper_execution_checkpoint ENABLE TRIGGER paper_execution_checkpoint_immutable; "
    "COMMIT;"
)


class ComposeHarness:
    def __init__(self, label: str) -> None:
        subprocess.run(
            [sys.executable, "scripts/prepare_local_secrets.py"],
            check=True,
            cwd=ROOT,
            capture_output=True,
        )
        self.project = f"crypidian-mb05b-e2e-{label}-{uuid.uuid4().hex[:8]}"
        self.image = build_image_reference(ROOT)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            web_port = listener.getsockname()[1]
        # Cổng admin-web ngẫu nhiên: phiên khác có thể đang chiếm 8000.
        self.environment = os.environ | {
            "CRYPIDIAN_APP_IMAGE": self.image,
            "TRADING_BOT_WEB_PORT": str(web_port),
        }
        self.base = [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--project-name",
            self.project,
            "-f",
            "compose.yaml",
        ]
        self.probe_code = (ROOT / "tests/e2e/probes/multibot_runtime.py").read_text("utf-8")
        self.state: dict[str, Any] = {}

    def compose(
        self, *args: str, success: bool = True, stdin: str | None = None, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                self.base + list(args),
                cwd=ROOT,
                env=self.environment,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            pytest.fail(f"Compose vượt timeout cho {args[:3]}: {str(error)[-2000:]}")
        assert (result.returncode == 0) is success, (
            args[:6],
            result.stdout[-6000:],
            result.stderr[-6000:],
        )
        return result

    def docker(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def start(self, *services: str) -> None:
        info = self.docker("info", "--format", "{{.ServerVersion}}", timeout=30)
        if info.returncode:
            pytest.fail(f"Docker daemon cần chạy cho E2E Compose: {info.stderr[-500:]}")
        ensure_image_available(ROOT, self.image)
        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
            "db-provisioner",
            *services,
        )
        self.compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

    def probe(self, phase: str, service: str = "trading-runtime", **extra: Any) -> dict[str, Any]:
        state = self.state | extra
        payload = json.dumps({"phase": phase, "state": state, "code": self.probe_code})
        result = self.compose(
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
        # stderr (cảnh báo/trace chẩn đoán, không chứa secret) giữ lại cho thông điệp assert.
        self.last_stderr = "\n".join(
            line for line in result.stderr.splitlines() if "[trace]" in line
        )[-4000:]
        parsed: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        return parsed

    def inspect(self, workspace: str = "main") -> dict[str, Any]:
        return self.probe("inspect", "migrator", inspect_workspace=workspace)

    def run_runtime(self, workspace_id: str, **env: str) -> str:
        args = [
            "run",
            "--detach",
            "--no-deps",
            "--env",
            f"TRADING_BOT_RUNTIME_WORKSPACE_ID={workspace_id}",
        ]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        result = self.compose(*args, "trading-runtime")
        return result.stdout.strip().splitlines()[-1]

    def health(self, container: str) -> tuple[int, dict[str, Any]]:
        result = self.docker(
            "exec", container, "python", "-c", HEALTH_PROBE, "/health/ready", timeout=20
        )
        if result.returncode:
            return 0, {"error": result.stderr[-500:]}
        try:
            parsed = json.loads(result.stdout.strip().splitlines()[-1])
        except IndexError, json.JSONDecodeError:
            return 0, {"raw": result.stdout[-500:]}
        return int(parsed["status"]), parsed["body"]

    def wait_health(self, container: str, predicate: Any, *, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            status, body = self.health(container)
            last = (status, body)
            if status and predicate(status, body):
                return body
            time.sleep(0.5)
        logs = self.docker("logs", "--tail", "60", container).stdout[-3000:]
        pytest.fail(f"Runtime không đạt trạng thái mong đợi: {last}\nlogs:\n{logs}")

    def down(self) -> None:
        subprocess.run(
            [*self.base, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
