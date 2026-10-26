"""MB05.2–05.4: lease/fencing, control append-only và incident trên PostgreSQL thật (Compose).

Chứng minh: một writer mỗi bot/account; token cũ bị từ chối ở application **và** trigger DB khi
bỏ qua application; takeover phải chờ writer đang giữ fence; hết hạn theo đồng hồ DB; control
append-only theo quyền (ruling 30) và downgrade guard của migration MB-05.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB05_PHASE=p['phase'],MB05_STATE=json.dumps(p.get('state',{})));"
    "exec(compile(p['code'],'multibot_runtime_fencing.py','exec'),{'__name__':'__main__'})"
)
PRE_MB05_HEAD = "c8f1e3a5b7d9"


def test_runtime_lease_fencing_controls_and_downgrade_guard_on_real_postgres() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-mb05-e2e-{uuid.uuid4().hex[:10]}"
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

    def compose(
        *args: str, success: bool = True, stdin: str | None = None
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
            result.stdout[-5000:],
            result.stderr[-5000:],
        )
        return result

    probe = (root / "tests/e2e/probes/multibot_runtime_fencing.py").read_text(encoding="utf-8")

    def run_probe(
        phase: str, state: dict[str, object] | None = None, service: str = "migrator"
    ) -> dict[str, object]:
        payload = json.dumps({"phase": phase, "state": state or {}, "code": probe})
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

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        state = run_probe("seed")

        fencing = run_probe("runtime_fencing", state, service="trading-runtime")
        assert fencing["transitional_null"] == "OK", fencing
        assert fencing["token_a1"] == 1
        assert fencing["b_before_expiry"] == "LEASE_HELD_ELSEWHERE"
        assert fencing["null_after_lease"] == "MB05_FENCING_TOKEN_REQUIRED"
        assert fencing["a_write_token1"] == "OK"
        # Writer giữ FOR SHARE: takeover (dù lease đã hết hạn) phải chờ, không chen ngang.
        assert fencing["takeover_while_fenced"] == "SQLSTATE_55P03"
        assert fencing["token_b2"] == 2
        assert fencing["stale_application"] == "STALE_FENCING_TOKEN"
        assert fencing["stale_repository_append"] == "MB05_STALE_FENCING_TOKEN"
        assert fencing["stale_raw_insert"] == "MB05_STALE_FENCING_TOKEN"
        assert fencing["stale_state"] == "STALE_FENCING_TOKEN"
        assert fencing["checkpoints_unchanged"] is True
        assert fencing["b_write_token2"] == "OK"
        assert fencing["b_state"] == "OK"
        assert fencing["token_b2_short"] == 2
        assert fencing["expired_raw_insert"] == "MB05_LEASE_EXPIRED"
        assert fencing["reacquire_same_owner"] == 3
        assert fencing["overlong_expiry"] == "MB05_LEASE_EXPIRY_INVALID"
        assert fencing["token_decrease"] == "MB05_LEASE_TOKEN_INVALID"
        assert fencing["delete_lease"] == "SQLSTATE_42501"
        assert fencing["race_bot_b"] == ["LEASE_HELD_ELSEWHERE", "OK"]
        assert fencing["incident_open"] == "OK"
        assert fencing["incident_stale"] == "STALE_FENCING_TOKEN"
        assert fencing["bot_a_sees_leases"] == 1
        assert fencing["other_workspace_sees"] == 0
        assert fencing["lease_tokens"] == {"bot_a": 3, "bot_b": 1}

        control = run_probe("admin_control", state, service="admin-web")
        assert control["operator_pause_a"] == [1, True], control
        assert control["operator_pause_a_again"] == [1, False]
        assert control["operator_pause_b"] == "BOT_NOT_FOUND"
        assert control["operator_kill"] == "CONTROL_WRITE_REJECTED"
        assert control["owner_kill"] == [1, True]
        assert control["owner_kill_again"] == [1, False]
        assert control["owner_release_wrong_revision"] == "CONTROL_REVISION_CONFLICT"
        assert control["owner_release"] == [2, True]
        assert control["forged_changed_by"] == "SQLSTATE_42501"
        assert control["skipped_revision"] == "MB05_CONTROL_REVISION_CONFLICT"
        assert control["other_workspace_controls"] == 0
        assert control["operator_resolve_b"] == "INCIDENT_NOT_VISIBLE"
        assert control["owner_resolve_b"] == "RESOLVED"

        read = run_probe("runtime_control_read", state, service="trading-runtime")
        assert read == {
            "bot_a": {"workspace": 2, "bot": 1, "entry": "PAUSED"},
            "bot_b": {"workspace": 2, "bot": 0, "entry": "ENABLED"},
            "workspace": "ACTIVE",
        }

        guard = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            PRE_MB05_HEAD,
            success=False,
        )
        assert "MB05_DOWNGRADE_REQUIRES_EXPORT_OF_RUNTIME_STATE" in guard.stderr
        run_probe("prepare_downgrade")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", PRE_MB05_HEAD)
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
