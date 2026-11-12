"""Compose smoke Task2 cho schema/ACL/persistence, không chạy proof scanner/race."""

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
    "import json,os,sys;p=json.load(sys.stdin);os.environ['WP06_OPPORTUNITY"
    "_STATE']=json.dumps(p['state']);exec(compile(p['code'],'wp06_opportuni"
    "ty_persistence_schema.py','exec'),{'__name__':'__main__'})"
)


def test_opportunity_schema_persistence_roles_and_legacy_migration() -> None:
    root = Path(__file__).parents[2]
    garage = root / ".secrets/garage.env"
    before = garage.read_bytes() if garage.exists() else None
    probe = (root / "tests/e2e/probes/wp06_opportunity_persistence_schema.py").read_text()
    state = {"workspace_id": str(uuid.uuid4()), "now": datetime.now(UTC).isoformat()}
    try:
        with isolated_compose_resources(root) as original:
            resources = replace(
                original,
                project=f"crypidian-opportunity-schema-{original.run_id}",
                image=f"crypidian-app:e2e-opportunity-schema-{original.run_id}",
            )
            environment = os.environ | {"CRYPIDIAN_APP_IMAGE": resources.image}
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

            def compose(*args: str, stdin: str | None = None, allowed_failure: bool = False):
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
                    assert result.returncode == 0, (
                        args,
                        result.stdout[-3000:],
                        result.stderr[-6000:],
                    )
                return result

            def run_probe(mode: str, service: str = "migrator"):
                result = compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    service,
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
                compose(
                    "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "d3a5c7e9b1d4"
                )
                state.update(run_probe("seed_legacy"))
                compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
                assert (
                    "No new upgrade operations detected"
                    in compose("run", "--rm", "--no-deps", "migrator", "alembic", "check").stdout
                )
                # V1 có dữ liệu nhưng chưa có V2 phải downgrade/upgrade an toàn.
                compose(
                    "run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "d3a5c7e9b1d4"
                )
                compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
                persisted = run_probe("persist", "backtest-worker")
                state.update(persisted)
                verified = run_probe("verify")
                assert all(value is True for value in verified.values()), verified
                assert persisted["writer_login"] == "crypidian_replay_writer"
                assert persisted["retry_idempotent"] is True
                assert persisted["changed_event_conflict"] is True
                assert persisted["transaction_rollback"] is True
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
                assert (
                    "No new upgrade operations detected"
                    in compose("run", "--rm", "--no-deps", "migrator", "alembic", "check").stdout
                )
                heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads").stdout
                assert heads.count("(head)") == 1
                assert "c3f5a7b9d1e3 (head)" in heads
                print(
                    json.dumps(
                        {
                            "project": resources.project,
                            **persisted,
                            **verified,
                            "populated_downgrade_guard": True,
                            "alembic_check": True,
                            "single_head": True,
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
        after = garage.read_bytes() if garage.exists() else None
        preserved = before == after
        assert preserved, "Cấu hình Garage đã thay đổi."
    print(json.dumps({"garage_config_preserved": True, "isolated_secrets_removed": True}))
