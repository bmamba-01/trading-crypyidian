"""PostgreSQL Compose cho WP-06 immutable manifest và trainer read-only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(WP06_PHASE=p['phase'],WP06_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp06_dataset_manifest.py','exec'),{'__name__':'__main__'})"
)
PREVIOUS_HEAD = "a7c9e2f4b6d8"
MANIFEST_HEAD = "b8d0f1a3c5e7"
PRE_TYPED_LINEAGE_HEAD = "f4a6c8d0e2b5"


def test_dataset_manifest_persists_immutably_with_workspace_rls_and_trainer_read_only() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-manifest-e2e-{uuid.uuid4().hex[:10]}"
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
        if (result.returncode == 0) is not success:
            raise AssertionError(
                f"Compose lệnh {args!r} thất bại:\n{result.stdout}\n{result.stderr}"
            )
        return result

    probe = (root / "tests/e2e/probes/wp06_dataset_manifest.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
    }

    def run_probe(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrator",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def migrate(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, success=success)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", PREVIOUS_HEAD)
        migrate("upgrade", MANIFEST_HEAD)
        run_probe("seed")
        run_probe("seed_downgrade_guard")
        dataset_guard = migrate("downgrade", PREVIOUS_HEAD, success=False)
        assert "WP06_DATASET_MANIFEST_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
            dataset_guard.stdout + dataset_guard.stderr
        )
        migrate("upgrade", "head")
        persisted = run_probe("persist")
        assert persisted["versions"] == ["dataset-manifest-v1", "dataset-manifest-v2"]
        assert persisted["folds"] == 2
        assert persisted["holdout_interval_overlap_rejected"] is True
        risk_guard = migrate("downgrade", "e0b2d4f6a8c1", success=False)
        assert "WP03_RISK_GUARD_DOWNGRADE_BLOCKED" in (risk_guard.stdout + risk_guard.stderr)
        typed = run_probe("persist_typed")
        assert typed == {
            "typed_ref_count": 1,
            "workspace_scope": True,
            "rights_hash_observed": True,
            "transaction_atomic": True,
        }
        assert run_probe("roles") == {
            "builder_payload_read": True,
            "trainer_payload_denied": True,
            "trainer_insert_denied": True,
            "append_only": True,
        }
        unscoped = run_probe("unscoped")
        assert unscoped == {"unscoped_rows": 0, "unscoped_source_refs": 0}
        typed_guard = migrate("downgrade", PRE_TYPED_LINEAGE_HEAD, success=False)
        assert "WP06_TYPED_SOURCE_REF_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
            typed_guard.stdout + typed_guard.stderr
        )
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
