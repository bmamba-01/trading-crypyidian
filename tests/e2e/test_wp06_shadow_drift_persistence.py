"""Compose E2E cho ledger drift shadow, quyền PostgreSQL và migration guard."""

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
    "os.environ['WP06_SHADOW_DRIFT_STATE']=json.dumps(p['state']);"
    "exec(compile(p['code'],'wp06_shadow_drift_persistence.py','exec'),{'__name__':'__main__'})"
)
PREVIOUS_HEAD = "d2b4c6e8f0a1"
CURRENT_HEAD = "c3f5a7b9d1e3"


def test_shadow_drift_postgres_append_only_rls_rights_and_empty_downgrade() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-drift-e2e-{uuid.uuid4().hex[:10]}"
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
        *args: str,
        stdin: str | None = None,
        success: bool = True,
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

    probe = (root / "tests/e2e/probes/wp06_shadow_drift_persistence.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
    }
    payload = json.dumps({"state": state, "code": probe})

    def migrate(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, success=success)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", "head")
        migrate("check")
        heads = migrate("heads")
        assert CURRENT_HEAD in heads.stdout
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "db-provisioner",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        migrate("downgrade", PREVIOUS_HEAD)
        previous = migrate("current")
        assert PREVIOUS_HEAD in previous.stdout
        migrate("upgrade", "head")
        current = migrate("current")
        assert CURRENT_HEAD in current.stdout
        migrate("check")
        assert data == {
            "bounded_policy_schema": True,
            "bounded_cutoffs_and_expiry": True,
            "empty_assessment_rejected": True,
            "assessment_rows_scoped_per_workspace": True,
            "drift_policy_rejected": True,
            "cross_workspace_insert_denied": True,
            "unscoped_read_denied": True,
            "observer_role_least_privilege": True,
            "observer_sensitive_columns_denied": True,
            "admin_select_only": True,
            "rights_hash_helper_execute_denied": True,
            "rights_receipt_helper_execute_allowed": True,
            "rights_receipt_fails_closed": True,
            "source_rights_mapping_execute_allowed": True,
            "source_rights_mapping_exact": True,
            "source_policy_direct_select_denied": True,
            "canonical_source_cost_execute_allowed": True,
            "canonical_source_cost_parity": True,
            "observation_hash_parity": True,
            "observation_content_hash_rejected": True,
            "observation_idempotency_rejected": True,
            "observation_lineage_fails_closed": True,
            "proposal_content_hash_rejected": True,
            "proposal_idempotency_rejected": True,
            "proposal_hash_parity": True,
            "ledger_immutable_trigger_definitions_present": True,
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
