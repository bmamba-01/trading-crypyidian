"""Compose E2E cho ranh giới ACL toàn ứng dụng của admin-web."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def _report_reader_column_allowlist() -> dict[str, list[str]]:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/f0a2c4e6b8e0_wp06_report_reader_column_allowlist.py"
    )
    spec = importlib.util.spec_from_file_location(
        "wp06_report_reader_column_allowlist", migration_path
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return {table: sorted(columns) for table, columns in migration.COLUMN_ALLOWLISTS.items()}


def _admin_safe_acl_manifest() -> list[dict[str, object]]:
    path = Path(__file__).parents[2] / "migrations/wp06_admin_safe_acl_manifest.json"
    return [
        row for row in json.loads(path.read_text(encoding="utf-8")) if row["kind"] != "membership"
    ]


def test_admin_web_does_not_inherit_application_or_raw_trade_access(
    subtests: pytest.Subtests,
) -> None:
    root = Path(__file__).parents[2]
    with isolated_compose_resources(root) as resources:
        project = f"crypidian-wp06-admin-role-e2e-{resources.run_id}"
        base = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--project-directory",
            str(root),
            "-f",
            str(resources.compose_file),
        ]
        environment = os.environ | {"CRYPIDIAN_APP_IMAGE": resources.image}
        secret = resources.compose_file.parent / ".secrets" / "database_admin_web_password"
        reader_secret = (
            resources.compose_file.parent / ".secrets" / "database_admin_report_reader_password"
        )

        def compose(*args: str) -> str:
            result = compose_result(*args)
            assert result.returncode == 0, (
                args,
                result.stdout[-3000:],
                result.stderr[-6000:],
            )
            return result.stdout

        def compose_result(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=360,
            )

        image_created = ensure_image_available(root, resources.image)
        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose(
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "upgrade",
                "f0a2c4e6b8d3",
            )
            probe_command = (
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_admin_acl_rollback.py",
            )
            catalog_probe = (*probe_command[:-1], "/e2e-tests/e2e/probes/wp06_admin_acl_catalog.py")
            before = compose(*catalog_probe, "baseline").strip()
            for scenario in (
                "preflight",
                "isolation",
                "barrier",
                "foreign_grant",
                "reader_membership",
                "global_writer",
                "upgrade_fault",
            ):
                with subtests.test(scenario=scenario):
                    compose(*catalog_probe, scenario)
            compose(*probe_command, "seed")
            rejected = compose_result(
                "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head"
            )
            assert rejected.returncode != 0, (rejected.stdout[-3000:], rejected.stderr[-6000:])
            assert "WP06_ADMIN_ACL_UNCLASSIFIED" in rejected.stdout + rejected.stderr
            rollback = json.loads(compose(*probe_command, "check").strip().splitlines()[-1])
            assert rollback == {
                "app_membership": True,
                "safe_role_exists": False,
                "snapshot_exists": False,
                "direct_grant_preserved": True,
                "revision": "f0a2c4e6b8d3",
            }

            compose(*probe_command, "cleanup")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            snapshot = json.loads(compose(*probe_command, "snapshot").strip().splitlines()[-1])
            manifest = _admin_safe_acl_manifest()
            assert snapshot == {
                "entries": len(manifest),
                "transferred": sum(entry["disposition"] == "transfer" for entry in manifest),
                "revoke_only": sum(entry["disposition"] == "revoke_only" for entry in manifest),
                "membership_snapshotted": True,
            }

            def role_probe(*, restored: bool = False) -> tuple[dict[str, object], ...]:
                arguments = [
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--volume",
                    f"{root / 'tests'}:/e2e-tests:ro",
                    "--volume",
                    f"{secret}:/run/secrets/database_admin_web_password:ro",
                    "-e",
                    "WP06_ADMIN_PASSWORD_FILE=/run/secrets/database_admin_web_password",
                    "migrator",
                ]
                if not restored:
                    arguments[3:3] = [
                        "--volume",
                        f"{reader_secret}:/run/secrets/database_admin_report_reader_password:ro",
                        "-e",
                        "WP06_ADMIN_REPORT_READER_PASSWORD_FILE="
                        "/run/secrets/database_admin_report_reader_password",
                    ]
                else:
                    arguments[-1:-1] = ["-e", "WP06_EXPECT_RESTORED_ROLE=1"]
                output = compose(
                    *arguments,
                    "python",
                    "/e2e-tests/e2e/probes/db_role_least_privilege.py",
                )
                return tuple(
                    json.loads(line)
                    for line in output.strip().splitlines()[-(1 if restored else 2) :]
                )

            admin, report_reader = role_probe()
            assert admin == {
                "inherits_application_role": False,
                "can_read_trade_net_pnl": False,
                "can_read_score_probabilities": False,
                "can_read_auth_account": True,
                "can_read_bot_config": True,
                "can_read_paper_checkpoint": False,
            }
            assert report_reader == {
                "report_reader_can_login": True,
                "report_reader_has_unsafe_role_attributes": False,
                "report_reader_inherits_scope_owner": False,
                "report_reader_can_insert_trade": False,
                "report_reader_can_select_paper_checkpoint": True,
                "report_reader_can_insert_paper_checkpoint": False,
                "report_reader_scope_without_identity": False,
                "report_reader_unscoped_trade_rows": 0,
                "report_reader_unscoped_decision_rows": 0,
                "report_reader_unscoped_paper_checkpoint_rows": 0,
                "report_reader_table_wide_select": [],
                "report_reader_selectable_columns": _report_reader_column_allowlist(),
                "report_reader_columns": {
                    "trade.net_pnl": True,
                    "order_intent.quantity": True,
                    "order_intent.evidence": True,
                    "order_intent.filled_quantity": True,
                    "bot_decision.payload": True,
                    "bot_decision.decision_hash": True,
                    "shared_signal_evidence.symbol": True,
                    "shared_signal_evidence.observed_at": True,
                    "shared_signal_evidence.canonical_json": False,
                    "shared_signal_evidence.source_refs": False,
                    "position_event.payload": True,
                },
                "report_reader_signal_outcome_dml": {
                    "insert": False,
                    "update": False,
                    "delete": False,
                    "truncate": False,
                },
                "report_reader_telegram_delivery_dml": {
                    "insert": False,
                    "update": False,
                    "delete": False,
                    "truncate": False,
                },
            }

            compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "f0a2c4e6b8d4")
            for scenario in ("snapshot", "snapshot_writer", "downgrade_fault", "restore_grantor"):
                with subtests.test(scenario=scenario):
                    compose(*catalog_probe, scenario)
            compose(
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "f0a2c4e6b8d3",
            )
            assert compose(*catalog_probe, "baseline").strip() == before
            (restored_admin,) = role_probe(restored=True)
            assert restored_admin["inherits_application_role"] is True
            assert restored_admin["can_read_trade_net_pnl"] is True

            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            upgraded_admin, upgraded_reader = role_probe()
            assert upgraded_admin == admin
            assert upgraded_reader == report_reader
        finally:
            compose("down", "--volumes", "--remove-orphans")
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
                assert not subprocess.run(
                    command, capture_output=True, text=True, check=True
                ).stdout.strip()
            if image_created:
                subprocess.run(
                    ["docker", "image", "rm", resources.image],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
