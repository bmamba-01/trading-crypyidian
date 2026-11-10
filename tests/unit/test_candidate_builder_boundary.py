"""Credential builder tách khỏi trainer và không tự khởi chạy cùng PAPER."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_compose_builder_secret_is_exclusive_and_opt_in():
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
    assert "candidate-proof-builder" in services
    builder_services = {"candidate-proof-builder", "learning-split-builder"}
    for name in builder_services:
        assert name in services
        builder = services[name]
        assert builder["profiles"] == ["learning-proof"]
        assert builder["environment"]["TRADING_BOT_DATABASE_USER"] == "crypidian_candidate_builder"
    secret = "database_candidate_builder_password"
    for name, service in services.items():
        mounted = {item["source"] for item in service.get("secrets", [])}
        assert (secret in mounted) is (name in builder_services | {"db-provisioner"})


@pytest.mark.asyncio
async def test_cli_pool_uses_fixed_builder_principal_and_fails_without_secret(
    tmp_path, monkeypatch
):
    from trading_bot.cli import candidate_proof

    secret = tmp_path / "builder-password"
    monkeypatch.setattr(candidate_proof, "PASSWORD_FILE", secret)
    with pytest.raises(FileNotFoundError):
        candidate_proof.create_builder_pool()
    secret.write_text("")
    with pytest.raises(ValueError, match="rỗng"):
        candidate_proof.create_builder_pool()
    secret.write_text("synthetic-unit-password")
    engine, sessions = candidate_proof.create_builder_pool()
    assert engine.url.username == "crypidian_candidate_builder"
    assert sessions.kw["bind"] is engine
    await engine.dispose()


def test_cli_does_not_accept_arbitrary_database_user_or_proof_payload():
    from trading_bot.cli.candidate_proof import parse_request

    with pytest.raises(SystemExit) as error:
        parse_request(["--database-user", "crypidian_trainer_worker", "--proof", "{}"])
    assert error.value.code == 2
