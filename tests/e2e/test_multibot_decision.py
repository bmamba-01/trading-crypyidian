"""MB-03: một shared evidence → quyết định riêng cho hai bot PAPER trên PostgreSQL thật.

Sau tích hợp, MB-03 nối sau MB-02 (`d2b4f6a8c1e3`): downgrade/upgrade chỉ lặp trên MB-03.
"""

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
    "os.environ.update(MB03_PHASE=p['phase'],MB03_STATE=p['state']);"
    "exec(compile(p['code'],'multibot_decision.py','exec'),{'__name__':'__main__'})"
)


def test_shared_evidence_fans_out_to_two_paper_bots_without_duplicates() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-mb03-e2e-{uuid.uuid4().hex[:10]}"
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

    probe = (root / "tests/e2e/probes/multibot_decision.py").read_text(encoding="utf-8")

    def run_probe(phase, state=None, service="migrator"):
        payload = json.dumps({"phase": phase, "state": json.dumps(state or {}), "code": probe})
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
        lines = result.stdout.strip().splitlines()
        return json.loads(lines[-1]) if lines else {}

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        seeded = run_probe("seed")
        # Login thật của trading runtime chạy dispatcher/store/consumer của ứng dụng.
        dispatched = run_probe("dispatch", seeded, service="trading-runtime")
        decisions = dispatched["decisions"]
        assert [item["bot_id"] for item in decisions] == [seeded["bot_a1"], seeded["bot_a2"]]
        assert [item["strategy_release_id"] for item in decisions] == ["h01-v1", "h03-v1"]
        assert [item["bot_config_version_id"] for item in decisions] == [
            dispatched["config_a1"],
            dispatched["config_a2"],
        ]
        assert [item["bot_config_version_id"] for item in decisions] == [
            seeded["config_a1"],
            seeded["config_a2"],
        ]
        assert dispatched["exclusions"] == [[seeded["bot_a3"], "BOT_CONFIG_NOT_PUBLISHED"]]
        assert {item["status"] for item in decisions} == {"CANDIDATE"}
        assert len({item["candidate_id"] for item in decisions}) == 2
        assert len({item["idempotency_key"] for item in decisions}) == 2
        assert run_probe("verify", seeded | dispatched) == {"verified": True}
        guard = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "d2b4f6a8c1e3",
            success=False,
        )
        assert "MB03_DOWNGRADE_REQUIRES_EXPORT_OF_DECISIONS" in guard.stderr
        run_probe("prepare_downgrade")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "d2b4f6a8c1e3")
        run_probe("verify_downgraded")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
    finally:
        compose("down", "--volumes", "--remove-orphans")
