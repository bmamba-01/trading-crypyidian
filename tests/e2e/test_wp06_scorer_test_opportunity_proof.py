"""PostgreSQL Compose gate cho schema và immutability proof TEST scorer C.3."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_postgres_scorer_test_opportunity_proof_rls_hash_and_downgrade() -> None:
    root = Path(__file__).parents[2]
    probe = root / "tests/e2e/probes/wp06_scorer_test_opportunity_proof.py"
    assert probe.is_file(), "C.3 cần probe PostgreSQL cho proof TEST trước khi chạy."
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
    }
    with isolated_compose_resources(root) as resources:
        base = [
            "docker",
            "compose",
            "--project-name",
            f"crypidian-wp06-scorer-test-proof-{resources.run_id}",
            "--project-directory",
            str(root),
            "-f",
            str(resources.compose_file),
        ]
        project = base[3]
        environment = os.environ | {"CRYPIDIAN_APP_IMAGE": resources.image}
        tests_mount = f"{root / 'tests'}:/e2e-tests:ro"
        secret_mounts = []
        for role in (
            "candidate_builder",
            "learning_evaluator",
            "admin_web",
            "trainer_worker",
        ):
            secret = f"database_{role}_password"
            secret_mounts.extend(
                (
                    "--volume",
                    str(resources.compose_file.parent / ".secrets" / secret)
                    + f":/run/secrets/{secret}:ro",
                )
            )

        def compose(*args: str, allowed_failure: bool = False) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=360,
            )
            if not allowed_failure and result.returncode != 0:
                raise AssertionError(
                    f"Compose {args!r} exit {result.returncode}:\n"
                    f"{result.stdout[-4000:]}\n{result.stderr[-7000:]}"
                )
            return result

        image_created = ensure_image_available(root, resources.image)
        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1, heads.stdout

            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                *secret_mounts,
                "--volume",
                tests_mount,
                "-e",
                "WP06_SCORER_TEST_PROOF_STATE=" + json.dumps(state),
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_scorer_test_opportunity_proof.py",
            )
            lines = result.stdout.strip().splitlines()
            assert lines, result.stderr[-4000:]
            observed = json.loads(lines[-1])
            assert observed["forced_rls"] is True
            assert observed["workspace_scope"] is True
            assert observed["grants_exact"] is True
            assert observed["canonical_hash_parity"] is True
            assert observed["bad_direct_insert_rejected"] is True
            assert observed["whitespace_tamper_rejected"] is True
            assert observed["key_order_tamper_rejected"] is True
            assert observed["denormalized_tamper_rejected"] is True
            assert observed["append_only"] is True
            assert observed["test_access_forced_rls"] is True
            assert observed["test_access_grants_exact"] is True
            assert observed["test_access_function_grants_exact"] is True
            assert observed["evaluator_no_direct_test_select"] is True

            downgrade = compose(
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "d5e7f9a1c3b5",
                allowed_failure=True,
            )
            assert downgrade.returncode != 0
            assert "WP06_SCORER_TEST_PROOF_DOWNGRADE_REQUIRES_EMPTY_TABLE" in (
                downgrade.stdout + downgrade.stderr
            )
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1, heads.stdout
        finally:
            compose("down", "--volumes", "--remove-orphans")
            for kind in ("container", "volume", "network"):
                command = [
                    "docker",
                    kind,
                    "ls",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ]
                if kind == "container":
                    command.append("--all")
                remaining = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                assert not remaining.stdout.strip(), (kind, remaining.stdout)
            if image_created:
                subprocess.run(
                    ["docker", "image", "rm", resources.image],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
