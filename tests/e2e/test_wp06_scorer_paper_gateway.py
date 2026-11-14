"""Compose âm tính cho wiring gateway PAPER với canonical source TRAINING-only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

from tests.e2e.test_wp06_source_pinned_replay_persistence import _compose_probe


def test_real_deployment_gateway_denies_training_only_canonical_source() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    project = f"crypidian-wp06-paper-gateway-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    linked_workspace_id = str(uuid.uuid4())
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "CRYPIDIAN_E2E_WORKSPACE_ID": linked_workspace_id,
    }
    image_created = ensure_image_available(root, image)
    volume = f"{root / 'tests'}:/e2e-tests:ro"

    def compose(*args):
        return _compose_probe(root, project, environment, *args)

    def probe(service, phase, values=None):
        arguments = [
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "-e",
            f"WP06_NORMALIZED_PHASE={phase}",
        ]
        for key, value in (values or {}).items():
            arguments.extend(["-e", f"{key}={value}"])
        result = compose(
            *arguments, service, "python", "/e2e-tests/e2e/probes/wp06_normalized_fold_samples.py"
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    try:
        compose("up", "--detach", "--wait", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        source = probe("admin-web", "source-admin-seed")
        source_values = {
            ("WP06_SOURCE_ID" if key == "source_id" else f"WP06_SOURCE_{key.upper()}"): value
            for key, value in source.items()
        }
        probe("ingestion-worker", "source-item-seed", source_values)
        manifest = probe("migrator", "source-manifest-seed", source_values)
        values = {
            "WP06_WORKSPACE_ID": manifest["workspace_id"],
            "WP06_MANIFEST_HASH": manifest["manifest_hash"],
        }
        built = probe("candidate-proof-builder", "builder", values)
        assert built["source_training_receipt_count"] == 1
        attempt = probe("admin-web", "create-training-attempt", values)
        legacy_denial = probe(
            "trainer-worker",
            "source-training-legacy-lineage-denied",
            {
                "WP06_SOURCE_WORKSPACE_ID": manifest["workspace_id"],
                "WP06_ATTEMPT_ID": attempt["attempt_id"],
            },
        )
        assert legacy_denial == {
            "legacy_v3_source_lineage_denied": True,
            "normalized_failure_code": "WP06_TRAINING_BATCH_SOURCE_RIGHTS_FAIL_CLOSED",
        }
        legacy_ledgers = probe(
            "migrator",
            "source-training-legacy-ledger-verify",
            {
                "WP06_SOURCE_WORKSPACE_ID": manifest["workspace_id"],
                "WP06_ATTEMPT_ID": attempt["attempt_id"],
            },
        )
        assert legacy_ledgers == {
            "training_disclosure_ledgers_unchanged": True,
        }

        linked_seed = compose(
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={linked_workspace_id}",
            "migrator",
            "python",
            "/e2e-tests/e2e/probes/wp06_source_pinned_replay_persistence.py",
            "--scorer-seed",
        )
        linked_state = json.loads(linked_seed.stdout.strip().splitlines()[-1])
        linked_state["evaluation_run_id"] = str(uuid.uuid4())

        def linked_phase(service, phase):
            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "--volume",
                volume,
                "-e",
                "WP06_SOURCE_LINKED_STATE=" + json.dumps(linked_state, separators=(",", ":")),
                service,
                "python",
                "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
                phase,
            )
            observed = json.loads(result.stdout.strip().splitlines()[-1])
            linked_state.update(observed)
            return observed

        linked_phase("candidate-proof-builder", "stage")
        linked_phase("admin-web", "approve")
        linked_phase("candidate-proof-builder", "seal")
        linked_phase("admin-web", "enqueue")
        trained = linked_phase("trainer-worker", "train")
        assert trained["status"] == "succeeded"
        proof = linked_phase("candidate-proof-builder", "proof")
        assert proof["scorer_artifact_id"]

        denied = compose(
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "-e",
            f"WP06_WORKSPACE_ID={linked_state['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={linked_state['attempt_id']}",
            "admin-web",
            "python",
            "/e2e-tests/e2e/probes/wp06_scorer_paper_gateway.py",
        )
        assert json.loads(denied.stdout.strip().splitlines()[-1]) == {
            "real_gateway_paper_rights_denied": True,
            "pointer_and_event_ledger_unchanged": True,
            "diagnostic_artifact_only": True,
            "positive_rights_evidence": False,
            "denial_returned_no_report_or_member_data": True,
        }
    finally:
        compose("down", "--volumes", "--remove-orphans")
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, check=True, timeout=120
            )
