"""Proof BASE và ranh giới quyền/khóa trên PostgreSQL Compose thật."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available

from tests.e2e.test_learning_fold_outcome_verifier import _compose


def test_database_candidate_proof_and_training_rights_boundaries():
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    project = f"crypidian-candidate-proof-{uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "CRYPIDIAN_E2E_WORKSPACE_ID": str(uuid4()),
    }
    ensure_image_available(root, image)
    probe = "/e2e-tests/e2e/probes/wp06_candidate_universe_proof.py"

    def run_probe(role, mode, state=None):
        env = environment | {"WP06_STATE": json.dumps(state or {})}
        output = _compose(
            root,
            project,
            env,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "CRYPIDIAN_E2E_WORKSPACE_ID",
            "-e",
            "WP06_STATE",
            "--volume",
            f"{root / 'tests'}:/e2e-tests:ro",
            role,
            "python",
            probe,
            mode,
        )
        return json.loads(output.stdout.strip().splitlines()[-1])

    try:
        _compose(
            root,
            project,
            environment,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
        )
        _compose(root, project, environment, "run", "--rm", "--no-deps", "db-provisioner")
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "upgrade",
            "head",
        )
        _compose(
            root, project, environment, "run", "--rm", "--no-deps", "migrator", "alembic", "check"
        )
        heads = _compose(
            root, project, environment, "run", "--rm", "--no-deps", "migrator", "alembic", "heads"
        )
        assert heads.stdout.count("(head)") == 1
        assert "b2e4a6c8d0f2 (head)" in heads.stdout
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "e5b7c9d1f3a6",
        )
        assert run_probe("migrator", "empty-downgrade") == {"locker_role_removed": True}
        _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "upgrade",
            "head",
        )
        _compose(
            root, project, environment, "run", "--rm", "--no-deps", "migrator", "alembic", "check"
        )
        state = run_probe("migrator", "seed")
        denied = run_probe("candidate-proof-builder", "denied", state)
        assert denied == {"denied_before_payload": True, "no_proof": True}
        run_probe("migrator", "grant-training", state)
        cli = _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "candidate-proof-builder",
            "python",
            "-m",
            "trading_bot.cli.candidate_proof",
            "--workspace-id",
            environment["CRYPIDIAN_E2E_WORKSPACE_ID"],
            "--trial-record-id",
            state["trial_ids"][0],
        )
        cli_result = json.loads(cli.stdout.strip().splitlines()[-1])
        assert cli_result["proof"]["candidate_count"] == 1
        assert cli_result["proof"]["training_authorized"] is False
        assert run_probe("candidate-proof-builder", "builder", state) == {"trusted_builder": True}
        assert run_probe("trainer-worker", "trainer", state) == {
            "cannot_forge": True,
            "no_escalation": True,
            "no_builder_secret": True,
        }
        result = run_probe("migrator", "verify", state)
        assert result == {
            "counts": [1, 1, 0],
            "retry": True,
            "rights_receipts": 2,
            "no_payload": True,
            "workspace_isolation": True,
            "append_after_proof_denied": True,
            "proof_before_append_race": True,
            "append_before_proof_race": True,
            "least_privilege": True,
        }
        assert run_probe("admin-web", "admin", state) == {
            "select_only": True,
            "workspace_isolation": True,
        }
        config = {
            "holdout_start": "2026-11-03T07:00:00+00:00",
            "evaluation_cutoff": "2026-11-03T07:00:00+00:00",
            "folds": [
                {
                    role: [f"2026-11-03T0{hour}:00:00+00:00", f"2026-11-03T0{hour + 1}:00:00+00:00"]
                    for role, hour in (("train", 4), ("validation", 5), ("test", 6))
                }
            ],
        }

        def candidate_cli(action, *arguments):
            output = _compose(
                root,
                project,
                environment,
                "run",
                "--rm",
                "--no-deps",
                "candidate-proof-builder",
                "python",
                "-m",
                "trading_bot.cli.learning_split_candidate",
                action,
                "--workspace-id",
                environment["CRYPIDIAN_E2E_WORKSPACE_ID"],
                *arguments,
            )
            return json.loads(output.stdout.strip().splitlines()[-1])

        stage_arguments = (
            "--trial-record-id",
            state["trial_ids"][6],
            "--config-json",
            json.dumps(config),
        )
        staged = candidate_cli("stage", *stage_arguments)
        assert staged["action"] == "staged"
        assert staged["duplicate"] is False
        assert candidate_cli("stage", *stage_arguments) == staged | {"duplicate": True}
        state |= {"candidate": staged}
        assert run_probe("candidate-proof-builder", "split-unapproved", state) == {
            "unapproved_denied": True
        }
        state |= run_probe("admin-web", "split-review", state)
        assert run_probe("candidate-proof-builder", "split-atomic", state) == {
            "atomic_rollback": True
        }
        sealed = candidate_cli("seal", "--candidate-id", staged["candidate_id"])
        assert sealed["duplicate"] is False
        assert sealed["manifest_hash"] == staged["manifest_hash"]
        assert sealed["fold_plan_hash"] == staged["fold_plan_hash"]
        assert sealed["approval_hash"] == state["approval_hash"]
        assert candidate_cli("seal", "--candidate-id", staged["candidate_id"]) == sealed | {
            "duplicate": True
        }
        assert run_probe("migrator", "split-verify", state) == {
            "persisted": True,
            "holdout_excluded": True,
            "immutable": True,
        }
        assert run_probe("trainer-worker", "split-private", state) == {"payload_denied": True}
        assert run_probe("candidate-proof-builder", "lease-boundary", state) == {
            "post_flush_rollback": True,
            "retry_expiry_rejected": True,
        }
        assert run_probe("migrator", "security", state) == {
            "policy_write_waited_for_proof": True,
            "policy_before_proof_denied_without_payload": True,
            "role_matrix": True,
            "rls_with_check": True,
            "immutable_triggers": True,
            "function_acl": True,
            "learning_job_acl": True,
        }
        assert run_probe("candidate-proof-builder", "split-revoked", state) == {
            "revoked_denied": True
        }
        downgrade = subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "paper",
                "--project-name",
                project,
                "-f",
                "compose.yaml",
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "e5b7c9d1f3a6",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert downgrade.returncode != 0
        assert (
            "WP06_SPLIT_CANDIDATE_DOWNGRADE_REQUIRES_EMPTY_TABLE"
            in downgrade.stdout + downgrade.stderr
        )
        current = _compose(
            root, project, environment, "run", "--rm", "--no-deps", "migrator", "alembic", "current"
        )
        assert "b2e4a6c8d0f2 (head)" in current.stdout
    finally:
        _compose(root, project, environment, "down", "--volumes", "--remove-orphans")
