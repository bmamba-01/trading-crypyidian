"""Compose E2E cho transaction lưu bar và checkpoint của WP-07C."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys,types;p=json.load(sys.stdin);"
    "os.environ.update(WP07B_STATE=json.dumps(p['state']),"
    "WP07_IDENTITY_ONLY='1' if p['identity_only'] else '0',"
    "WP07_REVOKE_BEFORE_PERSIST='1' if p['revoke_before_persist'] else '0');"
    "m=types.ModuleType('wp07_runtime_replay_fixture');"
    "sys.modules[m.__name__]=m;"
    "exec(compile(p['runtime_fixture'],'runtime_replay_fixture.py','exec'),m.__dict__);"
    "exec(compile(p['code'],'wp07_backfill_persistence.py','exec'),{'__name__':'__main__'})"
)


def run_backfill_compose_probe(
    *, identity_only: bool, revoke_before_persist: bool = False
) -> dict[str, Any]:
    """Chạy probe WP07 trong PostgreSQL Compose tạm thời."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp07-backfill-db-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = ["docker", "compose", "--project-name", project, "-f", "compose.yaml"]

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
            timeout=300,
        )
        error_tail = "\n".join(result.stderr.splitlines()[-30:])
        assert result.returncode == 0, (args, result.stdout[-5000:], error_tail)
        return result

    probe = (root / "tests/e2e/probes/wp07_backfill_persistence.py").read_text(encoding="utf-8")
    runtime_fixture = (root / "tests/fixtures/runtime_replay.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "instrument_id": str(uuid.uuid4()),
        "now": datetime.now(UTC).isoformat(),
        "revoke_before_persist": revoke_before_persist,
        "runtime_fixture": runtime_fixture,
    }

    def run_probe() -> dict[str, Any]:
        payload = json.dumps(
            {
                "state": state,
                "code": probe,
                "runtime_fixture": runtime_fixture,
                "identity_only": identity_only,
                "revoke_before_persist": revoke_before_persist,
            }
        )
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
        lines = result.stdout.strip().splitlines()
        assert lines, result.stderr[-3000:]
        return cast(dict[str, Any], json.loads(lines[-1]))

    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        result = run_probe()
        if not identity_only and not revoke_before_persist:
            downgrade = subprocess.run(
                [
                    *base,
                    "run",
                    "--rm",
                    "--no-deps",
                    "migrator",
                    "alembic",
                    "downgrade",
                    "d2e4f6a8b0c1",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            downgrade_output = downgrade.stdout + downgrade.stderr
            assert downgrade.returncode != 0, downgrade_output
            # Head tích hợp hiện từ chối downgrade sớm do preimage AI-5 không khớp;
            # guard SIG-0 bị che được kiểm riêng bằng unit trên migration gốc.
            error_marker = re.search(
                r"(?m)^[ \t]*psycopg\.errors\.RaiseException:[ \t]+"
                r"AI5_COHORT_PREIMAGE_UNEXPECTED[ \t]*$",
                downgrade_output,
            )
            assert error_marker is not None, downgrade_output[:3000]
        current = compose("run", "--rm", "--no-deps", "migrator", "alembic", "current")
        assert re.fullmatch(r"a6c8e0b2d4f7 \(head\)(?: \(mergepoint\))?", current.stdout.strip()), (
            current.stdout
        )
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        return result
    finally:
        cleanup = subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert cleanup.returncode == 0, cleanup.stderr[-3000:]
        if image_created:
            image_removal = subprocess.run(
                ["docker", "image", "rm", image],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=120,
                check=False,
            )
            assert image_removal.returncode == 0, image_removal.stderr[-3000:]


def test_backfill_bars_and_cursor_commit_or_rollback_as_one_transaction() -> None:
    result = run_backfill_compose_probe(identity_only=False)

    assert result == {
        "collector_paper_demo_collected": True,
        "collector_second_tick_without_new_bar_skips_network": True,
        "collector_without_policy_denied_before_network": True,
        "history_backfill_filled_before_earliest": True,
        "history_backfill_second_run_up_to_date": True,
        "history_backfill_without_policy_denied": True,
        "seeded": True,
        "rollback_left_no_market_rows": True,
        "committed_bars": 2,
        "committed_cursor": True,
        "resume_added_no_duplicates": True,
        "backtest_manifest_ready": True,
        "quote_volume_persisted": True,
        "taker_flow_persisted": True,
        "backtest_manifest_persisted": True,
        "backtest_manifest_duplicate_retry": True,
        "backtest_manifest_current_rights_allows_replay": True,
        "backtest_manifest_workspace_isolated": True,
        "backtest_manifest_rls_isolated": True,
        "backtest_manifest_immutable": True,
        "backtest_manifest_replay_denied_after_revoke": True,
        "backtest_multitimeframe_bundle_persisted": True,
        "backtest_multitimeframe_bundle_intervals": True,
        "backtest_multitimeframe_bundle_current_rights_replay": True,
        "backtest_multitimeframe_bundle_workspace_isolated": True,
        "backtest_multitimeframe_bundle_immutable": True,
        "backtest_multitimeframe_bundle_replay_denied_after_revoke": True,
        "backtest_runtime_replay_transaction_rollback": True,
        "backtest_runtime_replay_persisted": True,
        "backtest_runtime_replay_retry_kept_rights_receipts": True,
        "backtest_runtime_replay_denied_after_revoke": True,
        "backtest_replay_job_succeeded": True,
        "backtest_replay_job_role_boundaries": True,
        "backtest_replay_job_workspace_isolation": True,
        "backtest_replay_cancel_before_persist_wins": True,
        "backtest_replay_concurrent_claim_single_winner": True,
        "backtest_replay_lease_reclaim_fences_stale_worker": True,
        "backtest_replay_queued_cancel_terminal": True,
        "backtest_replay_attempt_exhaustion_terminal": True,
        "registry_rights_gate_allowed": True,
        "network_requests": 0,
    }


def test_backfill_manifest_v4_replay_loader_verifies_source_identity() -> None:
    result = run_backfill_compose_probe(identity_only=True)

    assert result == {
        "db_backfill_source_identity_hydrated": True,
        "single_manifest_v4_identity_digest": True,
        "single_manifest_replay_loader_verified_identity": True,
        "multiframe_manifests_v4": True,
        "multiframe_bundle_loader_preserved_identity": True,
    }


def test_replay_revocation_after_execution_prevents_outcome_persistence() -> None:
    result = run_backfill_compose_probe(identity_only=False, revoke_before_persist=True)

    assert result["replay_executed_before_revocation"] is True
    assert result["executed_replay_runs"] == 2, result
    assert result["persist_denied_after_revocation"] is True, result
    assert result["outcome_counts_after"] == result["outcome_counts_before"] == [0] * 6
