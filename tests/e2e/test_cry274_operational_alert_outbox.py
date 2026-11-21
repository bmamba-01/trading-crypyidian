"""CRY-274 E2E: outbox cảnh báo vận hành chịu Telegram outage trên Compose cô lập.

Chứng minh tiêu chí nghiệm thu của ticket `[WP-04D/alert-outbox]`:
- Pause thật qua Admin API service phát ``bot.paused`` cùng transaction; update lặp (noop)
  không phát thêm sự kiện.
- Telegram outage: consumer retry/backoff rồi dead-letter; trạng thái pause KHÔNG đổi.
- Cảnh báo DEAD vẫn hiển thị qua đường đọc Admin Web (``list_operational_alerts``).
- Gửi thành công có ConsumerReceipt + audit; replay sự kiện đã gửi không gửi trùng.
- Quyền DB tối thiểu: consumer chạy bằng role ``crypidian_signal_worker`` thật.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(CRY274_PHASE=p['phase'],CRY274_STATE=p['state']);"
    "exec(compile(p['code'],'cry274_operational_alert_outbox.py','exec'),{'__name__':'__main__'})"
)

PHASE_SERVICES = {
    "seed": "migrator",
    "admin_pause": "admin-web",
    "worker_outage": "telegram-signal-worker",
    "unblock": "migrator",
    "worker_deliver": "telegram-signal-worker",
    "replay": "migrator",
    "worker_idempotent": "telegram-signal-worker",
    "admin_verify": "admin-web",
    "inspect": "migrator",
}


def test_operational_alert_outbox_survives_telegram_outage_and_is_idempotent() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-cry274-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--profile",
        "signal",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]

    def compose(
        *args: str, stdin: str | None = None, success: bool = True
    ) -> subprocess.CompletedProcess[str]:
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
        assert (result.returncode == 0) is success, (
            args,
            result.stdout[-12000:],
            result.stderr[-25000:],
        )
        return result

    probe = (root / "tests/e2e/probes/cry274_operational_alert_outbox.py").read_text(
        encoding="utf-8"
    )
    state: dict[str, object] = {}

    def run_phase(phase: str) -> dict[str, object]:
        nonlocal state
        payload = json.dumps({"phase": phase, "state": json.dumps(state), "code": probe})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            PHASE_SERVICES[phase],
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        parsed: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
        state.update(parsed)
        return parsed

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator")  # alembic upgrade head
        run_phase("seed")
        paused = run_phase("admin_pause")
        assert paused["revision"] == 1
        assert run_phase("worker_outage")["dead_lettered"] == 2
        run_phase("unblock")
        delivered = run_phase("worker_deliver")
        assert delivered["delivered"] == 1
        run_phase("replay")
        assert run_phase("worker_idempotent")["skipped_duplicate"] == 1
        verified = run_phase("admin_verify")
        assert verified["dead_visible"] is True
        assert verified["still_paused"] is True
        inspected = run_phase("inspect")
        assert inspected["receipts"] == 1
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
