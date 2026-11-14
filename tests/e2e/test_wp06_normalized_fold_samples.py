"""PostgreSQL Compose xác minh persist normalized rows và quyền builder-only."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp06_normalized_samples_are_append_only_and_builder_only() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-normalized-{uuid.uuid4().hex[:10]}"
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

    def compose(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if (result.returncode == 0) is not success:
            raise AssertionError(
                f"Compose {args} failed.\nstdout:\n{result.stdout[-12000:]}"
                f"\nstderr:\n{result.stderr[-12000:]}"
            )
        return result

    ensure_image_available(root, image)
    probe = "/e2e-tests/e2e/probes/wp06_normalized_fold_samples.py"
    probe_mount = f"{root / 'tests'}:/e2e-tests:ro"

    def run_probe(service: str, phase: str, *extra: str) -> dict[str, object]:
        output = compose(
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            probe_mount,
            "-e",
            f"WP06_NORMALIZED_PHASE={phase}",
            *extra,
            service,
            "python",
            probe,
        )
        return json.loads(output.stdout.strip().splitlines()[-1])

    def run_probe_failure(
        service: str, phase: str, *extra: str
    ) -> subprocess.CompletedProcess[str]:
        return compose(
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            probe_mount,
            "-e",
            f"WP06_NORMALIZED_PHASE={phase}",
            *extra,
            service,
            "python",
            probe,
            success=False,
        )

    def run_builder_then_revoke_under_lock(
        *, workspace_id: object, manifest_hash: object, source_id: object
    ) -> tuple[dict[str, object], dict[str, object], float]:
        command = base + [
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            probe_mount,
            "-e",
            "WP06_NORMALIZED_PHASE=builder",
            "-e",
            f"WP06_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_MANIFEST_HASH={manifest_hash}",
            "-e",
            "WP06_BUILDER_HOLD_SOURCE_LOCK_SECONDS=4",
            "candidate-proof-builder",
            "python",
            probe,
        ]
        process = subprocess.Popen(
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
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 120)
            assert readable, "Candidate builder không phát trạng thái lock-held đúng hạn."
            sentinel = process.stdout.readline().strip()
            assert sentinel == "WP06_SOURCE_LOCK_HELD", sentinel
            started_at = time.monotonic()
            revoked = run_probe(
                "admin-web",
                "source-rights-revoke",
                "-e",
                f"WP06_SOURCE_WORKSPACE_ID={workspace_id}",
                "-e",
                f"WP06_SOURCE_ID={source_id}",
            )
            blocked_seconds = time.monotonic() - started_at
            stdout, stderr = process.communicate(timeout=120)
            assert process.returncode == 0, f"Builder lỗi. stdout={stdout}\nstderr={stderr}"
            builder = json.loads(stdout.strip().splitlines()[-1])
            assert blocked_seconds >= 2.0, (
                "Policy writer không chờ candidate builder nhả shared source lock; "
                f"chỉ chờ {blocked_seconds:.3f}s."
            )
            return builder, revoked, blocked_seconds
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=30)

    def run_evaluator_then_revoke_under_lock(
        *, workspace_id: object, manifest_hash: object, source_id: object, run_id: object
    ) -> tuple[dict[str, object], dict[str, object], float]:
        command = base + [
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            probe_mount,
            "-e",
            "WP06_NORMALIZED_PHASE=source-evaluation-open",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_SOURCE_MANIFEST_HASH={manifest_hash}",
            "-e",
            f"WP06_EVALUATION_RUN_ID={run_id}",
            "-e",
            "WP06_CONSUMER_HOLD_SOURCE_LOCK_SECONDS=4",
            "learning-evaluator",
            "python",
            probe,
        ]
        process = subprocess.Popen(
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
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 120)
            assert readable, "Evaluator không phát trạng thái source lock-held đúng hạn."
            sentinel = process.stdout.readline().strip()
            assert sentinel == "WP06_CONSUMER_SOURCE_LOCK_HELD", sentinel
            started_at = time.monotonic()
            revoked = run_probe(
                "admin-web",
                "source-rights-revoke",
                "-e",
                f"WP06_SOURCE_WORKSPACE_ID={workspace_id}",
                "-e",
                f"WP06_SOURCE_ID={source_id}",
            )
            blocked_seconds = time.monotonic() - started_at
            stdout, stderr = process.communicate(timeout=120)
            assert process.returncode == 0, f"Evaluator lỗi. stdout={stdout}\nstderr={stderr}"
            evaluator = json.loads(stdout.strip().splitlines()[-1])
            assert blocked_seconds >= 2.0, (
                "Policy writer không chờ evaluator commit receipt source; "
                f"chỉ chờ {blocked_seconds:.3f}s."
            )
            return evaluator, revoked, blocked_seconds
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=30)

    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        assert heads.stdout.count("(head)") == 1
        assert "c7e9a1b3d5f8 (head)" in heads.stdout
        assert "No new upgrade operations detected" in check.stdout + check.stderr
        compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "b2c4e6f8a0d3",
        )
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")

        # Vai trò migrator tạo anchor synthetic; UI E2E kiểm role admin thật.
        seed = run_probe("migrator", "admin")
        assert set(seed) == {"workspace_id", "manifest_hash"}
        builder = run_probe(
            "candidate-proof-builder",
            "builder",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
        )
        assert builder == {
            "normalized_payloads": 3,
            "fold_assignments": 3,
            "holdout_excluded": True,
            "idempotent_retry": True,
            "workspace_rls": True,
            "builder_only_acl": True,
            "derived_profile_trigger": True,
            "unscoped_zero_rows": True,
        }
        assert run_probe("admin-web", "denied") == {"trainer_payload_select_denied": True}
        assert run_probe("trainer-worker", "denied") == {"trainer_payload_select_denied": True}
        attempts = run_probe(
            "admin-web",
            "attempt-admin",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
        )
        assert attempts["idempotent_retry"] is True
        assert attempts["conflict_rejected"] is True
        assert attempts["trial_reuse_rejected"] is True
        assert attempts["budget_rejected"] is True
        assert attempts["database_count_guard"] is True
        assert attempts["initial_event_required"] is True
        assert attempts["recovery_attempt_id"]
        pre_receipt = run_probe(
            "trainer-worker",
            "pre-receipt-recovery",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
        )
        assert pre_receipt["lease_requeued_before_receipt"] is True
        assert pre_receipt["generation_advanced"] is True
        training_attempt_id = str(pre_receipt["attempt_id"])
        remaining_attempt_id = str(
            attempts[
                "recovery_attempt_id"
                if training_attempt_id == attempts["attempt_id"]
                else "attempt_id"
            ]
        )
        training = compose(
            "run",
            "--rm",
            "--no-deps",
            "trainer-worker",
            "python",
            "-m",
            "trading_bot.cli.learning_trainer",
            "work-once",
            "--workspace-id",
            str(seed["workspace_id"]),
            "--worker-id",
            "e2e-trainer-worker",
        )
        training_result = json.loads(training.stdout.strip().splitlines()[-1])
        assert training_result["status"] == "succeeded"
        assert training_result["job_id"]
        assert training_result["recovered_jobs"] == 0
        assert training_result["duplicate"] is False
        assert training_result["attempt_id"] == training_attempt_id
        assert len(training_result["artifact_hash"]) == 64
        assert training_result["elapsed_milliseconds"] >= 0
        assert training_result["cpu_time_milliseconds"] >= 0
        assert training_result["peak_memory_bytes"] > 0
        assert training_result["actual_operation_units"] > 0
        assert run_probe(
            "admin-web",
            "source-run-rights-verify",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            "WP06_ACCESS_KIND=training",
            "-e",
            f"WP06_ACCESS_ID={training_attempt_id}",
            "-e",
            "WP06_EXPECTED_SOURCE_RECEIPTS=0",
        ) == {
            "gate_execute_scope": True,
            "run_source_rights_metadata_only": True,
            "workspace_isolation": True,
        }
        receipt = run_probe(
            "admin-web",
            "training-receipt",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={training_attempt_id}",
        )
        assert receipt == {"access_receipt_auditable": True, "holdout_not_recorded": True}
        lifecycle = run_probe(
            "trainer-worker",
            "attempt-trainer",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={training_attempt_id}",
            "-e",
            f"WP06_REJECTED_ATTEMPT_ID={attempts['rejected_attempt_id']}",
        )
        assert lifecycle["event_sequence_valid"] is True
        assert lifecycle["terminal_cost_persisted"] is True
        assert lifecycle["elapsed_milliseconds"] == training_result["elapsed_milliseconds"]
        assert lifecycle["cpu_time_milliseconds"] == training_result["cpu_time_milliseconds"]
        assert lifecycle["peak_memory_bytes"] == training_result["peak_memory_bytes"]
        assert lifecycle["actual_operation_units"] == training_result["actual_operation_units"]
        assert lifecycle["artifact_hash"] == training_result["artifact_hash"]
        assert lifecycle["artifact_persisted"] is True
        assert lifecycle["artifact_checksum_valid"] is True
        assert lifecycle["artifact_scorer_abstains"] is True
        assert lifecycle["resource_limit_recorded"] is True
        assert lifecycle["workspace_rls"] is True
        assert lifecycle["trainer_artifact_insert_only_acl"] is True
        assert lifecycle["job_success_fenced_and_persisted"] is True

        post_receipt = run_probe(
            "trainer-worker",
            "post-receipt-recovery",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={remaining_attempt_id}",
        )
        assert post_receipt["receipt_consumed_attempt_failed"] is True
        assert post_receipt["consumed_batch_not_reissued"] is True
        cancel_attempt_id = next(
            attempt_id
            for attempt_id in {
                str(attempts["attempt_id"]),
                str(attempts["recovery_attempt_id"]),
                str(attempts["cancel_attempt_id"]),
            }
            if attempt_id not in {training_attempt_id, str(post_receipt["attempt_id"])}
        )

        cancel_request = run_probe(
            "admin-web",
            "cancel-queued-job",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={cancel_attempt_id}",
            "-e",
            f"WP06_MEMBERSHIP_ID={attempts['membership_id']}",
        )
        assert cancel_request == {"cancel_request_recorded": True}
        cancellation = compose(
            "run",
            "--rm",
            "--no-deps",
            "trainer-worker",
            "python",
            "-m",
            "trading_bot.cli.learning_trainer",
            "work-once",
            "--workspace-id",
            str(seed["workspace_id"]),
            "--worker-id",
            "e2e-cancel-worker",
        )
        cancellation_result = json.loads(cancellation.stdout.strip().splitlines()[-1])
        assert cancellation_result["status"] == "cancelled"
        assert cancellation_result["cancelled_jobs"] == 1
        assert run_probe(
            "admin-web",
            "verify-cancelled-job",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={cancel_attempt_id}",
        ) == {"cancelled_event_persisted": True, "receipt_not_created": True}

        running_attempt = run_probe(
            "admin-web",
            "create-training-attempt",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
            "-e",
            "WP06_TRIAL_INDEX=5",
        )
        running_cancel_attempt_id = str(running_attempt["attempt_id"])
        running_worker = subprocess.Popen(
            base
            + [
                "run",
                "--rm",
                "--no-deps",
                "--volume",
                probe_mount,
                "-e",
                "WP06_NORMALIZED_PHASE=running-worker",
                "-e",
                f"WP06_WORKSPACE_ID={seed['workspace_id']}",
                "-e",
                f"WP06_ATTEMPT_ID={running_cancel_attempt_id}",
                "trainer-worker",
                "python",
                probe,
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            running_deadline = time.monotonic() + 30
            while True:
                if running_worker.poll() is not None:
                    stdout, stderr = running_worker.communicate()
                    raise AssertionError(
                        "Worker chạy thử kết thúc trước khi nhận hủy."
                        f"\nstdout:\n{stdout[-6000:]}\nstderr:\n{stderr[-6000:]}"
                    )
                running_state = run_probe(
                    "admin-web",
                    "running-cancel-status",
                    "-e",
                    f"WP06_WORKSPACE_ID={seed['workspace_id']}",
                    "-e",
                    f"WP06_ATTEMPT_ID={running_cancel_attempt_id}",
                )
                if running_state == {"status": "running", "receipt_created": True}:
                    break
                if time.monotonic() >= running_deadline:
                    raise AssertionError(
                        f"Worker không vào running trước deadline: {running_state}"
                    )
                time.sleep(0.2)

            assert run_probe(
                "admin-web",
                "cancel-running-job",
                "-e",
                f"WP06_WORKSPACE_ID={seed['workspace_id']}",
                "-e",
                f"WP06_ATTEMPT_ID={running_cancel_attempt_id}",
                "-e",
                f"WP06_MEMBERSHIP_ID={attempts['membership_id']}",
            ) == {"cancel_request_recorded": True}
            worker_stdout, worker_stderr = running_worker.communicate(timeout=60)
            assert running_worker.returncode == 0, (
                f"Worker hủy lỗi.\nstdout:\n{worker_stdout[-6000:]}"
                f"\nstderr:\n{worker_stderr[-6000:]}"
            )
            running_cancel_result = json.loads(worker_stdout.strip().splitlines()[-1])
            assert running_cancel_result["status"] == "cancelled"
            assert run_probe(
                "admin-web",
                "verify-running-cancelled-job",
                "-e",
                f"WP06_WORKSPACE_ID={seed['workspace_id']}",
                "-e",
                f"WP06_ATTEMPT_ID={running_cancel_attempt_id}",
            ) == {
                "cancelled_event_persisted": True,
                "receipt_preserved": True,
                "artifact_not_created": True,
            }
        finally:
            if running_worker.poll() is None:
                running_worker.terminate()
                try:
                    running_worker.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    running_worker.kill()
                    running_worker.communicate(timeout=10)

        environment["TRADING_BOT_LEARNING_TRAINING_WORKSPACE_ID"] = str(seed["workspace_id"])
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "trainer-worker")
        try:
            assert run_probe("trainer-worker", "runtime-resource-limits") == {
                "cpu_millicores": 1000,
                "memory_limit_bytes": 1_073_741_824,
                "pids_limit": 64,
            }
            compose("restart", "--timeout", "10", "trainer-worker")
            service_attempt = run_probe(
                "admin-web",
                "create-training-attempt",
                "-e",
                f"WP06_WORKSPACE_ID={seed['workspace_id']}",
                "-e",
                f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
                "-e",
                "WP06_TRIAL_INDEX=6",
            )
            service_attempt_id = str(service_attempt["attempt_id"])
            service_deadline = time.monotonic() + 60
            while True:
                service_state = run_probe(
                    "admin-web",
                    "training-job-status",
                    "-e",
                    f"WP06_WORKSPACE_ID={seed['workspace_id']}",
                    "-e",
                    f"WP06_ATTEMPT_ID={service_attempt_id}",
                )
                if service_state["status"] == "succeeded":
                    break
                if service_state["status"] in {"failed", "cancelled"}:
                    raise AssertionError(f"Trainer service kết thúc ngoài dự kiến: {service_state}")
                if time.monotonic() >= service_deadline:
                    raise AssertionError(f"Trainer service không xử lý queue: {service_state}")
                time.sleep(0.5)
        finally:
            compose("stop", "--timeout", "10", "trainer-worker")
        assert service_state["status"] == "succeeded"
        assert service_state["event_state"] == "succeeded"
        assert service_state["artifact_created"] is True
        assert service_state["artifact_id"]

        job_downgrade = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "f9b1d3e5a7c9",
            success=False,
        )
        # Artifact scorer là guard đầu tiên khi downgrade từ head sau quy trình hoàn chỉnh.
        assert "WP06_SCORER_ARTIFACT_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
            job_downgrade.stdout + job_downgrade.stderr
        )
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

        retry = compose(
            "run",
            "--rm",
            "--no-deps",
            "trainer-worker",
            "python",
            "-m",
            "trading_bot.cli.learning_trainer",
            "run",
            "--workspace-id",
            str(seed["workspace_id"]),
            "--attempt-id",
            training_attempt_id,
        )
        retry_result = json.loads(retry.stdout.strip().splitlines()[-1])
        assert retry_result["duplicate"] is True
        assert retry_result["artifact_hash"] == training_result["artifact_hash"]
        assert retry_result["artifact_id"] == training_result["artifact_id"]

        rejected_model_attempt = run_probe(
            "admin-web",
            "create-training-attempt",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
            "-e",
            "WP06_TRIAL_INDEX=7",
        )
        reject_model_training = compose(
            "run",
            "--rm",
            "--no-deps",
            "trainer-worker",
            "python",
            "-m",
            "trading_bot.cli.learning_trainer",
            "run",
            "--workspace-id",
            str(seed["workspace_id"]),
            "--attempt-id",
            str(rejected_model_attempt["attempt_id"]),
        )
        reject_model_training_result = json.loads(
            reject_model_training.stdout.strip().splitlines()[-1]
        )
        assert reject_model_training_result["status"] == "succeeded"

        report = run_probe(
            "learning-evaluator",
            "registry-report",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
        )
        # Observer shadow được kiểm tra end-to-end trong Task9; tại đây kiểm vòng đời registry.
        registry = run_probe(
            "admin-web",
            "model-registry-lifecycle",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            "WP06_ARTIFACT_IDS="
            f"{training_result['artifact_id']},{service_state['artifact_id']},"
            f"{reject_model_training_result['artifact_id']}",
            "-e",
            f"WP06_REPORT_ID={report['report_id']}",
            "-e",
            f"WP06_REPORT_HASH={report['report_hash']}",
        )
        assert registry == {
            "register_idempotency_and_conflict": True,
            "diagnostic_report_lineage_checked": True,
            "stale_revision_and_pointer_rejected": True,
            "approval_rejection_and_no_live_state": True,
            "activation_replacement_rollback_cas": True,
            "active_retire_rejected": True,
            "append_only_events_and_row_immutability": True,
            "workspace_isolation": True,
        }

        downgrade = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "d3f5a7c9e1b2",
            success=False,
        )
        assert "WP06_SCORER_ARTIFACT_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
            downgrade.stdout + downgrade.stderr
        )
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

        # Typed source rights are checked by the trusted builder, receipted
        # atomically, and rechecked on duplicate retry after policy revocation.
        source = run_probe("admin-web", "source-admin-seed")
        item = run_probe(
            "ingestion-worker",
            "source-item-seed",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_ID={source['source_id']}",
            "-e",
            f"WP06_SOURCE_ITEM_ID={source['item_id']}",
            "-e",
            f"WP06_SOURCE_REVISION_ID={source['revision_id']}",
        )
        assert item["item_revision_id"] == source["revision_id"]
        assert item["rights_events_processed"] >= 4
        assert item["rights_backlog_empty"] is True
        source_manifest = run_probe(
            "migrator",
            "source-manifest-seed",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_ID={source['source_id']}",
            "-e",
            f"WP06_SOURCE_ITEM_ID={source['item_id']}",
            "-e",
            f"WP06_SOURCE_REVISION_ID={source['revision_id']}",
            "-e",
            f"WP06_SOURCE_POLICY_ID={source['policy_id']}",
            "-e",
            f"WP06_SOURCE_POLICY_SEQUENCE={source['policy_sequence']}",
            "-e",
            f"WP06_SOURCE_RIGHTS_HASH={source['rights_hash']}",
        )
        source_builder, revoked, writer_wait_seconds = run_builder_then_revoke_under_lock(
            workspace_id=source_manifest["workspace_id"],
            manifest_hash=source_manifest["manifest_hash"],
            source_id=source["source_id"],
        )
        assert source_builder["source_training_receipt_count"] == 1
        assert source_builder["source_receipt_metadata_only"] is True
        assert revoked == {"revoked": True, "observed": True}
        assert writer_wait_seconds >= 2.0
        assert run_probe(
            "migrator",
            "source-receipt-immutable",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={source['workspace_id']}",
        ) == {"receipt_update_trigger_immutable": True}
        for role_service, expected_role in (
            ("admin-web", "crypidian_admin_web"),
            ("trainer-worker", "crypidian_trainer_worker"),
        ):
            assert run_probe(role_service, "source-receipt-denied") == {
                "receipt_access_denied_for": expected_role,
                "app_receipt_access_denied": True,
            }

        revocation_batch = run_probe(
            "ingestion-worker",
            "source-rights-consume",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={source['workspace_id']}",
        )
        assert revocation_batch["rights_events_processed"] >= 1
        assert revocation_batch["rights_backlog_empty"] is True
        stale_retry = run_probe_failure(
            "candidate-proof-builder",
            "builder",
            "-e",
            f"WP06_WORKSPACE_ID={source_manifest['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={source_manifest['manifest_hash']}",
        )
        assert "Source TRAINING rights bị từ chối" in stale_retry.stdout + stale_retry.stderr
        unchanged = run_probe(
            "candidate-proof-builder",
            "source-rights-verify-unchanged",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_MANIFEST_HASH={source_manifest['manifest_hash']}",
        )
        assert unchanged == {
            "receipt_unchanged": True,
            "normalized_sample_count_unchanged": True,
        }
        source_downgrade = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "e1a2c4e6f8b0",
            success=False,
        )
        assert "WP06_SCORER_ARTIFACT_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
            source_downgrade.stdout + source_downgrade.stderr
        )
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")

        # Consumer gateways phải kiểm quyền hiện hành dưới lock và lưu biên nhận theo run.
        consumer_source = run_probe("admin-web", "source-admin-seed")
        consumer_item = run_probe(
            "ingestion-worker",
            "source-item-seed",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_ID={consumer_source['source_id']}",
            "-e",
            f"WP06_SOURCE_ITEM_ID={consumer_source['item_id']}",
            "-e",
            f"WP06_SOURCE_REVISION_ID={consumer_source['revision_id']}",
        )
        assert consumer_item["rights_backlog_empty"] is True
        consumer_manifest = run_probe(
            "migrator",
            "source-manifest-seed",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_ID={consumer_source['source_id']}",
            "-e",
            f"WP06_SOURCE_ITEM_ID={consumer_source['item_id']}",
            "-e",
            f"WP06_SOURCE_REVISION_ID={consumer_source['revision_id']}",
            "-e",
            f"WP06_SOURCE_POLICY_ID={consumer_source['policy_id']}",
            "-e",
            f"WP06_SOURCE_POLICY_SEQUENCE={consumer_source['policy_sequence']}",
            "-e",
            f"WP06_SOURCE_RIGHTS_HASH={consumer_source['rights_hash']}",
        )
        consumer_builder = run_probe(
            "candidate-proof-builder",
            "builder",
            "-e",
            f"WP06_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={consumer_manifest['manifest_hash']}",
        )
        assert consumer_builder["source_training_receipt_count"] == 1
        training_attempt = run_probe(
            "admin-web",
            "create-training-attempt",
            "-e",
            f"WP06_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={consumer_manifest['manifest_hash']}",
            "-e",
            "WP06_TRIAL_INDEX=1",
        )
        training_run = compose(
            "run",
            "--rm",
            "--no-deps",
            "trainer-worker",
            "python",
            "-m",
            "trading_bot.cli.learning_trainer",
            "work-once",
            "--workspace-id",
            str(consumer_manifest["workspace_id"]),
            "--worker-id",
            "e2e-source-rights-worker",
        )
        training_result = json.loads(training_run.stdout.strip().splitlines()[-1])
        assert training_result["status"] == "succeeded"
        assert training_result["attempt_id"] == training_attempt["attempt_id"]
        assert run_probe(
            "admin-web",
            "source-run-rights-verify",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            "WP06_ACCESS_KIND=training",
            "-e",
            f"WP06_ACCESS_ID={training_attempt['attempt_id']}",
        ) == {
            "gate_execute_scope": True,
            "run_source_rights_metadata_only": True,
            "workspace_isolation": True,
        }

        evaluation_run_id = uuid.uuid4()
        opened, consumer_revoked, consumer_lock_wait = run_evaluator_then_revoke_under_lock(
            workspace_id=consumer_manifest["workspace_id"],
            manifest_hash=consumer_manifest["manifest_hash"],
            source_id=consumer_source["source_id"],
            run_id=evaluation_run_id,
        )
        assert opened == {"evaluation_access_opened": True, "receipt_committed": True}
        assert consumer_revoked == {"revoked": True, "observed": True}
        assert consumer_lock_wait >= 2.0
        assert run_probe(
            "admin-web",
            "source-run-rights-verify",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            "WP06_ACCESS_KIND=evaluation",
            "-e",
            f"WP06_ACCESS_ID={evaluation_run_id}",
        ) == {
            "gate_execute_scope": True,
            "run_source_rights_metadata_only": True,
            "workspace_isolation": True,
        }
        assert run_probe(
            "migrator",
            "source-run-rights-immutable",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            f"WP06_ACCESS_ID={evaluation_run_id}",
        ) == {"run_source_rights_immutable": True}

        pending_evaluation_id = uuid.uuid4()
        pending_denial = run_probe_failure(
            "learning-evaluator",
            "source-evaluation-open",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            f"WP06_SOURCE_MANIFEST_HASH={consumer_manifest['manifest_hash']}",
            "-e",
            f"WP06_EVALUATION_RUN_ID={pending_evaluation_id}",
        )
        assert "WP06_SOURCE_TRAINING_RIGHTS_INVALIDATED" in (
            pending_denial.stdout + pending_denial.stderr
        )
        assert run_probe(
            "admin-web",
            "source-run-rights-verify",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            "WP06_ACCESS_KIND=evaluation",
            "-e",
            f"WP06_ACCESS_ID={pending_evaluation_id}",
            "-e",
            "WP06_EXPECTED_SOURCE_RECEIPTS=0",
        ) == {
            "gate_execute_scope": True,
            "run_source_rights_metadata_only": True,
            "workspace_isolation": True,
        }
        consumer_revocation_batch = run_probe(
            "ingestion-worker",
            "source-rights-consume",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
        )
        assert consumer_revocation_batch["rights_events_processed"] >= 1
        denied_attempt = run_probe(
            "admin-web",
            "create-training-attempt",
            "-e",
            f"WP06_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={consumer_manifest['manifest_hash']}",
            "-e",
            "WP06_TRIAL_INDEX=2",
        )
        trainer_denial = run_probe_failure(
            "trainer-worker",
            "source-training-claim",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            f"WP06_ATTEMPT_ID={denied_attempt['attempt_id']}",
        )
        assert "WP06_SOURCE_TRAINING_RIGHTS_INVALIDATED" in (
            trainer_denial.stdout + trainer_denial.stderr
        )
        assert run_probe(
            "admin-web",
            "source-run-rights-verify",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={consumer_manifest['workspace_id']}",
            "-e",
            "WP06_ACCESS_KIND=training",
            "-e",
            f"WP06_ACCESS_ID={denied_attempt['attempt_id']}",
            "-e",
            "WP06_EXPECTED_SOURCE_RECEIPTS=0",
        ) == {
            "gate_execute_scope": True,
            "run_source_rights_metadata_only": True,
            "workspace_isolation": True,
        }
        retention_source = run_probe("admin-web", "source-admin-seed")
        retention_item = run_probe(
            "ingestion-worker",
            "source-item-seed",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={retention_source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_ID={retention_source['source_id']}",
            "-e",
            f"WP06_SOURCE_ITEM_ID={retention_source['item_id']}",
            "-e",
            f"WP06_SOURCE_REVISION_ID={retention_source['revision_id']}",
            "-e",
            "WP06_SOURCE_RETENTION_SECONDS=45",
        )
        assert retention_item["rights_backlog_empty"] is True
        assert retention_item["retention_expires_at"] is not None
        retention_manifest = run_probe(
            "migrator",
            "source-manifest-seed",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={retention_source['workspace_id']}",
            "-e",
            f"WP06_SOURCE_ID={retention_source['source_id']}",
            "-e",
            f"WP06_SOURCE_ITEM_ID={retention_source['item_id']}",
            "-e",
            f"WP06_SOURCE_REVISION_ID={retention_source['revision_id']}",
            "-e",
            f"WP06_SOURCE_POLICY_ID={retention_source['policy_id']}",
            "-e",
            f"WP06_SOURCE_POLICY_SEQUENCE={retention_source['policy_sequence']}",
            "-e",
            f"WP06_SOURCE_RIGHTS_HASH={retention_source['rights_hash']}",
        )
        retention_builder = run_probe(
            "candidate-proof-builder",
            "builder",
            "-e",
            f"WP06_WORKSPACE_ID={retention_manifest['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={retention_manifest['manifest_hash']}",
        )
        assert retention_builder["source_training_receipt_count"] == 1
        retention_expiry = datetime.fromisoformat(retention_item["retention_expires_at"])
        time.sleep(max(0.0, (retention_expiry - datetime.now(UTC)).total_seconds() + 0.2))
        retention_run_id = uuid.uuid4()
        expired_retention = run_probe_failure(
            "learning-evaluator",
            "source-evaluation-open",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={retention_manifest['workspace_id']}",
            "-e",
            f"WP06_SOURCE_MANIFEST_HASH={retention_manifest['manifest_hash']}",
            "-e",
            f"WP06_EVALUATION_RUN_ID={retention_run_id}",
        )
        assert "WP06_SOURCE_TRAINING_RIGHTS_ITEM_DENIED" in (
            expired_retention.stdout + expired_retention.stderr
        )
        assert run_probe(
            "admin-web",
            "source-run-rights-verify",
            "-e",
            f"WP06_SOURCE_WORKSPACE_ID={retention_manifest['workspace_id']}",
            "-e",
            "WP06_ACCESS_KIND=evaluation",
            "-e",
            f"WP06_ACCESS_ID={retention_run_id}",
            "-e",
            "WP06_EXPECTED_SOURCE_RECEIPTS=0",
        ) == {
            "gate_execute_scope": True,
            "run_source_rights_metadata_only": True,
            "workspace_isolation": True,
        }
        run_rights_downgrade = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            "f0b2d4e6a8c3",
            success=False,
        )
        assert "WP06_SCORER_ARTIFACT_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
            run_rights_downgrade.stdout + run_rights_downgrade.stderr
        )
        assert run_probe(
            "admin-web",
            "training-admin-lifecycle",
            "-e",
            f"WP06_WORKSPACE_ID={seed['workspace_id']}",
            "-e",
            f"WP06_MANIFEST_HASH={seed['manifest_hash']}",
        ) == {
            "fold_exactly_approved": True,
            "metadata_only_admin_query": True,
            "admin_payload_column_denied": True,
            "enqueue_idempotent": True,
            "cancel_actor_verified": True,
        }
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
