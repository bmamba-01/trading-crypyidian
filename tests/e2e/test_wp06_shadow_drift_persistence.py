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
CURRENT_HEAD = "c6e8a0b2d4f9"


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
            "assessment_rls_fixture_cleaned": True,
            "drift_policy_rejected": True,
            "cross_workspace_insert_denied_by_member_scope_guard": True,
            "unscoped_read_denied": True,
            "observer_role_least_privilege": True,
            "observer_sensitive_columns_denied": True,
            "admin_direct_select_denied": True,
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


def test_populated_coverage_v1_ledger_survives_bounded_window_upgrade() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-legacy-e2e-{uuid.uuid4().hex[:10]}"
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
        *args: str, stdin: str | None = None, success: bool = True
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
        assert (result.returncode == 0) is success, (
            f"Compose lệnh {args!r} thất bại:\n{result.stdout}\n{result.stderr}"
        )
        return result

    probe = (root / "tests/e2e/probes/wp06_legacy_drift_upgrade.py").read_text(encoding="utf-8")

    def snapshot(action: str) -> dict[str, Any]:
        payload = json.dumps({"state": {"action": action}, "code": probe})
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
        return json.loads(result.stdout.strip().splitlines()[-1])

    def database(sql: str) -> str:
        return compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-XAt",
            "-v",
            "ON_ERROR_STOP=1",
            "--username",
            "crypidian_admin",
            "--dbname",
            "crypidian",
            "--command",
            sql,
        ).stdout.strip()

    def drift_contract() -> list[list[str]]:
        rows = database("""
            SELECT proname, pg_catalog.md5(pg_catalog.pg_get_functiondef(oid)),
                   oid, proowner, prosecdef, proconfig, proacl,
                   pg_catalog.has_function_privilege('crypidian_learning_shadow_observer',
                                                     oid, 'EXECUTE')
              FROM pg_catalog.pg_proc
             WHERE oid IN (
                'public.wp06_authorize_learning_drift_source_scope(uuid,uuid,timestamptz)'::regprocedure,
                'public.wp06_authorize_paper_performance_decision_source_scope(uuid,uuid,timestamptz)'::regprocedure,
                'public.wp06_paper_performance_decision_source_ids(uuid,uuid)'::regprocedure
             )
             ORDER BY proname
        """)
        return [line.split("|") for line in rows.splitlines()]

    def observer_gate_contract() -> list[list[str]]:
        rows = database("""
            SELECT pg_catalog.md5(pg_catalog.pg_get_functiondef(function.oid)),
                   function.proowner::pg_catalog.regrole,
                   function.prosecdef, function.proconfig, function.proacl,
                   pg_catalog.has_function_privilege(
                       'crypidian_learning_shadow_observer', function.oid, 'EXECUTE'),
                   pg_catalog.has_function_privilege('crypidian_app', function.oid, 'EXECUTE'),
                   pg_catalog.has_function_privilege(
                       'crypidian_trading_runtime', function.oid, 'EXECUTE')
              FROM pg_catalog.pg_proc AS function
             WHERE function.oid = pg_catalog.to_regprocedure(
                'public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)')
        """)
        return [line.split("|") for line in rows.splitlines()]

    def migrate(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, success=success)

    def writer_contract() -> str:
        return database("""
            SELECT proname, pg_catalog.md5(pg_catalog.pg_get_functiondef(oid)), proacl,
                   pg_catalog.has_function_privilege(
                       'crypidian_learning_shadow_integrity_guard', oid, 'EXECUTE')
              FROM pg_catalog.pg_proc
             WHERE pronamespace = 'public'::regnamespace AND proname IN (
                'wp06_drift_assessment_idempotency_key', 'wp06_drift_assessment_content_hash',
                'wp06_require_learning_drift_member_lineage', 'wp06_license_canonical_json',
                'wp06_authorize_learning_drift_source_scope_current',
                'wp06_learning_drift_current_rights_digest', 'wp06_sha256_canonical_json',
                'wp06_canonical_float_json')
             ORDER BY oid
        """)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "b2e4a6c8d0f2")
        before = snapshot("seed")
        assert before["canonical"] == [True, True, True, True]
        assert all(len(rows) == 1 for rows in before["rows"].values())
        migrate("upgrade", "f4b6d8e0a2c7")
        old_contract = drift_contract()
        assert observer_gate_contract() == []
        revisions = (
            ("a6c8e0b2d4f7", "WP06_DRIFT_OBSERVER_SCOPE_DOWNGRADE_POPULATED_DENIED"),
            ("b4c6d8e0f2a9", "WP06_DRIFT_GUARD_SCOPE_GRANT_DOWNGRADE_POPULATED_DENIED"),
            ("c6e8a0b2d4f9", "WP06_DRIFT_SOURCE_RIGHTS_HASH_DOWNGRADE_POPULATED_DENIED"),
        )
        contracts = {"f4b6d8e0a2c7": writer_contract()}
        for helper in ("wp06_sha256_canonical_json(json)", "wp06_canonical_float_json(float8)"):
            assert (
                database(
                    "SELECT pg_catalog.has_function_privilege("
                    f"'crypidian_learning_shadow_integrity_guard','public.{helper}','EXECUTE')"
                )
                == "t"
            )
        for revision, marker in revisions:
            migrate("upgrade", revision)
            contracts[revision] = writer_contract()
            populated = snapshot("read")
            denied = migrate("downgrade", "-1", success=False)
            assert f"psycopg.errors.RaiseException: {marker}" in denied.stderr
            assert database("SELECT version_num FROM alembic_version") == revision
            assert writer_contract() == contracts[revision]
            assert snapshot("read") == populated
        contract = drift_contract()
        observer_gate = observer_gate_contract()
        assert len(contract) == len(old_contract) == 3
        assert len(observer_gate) == 1
        assert observer_gate[0][1] == "crypidian_scorer_deployment_rights_reader"
        assert observer_gate[0][2] == "t"
        assert observer_gate[0][5:] == ["t", "f", "f"]
        for old, new in zip(old_contract, contract, strict=True):
            assert old[0] == new[0]
            assert old[1] != new[1]
            assert old[2:6] == new[2:6]
            assert old[-1] == "f"
            assert old[-1] == new[-1] == "f"
            assert old[6:] == new[6:]
        assert CURRENT_HEAD + " (head)" in migrate("current").stdout
        after = snapshot("read")
        assert after["canonical"] == [True, True, True, True]
        legacy = after["rows"]["learning_drift_assessment"][0][0]
        assert legacy["policy_version"] == "coverage-v1"
        added_columns = {
            "learning_shadow_observation": ("source_scope_fingerprint",),
            "learning_drift_assessment": ("observation_window_days", "source_rights_digest"),
            "learning_drift_assessment_member": (
                "current_rights_eligible",
                "current_rights_fingerprint",
                "current_source_ids",
                "current_eligible",
            ),
        }
        for table, columns in added_columns.items():
            row = after["rows"][table][0][0]
            for column in columns:
                assert row.pop(column) is None, (table, column)
        assert after["rows"] == before["rows"]
        # Chỉ dọn ba hàng fixture v1 trong DB dùng riêng cho phép thử.
        workspace = uuid.UUID(legacy["workspace_id"])
        database(f"""
            BEGIN;
            SET LOCAL session_replication_role = replica;
            DELETE FROM public.learning_drift_assessment_member WHERE workspace_id = '{workspace}';
            DELETE FROM public.learning_drift_assessment WHERE workspace_id = '{workspace}';
            DELETE FROM public.learning_shadow_observation WHERE workspace_id = '{workspace}';
            COMMIT;
        """)
        for previous in ("b4c6d8e0f2a9", "a6c8e0b2d4f7", "f4b6d8e0a2c7"):
            migrate("downgrade", "-1")
            assert database("SELECT version_num FROM alembic_version") == previous
            assert writer_contract() == contracts[previous]
        assert drift_contract() == old_contract
        assert observer_gate_contract() == []
        for revision, _ in revisions:
            migrate("upgrade", revision)
            assert writer_contract() == contracts[revision]
        assert drift_contract() == contract
        assert observer_gate_contract() == observer_gate
        assert CURRENT_HEAD + " (head)" in migrate("current").stdout
        assert "No new upgrade operations detected" in migrate("check").stdout
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
