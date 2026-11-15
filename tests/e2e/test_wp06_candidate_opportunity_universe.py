"""PostgreSQL Compose Task4: proof BASE, quyền metadata và hai thứ tự seal race."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources

BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ['WP06_UNIVERSE_STATE']=json.dumps(p['state']);"
    "exec(compile(p['code'],'wp06_candidate_opportunity_universe.py','exec'),"
    "{'__name__':'__main__'})"
)


def test_postgres_candidate_opportunity_universe_rights_seal_and_races():
    root = Path(__file__).parents[2]
    probe = (root / "tests/e2e/probes/wp06_candidate_opportunity_universe.py").read_text()
    garage = root / ".secrets/garage.env"
    garage_before = garage.read_bytes() if garage.exists() else None
    state = {"workspace_id": str(uuid.uuid4()), "now": datetime.now(UTC).isoformat()}
    try:
        with isolated_compose_resources(root) as original:
            resources = replace(
                original,
                project=f"crypidian-opportunity-universe-{original.run_id}",
                image=f"crypidian-app:e2e-opportunity-universe-{original.run_id}",
            )
            base = [
                "docker",
                "compose",
                "--project-name",
                resources.project,
                "--project-directory",
                str(root),
                "-f",
                str(resources.compose_file),
            ]
            environment = os.environ | {"CRYPIDIAN_APP_IMAGE": resources.image}

            def compose(*args, stdin=None, allowed_failure=False):
                result = subprocess.run(
                    [*base, *args],
                    cwd=root,
                    env=environment,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )
                if not allowed_failure:
                    if result.returncode != 0:
                        raise AssertionError(
                            f"Compose {args[0]} exit{result.returncode}:\n"
                            f"{result.stdout[-3000:]}\n{result.stderr[-7000:]}"
                        )
                return result

            def run_probe(mode):
                mounts = []
                for role in ("candidate_builder", "replay_writer"):
                    secret = f"database_{role}_password"
                    mounts.extend(
                        [
                            "--volume",
                            str(resources.compose_file.parent / ".secrets" / secret)
                            + f":/run/secrets/{secret}:ro",
                        ]
                    )
                result = compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    *mounts,
                    "migrator",
                    "python",
                    "-c",
                    BOOTSTRAP,
                    stdin=json.dumps({"state": state | {"mode": mode}, "code": probe}),
                )
                return json.loads(result.stdout.strip().splitlines()[-1])

            image_created = ensure_image_available(root, resources.image)
            try:
                compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
                compose("run", "--rm", "--no-deps", "db-provisioner")
                compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
                assert (
                    "No new upgrade operations detected"
                    in compose("run", "--rm", "--no-deps", "migrator", "alembic", "check").stdout
                )
                compose(
                    "run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "d3a5c7e9b1d4"
                )
                compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
                result = run_probe("verify")
                assert result["metadata_columns"] == 14
                assert result["positive"] == {
                    "opportunity_count": 4,
                    "candidate_count": 3,
                    "no_candidate_count": 1,
                    "eligible_count": 1,
                    "unlabeled_candidate_count": 2,
                    "completeness_verified": True,
                }
                for marker in (
                    "payload_denied",
                    "rls",
                    "rights",
                    "incomplete",
                    "negative",
                    "writer_before_proof",
                    "proof_before_writer",
                    "sealed",
                    "immutable",
                ):
                    assert result[marker] is True
                assert result["rights_cases"] == dict.fromkeys(
                    (
                        "research_only",
                        "training_child_receipts",
                        "source_policy_lock",
                        "revoked_new",
                        "revoked_retry",
                        "expired_verified_policy",
                        "expired_state",
                    ),
                    True,
                )
                assert result["delete_checks"] == {
                    table: {
                        "trigger_marker": marker,
                        "trigger_sqlstate": "P0001",
                        "candidate_builder_sqlstate": "42501",
                        "replay_writer_sqlstate": "42501",
                        "rows_preserved": True,
                    }
                    for table, marker in (
                        ("backtest_decision_opportunity", "WP06_OPPORTUNITY_IMMUTABLE"),
                        ("candidate_universe_proof", "WP06_CANDIDATE_PROOF_IMMUTABLE"),
                        ("candidate_training_rights", "WP06_CANDIDATE_PROOF_IMMUTABLE"),
                    )
                }
                assert result["scanner_negative_checks"] == [
                    "extra_event",
                    "feature_schema",
                    "source_identity",
                    "source_revision",
                    "source_timeframe",
                    "not_base",
                    "ordinal_with_other_child_gap",
                    "missing_source_diagnostic",
                ]
                assert set(result["race_pids"]) == {"writer_first", "proof_first", "source_policy"}
                for pids in result["race_pids"].values():
                    assert pids["waiter_pid"] != pids["blocker_pid"]
                    assert pids["waiter_pid"] > 0
                    assert pids["blocker_pid"] > 0
                downgrade = compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "migrator",
                    "alembic",
                    "downgrade",
                    "d3a5c7e9b1d4",
                    allowed_failure=True,
                )
                assert downgrade.returncode != 0
                assert (
                    "WP06_OPPORTUNITY_DOWNGRADE_REQUIRES_EMPTY_V2"
                    in downgrade.stdout + downgrade.stderr
                )
                compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
                assert (
                    "No new upgrade operations detected"
                    in compose("run", "--rm", "--no-deps", "migrator", "alembic", "check").stdout
                )
                heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads").stdout
                assert heads.count("(head)") == 1
                assert "d2e4f6a8b0c1 (head)" in heads
                assert run_probe("post-lifecycle") == {
                    "grant_retained_after_populated_downgrade": True,
                    "metadata_columns": 14,
                }
                print(
                    json.dumps(
                        {
                            "project": resources.project,
                            **result,
                            "migration_lifecycle": True,
                            "populated_downgrade_guard": True,
                        },
                        sort_keys=True,
                    )
                )
            finally:
                compose("down", "--volumes", "--remove-orphans")
                for kind in ("container", "volume", "network"):
                    args = [
                        "docker",
                        kind,
                        "ls",
                        "--quiet",
                        "--filter",
                        f"label=com.docker.compose.project={resources.project}",
                    ]
                    if kind == "container":
                        args.append("--all")
                    inventory = subprocess.run(args, capture_output=True, text=True, check=True)
                    assert not inventory.stdout.strip(), (kind, inventory.stdout)
                if image_created:
                    subprocess.run(
                        ["docker", "image", "rm", resources.image],
                        capture_output=True,
                        check=True,
                        timeout=120,
                    )
                print(
                    json.dumps(
                        {
                            "cleanup_project": resources.project,
                            "containers": 0,
                            "volumes": 0,
                            "networks": 0,
                            "app_image": resources.image,
                            "app_image_removed": image_created,
                        },
                        sort_keys=True,
                    )
                )
        assert not resources.compose_file.parent.exists()
    finally:
        garage_after = garage.read_bytes() if garage.exists() else None
        preserved = garage_before == garage_after
        assert preserved, "Cấu hình Garage của người dùng đã thay đổi."
    print(json.dumps({"garage_config_preserved": True, "isolated_secrets_removed": True}))
