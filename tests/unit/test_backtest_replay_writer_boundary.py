"""Replay persistence dùng credential riêng khỏi trainer-model."""

import json
import os
import subprocess
from pathlib import Path


def test_compose_mounts_replay_writer_only_for_backtest_persistence() -> None:
    root = Path(__file__).parents[2]
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env=os.environ,
    )
    services = json.loads(result.stdout)["services"]
    worker = services["backtest-worker"]
    assert worker["environment"]["TRADING_BOT_BACKTEST_REPLAY_DATABASE_USER"] == (
        "crypidian_replay_writer"
    )
    mounted = {item["source"] for item in worker.get("secrets", [])}
    assert "database_replay_writer_password" in mounted
    assert "database_trainer_worker_password" not in mounted

    for name, service in services.items():
        service_secrets = {item["source"] for item in service.get("secrets", [])}
        assert ("database_replay_writer_password" in service_secrets) is (
            name in {"backtest-worker", "db-provisioner"}
        )


def test_local_secret_and_role_provisioning_include_replay_writer() -> None:
    root = Path(__file__).parents[2]
    secrets_source = (root / "scripts/prepare_local_secrets.py").read_text(encoding="utf-8")
    roles_source = (root / "scripts/provision_database_roles.py").read_text(encoding="utf-8")

    assert '"replay_writer"' in secrets_source
    assert '"crypidian_replay_writer", "/run/secrets/database_replay_writer_password"' in (
        roles_source
    )
