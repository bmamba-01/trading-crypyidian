"""Compose E2E cho hành trình learning tích hợp WP06 Task9."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from scripts.docker_image import build_image_reference, ensure_image_available

TASK9_PROBE = "/e2e-tests/e2e/probes/wp06_task9_integrated_learning_journey.py"
CANDIDATE_PROBE = "/e2e-tests/e2e/probes/wp06_candidate_universe_proof.py"


def test_wp06_task9_integrated_learning_journey_uses_real_training_and_paper_lineage() -> None:
    root = Path(__file__).parents[2]
    probe_path = root / "tests/e2e/probes/wp06_task9_integrated_learning_journey.py"
    assert probe_path.is_file(), "Task9 cần probe Compose tích hợp trước khi chạy journey."

    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-task9-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--profile",
        "learning-proof",
        "--profile",
        "learning",
        "--profile",
        "signal-producer",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]
    workspace_id = str(uuid.uuid4())
    state: dict[str, Any] = {"workspace_id": workspace_id}
    tests_mount = f"{root / 'tests'}:/e2e-tests:ro"

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
            timeout=360,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Compose lệnh {args!r} thất bại:\n"
                f"stdout đầu:\n{result.stdout[:3500]}\nstdout cuối:\n{result.stdout[-8000:]}\n"
                f"stderr đầu:\n{result.stderr[:3500]}\nstderr cuối:\n{result.stderr[-8000:]}"
            )
        return result

    def run_probe(
        service: str,
        phase: str,
        *,
        state_value: dict[str, Any] | None = None,
        candidate: bool = False,
    ) -> dict[str, Any]:
        probe = CANDIDATE_PROBE if candidate else TASK9_PROBE
        state_key = "WP06_STATE" if candidate else "WP06_TASK9_STATE"
        payload = json.dumps(state_value if state_value is not None else state)
        output = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--volume",
            tests_mount,
            "-e",
            "CRYPIDIAN_E2E_WORKSPACE_ID=" + workspace_id,
            "-e",
            state_key + "=" + payload,
            service,
            "python",
            probe,
            phase,
        )
        lines = output.stdout.strip().splitlines()
        assert lines, f"Probe {phase} không trả kết quả JSON: {output.stderr[-4000:]}"
        return json.loads(lines[-1])

    def run_observer_then_activate_challenger() -> tuple[dict[str, Any], dict[str, Any], float]:
        command = base + [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--volume",
            tests_mount,
            "-e",
            "CRYPIDIAN_E2E_WORKSPACE_ID=" + workspace_id,
            "-e",
            "WP06_TASK9_STATE=" + json.dumps(state),
            "-e",
            "WP06_TASK9_HOLD_POINTER_LOCK_SECONDS=4",
            "learning-shadow-observer",
            "python",
            TASK9_PROBE,
            "observe-hold-pointer",
        ]
        observer = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        try:
            assert observer.stdout is not None
            readable, _, _ = select.select([observer.stdout], [], [], 120)
            assert readable, "Observer không báo đã giữ active-pointer lock đúng hạn."
            sentinel = observer.stdout.readline().strip()
            assert sentinel == "WP06_OBSERVER_ACTIVE_POINTER_LOCK_HELD", sentinel

            started_at = time.monotonic()
            activation = run_probe("admin-web", "activate-challenger")
            blocked_seconds = time.monotonic() - started_at
            stdout, stderr = observer.communicate(timeout=120)
            assert observer.returncode == 0, f"Observer lỗi. stdout={stdout}\nstderr={stderr}"
            lines = stdout.strip().splitlines()
            assert lines, f"Observer không trả JSON: {stderr[-4000:]}"
            observations = json.loads(lines[-1])
            assert blocked_seconds >= 2.0, (
                "Thay đổi active pointer không chờ observer commit; "
                f"chỉ chờ {blocked_seconds:.3f}s."
            )
            return observations, activation, blocked_seconds
        finally:
            if observer.poll() is None:
                observer.terminate()
                observer.communicate(timeout=30)

    def migrate(*args: str) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args)

    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", "head")
        migrate("check")
        heads = migrate("heads")
        assert heads.stdout.count("(head)") == 1, heads.stdout

        # Từng công đoạn dùng đúng role Compose; không tạo artifact hoặc report giả.
        seeded = run_probe("migrator", "seed", candidate=True)
        state.update(seeded)
        assert run_probe("migrator", "grant-training", candidate=True) == {"training_granted": True}
        assert run_probe("candidate-proof-builder", "builder", candidate=True) == {
            "trusted_builder": True
        }

        config = {
            "holdout_start": "2026-11-03T07:00:00+00:00",
            "evaluation_cutoff": "2026-11-03T07:00:00+00:00",
            "folds": [
                {
                    role: [
                        f"2026-11-03T0{hour}:00:00+00:00",
                        f"2026-11-03T0{hour + 1}:00:00+00:00",
                    ]
                    for role, hour in (("train", 4), ("validation", 5), ("test", 6))
                }
            ],
        }
        staged = compose(
            "run",
            "--rm",
            "--no-deps",
            "candidate-proof-builder",
            "python",
            "-m",
            "trading_bot.cli.learning_split_candidate",
            "stage",
            "--workspace-id",
            workspace_id,
            "--trial-record-id",
            seeded["trial_ids"][6],
            "--config-json",
            json.dumps(config),
        )
        staged_result = json.loads(staged.stdout.strip().splitlines()[-1])
        assert staged_result["action"] == "staged"
        state["candidate"] = staged_result
        state["config"] = config
        state.update(run_probe("admin-web", "approve-split", state_value=state))
        sealed = compose(
            "run",
            "--rm",
            "--no-deps",
            "candidate-proof-builder",
            "python",
            "-m",
            "trading_bot.cli.learning_split_candidate",
            "seal",
            "--workspace-id",
            workspace_id,
            "--candidate-id",
            staged_result["candidate_id"],
        )
        sealed_result = json.loads(sealed.stdout.strip().splitlines()[-1])
        assert sealed_result["duplicate"] is False
        state["sealed"] = sealed_result
        assert run_probe("migrator", "verify-sealed", state_value=state) == {
            "actual_candidate_proof": True,
            "reviewer_mfa_approval": True,
            "normalized_payload_sealed": True,
            "holdout_excluded": True,
        }

        paper_seed = run_probe("migrator", "seed-paper", state_value=state)
        state.update(paper_seed)
        attempts = run_probe("admin-web", "enqueue-training", state_value=state)
        assert len(attempts["attempt_ids"]) == 3
        state.update(attempts)
        artifacts = []
        for attempt_id in state["attempt_ids"]:
            output = compose(
                "run",
                "--rm",
                "--no-deps",
                "trainer-worker",
                "python",
                "-m",
                "trading_bot.cli.learning_trainer",
                "run",
                "--workspace-id",
                workspace_id,
                "--attempt-id",
                attempt_id,
            )
            result = json.loads(output.stdout.strip().splitlines()[-1])
            assert result["status"] == "succeeded"
            assert result["duplicate"] is False
            assert len(result["artifact_hash"]) == 64
            artifacts.append(result)
        state["artifacts"] = artifacts
        scorer_proof = run_probe("trainer-worker", "verify-scorer-artifacts", state_value=state)
        assert scorer_proof["scorer_artifact_count"] == 3
        assert scorer_proof["base_v1_unchanged_diagnostic_only"] is True
        assert scorer_proof["canonical_hash_and_lineage_verified"] is True
        assert scorer_proof["trainer_least_privilege_and_workspace_rls"] is True
        assert scorer_proof["base_v1_update_denied"] is True
        assert scorer_proof["denormalized_tampering_rejected"] is True
        assert scorer_proof["raw_outcome_and_opportunity_reads_denied"] is True
        state.update(scorer_proof)

        scorer_retry = run_probe(
            "trainer-worker", "verify-scorer-repository-retries", state_value=state
        )
        assert scorer_retry == {
            "identical_retry_is_duplicate": True,
            "changed_retry_conflicts": True,
        }

        retried = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "trainer-worker",
            "python",
            "-m",
            "trading_bot.cli.learning_trainer",
            "run",
            "--workspace-id",
            workspace_id,
            "--attempt-id",
            state["attempt_ids"][0],
        )
        retry_result = json.loads(retried.stdout.strip().splitlines()[-1])
        assert retry_result["duplicate"] is True
        assert retry_result["artifact_hash"] == artifacts[0]["artifact_hash"]
        assert run_probe("trainer-worker", "verify-scorer-artifacts", state_value=state) == (
            scorer_proof
        )

        trainer_permissions = run_probe("trainer-worker", "role-matrix", state_value=state)
        evaluator_permissions = run_probe("learning-evaluator", "role-matrix", state_value=state)
        assert trainer_permissions["observer_write_denied"] is True
        assert evaluator_permissions["observer_write_denied"] is True
        report = run_probe("learning-evaluator", "evaluate-fold", state_value=state)
        assert report["candidate_completeness_verified"] is False
        assert len(report["report_hash"]) == 64
        state.update(report)

        registry = run_probe("admin-web", "register-and-approve", state_value=state)
        state.update(registry)
        assert registry["artifact_ids"] == [item["artifact_id"] for item in artifacts[:2]]
        assert registry["human_approval_events"] == 2
        assert registry["all_artifacts_abstain_uncalibrated"] is True

        rights = run_probe("migrator", "grant-paper-demo-rights", state_value=state)
        state.update(rights)
        paper = run_probe("migrator", "seed-paper-market", state_value=state)
        state.update(paper)
        produced = run_probe("market-signal-worker", "produce-paper-decision", state_value=state)
        state.update(produced)
        assert produced["decision_count"] >= 1
        persisted_decisions = run_probe("migrator", "verify-paper-decisions", state_value=state)
        state.update(persisted_decisions)
        assert persisted_decisions["decision_count"] >= 3
        assert persisted_decisions["snapshot_count"] >= 3

        before_gate = run_probe("migrator", "execution-snapshot", state_value=state)
        scorer_gate = run_probe("admin-web", "verify-scorer-active-mode-gate", state_value=state)
        assert scorer_gate == {
            "rollback_restored_database_state": True,
            "disabled_retry_is_idempotent": True,
            "changed_disabled_intent_conflicts": True,
            "shadow_denied": True,
            "paper_gate_denied": True,
            "active_mode_denials_are_side_effect_free": True,
            "diagnostic_report_is_incomplete": True,
            "diagnostic_report_rejected_by_authoritative_gateway": True,
            "score_event_count_unchanged": True,
        }
        after_gate = run_probe("migrator", "execution-snapshot", state_value=state)
        assert after_gate["order_counts"] == before_gate["order_counts"]
        assert after_gate["training_job_count"] == before_gate["training_job_count"]
        assert after_gate["paper_decision_count"] == before_gate["paper_decision_count"]
        assert after_gate["score_event_count"] == before_gate["score_event_count"]
        assert after_gate["scoring_deployment_event_count"] == (
            before_gate["scoring_deployment_event_count"] + 1
        )
        assert after_gate["scoring_deployment_pointer"]["mode"] == "DISABLED"
        assert after_gate["scoring_deployment_pointer"]["revision"] == 1
        before = after_gate
        state["initial_execution_snapshot"] = before
        observed, activation, pointer_lock_wait = run_observer_then_activate_challenger()
        state["raced_pointer_version"] = activation["pointer_version"]
        assert observed["created_count"] >= 1
        assert observed["all_abstain"] is True
        assert observed["rights_verified"] is True
        assert activation["active_registry_id"] == state["registry_id"]
        assert activation["pointer_version"] == state["initial_pointer_version"] + 1
        assert pointer_lock_wait >= 2.0
        assert all(
            item["active_registry_id"] == state["incumbent_registry_id"]
            and item["active_pointer_version"] == state["initial_pointer_version"]
            and item["challenger_registry_id"] == state["registry_id"]
            and item["challenger_revision"] == 3
            for item in observed["observations"]
        ), "Observer phải ghim pointer cũ trước khi writer commit pointer mới."
        rights_receipt_mismatch = observed["rights_receipt_checks"]
        assert rights_receipt_mismatch["wrong_policy_source_rejected"] is True
        assert rights_receipt_mismatch["wrong_fingerprint_rejected"] is True
        assert rights_receipt_mismatch["observation_count_unchanged"] is True
        observer_permissions = run_probe(
            "learning-shadow-observer", "role-matrix", state_value=state
        )
        assert observer_permissions["normalized_payload_denied"] is True
        assert observer_permissions["holdout_denied"] is True
        assert observer_permissions["order_write_denied"] is True
        assert observer_permissions["risk_write_denied"] is True
        assert observer_permissions["pointer_write_denied"] is True
        assert observer_permissions["training_job_write_denied"] is True
        assert run_probe("learning-shadow-observer", "cross-workspace", state_value=state) == {
            "cross_workspace_hidden": True
        }
        assessed = run_probe("learning-shadow-observer", "assess-and-retry", state_value=state)
        assert assessed["assessment_status"] == "inconclusive"
        assert assessed["performance_status"] == "inconclusive"
        assert assessed["member_count"] == len(observed["observations"])
        assert assessed["reasons"]
        assert assessed["proposal_created"] is False
        assert (
            run_probe("learning-shadow-observer", "observe", state_value=state)["created_count"]
            == 0
        )
        after = run_probe("migrator", "execution-snapshot", state_value=state)
        assert after["order_counts"] == before["order_counts"]
        assert after["training_job_count"] == before["training_job_count"]
        assert after["pointer_version"] == activation["pointer_version"]
        assert after["pointer_registry_id"] == activation["active_registry_id"]

        rollback = run_probe("admin-web", "rollback-after-pointer-race", state_value=state)
        state.update(rollback)
        assert rollback["pointer_version"] == 3
        assert rollback["incumbent_revision"] > 4
        third = run_probe("admin-web", "register-third", state_value=state)
        state.update(third)
        second_observation = run_probe(
            "learning-shadow-observer", "observe-after-rollback", state_value=state
        )
        assert second_observation["created_count"] >= 1
        assert second_observation["rights_verified"] is True
        assert second_observation["pins_rolled_back_incumbent"] is True
        second_assessment = run_probe(
            "learning-shadow-observer", "assess-after-rollback", state_value=state
        )
        assert second_assessment["assessment_status"] == "inconclusive"
        assert second_assessment["proposal_created"] is False
        after_rollback_journey = run_probe("migrator", "execution-snapshot", state_value=state)
        assert after_rollback_journey["order_counts"] == before["order_counts"]
        assert after_rollback_journey["training_job_count"] == before["training_job_count"]
        assert after_rollback_journey["pointer_version"] == rollback["pointer_version"]
        state["post_rollback_execution_snapshot"] = after_rollback_journey
        assert run_probe("learning-shadow-observer", "restart-check", state_value=state) == {
            "ledger_survived_restart": True,
            "observation_retry_is_idempotent": True,
            "assessment_retry_is_idempotent": True,
        }
        restarted_snapshot = run_probe("migrator", "execution-snapshot", state_value=state)
        assert restarted_snapshot == after_rollback_journey

        revoked = run_probe("admin-web", "revoke-paper-demo-rights", state_value=state)
        assert revoked["state"] == "revoked"
        rights_denial = run_probe(
            "learning-shadow-observer", "assess-after-rights-revocation", state_value=state
        )
        assert rights_denial["current_rights_receipt_missing"] is True
        assert rights_denial["eligible_cohort_rejected"] is True
        assert rights_denial["assessment_count_unchanged"] is True
        assert rights_denial["eligible_member_count"] >= 1
        assert rights_denial["assessment_count_before"] >= 1
        assert rights_denial["assessment_count_after"] == rights_denial["assessment_count_before"]
        assert run_probe("migrator", "execution-snapshot", state_value=state) == restarted_snapshot

        migration_probe = subprocess.run(
            base
            + [
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "c4e6a8b0d2f3",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=360,
        )
        migration_output = migration_probe.stdout + migration_probe.stderr
        assert migration_probe.returncode != 0
        assert "WP06_SCORING_DEPLOYMENT_POPULATED_DOWNGRADE_DENIED" in migration_output
        migrate("check")
        heads = migrate("heads")
        assert heads.stdout.count("(head)") == 1, heads.stdout
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
