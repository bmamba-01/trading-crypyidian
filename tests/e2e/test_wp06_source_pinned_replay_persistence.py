"""Compose E2E cho replay H03 có source identity ghim và outcome lineage thật."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

from tests.e2e.test_learning_fold_outcome_verifier import _compose


def _compose_probe(
    root: Path, project: str, environment: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--profile",
            "learning",
            "--profile",
            "learning-proof",
            "--profile",
            "signal-producer",
            "--project-name",
            project,
            "-f",
            "compose.yaml",
            *args,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0:
        safe_args = tuple(
            "WP06_SOURCE_LINKED_STATE=<metadata>"
            if argument.startswith("WP06_SOURCE_LINKED_STATE=")
            else argument
            for argument in args
        )
        raise AssertionError((safe_args, result.stdout[-1000:], result.stderr[-15000:]))
    return result


@pytest.mark.parametrize("scorer_test", [False, True], ids=["base", "source-linked-test"])
def test_source_pinned_runtime_replay_persists_exact_lineage_in_postgres(scorer_test: bool) -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    project = f"crypidian-wp06-replay-persistence-{uuid.uuid4().hex[:10]}"
    workspace_id = str(uuid.uuid4())
    image = build_image_reference(root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id,
    }
    image_created = ensure_image_available(root, image)
    volume = f"{root / 'tests'}:/e2e-tests:ro"

    try:
        _compose(root, project, environment, "up", "--detach", "--wait", "postgres")
        _compose(root, project, environment, "run", "--rm", "--no-deps", "db-provisioner")
        lineage_contract = None
        if scorer_test:
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
                "e8f0a2c4d6b8",
            )
            assert _function_definition_hashes(
                root, project, environment, volume, workspace_id
            ) == _migration_hashes(root, "preimage")
            lineage_contract = _lineage_contract(root, project, environment, volume, workspace_id)
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
        if scorer_test:
            assert (
                _lineage_contract(root, project, environment, volume, workspace_id)
                == lineage_contract
            )
            assert _function_definition_hashes(
                root, project, environment, volume, workspace_id
            ) == _migration_hashes(root, "postimage")
            assert _migration_body_guard(root, project, environment, volume, workspace_id) == {
                "altered_function_bodies_rejected": True
            }

        check = _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "check",
        )
        assert "No new upgrade operations detected" in check.stdout
        heads = _compose(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "heads",
        )
        assert heads.stdout.count("(head)") == 1

        probe = _compose_probe(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "CRYPIDIAN_E2E_WORKSPACE_ID",
            "--volume",
            volume,
            "migrator",
            "python",
            "/e2e-tests/e2e/probes/wp06_source_pinned_replay_persistence.py",
            *(["--scorer-seed"] if scorer_test else []),
        )
        result = json.loads(probe.stdout.strip().splitlines()[-1])
        if scorer_test:
            assert result["qa_provider"] == "CRYPIDIAN_FIRST_PARTY_QA"
            assert result["bars"] == 470
            assert result["linked_outcomes"] == 27
            assert result["opportunities"] == 270
            assert result["test_candidate_count"] == 9
            _run_source_linked_test(root, project, environment, volume, result)
            _run_qa_migration_downgrade_guard(root, project, environment, volume, workspace_id)
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
                "e8f0a2c4d6b8",
            )
            assert (
                _lineage_contract(root, project, environment, volume, workspace_id)
                == lineage_contract
            )
            assert _function_definition_hashes(
                root, project, environment, volume, workspace_id
            ) == _migration_hashes(root, "preimage")
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
            assert (
                _lineage_contract(root, project, environment, volume, workspace_id)
                == lineage_contract
            )
            assert _function_definition_hashes(
                root, project, environment, volume, workspace_id
            ) == _migration_hashes(root, "postimage")
            check = _compose(
                root,
                project,
                environment,
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "check",
            )
            assert "No new upgrade operations detected" in check.stdout
            return
        assert result == {
            "bars": 206,
            "base_opportunity_complete": True,
            "bundle_round_trip": True,
            "closed_trades": 2,
            "linked_outcomes": 2,
            "opportunities": 6,
            "proof_retry_idempotent": True,
            "source_ids_exact": True,
            "training_receipts": 1,
        }
    finally:
        _compose(root, project, environment, "down", "--volumes", "--remove-orphans")
        for resource in ("container", "volume", "network"):
            command = [
                "docker",
                resource,
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
            if resource == "container":
                command.append("--all")
            inventory = subprocess.run(
                command, capture_output=True, text=True, check=True, encoding="utf-8"
            )
            assert not inventory.stdout.strip(), (resource, inventory.stdout)
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image],
                capture_output=True,
                check=True,
                timeout=120,
            )


def _run_source_linked_test(root, project, environment, volume, state):
    state["evaluation_run_id"] = str(uuid.uuid4())
    for service, phase in (
        ("candidate-proof-builder", "stage"),
        ("admin-web", "approve"),
        ("candidate-proof-builder", "seal"),
        ("admin-web", "enqueue"),
        ("trainer-worker", "train"),
        ("candidate-proof-builder", "proof"),
        ("learning-evaluator", "evaluate"),
        ("migrator", "current-rights"),
        ("learning-evaluator", "deployment-role-denied"),
        ("market-signal-worker", "deployment-market-signal-worker"),
        ("admin-web", "deployment-inconclusive"),
        ("migrator", "deployment-audit"),
    ):
        command = ["python", "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py", phase]
        output = _compose_probe(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "-e",
            "WP06_SOURCE_LINKED_STATE=" + json.dumps(state),
            service,
            *command,
        )
        observed = json.loads(output.stdout.strip().splitlines()[-1])
        if phase == "train":
            assert observed["status"] == "succeeded"
        state.update(observed)
    assert state["test_candidates"] == 9
    assert state["test_no_candidates"] == 81
    assert state["expected_test_events"] == 90
    for name in (
        "receipt_committed_before_payload",
        "atomic_rollback",
        "one_consumption_one_report",
        "safe_report_retry",
        "changed_request_rejected",
        "exact_scope",
        "current_training_rights_bound",
        "metadata_only_report",
        "current_rights_verified",
        "c4_writer_first_verified",
        "c4_resolver_first_verified",
        "exact_train_validation_membership_verified",
        "backfill_provenance_backtest_research_verified",
        "current_training_scope_digest_matched",
        "no_test_receipt_mutation",
        "inconclusive_deployment_denied",
        "qa_source_deployment_denied",
        "qa_denial_side_effect_free",
        "outer_gateway_no_test_payload_mutation",
        "deployment_caller_denied",
        "worker_execute_granted",
        "execution_table_privileges_absent",
    ):
        assert state[name] is True
    assert state["report_status"] == "INCONCLUSIVE"
    assert state["sample_support_block"] == {
        "sample_count": 9,
        "minimum_sample_count": 1000,
        "class_support": {"-1": 3, "0": 3, "1": 3},
        "minimum_class_support": 100,
    }


def _run_qa_migration_downgrade_guard(root, project, environment, volume, workspace_id):
    # Đưa schema về đúng preimage của guard QA trước khi cố ý làm drift function.
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
        "c3f5a7b9d1e3",
    )
    migration_cases = (
        (
            "test-lock",
            "WP06_SCORER_TEST_QA_LOCK_DOWNGRADE_PREIMAGE_UNEXPECTED",
        ),
        (
            "current-rights",
            "WP06_SCORER_CURRENT_RIGHTS_QA_DOWNGRADE_PREIMAGE_UNEXPECTED",
        ),
        (
            "deployment",
            "WP06_SCORER_DEPLOYMENT_QA_GUARD_DOWNGRADE_MARKER_PREIMAGE_UNEXPECTED",
        ),
    )
    for case_name, error_marker in migration_cases:
        state = {"workspace_id": workspace_id, "migration_case": case_name}
        prepare = _compose_probe(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "-e",
            "WP06_SOURCE_LINKED_STATE=" + json.dumps(state),
            "migrator",
            "python",
            "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
            "qa-migration-downgrade-prepare",
        )
        state.update(json.loads(prepare.stdout.strip().splitlines()[-1]))
        failed = _compose_expected_failure(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "55c504fe5b86",
        )
        output = failed.stdout + failed.stderr
        assert failed.returncode != 0
        assert error_marker in output, (case_name, output[-6000:])
        restored = _compose_probe(
            root,
            project,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "-e",
            "WP06_SOURCE_LINKED_STATE=" + json.dumps(state),
            "migrator",
            "python",
            "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
            "qa-migration-downgrade-check-restore",
        )
        result = json.loads(restored.stdout.strip().splitlines()[-1])
        assert result["migration_case"] == case_name
        assert result["migration_error_marker"] == error_marker
        for flag in (
            "migration_downgrade_rolled_back",
            "migration_revision_preserved",
            "migration_drift_survived_rollback",
            "migration_body_restored",
        ):
            assert result[flag] is True


def _compose_expected_failure(root, project, environment, *args):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--profile",
            "learning",
            "--profile",
            "learning-proof",
            "--profile",
            "signal-producer",
            "--project-name",
            project,
            "-f",
            "compose.yaml",
            *args,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return result


def _lineage_contract(root, project, environment, volume, workspace_id):
    output = _compose_probe(
        root,
        project,
        environment,
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        volume,
        "-e",
        "WP06_SOURCE_LINKED_STATE=" + json.dumps({"workspace_id": workspace_id}),
        "migrator",
        "python",
        "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
        "lineage-contract",
    )
    return json.loads(output.stdout.strip().splitlines()[-1])


def _migration_hashes(root: Path, image_side: str) -> dict[str, str]:
    migration_files = (
        "f9c1e3a5b7d9_wp06_trainer_source_trial_lineage.py",
        "a4c6e8f0b2d4_wp06_scorer_test_entry_hashes.py",
        "b3d5f7a9c1e3_wp06_scorer_test_gateway_return_types.py",
    )
    expected = {}
    for filename in migration_files:
        spec = importlib.util.spec_from_file_location(
            filename, root / "migrations/versions" / filename
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected.update(
            {
                signature: hashes[image_side]
                for signature, hashes in module.FUNCTION_DEFINITION_HASHES.items()
            }
        )
    return expected


def _function_definition_hashes(root, project, environment, volume, workspace_id):
    output = _compose_probe(
        root,
        project,
        environment,
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        volume,
        "-e",
        "WP06_SOURCE_LINKED_STATE=" + json.dumps({"workspace_id": workspace_id}),
        "migrator",
        "python",
        "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
        "function-definition-hashes",
    )
    return json.loads(output.stdout.strip().splitlines()[-1])


def _migration_body_guard(root, project, environment, volume, workspace_id):
    output = _compose_probe(
        root,
        project,
        environment,
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        volume,
        "-e",
        "WP06_SOURCE_LINKED_STATE=" + json.dumps({"workspace_id": workspace_id}),
        "migrator",
        "python",
        "/e2e-tests/e2e/probes/wp06_source_linked_scorer_test.py",
        "migration-body-guard",
    )
    return json.loads(output.stdout.strip().splitlines()[-1])
