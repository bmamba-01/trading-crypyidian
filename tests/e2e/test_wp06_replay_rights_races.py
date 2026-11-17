"""Compose E2E đua quyền trên bundle replay v5 (CRY-427).

Khôi phục coverage mất khi kịch bản source-item WP05 thành fail-closed (memory 15/11/2026):

1. Consumer-first: evaluator authorize EVALUATION và giữ khóa nguồn; writer revoke phải chờ
   khóa (≥ 2 s), sau đó consumer mới bị từ chối bằng token hiện hành.
2. Writer-first: writer giữ khóa rồi revoke; builder authorize TRAINING bị chặn trong lúc chờ
   (≥ 2 s) và sau commit thấy policy đã thu hồi → từ chối, không trả receipt.
3. Retention: license mới có `max_retention_days` hữu hạn → gate từ chối
   `WP06_TRAINING_REPLAY_SCOPE_NOT_COVERED` (chưa có lifecycle xóa, spec 2026-11-13).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

from tests.e2e.test_wp06_source_pinned_replay_persistence import _compose_probe

PINNED_PROBE = "/e2e-tests/e2e/probes/wp06_source_pinned_replay_persistence.py"
RACE_PROBE = "/e2e-tests/e2e/probes/wp06_replay_rights_race.py"
PROFILES = (
    "--profile",
    "paper",
    "--profile",
    "learning",
    "--profile",
    "learning-proof",
    "--profile",
    "signal-producer",
)


def test_replay_rights_lock_races_and_retention_fail_closed() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    project = f"crypidian-wp06-replay-race-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    image_created = ensure_image_available(root, image)
    volume = f"{root / 'tests'}:/e2e-tests:ro"

    def compose(*args):
        return _compose_probe(root, project, environment, *args)

    def probe_args(service, script, workspace, values, *extra):
        arguments = ["run", "--rm", "--no-deps", "-T", "--volume", volume]
        arguments += ["-e", f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace}"]
        for key, value in values.items():
            arguments += ["-e", f"{key}={value}"]
        return [*arguments, service, "python", script, *extra]

    def run_json(service, script, workspace, values=None, *extra):
        result = compose(*probe_args(service, script, workspace, values or {}, *extra))
        return json.loads(result.stdout.strip().splitlines()[-1])

    def spawn(service, workspace, values):
        command = [
            "docker",
            "compose",
            *PROFILES,
            "--project-name",
            project,
            "-f",
            "compose.yaml",
            *probe_args(service, RACE_PROBE, workspace, values),
        ]
        return subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def wait_sentinel(process, sentinel, timeout=120.0):
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([process.stdout], [], [], 1.0)
            if readable:
                line = process.stdout.readline()
                if line.strip() == sentinel:
                    return
                if not line and process.poll() is not None:
                    break
        raise AssertionError(f"Không thấy {sentinel}: {process.stderr.read()[-4000:]}")

    def finish(process):
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, (stdout[-2000:], stderr[-6000:])
        return json.loads(stdout.strip().splitlines()[-1])

    def seed(workspace):
        state = run_json("migrator", PINNED_PROBE, workspace, None, "--scorer-seed")
        assert state["qa_provider"] == "CRYPIDIAN_FIRST_PARTY_QA"
        return state["bundle_hash"]

    try:
        compose("up", "--detach", "--wait", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

        # 1) Consumer-first: evaluator giữ khóa nguồn; revoke phải chờ, sau đó bị từ chối.
        consumer_ws = str(uuid.uuid4())
        bundle = seed(consumer_ws)
        holder = spawn(
            "learning-evaluator",
            consumer_ws,
            {
                "WP06_RACE_PHASE": "consumer-hold",
                "WP06_RACE_OPERATION": "evaluation",
                "WP06_RACE_BUNDLE_HASH": bundle,
                "WP06_RACE_HOLD_SECONDS": "15",
            },
        )
        # Giữ khóa 15 s: riêng khởi động container bên kia đã mất vài giây.
        wait_sentinel(holder, "WP06_REPLAY_CONSUMER_LOCK_HELD")
        revoked = run_json(
            "migrator", RACE_PROBE, consumer_ws, {"WP06_RACE_PHASE": "writer-revoke"}
        )
        held = finish(holder)
        assert held["authorized_receipts"] >= 1
        assert revoked["revoked"] is True
        assert revoked["lock_wait_seconds"] >= 2.0, revoked
        denied = run_json(
            "learning-evaluator",
            RACE_PROBE,
            consumer_ws,
            {
                "WP06_RACE_PHASE": "consumer-denied",
                "WP06_RACE_OPERATION": "evaluation",
                "WP06_RACE_BUNDLE_HASH": bundle,
            },
        )
        assert denied["denied_token"] == "WP06_TRAINING_REPLAY_CURRENT_POLICY_DENIED", denied

        # 2) Writer-first: writer giữ khóa rồi revoke; builder chờ, sau commit bị từ chối.
        writer_ws = str(uuid.uuid4())
        bundle = seed(writer_ws)
        writer = spawn(
            "migrator",
            writer_ws,
            {
                "WP06_RACE_PHASE": "writer-revoke",
                "WP06_RACE_WRITER_FIRST": "1",
                "WP06_RACE_HOLD_SECONDS": "15",
            },
        )
        wait_sentinel(writer, "WP06_REPLAY_WRITER_LOCK_HELD")
        blocked = run_json(
            "candidate-proof-builder",
            RACE_PROBE,
            writer_ws,
            {
                "WP06_RACE_PHASE": "consumer-denied",
                "WP06_RACE_OPERATION": "training",
                "WP06_RACE_BUNDLE_HASH": bundle,
            },
        )
        assert finish(writer)["revoked"] is True
        assert blocked["denied_token"] == "WP06_TRAINING_REPLAY_CURRENT_POLICY_DENIED", blocked
        assert blocked["elapsed_seconds"] >= 2.0, blocked

        # 3) Retention hữu hạn: license mới vẫn đủ purpose nhưng gate TRAINING từ chối scope.
        retention_ws = str(uuid.uuid4())
        bundle = seed(retention_ws)
        assert run_json(
            "migrator",
            RACE_PROBE,
            retention_ws,
            {"WP06_RACE_PHASE": "relicense-retention", "WP06_RACE_RETENTION_DAYS": "30"},
        ) == {"relicensed_retention_days": 30}
        retention = run_json(
            "candidate-proof-builder",
            RACE_PROBE,
            retention_ws,
            {
                "WP06_RACE_PHASE": "consumer-denied",
                "WP06_RACE_OPERATION": "training",
                "WP06_RACE_BUNDLE_HASH": bundle,
            },
        )
        assert retention["denied_token"] == "WP06_TRAINING_REPLAY_SCOPE_NOT_COVERED", retention
    finally:
        compose("down", "--volumes", "--remove-orphans")
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, check=True, timeout=120
            )
