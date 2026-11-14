"""Compose E2E cho scope license nguồn dùng trong training WP06."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp06_source_license_scope_and_receipts_are_database_verified() -> None:
    """Kiểm policy, lineage, idempotency và ACL bằng PostgreSQL Compose thật."""
    repository_root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    project_name = f"crypidian-wp06-license-scope-{uuid.uuid4().hex[:10]}"
    workspace_id = str(uuid.uuid4())
    image_reference = build_image_reference(repository_root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image_reference,
        "TRADING_BOT_INGESTION_WORKSPACE_ID": workspace_id,
    }
    probe_mount = f"{repository_root / 'tests'}:/e2e-tests:ro"
    probe_script = "/e2e-tests/e2e/probes/wp06_source_license_scope.py"
    profiles = (
        "--profile",
        "paper",
        "--profile",
        "learning-proof",
        "--profile",
        "learning",
    )

    def compose(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "docker",
                "compose",
                *profiles,
                "--project-name",
                project_name,
                "-f",
                "compose.yaml",
                *args,
            ],
            check=False,
            capture_output=True,
            cwd=repository_root,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        safe_args = tuple(
            "WP06_SOURCE_SCOPE_STATE=<omitted>"
            if argument.startswith("WP06_SOURCE_SCOPE_STATE=")
            else argument
            for argument in args
        )
        assert result.returncode == 0, (
            f"Compose command failed: {safe_args}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr start:\n{result.stderr[:5000]}\n"
            f"stderr end:\n{result.stderr[-3000:]}"
        )
        return result

    def run_probe(service: str, phase: str, state: dict[str, object] | None = None):
        command = [
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            probe_mount,
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_SOURCE_SCOPE_PHASE={phase}",
        ]
        if service == "candidate-proof-builder":
            password_secret = repository_root / ".secrets" / "postgres_password"
            command.extend(
                (
                    "--volume",
                    f"{password_secret}:/run/secrets/wp06_e2e_postgres_password:ro",
                    "-e",
                    "WP06_E2E_SUPERUSER_PASSWORD_FILE=/run/secrets/wp06_e2e_postgres_password",
                )
            )
        if state is not None:
            command.extend(("-e", f"WP06_SOURCE_SCOPE_STATE={json.dumps(state)}"))
        result = compose(*command, service, "python", probe_script)
        return json.loads(result.stdout.strip().splitlines()[-1])

    ensure_image_available(repository_root, image_reference)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        assert heads.stdout.strip().count("(head)") == 1, heads.stdout

        seeded = run_probe("migrator", "seed")
        assert seeded["workspace_id"] == workspace_id
        assert seeded["bundle_diagnostics"] == {
            "json_matches_canonical": True,
            "key_count": 9,
            "schema_matches": True,
            "workspace_matches": True,
            "symbol_matches": True,
            "evaluation_start_matches": True,
            "range_end_matches": True,
            "provider_matches": True,
            "purpose_matches": True,
            "backtest_purpose_expected": True,
            "fingerprint_matches": True,
            "hash_matches": True,
            "component_array": True,
        }
        assert seeded["child_diagnostics"]
        assert all(
            child["json_matches_canonical"]
            and child["schema_version"] == 4
            and child["key_count"] == 28
            and child["purpose"] == "BACKTEST_RESEARCH"
            and child["provider"] == "CRYPIDIAN_FIRST_PARTY_QA"
            and child["bar_identity_hash_present"]
            for child in seeded["child_diagnostics"]
        )
        assert seeded["child_sql_diagnostics"] == {
            "row_present": True,
            "status_ready": True,
            "component_interval_matches": True,
            "bundle_symbol_matches": True,
            "evaluation_start_matches": True,
            "range_end_matches": True,
            "json_matches_canonical": True,
            "manifest_key_count": 28,
            "manifest_hash_matches": True,
            "schema_version_matches": True,
            "dataset_hash_matches": True,
            "status_matches": True,
            "symbol_matches": True,
            "interval_matches": True,
            "warmup_start_matches": True,
            "prepared_at_matches": True,
            "policy_fingerprint_matches": True,
            "bar_counts_match": True,
            "provider_matches": True,
            "rights_workspace_matches": True,
            "purpose_matches": True,
            "bar_identity_hash_present": True,
            "component_bar_hash_matches": True,
            "rights_row_matches": True,
        }
        license_checks = {
            key: value
            for key, value in seeded["license_evidence_diagnostics"].items()
            if key not in {"scope_keys", "scope_json", "scope_json_db"}
        }
        assert all(value is True for value in license_checks.values()), (
            license_checks,
            seeded["license_evidence_diagnostics"],
        )
        assert (
            seeded["license_evidence_diagnostics"]["scope_json"]
            == seeded["license_evidence_diagnostics"]["scope_json_db"]
        )
        compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "90",
            "ingestion-worker",
        )
        deadline = time.monotonic() + 30
        while True:
            rights_status = run_probe("migrator", "rights-status")
            if rights_status["pending_rights_events"] == 0:
                break
            assert time.monotonic() < deadline, rights_status
            time.sleep(0.5)
        state = run_probe("candidate-proof-builder", "prepare-manifest", seeded)
        state = run_probe("candidate-proof-builder", "seed-negative-manifests", state)
        authorization = run_probe("candidate-proof-builder", "authorize", state)
        assert authorization["exact_training_scope_allowed"] is True
        assert authorization["evaluation_operation_denied_to_builder"] is True
        assert authorization["cross_workspace_denied"] is True

        normalized = run_probe("candidate-proof-builder", "normalize", state)
        state = state | normalized
        assert normalized["rights_receipt_set_valid"] is True
        assert normalized["source_receipt_count"] == state["expected_source_receipt_count"]
        assert normalized["retry_is_idempotent"] is True
        assert normalized["source_free_wp02_allowed"] is True
        assert normalized["forged_source_free_outcome_denied"] is True
        assert normalized["legacy_v1_v3_source_backed_denied"] is True
        scope_denials = run_probe("candidate-proof-builder", "check-scope-variants", state)
        assert scope_denials["missing_child"] is True, scope_denials
        assert scope_denials["extra_child"] is True, scope_denials
        assert scope_denials["market_bar_substitution"] is True, scope_denials
        gate_acl = run_probe("learning-evaluator", "check-evaluator-gate-acl", state)
        assert gate_acl["direct_gate_denied_to_evaluator"] is True

        receipts = run_probe("admin-web", "inspect-receipts", state)
        assert receipts["metadata_only_receipts_persisted"] is True
        assert receipts["receipt_update_denied"] is True
        assert receipts["receipt_delete_denied"] is True

        revoked = run_probe("migrator", "revoke", state)
        assert revoked["current_policy_revoked"] is True
        claim = run_probe("trainer-worker", "revoke-before-claim", state)
        assert claim["current_rights_denied_before_training_receipt"] is True, claim
        assert claim["no_training_payload_returned"] is True
        after_claim = run_probe("admin-web", "inspect-after-claim", state)
        assert after_claim["normalization_receipts_unchanged"] is True
        assert after_claim["no_training_access_receipt"] is True
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                *profiles,
                "--project-name",
                project_name,
                "-f",
                "compose.yaml",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            check=False,
            capture_output=True,
            cwd=repository_root,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
