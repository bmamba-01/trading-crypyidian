import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from textwrap import dedent
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
        "db-provisioner",
        "migrator",
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
        {
            "source": "database_execution_gateway_password",
            "target": "/run/secrets/database_execution_gateway_password",
        },
    ]
    for service_name in ("admin-web", "trading-runtime", "ingestion-worker", "trainer-worker"):
        assert len(services[service_name]["secrets"]) == 1
        assert "binance_api_key" not in str(services[service_name]["secrets"])
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


def test_each_process_has_a_separate_database_identity() -> None:
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
    assert services["migrator"]["depends_on"]["db-provisioner"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["db-provisioner"]["command"] == [
        "python",
        "scripts/provision_database_roles.py",
    ]
    expected_roles = {
        "admin-web": "crypidian_admin_web",
        "trading-runtime": "crypidian_trading_runtime",
        "execution-gateway": "crypidian_execution_gateway",
        "ingestion-worker": "crypidian_ingestion_worker",
        "trainer-worker": "crypidian_trainer_worker",
    }

    for service_name, database_user in expected_roles.items():
        service = services[service_name]
        assert service["environment"]["TRADING_BOT_DATABASE_USER"] == database_user
        assert service["secrets"][-1]["source"] == (
            f"database_{service_name.replace('-', '_')}_password"
        )


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
                f"{' '.join(arguments)}\n"
                f"stdout:\n{result.stdout.strip()}\n"
                f"stderr:\n{result.stderr.strip()}"
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
        schema_tables = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-Atc",
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('workspace','user_account','user_membership','role_binding','session','mfa_factor',"
            "'telegram_binding','trading_account','secret_reference','config_version',"
            "'risk_profile_version','approval_request','watchlist_version','watchlist_item',"
            "'strategy_release')",
        )
        assert schema_tables.stdout.strip() == "15"
        role_flags = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-Atc",
            "SELECT bool_and(NOT rolsuper AND NOT rolbypassrls) FROM pg_roles "
            "WHERE rolname = ANY(ARRAY['crypidian_admin_web','crypidian_trading_runtime',"
            "'crypidian_execution_gateway','crypidian_ingestion_worker','crypidian_trainer_worker'])",
        )
        assert role_flags.stdout.strip() == "t"
        identity_access = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-Atc",
            "SELECT has_table_privilege('crypidian_admin_web','user_account','SELECT') "
            "|| ':' || has_table_privilege('crypidian_trainer_worker','user_account','SELECT')",
        )
        assert identity_access.stdout.strip() == "true:false"
        compose("run", "--rm", "migrator", "alembic", "check")
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
            "INSERT INTO workspace(id,name,slug,mode,status) VALUES "
            "('00000000-0000-0000-0000-000000000001','A','workspace-a','PAPER','ACTIVE'),"
            "('00000000-0000-0000-0000-000000000002','B','workspace-b','PAPER','ACTIVE'); "
            "INSERT INTO trading_account(id,workspace_id,name,venue,mode,environment,status,"
            "max_notional) "
            "VALUES ('10000000-0000-0000-0000-000000000001',"
            "'00000000-0000-0000-0000-000000000001','account-a','binance_usdm',"
            "'PAPER','local','DISABLED',123.450000000000000000),"
            "('10000000-0000-0000-0000-000000000002',"
            "'00000000-0000-0000-0000-000000000002','account-b','binance_usdm',"
            "'PAPER','local','DISABLED',456.780000000000000000)",
        )
        probe_code = dedent(
            """\
            import asyncio
            import os
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine,
                database_url_from_secret,
            )
            from trading_bot.adapters.database.repositories import WorkspaceRepository

            async def main():
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host='postgres',
                    database='crypidian',
                )
                engine = create_database_engine(url)
                async with async_sessionmaker(
                    engine, expire_on_commit=False
                )() as session:
                    user = await session.scalar(text('SELECT current_user'))
                    accounts = await WorkspaceRepository(session).list_trading_accounts(
                        UUID('00000000-0000-0000-0000-000000000001')
                    )
                    assert len(accounts) == 1
                    assert isinstance(accounts[0].max_notional, Decimal)
                    print(
                        user + ':' + accounts[0].name + ':' + str(accounts[0].max_notional)
                    )
                await engine.dispose()

            asyncio.run(main())
            """
        )
        repository_probe = compose(
            "exec",
            "--no-TTY",
            "admin-web",
            "python",
            "-c",
            probe_code,
        )
        assert repository_probe.stdout.strip() == (
            "crypidian_admin_web:account-a:123.450000000000000000"
        )
        scoped_count = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-Atc",
            "BEGIN; SET LOCAL ROLE crypidian_admin_web; "
            "SET LOCAL app.workspace_id = '00000000-0000-0000-0000-000000000001'; "
            "SELECT count(*) FROM trading_account; COMMIT",
        )
        assert "1" in scoped_count.stdout.strip().splitlines()
        unscoped_count = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-Atc",
            "BEGIN; SET LOCAL ROLE crypidian_admin_web; "
            "SELECT count(*) FROM trading_account; COMMIT",
        )
        assert "0" in unscoped_count.stdout.strip().splitlines()
        compose("run", "--rm", "migrator", "alembic", "downgrade", "base")
        compose("run", "--rm", "migrator", "alembic", "upgrade", "head")
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
            timeout=180,
            check=False,
        )
