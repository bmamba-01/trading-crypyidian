"""WP-07B: Compose E2E cho subscription opt-in và giới hạn DB của signal-worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(WP07_PHASE=p['phase'],WP07_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp07_telegram_signal.py','exec'),{'__name__':'__main__'})"
)


def test_signal_worker_sends_only_to_active_opted_in_private_binding() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp07-signal-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "signal",
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
        assert result.returncode == 0, (args, result.stdout[-4000:], result.stderr[-4000:])
        return result

    probe = (root / "tests/e2e/probes/wp07_telegram_signal.py").read_text(encoding="utf-8")
    state = {
        name: str(uuid.uuid4())
        for name in (
            "workspace_id",
            "membership_id",
            "binding_id",
            "subscription_id",
            "bot_id",
            "account_id",
        )
    }

    def run_probe(phase: str, *, worker: bool = False) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe})
        service = "telegram-signal-worker" if worker else "migrator"
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
        return cast(dict[str, Any], json.loads(result.stdout.strip().splitlines()[-1]))

    def migrate(*args: str) -> None:
        compose("run", "--rm", "--no-deps", "migrator", "alembic", *args)

    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", "head")
        migrate("check")
        seeded = run_probe("seed")
        assert seeded["seeded"] is True
        state["bot_id"] = str(seeded["bot_id"])
        assert run_probe("worker_active_binding", worker=True) == {
            "active_targets": 1,
            "order_insert": False,
        }
        assert run_probe("acl", worker=True) == {
            "signal_scope_grants": True,
            "order_privileges": False,
        }
        assert run_probe("revoke_binding") == {"revoked": True}
        assert run_probe("worker_revoked_binding", worker=True) == {
            "active_targets": 0,
            "revoked_binding_blocked": True,
        }
        assert run_probe("deactivate_member") == {"membership_disabled": True}
        assert run_probe("worker_inactive_member", worker=True) == {
            "active_targets": 0,
            "inactive_membership_blocked": True,
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
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=120,
                check=False,
            )
