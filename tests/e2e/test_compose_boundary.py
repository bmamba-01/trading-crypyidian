import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.request import urlopen

import pytest


def prepare_local_secrets(repository_root: Path) -> None:
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_compose_keeps_exchange_secrets_inside_execution_gateway() -> None:
    repository_root = Path(__file__).parents[2]
    prepare_local_secrets(repository_root)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "-f",
            "compose.yaml",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    model = json.loads(result.stdout)
    services = model["services"]

    assert {
        "admin-web",
        "trading-runtime",
        "execution-gateway",
        "ingestion-worker",
        "trainer-worker",
        "postgres",
        "object-store",
    } <= services.keys()
    assert services["execution-gateway"]["secrets"] == [
        {"source": "binance_api_key", "target": "binance_api_key"},
        {"source": "binance_api_secret", "target": "binance_api_secret"},
    ]
    for service_name in ("admin-web", "trading-runtime", "ingestion-worker", "trainer-worker"):
        assert "secrets" not in services[service_name]
    assert services["object-store"]["secrets"] == [
        {
            "source": "garage_rpc_secret",
            "target": "/run/secrets/garage_rpc_secret",
        }
    ]
    assert services["object-store"]["environment"]["GARAGE_CONFIG_FILE"] == "/etc/garage.toml"
    assert services["object-store"]["environment"]["GARAGE_DEFAULT_BUCKET"] == (
        "crypidian-artifacts"
    )
    assert bool(services["object-store"]["environment"]["GARAGE_DEFAULT_ACCESS_KEY"])
    assert bool(services["object-store"]["environment"]["GARAGE_DEFAULT_SECRET_KEY"])


def test_compose_services_run_without_root_and_have_healthchecks() -> None:
    repository_root = Path(__file__).parents[2]
    prepare_local_secrets(repository_root)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "-f",
            "compose.yaml",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    services = json.loads(result.stdout)["services"]

    for service_name in (
        "admin-web",
        "trading-runtime",
        "execution-gateway",
        "ingestion-worker",
        "trainer-worker",
    ):
        assert services[service_name]["user"] != "0:0"
        assert services[service_name]["read_only"] is True
        assert "healthcheck" in services[service_name]


def test_paper_stack_starts_and_keeps_database_and_object_store_data() -> None:
    repository_root = Path(__file__).parents[2]
    prepare_local_secrets(repository_root)
    project_name = f"crypidian-e2e-{uuid.uuid4().hex[:10]}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ | {"TRADING_BOT_WEB_PORT": str(port)}
    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        pytest.fail(f"Docker daemon cần chạy để xác minh E2E Compose: {error}")

    def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
        command = [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--project-name",
            project_name,
            "-f",
            "compose.yaml",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                cwd=repository_root,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=240,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stderr or error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            pytest.fail(
                f"Docker Compose vượt quá timeout cho {' '.join(arguments)}:\n{output[-4000:]}"
            )
        if result.returncode:
            pytest.fail(
                "Lệnh Docker Compose thất bại: "
                f"{' '.join(arguments)}\n{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    try:
        compose("up", "--detach", "--build", "--wait", "--wait-timeout", "180")
        with urlopen(f"http://127.0.0.1:{port}/health/live", timeout=5) as response:
            assert json.load(response) == {
                "status": "ok",
                "mode": "PAPER",
                "role": "admin-web",
            }
        compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-c",
            "CREATE TABLE persistence_probe (id integer PRIMARY KEY)",
        )
        compose(
            "exec",
            "--no-TTY",
            "object-store",
            "/garage",
            "bucket",
            "list",
        )
        compose("down")
        compose("up", "--detach", "--wait", "--wait-timeout", "180")
        result = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-Atc",
            "SELECT to_regclass('public.persistence_probe')",
        )
        assert result.stdout.strip() == "persistence_probe"
        bucket_list = compose(
            "exec",
            "--no-TTY",
            "object-store",
            "/garage",
            "bucket",
            "list",
        )
        assert "crypidian-artifacts" in bucket_list.stdout
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "paper",
                "--project-name",
                project_name,
                "-f",
                "compose.yaml",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
