from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp06_training_child_pid_is_reaped_after_cancellation() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    image = build_image_reference(root)
    ensure_image_available(root, image)
    project = f"crypidian-wp06-pid-{uuid.uuid4().hex[:10]}"
    command = [
        "docker",
        "compose",
        "--profile",
        "learning",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        f"{root / 'tests'}:/e2e-tests:ro",
        "trainer-worker",
        "python",
        "/e2e-tests/e2e/probes/wp06_training_process_pid.py",
    ]
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    finally:
        subprocess.run(
            command[: command.index("run")] + ["down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert result.returncode == 0, (
        f"Compose PID cancellation failed.\nstdout:\n{result.stdout[-6000:]}"
        f"\nstderr:\n{result.stderr[-6000:]}"
    )
    assert '"child_pid_reaped":' in result.stdout
