import ast
import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.cookiejar import CookieJar
from pathlib import Path
from textwrap import dedent
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pyotp
import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

from trading_bot.auth.cookies import session_cookie_name
from trading_bot.common.http_security import CONTENT_SECURITY_POLICY
from trading_bot.config.settings import AppSettings

# Compose chạy `TRADING_BOT_APP_ENV=local` qua HTTP nên cookie không Secure, không `__Host-`.
LOCAL_SESSION_COOKIE_NAME = session_cookie_name(AppSettings(app_env="local"))


def test_h03_paper_application_probe_embedded_python_compiles() -> None:
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    snippets = [
        node.value.args[0].value
        for node in ast.walk(module)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "paper_decision_cycle_probe_code"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "dedent"
        and node.value.args
        and isinstance(node.value.args[0], ast.Constant)
        and isinstance(node.value.args[0].value, str)
    ]
    assert len(snippets) == 1
    compile(dedent(snippets[0]), "<h03-paper-application-probe>", "exec")


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
        "mfa-volume-provisioner",
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
    for service_name in ("trading-runtime", "ingestion-worker", "trainer-worker"):
        assert len(services[service_name]["secrets"]) == 1
        assert "binance_api_key" not in str(services[service_name]["secrets"])
    assert "mfa_encryption_key" in str(services["admin-web"]["secrets"])
    assert "bootstrap_token" in str(services["admin-web"]["secrets"])
    assert "binance_api_key" not in str(services["admin-web"]["secrets"])
    assert services["object-store"]["secrets"] == [
        {
            "source": "garage_rpc_secret",
            "target": "/run/secrets/garage_rpc_secret",
        }
    ]
    assert services["object-store"]["environment"]["GARAGE_CONFIG_FILE"] == "/etc/garage.toml"
    assert services["admin-web"]["depends_on"]["mfa-volume-provisioner"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["mfa-volume-provisioner"]["command"] == [
        "python",
        "scripts/provision_mfa_secret_volume.py",
    ]
    assert services["mfa-volume-provisioner"]["cap_add"] == ["CHOWN"]
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
        assert f"database_{service_name.replace('-', '_')}_password" in str(service["secrets"])


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

    application_services = (
        "admin-web",
        "mfa-volume-provisioner",
        "migrator",
        "db-provisioner",
        "trading-runtime",
        "execution-gateway",
        "ingestion-worker",
        "trainer-worker",
    )
    assert {services[name]["image"] for name in application_services} == {"crypidian-app:local"}
    assert [name for name in application_services if "build" in services[name]] == ["admin-web"]


def test_paper_stack_starts_and_keeps_database_and_object_store_data() -> None:
    repository_root = Path(__file__).parents[2]
    prepare_local_secrets(repository_root)
    project_name = f"crypidian-e2e-{uuid.uuid4().hex[:10]}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    image_reference = build_image_reference(repository_root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image_reference,
        "TRADING_BOT_WEB_PORT": str(port),
    }
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
    try:
        ensure_image_available(repository_root, image_reference)
    except RuntimeError as error:
        pytest.fail(str(error))

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
        compose("up", "--detach", "--wait", "--wait-timeout", "180")
        with urlopen(f"http://127.0.0.1:{port}/health/live", timeout=5) as response:
            assert json.load(response) == {
                "status": "ok",
                "mode": "PAPER",
                "role": "admin-web",
            }

        cookie_jar = CookieJar()
        client = build_opener(HTTPCookieProcessor(cookie_jar))

        def api_request(
            method: str,
            path: str,
            payload: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, object]]:
            request_headers = {"Content-Type": "application/json", **(headers or {})}
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                headers=request_headers,
                method=method,
            )
            try:
                with client.open(request, timeout=10) as response:
                    body = response.read()
                    return response.status, json.loads(body) if body else {}
            except HTTPError as error:
                body = error.read()
                return error.code, json.loads(body) if body else {}

        bootstrap_payload = {
            "workspace_name": "Paper workspace",
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "mode": "PAPER",
        }
        missing_bootstrap_token_status, _ = api_request(
            "POST", "/api/v1/auth/bootstrap", bootstrap_payload
        )
        assert missing_bootstrap_token_status == 403
        bootstrap_status, bootstrap = api_request(
            "POST",
            "/api/v1/auth/bootstrap",
            bootstrap_payload,
            {"X-Bootstrap-Token": (repository_root / ".secrets/bootstrap_token").read_text()},
        )
        assert bootstrap_status == 201
        setup_uri = str(bootstrap["mfa_setup_uri"])
        assert setup_uri.startswith("otpauth://totp/")
        totp_secret = parse_qs(urlparse(setup_uri).query)["secret"][0]
        previous_code = pyotp.TOTP(totp_secret).at(datetime.now(UTC) - timedelta(seconds=30))
        verify_status, _ = api_request(
            "POST",
            "/api/v1/auth/bootstrap/verify",
            {
                "workspace_slug": "paper-workspace",
                "email": "owner@example.test",
                "code": previous_code,
            },
        )
        assert verify_status == 200
        initial_totp_code = pyotp.TOTP(totp_secret).now()
        login_status, login = api_request(
            "POST",
            "/api/v1/auth/login",
            {
                "workspace_slug": "paper-workspace",
                "email": "owner@example.test",
                "password": "a long owner password for test",
                "code": initial_totp_code,
            },
        )
        assert login_status == 200
        csrf_token = str(login["csrf_token"])
        # SEC-13: cookie phiên thật qua Compose (HTTP cục bộ) có Path=/, HttpOnly, Strict.
        session_cookie = next(
            cookie for cookie in cookie_jar if cookie.name == LOCAL_SESSION_COOKIE_NAME
        )
        assert session_cookie.path == "/"
        assert not session_cookie.secure
        assert session_cookie.has_nonstandard_attr("HttpOnly")
        assert str(session_cookie.get_nonstandard_attr("SameSite")).lower() == "strict"
        # SEC-27: header bảo mật trên response đã xác thực của admin-web thật.
        with client.open(f"http://127.0.0.1:{port}/api/v1/auth/me", timeout=10) as me_response:
            assert me_response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
            assert me_response.headers["X-Content-Type-Options"] == "nosniff"
            assert me_response.headers["Referrer-Policy"] == "no-referrer"
            assert me_response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
            assert me_response.headers["Cache-Control"] == "no-store"
            assert me_response.headers.get("Strict-Transport-Security") is None
        identity_status, identity = api_request("GET", "/api/v1/auth/me")
        assert identity_status == 200
        assert identity["roles"] == ["owner"]
        rejected_status, _ = api_request(
            "POST",
            "/api/v1/risk-profiles",
            {
                "name": "Conservative",
                "max_risk_fraction": "0.01",
                "settings": {
                    "schema_version": 1,
                    "hard_caps": {
                        "max_aggregate_open_risk": "250",
                        "max_notional": "10000",
                        "max_leverage": "5",
                        "max_positions": 2,
                        "max_symbol_notional": "2000",
                        "max_concentration_fraction": "0.2",
                        "max_daily_loss": "200",
                        "max_rolling_loss": "500",
                        "max_account_age_seconds": 30,
                        "max_data_age_seconds": 300,
                    },
                },
            },
        )
        assert rejected_status == 403
        risk_status, risk_profile = api_request(
            "POST",
            "/api/v1/risk-profiles",
            {
                "name": "Conservative",
                "max_risk_fraction": "0.01",
                "settings": {
                    "schema_version": 1,
                    "hard_caps": {
                        "max_aggregate_open_risk": "250",
                        "max_notional": "10000",
                        "max_leverage": "5",
                        "max_positions": 2,
                        "max_symbol_notional": "2000",
                        "max_concentration_fraction": "0.2",
                        "max_daily_loss": "200",
                        "max_rolling_loss": "500",
                        "max_account_age_seconds": 30,
                        "max_data_age_seconds": 300,
                    },
                },
            },
            {"X-CSRF-Token": csrf_token},
        )
        assert risk_status == 201
        current_status, current_settings = api_request("GET", "/api/v1/settings/current")
        assert current_status == 200
        assert current_settings == {"current": None}
        root_settings = {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT"],
                "timeframes": ["5m", "1h", "4h"],
                "max_age_seconds": 30,
            },
            "features": {
                "formula_version": "crypidian-features-v1",
                "sessions_timezone": "UTC",
            },
            "strategy": {
                "default_release": "h01-v1",
                "parameters": {},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {"profile_version_id": risk_profile["id"]},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
        settings_headers = {"X-CSRF-Token": csrf_token}
        create_status, created_draft = api_request(
            "POST",
            "/api/v1/settings/drafts",
            {"settings": root_settings, "expected_parent_version_id": None},
            settings_headers,
        )
        assert create_status == 201
        draft_id = str(created_draft["draft"]["id"])
        validate_status, validation = api_request(
            "POST", f"/api/v1/settings/drafts/{draft_id}/validate", headers=settings_headers
        )
        assert validate_status == 200
        assert validation["valid"] is True
        publish_status, published = api_request(
            "POST",
            f"/api/v1/settings/drafts/{draft_id}/publish",
            {"expected_revision": 1},
            settings_headers,
        )
        assert publish_status == 200
        assert published["version"]["status"] == "PUBLISHED"
        parent_id = str(published["version"]["id"])
        competing_settings = root_settings | {
            "strategy": root_settings["strategy"] | {"default_release": "h03-v1"}
        }
        competing_drafts = []
        for _ in range(2):
            status, body = api_request(
                "POST",
                "/api/v1/settings/drafts",
                {"settings": competing_settings, "expected_parent_version_id": parent_id},
                settings_headers,
            )
            assert status == 201
            competing_drafts.append(str(body["draft"]["id"]))
        winner_status, winner = api_request(
            "POST",
            f"/api/v1/settings/drafts/{competing_drafts[0]}/publish",
            {"expected_revision": 1},
            settings_headers,
        )
        assert winner_status == 200
        stale_status, _ = api_request(
            "POST",
            f"/api/v1/settings/drafts/{competing_drafts[1]}/publish",
            {"expected_revision": 1},
            settings_headers,
        )
        assert stale_status == 409
        rollback_status, rollback = api_request(
            "POST",
            "/api/v1/settings/versions/1/rollback",
            {"reason": "Compose E2E rollback"},
            settings_headers,
        )
        assert rollback_status == 200
        assert rollback["version"]["status"] == "PUBLISHED"
        assert rollback["version"]["parent_version_id"] == winner["version"]["id"]
        versions_status, versions = api_request("GET", "/api/v1/settings/versions")
        assert versions_status == 200
        assert len(versions["versions"]) == 4
        logout_status, _ = api_request(
            "POST", "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        assert logout_status == 204
        revoked_status, _ = api_request("GET", "/api/v1/auth/me")
        assert revoked_status == 401

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
            "'strategy_release','outbox_event','consumer_receipt','audit_event',"
            "'workspace_settings_version','market_instrument','market_raw_reference',"
            "'market_bar','market_funding','market_open_interest','ingestion_cursor',"
            "'ingestion_gap')",
        )
        assert schema_tables.stdout.strip() == "26"
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
            "|| ':' || has_table_privilege('crypidian_trainer_worker','user_account','SELECT') "
            "|| ':' || has_table_privilege('crypidian_admin_web','audit_event','SELECT') "
            "|| ':' || has_table_privilege('crypidian_trainer_worker','audit_event','SELECT')",
        )
        assert identity_access.stdout.strip() == "true:false:true:false"
        compose("run", "--rm", "migrator", "alembic", "check")
        settings_probe_code = dedent(
            """\
            import asyncio
            from dataclasses import dataclass, replace
            import os
            from datetime import UTC, datetime
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import select, text, update
            from sqlalchemy.exc import SQLAlchemyError
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine, database_url_from_secret,
            )
            from trading_bot.adapters.database.models import (
                RiskProfileVersion, UserAccount, UserMembership, WorkspaceSettingsVersion,
            )
            from trading_bot.adapters.database.repositories.workspace_settings import (
                SettingsVersionConflict, WorkspaceSettingsRepository,
            )
            from trading_bot.config.workspace_settings import WorkspaceSettings

            async def main():
                workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host='postgres', database='crypidian',
                )
                engine = create_database_engine(url)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    membership_id = await session.scalar(
                        select(UserMembership.id).join(
                            UserAccount, UserAccount.id == UserMembership.user_id
                        ).where(
                            UserMembership.workspace_id == workspace_id,
                            UserAccount.email_normalized == 'owner@example.test',
                        )
                    )
                    risk_id = await session.scalar(select(RiskProfileVersion.id).where(
                        RiskProfileVersion.workspace_id == workspace_id
                    ))
                    assert membership_id is not None and risk_id is not None
                    payload = {
                        'schema_version': 1,
                        'runtime': {'mode': 'PAPER', 'timezone': 'UTC'},
                        'market_data': {
                            'venue': 'BINANCE_USDM', 'symbols': ['BTCUSDT'],
                            'timeframes': ['5m', '1h', '4h'], 'max_age_seconds': 30,
                        },
                        'features': {
                            'formula_version': 'crypidian-features-v1',
                            'sessions_timezone': 'UTC',
                        },
                        'strategy': {
                            'default_release': 'h01-v1', 'parameters': {},
                            'entry_timeframe': '1h', 'confirmation_timeframe': '4h',
                        },
                        'risk': {'profile_version_id': str(risk_id)},
                        'paper_execution': {
                            'quote_asset': 'USDT', 'starting_equity': '10000',
                            'fee_schedule_version': 'paper-fees-v1',
                            'fill_model': 'next_bar_with_adverse_costs',
                        },
                    }
                    settings = WorkspaceSettings.model_validate(payload)
                    repository = WorkspaceSettingsRepository(session)
                    current = await repository.get_effective(workspace_id=workspace_id)
                    assert current is not None
                    draft = await repository.create_draft(
                        workspace_id=workspace_id, settings=settings, created_by=membership_id,
                        parent_version_id=current.id,
                    )
                    edited_strategy = payload['strategy'] | {'default_release': 'h03-v1'}
                    edited = payload | {'strategy': edited_strategy}
                    settings = WorkspaceSettings.model_validate(edited)
                    revised = await repository.update_draft(
                        workspace_id=workspace_id, draft_id=draft.id,
                        expected_revision=1, settings=WorkspaceSettings.model_validate(edited),
                    )
                    assert revised.revision == 2
                    try:
                        await repository.publish_draft(
                            workspace_id=workspace_id, draft_id=draft.id,
                            expected_revision=1, approved_by=membership_id,
                            expected_parent_version_id=current.id,
                        )
                    except SettingsVersionConflict:
                        pass
                    else:
                        raise AssertionError('stale revision was published')
                    published = await repository.publish_draft(
                        workspace_id=workspace_id, draft_id=draft.id,
                        expected_revision=2, approved_by=membership_id,
                        expected_parent_version_id=current.id,
                    )
                    assert published.status == 'PUBLISHED'
                    assert published.settings_hash == revised.settings_hash
                    current = await repository.get_effective(workspace_id=workspace_id)
                    assert current is not None and current.id == published.id
                    effective_config = await repository.load_effective_config(
                        workspace_id=workspace_id
                    )
                    assert effective_config is not None
                    assert effective_config.settings_version_id == published.id
                    assert effective_config.settings_hash == published.settings_hash
                    assert effective_config.settings.strategy.default_release == 'h03-v1'
                    published_id = published.id

                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    repository = WorkspaceSettingsRepository(session)
                    concurrent_drafts = [
                        await repository.create_draft(
                            workspace_id=workspace_id,
                            settings=settings,
                            created_by=membership_id,
                            parent_version_id=published_id,
                        )
                        for _ in range(2)
                    ]
                    concurrent_draft_ids = [draft.id for draft in concurrent_drafts]

                async def attempt_publish(draft_id):
                    try:
                        async with sessions() as session, session.begin():
                            await session.execute(text(
                                "SELECT set_config('app.workspace_id', :workspace_id, true)"
                            ), {'workspace_id': str(workspace_id)})
                            return await WorkspaceSettingsRepository(session).publish_draft(
                                workspace_id=workspace_id,
                                draft_id=draft_id,
                                expected_revision=1,
                                approved_by=membership_id,
                                expected_parent_version_id=published_id,
                            )
                    except SettingsVersionConflict:
                        return None

                publish_results = await asyncio.gather(*(
                    attempt_publish(draft_id) for draft_id in concurrent_draft_ids
                ))
                assert sum(result is not None for result in publish_results) == 1
                assert sum(result is None for result in publish_results) == 1
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    effective_config = await WorkspaceSettingsRepository(
                        session
                    ).load_effective_config(workspace_id=workspace_id)
                    assert effective_config is not None
                    assert effective_config.settings.strategy.default_release == 'h03-v1'
                    published_id = effective_config.settings_version_id

                immutable = False
                try:
                    async with sessions() as session, session.begin():
                        await session.execute(text(
                            "SELECT set_config('app.workspace_id', :workspace_id, true)"
                        ), {'workspace_id': str(workspace_id)})
                        await session.execute(update(WorkspaceSettingsVersion).where(
                            WorkspaceSettingsVersion.workspace_id == workspace_id,
                            WorkspaceSettingsVersion.id == published_id,
                        ).values(settings_hash='0' * 64))
                except SQLAlchemyError:
                    immutable = True
                assert immutable
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(uuid4())})
                    hidden = await session.scalar(select(WorkspaceSettingsVersion.id).where(
                        WorkspaceSettingsVersion.id == published_id
                    ))
                    assert hidden is None
                await engine.dispose()
                print('published_version_is_immutable_and_tenant_scoped')
                print(f'published_settings_id={published_id}')

            asyncio.run(main())
            """
        )
        settings_probe = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={bootstrap['workspace_id']}",
            "admin-web",
            "python",
            "-c",
            settings_probe_code,
        )
        assert "published_version_is_immutable_and_tenant_scoped" in settings_probe.stdout
        published_settings_id = next(
            line.split("=", 1)[1]
            for line in settings_probe.stdout.splitlines()
            if line.startswith("published_settings_id=")
        )
        race_deadline = time.monotonic() + 35
        while pyotp.TOTP(totp_secret).now() == initial_totp_code:
            if time.monotonic() >= race_deadline:
                pytest.fail("TOTP chưa chuyển sang cửa sổ hợp lệ tiếp theo cho phiên E2E.")
            time.sleep(0.25)
        race_login_status, race_login = api_request(
            "POST",
            "/api/v1/auth/login",
            {
                "workspace_slug": "paper-workspace",
                "email": "owner@example.test",
                "password": "a long owner password for test",
                "code": pyotp.TOTP(totp_secret).now(),
            },
        )
        assert race_login_status == 200
        race_csrf_token = str(race_login["csrf_token"])
        race_session_cookie = next(
            cookie for cookie in cookie_jar if cookie.name == LOCAL_SESSION_COOKIE_NAME
        )
        race_strategy = competing_settings["strategy"] | {"parameters": {"donchian_lookback": 56}}
        race_settings = competing_settings | {"strategy": race_strategy}
        race_draft_status, race_draft_response = api_request(
            "POST",
            "/api/v1/settings/drafts",
            {
                "settings": race_settings,
                "expected_parent_version_id": published_settings_id,
            },
            {"X-CSRF-Token": race_csrf_token},
        )
        assert race_draft_status == 201
        race_draft_id = str(race_draft_response["draft"]["id"])
        # SEC-08/SECT-05: reauthenticate sai 5 lần → lần 6 bị 429 trước khi kiểm mật khẩu;
        # phiên không bị thu hồi. Đặt sau lần login cuối của owner vì bộ đếm IP+email
        # cũng chặn login của chính tài khoản này trong cửa sổ 5 phút.
        reauth_statuses = [
            api_request(
                "POST",
                "/api/v1/auth/reauthenticate",
                {"password": "not the owner password", "code": "000000"},
                {"X-CSRF-Token": race_csrf_token},
            )
            for _ in range(6)
        ]
        assert [status for status, _ in reauth_statuses] == [401] * 5 + [429]
        assert reauth_statuses[-1][1] == {"detail": "Thử lại sau khi hết thời gian giới hạn"}
        assert reauth_statuses[0][1] == {"detail": "Thông tin xác thực không hợp lệ"}
        assert api_request("GET", "/api/v1/auth/me")[0] == 200
        environment["CRYPIDIAN_E2E_SESSION_COOKIE"] = (
            f"{race_session_cookie.name}={race_session_cookie.value}"
        )
        environment["CRYPIDIAN_E2E_CSRF_TOKEN"] = race_csrf_token
        environment["CRYPIDIAN_E2E_SETTINGS_DRAFT_ID"] = race_draft_id
        from trading_bot.domain.strategy.registry import StrategyRegistry

        h03_content_hash = StrategyRegistry().get_release("h03-v1").content_hash
        paper_context_seed = compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-c",
            "INSERT INTO trading_account(id,workspace_id,name,venue,mode,environment,status,"
            "max_notional) VALUES ('10000000-0000-0000-0000-000000000003',"
            f"'{bootstrap['workspace_id']}','paper-e2e-account','binance_usdm','PAPER','local',"
            "'DISABLED',10000); INSERT INTO strategy_release(id,workspace_id,name,version,"
            "status,recipe,content_hash) VALUES ('20000000-0000-0000-0000-000000000001',"
            f"'{bootstrap['workspace_id']}','h01-v1',1,'PUBLISHED','{{}}'::jsonb,'{'a' * 64}'),"
            "('20000000-0000-0000-0000-000000000002',"
            f"'{bootstrap['workspace_id']}','h03-v1',1,'PUBLISHED','{{}}'::jsonb,"
            f"'{h03_content_hash}')",
        )
        assert "INSERT 0 1" in paper_context_seed.stdout
        assert "INSERT 0 2" in paper_context_seed.stdout
        market_probe_code = dedent(
            """\
            import asyncio
            import json
            from dataclasses import dataclass, replace
            from datetime import UTC, datetime, timedelta
            import os
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine, database_url_from_secret,
            )
            from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
            from trading_bot.adapters.database.repositories.feature_snapshots import (
                FeatureSnapshotRepository,
            )
            from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
            from trading_bot.domain.market.feature_snapshot import (
                FeatureCalculator,
                FeatureSettings,
            )
            from trading_bot.domain.market.models import MarketSnapshot
            from sqlalchemy import select
            from sqlalchemy.exc import SQLAlchemyError
            from trading_bot.domain.market.ingestion import (
                IngestionDisposition, InstrumentObservation, MarketRecordVersion, MarketStream,
            )
            from trading_bot.domain.market.models import Instrument, OHLCVBar

            async def main():
                workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host='postgres', database='crypidian',
                )
                engine = create_database_engine(url)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                now = datetime.now(UTC)
                stream = MarketStream('BINANCE_USDM', 'BTCUSDT', '1m')
                open_time = now - timedelta(minutes=2)
                close_time = open_time + timedelta(minutes=1)
                received_at = close_time + timedelta(milliseconds=1)
                available_at = received_at + timedelta(milliseconds=1)
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    repository = MarketDataRepository(session)
                    observation = InstrumentObservation(
                        instrument=Instrument(
                            symbol='BTCUSDT', base_asset='BTC', quote_asset='USDT',
                            tick_size=Decimal('0.1'), quantity_step=Decimal('0.001'),
                            min_notional=Decimal('5'),
                        ), source='BINANCE_USDM', observed_at=now,
                    )
                    instrument = await repository.append_instrument(
                        workspace_id=workspace_id, observation=observation,
                        idempotency_key='e2e:exchange-info:BTCUSDT',
                    )
                    bar = OHLCVBar(
                        symbol='BTCUSDT', timeframe='1m', open_time=open_time,
                        close_time=close_time, event_at=close_time,
                        received_at=received_at, available_at=available_at,
                        open=Decimal('100'), high=Decimal('102'), low=Decimal('99'),
                        close=Decimal('101'), volume=Decimal('10.25000000'),
                        is_closed=True, source='BINANCE_USDM',
                        source_cursor='e2e:BTCUSDT:1m', checksum='a' * 64,
                    )
                    version = MarketRecordVersion(
                        record_id=uuid4(), stream=stream,
                        logical_key=f'BTCUSDT:1m:{open_time.isoformat()}',
                        idempotency_key='e2e:BTCUSDT:1m:bar', revision=1,
                        content_checksum='a' * 64, event_at=close_time,
                        received_at=received_at, available_at=available_at,
                    )
                    first = await repository.append_bar(
                        workspace_id=workspace_id, instrument_id=instrument.record_id,
                        bar=bar, version=version,
                    )
                    retry = await repository.append_bar(
                        workspace_id=workspace_id, instrument_id=instrument.record_id,
                        bar=bar, version=version,
                    )
                    assert first.disposition is IngestionDisposition.ACCEPT
                    assert retry.disposition is IngestionDisposition.DUPLICATE
                    before_available = await repository.list_bars(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1m',
                        start=open_time, end=open_time + timedelta(minutes=1),
                        as_of=available_at - timedelta(microseconds=1),
                    )
                    visible = await repository.list_bars(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1m',
                        start=open_time, end=open_time + timedelta(minutes=1),
                        as_of=available_at, closed_only=True,
                    )
                    assert before_available == []
                    assert len(visible) == 1 and visible[0].volume == Decimal('10.25000000')
                    feature_settings = FeatureSettings(
                        code_hash='c' * 64,
                        expected_interval=timedelta(minutes=1),
                        max_event_age=timedelta(minutes=2),
                        regime_history=timedelta(minutes=1),
                        sma_period=1,
                        ema_periods=(1,),
                        atr_period=1,
                        rsi_period=1,
                        adx_period=1,
                        bollinger_period=1,
                        realized_volatility_period=1,
                        efficiency_ratio_period=1,
                        zscore_period=1,
                    )
                    feature_snapshot = FeatureCalculator(feature_settings).calculate(
                        MarketSnapshot(as_of=available_at, bars=tuple(visible)),
                        workspace_id=workspace_id,
                        input_event_ids=('e2e:bar-event',),
                        computed_at=now,
                    )
                    feature_repository = FeatureSnapshotRepository(session)
                    stored = await feature_repository.append(
                        workspace_id=workspace_id, snapshot=feature_snapshot,
                    )
                    duplicate = await feature_repository.append(
                        workspace_id=workspace_id, snapshot=feature_snapshot,
                    )
                    assert stored.duplicate is False and duplicate.duplicate is True
                    before_computed = await feature_repository.get_as_of(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1m',
                        as_of=now - timedelta(microseconds=1),
                    )
                    restored = await feature_repository.get_as_of(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1m',
                        as_of=now + timedelta(microseconds=1),
                        schema_hash=feature_snapshot.schema_hash,
                        formula_hash=feature_snapshot.formula_hash,
                        code_hash=feature_snapshot.code_hash,
                    )
                    assert before_computed is None
                    assert restored is not None
                    assert restored.canonical_json() == feature_snapshot.canonical_json()
                    try:
                        async with session.begin_nested():
                            await session.execute(
                                select(FeatureSnapshotRecord).where(
                                    FeatureSnapshotRecord.id == stored.record_id
                                ).with_for_update()
                            )
                            await session.execute(text(
                                "UPDATE feature_snapshot SET is_usable = NOT is_usable "
                                "WHERE workspace_id = :workspace_id AND id = :record_id"
                            ), {
                                'workspace_id': str(workspace_id),
                                'record_id': str(stored.record_id),
                            })
                    except SQLAlchemyError:
                        feature_is_immutable = True
                    else:
                        feature_is_immutable = False
                    assert feature_is_immutable
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(uuid4())})
                    hidden = await repository.list_bars(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1m',
                        start=open_time, end=open_time + timedelta(minutes=1), as_of=now,
                    )
                    assert hidden == []
                await engine.dispose()
                print('market_and_feature_as_of_idempotency_lineage_immutable_rls_verified')
                print(f'feature_snapshot_id={feature_snapshot.snapshot_id}')

            asyncio.run(main())
            """
        )
        market_probe = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={bootstrap['workspace_id']}",
            "admin-web",
            "python",
            "-c",
            market_probe_code,
        )
        assert (
            "market_and_feature_as_of_idempotency_lineage_immutable_rls_verified"
            in market_probe.stdout
        )
        feature_snapshot_id = next(
            line.split("=", 1)[1]
            for line in market_probe.stdout.splitlines()
            if line.startswith("feature_snapshot_id=")
        )
        assert len(feature_snapshot_id) == 64
        outbox_probe_code = dedent(
            """\
            import asyncio
            import json
            import os
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import func, select, text, update
            from sqlalchemy.exc import SQLAlchemyError
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine,
                database_url_from_secret,
            )
            from trading_bot.adapters.database.models import (
                AuditEvent,
                ConsumerReceipt,
                OutboxEvent,
                RiskProfileVersion,
            )
            from trading_bot.application.outbox import OutboxRepository, UnitOfWork
            from trading_bot.domain.events import EventEnvelope

            async def main():
                workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host='postgres',
                    database='crypidian',
                )
                engine = create_database_engine(url)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                first_id = uuid4()
                now = datetime.now(UTC)
                envelope = EventEnvelope(
                    event_type='risk.profile.created.v1',
                    workspace_id=workspace_id,
                    aggregate_type='risk_profile',
                    aggregate_id=uuid4(),
                    occurred_at=now,
                    correlation_id=uuid4(),
                    idempotency_key='e2e:retry',
                    schema_version=1,
                    payload={'risk_profile_id': str(uuid4())},
                    event_id=first_id,
                )
                rollback_ok = False
                try:
                    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                        unit.session.add(RiskProfileVersion(
                            workspace_id=workspace_id,
                            name='Rollback probe',
                            version=1,
                            max_risk_fraction=Decimal('0.01'),
                            settings={},
                            is_active=False,
                        ))
                        unit.enqueue(envelope)
                        unit.record_audit(action='e2e.create', target_type='e2e_outbox')
                        raise RuntimeError('intentional rollback probe')
                except RuntimeError:
                    rollback_ok = True
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    rollback_rows = await session.scalar(select(func.count()).select_from(
                        OutboxEvent).where(OutboxEvent.id == first_id))
                    rollback_audit = await session.scalar(select(func.count()).select_from(
                        AuditEvent).where(AuditEvent.target_type == 'e2e_outbox'))
                    rollback_profile = await session.scalar(select(func.count()).select_from(
                        RiskProfileVersion).where(RiskProfileVersion.name == 'Rollback probe'))
                assert rollback_ok and rollback_rows == 0 and rollback_audit == 0
                assert rollback_profile == 0

                async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                    unit.session.add(RiskProfileVersion(
                        workspace_id=workspace_id,
                        name='Outbox probe',
                        version=1,
                        max_risk_fraction=Decimal('0.01'),
                        settings={},
                        is_active=False,
                    ))
                    unit.enqueue(envelope)
                    unit.record_audit(
                        action='e2e.create',
                        target_type='e2e_outbox',
                        target_id=envelope.aggregate_id,
                        reason='test',
                        after_state={'status': 'PAPER'},
                    )

                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    # Cô lập probe retry khỏi các sự kiện cấu hình phát sinh trước đó.
                    await session.execute(update(OutboxEvent).where(
                        OutboxEvent.workspace_id == workspace_id,
                        OutboxEvent.id != first_id,
                        OutboxEvent.status == 'PENDING',
                    ).values(status='DELIVERED', completed_at=now))
                    repository = OutboxRepository(session)
                    claim_time = now + timedelta(seconds=1)
                    claimed = await repository.claim_ready(
                        workspace_id=workspace_id,
                        worker_id='e2e-worker-a',
                        now=claim_time,
                        lease_duration=timedelta(seconds=30),
                        max_attempts=2,
                    )
                    assert [event.id for event in claimed] == [first_id]
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    repository = OutboxRepository(session)
                    assert await repository.claim_ready(
                        workspace_id=workspace_id,
                        worker_id='e2e-worker-b',
                        now=claim_time,
                        lease_duration=timedelta(seconds=30),
                        max_attempts=2,
                    ) == []
                    assert await repository.record_failure(
                        workspace_id=workspace_id,
                        event_id=first_id,
                        worker_id='e2e-worker-a',
                        now=claim_time,
                        error=RuntimeError('sensitive secret must not be stored'),
                        max_attempts=2,
                    ) == 'PENDING'
                retry_time = claim_time + timedelta(seconds=3)
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    repository = OutboxRepository(session)
                    retried = await repository.claim_ready(
                        workspace_id=workspace_id,
                        worker_id='e2e-worker-a',
                        now=retry_time,
                        lease_duration=timedelta(seconds=30),
                        max_attempts=2,
                    )
                    assert [event.attempts for event in retried] == [2]
                    assert await repository.record_failure(
                        workspace_id=workspace_id,
                        event_id=first_id,
                        worker_id='e2e-worker-a',
                        now=retry_time,
                        error=RuntimeError('sensitive secret must not be stored'),
                        max_attempts=2,
                    ) == 'DEAD'

                delivered_id = uuid4()
                delivered_envelope = EventEnvelope(
                    event_type='risk.profile.created.v1',
                    workspace_id=workspace_id,
                    aggregate_type='risk_profile',
                    aggregate_id=uuid4(),
                    occurred_at=retry_time,
                    correlation_id=uuid4(),
                    idempotency_key='e2e:delivered',
                    schema_version=1,
                    payload={'risk_profile_id': str(uuid4())},
                    event_id=delivered_id,
                )
                async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                    unit.enqueue(delivered_envelope)
                delivered_time = datetime.now(UTC) + timedelta(seconds=1)
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    repository = OutboxRepository(session)
                    delivered = await repository.claim_ready(
                        workspace_id=workspace_id,
                        worker_id='e2e-worker-a',
                        now=delivered_time,
                        lease_duration=timedelta(seconds=30),
                        max_attempts=2,
                    )
                    assert [event.id for event in delivered] == [delivered_id]
                    assert await repository.acknowledge(
                        workspace_id=workspace_id,
                        event_id=delivered_id,
                        worker_id='e2e-worker-a',
                        consumer_name='risk-profile-consumer',
                        now=delivered_time,
                    ) is True
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    audit_row = await session.scalar(select(AuditEvent).where(
                        AuditEvent.target_type == 'e2e_outbox'))
                    assert audit_row is not None and audit_row.after_hash is not None
                    try:
                        async with session.begin_nested():
                            await session.execute(update(AuditEvent).where(
                                AuditEvent.id == audit_row.id).values(reason='tampered'))
                    except SQLAlchemyError:
                        audit_is_immutable = True
                    else:
                        audit_is_immutable = False
                assert audit_is_immutable
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    first = await session.get(OutboxEvent, first_id)
                    final = await session.get(OutboxEvent, delivered_id)
                    receipts = await session.scalar(select(func.count()).select_from(
                        ConsumerReceipt).where(ConsumerReceipt.outbox_event_id == delivered_id))
                    assert first is not None and first.status == 'DEAD'
                    assert first.last_error_code == 'RuntimeError'
                    assert final is not None and final.status == 'DELIVERED'
                    assert receipts == 1
                await engine.dispose()
                print(json.dumps({
                    'rollback': True,
                    'retry': 'DEAD',
                    'receipt_count': receipts,
                    'audit_immutable': audit_is_immutable,
                }))

            asyncio.run(main())
            """
        )
        outbox_probe = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={bootstrap['workspace_id']}",
            "admin-web",
            "python",
            "-c",
            outbox_probe_code,
        )
        assert '"rollback": true' in outbox_probe.stdout
        assert '"retry": "DEAD"' in outbox_probe.stdout
        assert '"receipt_count": 1' in outbox_probe.stdout
        assert '"audit_immutable": true' in outbox_probe.stdout
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
        risk_reservation_probe_code = dedent(
            """\
            import asyncio
            import json
            from dataclasses import dataclass, replace
            from datetime import UTC, datetime, timedelta
            import os
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import func, select, text
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine,
                database_url_from_secret,
            )
            from trading_bot.adapters.database.models import RiskReservationRecord
            from trading_bot.adapters.database.repositories.risk_reservations import (
                RiskReservationRepository,
            )
            from trading_bot.domain.risk.engine import RiskDecision
            from trading_bot.domain.risk.planner import calculate_plan_hash
            from trading_bot.domain.risk.reservations import ReservationConflict

            workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
            account_id = UUID('10000000-0000-0000-0000-000000000003')
            settings_version_id = os.environ['CRYPIDIAN_E2E_SETTINGS_VERSION_ID']
            candidate_expires_at = datetime.now(UTC) + timedelta(hours=1)

            @dataclass(frozen=True)
            class PlanFixture:
                workspace_id: str
                account_id: str
                plan_hash: str
                symbol: str
                notional: Decimal
                evidence: tuple[str, ...]
                quantity: Decimal
                direction: str
                settings_version_id: str
                strategy_release_id: str
                release_content_hash: str
                snapshot_id: str

            def make_plan(label):
                plan = PlanFixture(
                    workspace_id=str(workspace_id),
                    account_id=str(account_id),
                    plan_hash='',
                    symbol='BTCUSDT',
                    notional=Decimal('100'),
                    evidence=(label,),
                    quantity=Decimal('1'),
                    direction='LONG',
                    settings_version_id=settings_version_id,
                    strategy_release_id='h01-v1',
                    release_content_hash='a' * 64,
                    snapshot_id=os.environ['CRYPIDIAN_E2E_FEATURE_SNAPSHOT_ID'],
                )
                return replace(plan, plan_hash=calculate_plan_hash(plan))

            def approved_decision(plan_hash, *, pending_count=0, pending_risk='0',
                                  pending_margin='0', requested_risk='60',
                                  requested_margin='60', pending_notional='0'):
                return RiskDecision(
                    approved=True,
                    reason_code='APPROVED',
                    detail_vi='Quyết định kiểm thử concurrency.',
                    plan_hash=plan_hash,
                    workspace_id=str(workspace_id),
                    account_id=str(account_id),
                    settings_version_id=settings_version_id,
                    risk_profile_version_id='e2e-risk-v1',
                    requested_risk=Decimal(requested_risk),
                    open_risk=Decimal(pending_risk),
                    max_aggregate_open_risk=Decimal('100'),
                    candidate_expires_at=candidate_expires_at,
                    snapshot_id='e2e-market-snapshot',
                    open_position_count=0,
                    max_positions=2,
                    pending_reservation_count=pending_count,
                    pending_reservation_risk=Decimal(pending_risk),
                    symbol='BTCUSDT',
                    requested_notional=Decimal('100'),
                    open_notional=Decimal(pending_notional),
                    symbol_open_notional=Decimal(pending_notional),
                    max_notional=Decimal('1000'),
                    max_symbol_notional=Decimal('500'),
                    pending_reservation_notional=Decimal(pending_notional),
                    pending_symbol_reservation_notional=Decimal(pending_notional),
                    available_margin=Decimal('100'),
                    requested_initial_margin=Decimal(requested_margin),
                    pending_reservation_initial_margin=Decimal(pending_margin),
                )

            async def main():
                url = database_url_from_secret(
                    username=os.environ['TRADING_BOT_DATABASE_USER'],
                    password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                    host='postgres',
                    database='crypidian',
                )
                engine = create_database_engine(url)
                sessions = async_sessionmaker(engine, expire_on_commit=False)

                async def reserve(key):
                    try:
                        async with sessions() as session, session.begin():
                            await session.execute(text(
                                "SELECT set_config('app.workspace_id', :workspace_id, true)"
                            ), {'workspace_id': str(workspace_id)})
                            repository = RiskReservationRepository(session)
                            plan = make_plan('concurrent')
                            decision = approved_decision(plan.plan_hash)
                            evaluation = await repository.record_evaluation(
                                plan=plan, decision=decision
                            )
                            result = await repository.reserve(
                                decision,
                                idempotency_key=key,
                                risk_decision_id=evaluation.risk_decision_id,
                            )
                            return {
                                'status': result.status.value,
                                'reservation_id': str(result.reservation_id),
                                'risk_decision_id': str(evaluation.risk_decision_id),
                                'plan_hash': plan.plan_hash,
                            }
                    except ReservationConflict:
                        return {'status': 'CONFLICT'}

                outcomes = await asyncio.gather(reserve('e2e-risk-a'), reserve('e2e-risk-b'))
                winner = next(item for item in outcomes if item['status'] == 'RESERVED')
                margin_conflict = False
                async with sessions() as session, session.begin():
                    await session.execute(text(
                        "SELECT set_config('app.workspace_id', :workspace_id, true)"
                    ), {'workspace_id': str(workspace_id)})
                    repository = RiskReservationRepository(session)
                    margin_plan = make_plan('margin-recheck')
                    margin_decision = approved_decision(
                        margin_plan.plan_hash,
                        pending_count=1,
                        pending_risk='60',
                        pending_margin='60',
                        requested_risk='10',
                        requested_margin='60',
                        pending_notional='100',
                    )
                    evaluation = await repository.record_evaluation(
                        plan=margin_plan, decision=margin_decision
                    )
                    try:
                        await repository.reserve(
                            margin_decision,
                            idempotency_key='e2e-margin-overcommit',
                            risk_decision_id=evaluation.risk_decision_id,
                        )
                    except ReservationConflict as error:
                        margin_conflict = 'margin' in str(error).lower()
                    rows = await session.execute(select(
                        func.count(RiskReservationRecord.id),
                        func.coalesce(func.sum(RiskReservationRecord.reserved_risk), Decimal('0')),
                    ).where(
                        RiskReservationRecord.workspace_id == workspace_id,
                        RiskReservationRecord.account_id == account_id,
                    ))
                    count, reserved_risk = rows.one()
                await engine.dispose()
                assert sum(item['status'] == 'RESERVED' for item in outcomes) == 1, outcomes
                assert sum(item['status'] == 'CONFLICT' for item in outcomes) == 1, outcomes
                assert margin_conflict
                assert count == 1 and reserved_risk == Decimal('60')
                print(json.dumps({
                    'outcomes': outcomes,
                    'winner': winner,
                    'count': count,
                    'reserved_risk': str(reserved_risk),
                    'margin_overcommit_blocked': margin_conflict,
                }))

            asyncio.run(main())
            """
        )
        risk_reservation_probe = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={bootstrap['workspace_id']}",
            "--env",
            f"CRYPIDIAN_E2E_SETTINGS_VERSION_ID={published_settings_id}",
            "--env",
            f"CRYPIDIAN_E2E_FEATURE_SNAPSHOT_ID={feature_snapshot_id}",
            "trading-runtime",
            "python",
            "-c",
            risk_reservation_probe_code,
        )
        risk_result = json.loads(risk_reservation_probe.stdout.strip())
        assert [item["status"] for item in risk_result["outcomes"]].count("RESERVED") == 1
        assert [item["status"] for item in risk_result["outcomes"]].count("CONFLICT") == 1
        assert Decimal(risk_result["reserved_risk"]) == Decimal("60")
        assert risk_result["margin_overcommit_blocked"] is True
        paper_ledger_probe_code = dedent(
            """\
            import asyncio
            import json
            import os
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import func, select
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine, database_url_from_secret,
            )
            from trading_bot.adapters.database.models.paper_entities import (
                FillRecord, OrderIntentRecord, PaperExecutionCheckpointRecord,
                PositionEventRecord, TradeCostRecord, TradeRecord,
            )
            from trading_bot.adapters.database.models.feature_entities import (
                FeatureSnapshotRecord,
            )
            from trading_bot.adapters.database.models.entities import (
                PaperAccountSeed,
                StrategyRelease as StrategyReleaseRecord,
                WorkspaceSettingsVersion,
            )
            from trading_bot.adapters.database.repositories.paper_ledger import (
                PaperLedgerRepository,
            )
            from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
            from trading_bot.adapters.database.repositories.feature_snapshots import (
                FeatureSnapshotRepository,
            )
            from trading_bot.application.outbox import UnitOfWork

            workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
            account_id = UUID('10000000-0000-0000-0000-000000000003')
            settings_id = UUID(os.environ['CRYPIDIAN_E2E_SETTINGS_VERSION_ID'])
            strategy_release_id = UUID('20000000-0000-0000-0000-000000000001')
            feature_snapshot_id = os.environ['CRYPIDIAN_E2E_FEATURE_SNAPSHOT_ID']
            risk_decision_id = UUID(os.environ['CRYPIDIAN_E2E_RISK_DECISION_ID'])
            reservation_id = UUID(os.environ['CRYPIDIAN_E2E_RESERVATION_ID'])
            plan_hash = os.environ['CRYPIDIAN_E2E_PLAN_HASH']
            now = datetime.now(UTC)
            url = database_url_from_secret(
                username=os.environ['TRADING_BOT_DATABASE_USER'],
                password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                host='postgres', database='crypidian',
            )
            engine = create_database_engine(url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            repository = PaperLedgerRepository()
            evidence = {'bar_keys': ['fixture:bar-1'], 'feature_snapshot_id': feature_snapshot_id}

            async def main():
                async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                    args = {
                        'account_id': account_id,
                        'idempotency_key': 'e2e-paper-intent-1',
                        'plan_hash': plan_hash,
                        'settings_version_id': settings_id,
                        'strategy_release_id': strategy_release_id,
                        'strategy_release_hash': 'a' * 64,
                        'feature_snapshot_id': feature_snapshot_id,
                        'candidate_id': 'd' * 64,
                        'risk_decision_id': risk_decision_id,
                        'risk_reservation_id': reservation_id,
                        'symbol': 'BTCUSDT',
                        'side': 'LONG',
                        'quote_asset': 'USDT',
                        'quantity': Decimal('1'),
                        'created_at': now,
                        'evidence': evidence,
                    }
                    created = await repository.create_intent(uow, **args)
                    retry = await repository.create_intent(uow, **args)
                    assert not created.duplicate and retry.duplicate
                    await repository.append_fill(
                        uow, intent_id=created.intent_id, fill_key='e2e-fill-1',
                        bar_key='fixture:bar-1', fill_policy_version='paper-v1',
                        quantity=Decimal('0.4'), price=Decimal('100'),
                        fee_amount=Decimal('0.08'), slippage_amount=Decimal('0.02'),
                        occurred_at=now + timedelta(minutes=1),
                    )
                    await repository.append_fill(
                        uow, intent_id=created.intent_id, fill_key='e2e-fill-2',
                        bar_key='fixture:bar-2', fill_policy_version='paper-v1',
                        quantity=Decimal('0.6'), price=Decimal('101'),
                        fee_amount=Decimal('0.12'), slippage_amount=Decimal('0.02'),
                        occurred_at=now + timedelta(minutes=2),
                    )
                    trade = await uow.session.scalar(select(TradeRecord).where(
                        TradeRecord.workspace_id == workspace_id,
                        TradeRecord.account_id == account_id,
                        TradeRecord.intent_id == created.intent_id,
                    ))
                    assert trade is not None and trade.opened_at == now + timedelta(minutes=1)
                    await repository.append_position_event(
                        uow, intent_id=created.intent_id, event_key='e2e-position-open',
                        event_type='OPENED', quantity=Decimal('1'),
                        occurred_at=now + timedelta(minutes=2), payload={'side': 'LONG'},
                    )
                    await repository.append_trade_cost(
                        uow, trade_id=trade.id, cost_key='e2e-funding-1',
                        cost_type='FUNDING', amount=Decimal('-0.03'), currency='USDT',
                        source_event_id='fixture:funding-1',
                        occurred_at=now + timedelta(minutes=3),
                    )
                    closed = await repository.close_trade(
                        uow, trade_id=trade.id, gross_pnl=Decimal('10'),
                        closed_at=now + timedelta(minutes=4),
                    )
                    await repository.append_position_event(
                        uow, intent_id=created.intent_id, event_key='e2e-position-close',
                        event_type='CLOSED', quantity=Decimal('0'),
                        occurred_at=now + timedelta(minutes=4), payload={'reason': 'FIXTURE_EXIT'},
                    )
                    assert closed.status == 'CLOSED'
                    assert closed.fee_total == Decimal('0.20')
                    assert closed.funding_total == Decimal('-0.03')
                    assert closed.slippage_total == Decimal('0.04')
                    assert closed.net_pnl == Decimal('9.79')
                    assert closed.lineage['feature_snapshot_id'] == feature_snapshot_id
                    assert closed.lineage['risk_decision_id'] == str(risk_decision_id)
                    counts = {}
                    for name, model in (
                        ('fills', FillRecord), ('costs', TradeCostRecord),
                        ('positions', PositionEventRecord), ('trades', TradeRecord),
                    ):
                        counts[name] = await uow.session.scalar(
                            select(func.count(model.id)).where(
                                model.workspace_id == workspace_id,
                                model.account_id == account_id,
                            )
                        )
                    assert counts == {'fills': 2, 'costs': 5, 'positions': 2, 'trades': 1}
                    print(json.dumps({
                        'intent_id': str(created.intent_id), 'trade_id': str(closed.id),
                        'status': closed.status, 'net_pnl': str(closed.net_pnl),
                        'counts': counts, 'duplicate_retry': retry.duplicate,
                    }))
                await engine.dispose()

            asyncio.run(main())
            """
        )
        paper_ledger_probe = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={bootstrap['workspace_id']}",
            "--env",
            f"CRYPIDIAN_E2E_SETTINGS_VERSION_ID={published_settings_id}",
            "--env",
            f"CRYPIDIAN_E2E_FEATURE_SNAPSHOT_ID={feature_snapshot_id}",
            "--env",
            f"CRYPIDIAN_E2E_RISK_DECISION_ID={risk_result['winner']['risk_decision_id']}",
            "--env",
            f"CRYPIDIAN_E2E_RESERVATION_ID={risk_result['winner']['reservation_id']}",
            "--env",
            f"CRYPIDIAN_E2E_PLAN_HASH={risk_result['winner']['plan_hash']}",
            "trading-runtime",
            "python",
            "-c",
            paper_ledger_probe_code,
        )
        paper_result = json.loads(paper_ledger_probe.stdout.strip())
        assert paper_result["status"] == "CLOSED"
        assert Decimal(paper_result["net_pnl"]) == Decimal("9.79")
        assert paper_result["duplicate_retry"] is True
        paper_decision_cycle_probe_code = dedent(
            """\
            import asyncio
            import hashlib
            import json
            import os
            from datetime import UTC, datetime, timedelta
            from decimal import Decimal
            from pathlib import Path
            from uuid import UUID, uuid4
            from sqlalchemy import func, select, text
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from trading_bot.adapters.database.engine import (
                create_database_engine, database_url_from_secret,
            )
            from trading_bot.adapters.database.models.paper_entities import (
                FillRecord, OrderIntentRecord, PaperExecutionCheckpointRecord,
                PositionEventRecord, TradeCostRecord, TradeRecord,
            )
            from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
            from trading_bot.adapters.database.models.entities import (
                PaperAccountSeed,
                StrategyRelease as StrategyReleaseRecord,
                WorkspaceSettingsVersion,
            )
            from trading_bot.adapters.database.models.feature_entities import (
                FeatureSnapshotRecord,
            )
            from trading_bot.adapters.database.repositories.paper_ledger import (
                PaperLedgerRepository,
            )
            from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
            from trading_bot.adapters.database.repositories.feature_snapshots import (
                FeatureSnapshotRepository,
            )
            from trading_bot.adapters.database.repositories.risk_reservations import (
                RiskReservationRepository,
            )
            from trading_bot.adapters.database.repositories.paper_account_state import (
                PaperAccountStateRepository,
            )
            from trading_bot.adapters.database.repositories.paper_checkpoints import (
                PaperCheckpointRepository,
            )
            from trading_bot.adapters.database.repositories.workspace_settings import (
                WorkspaceSettingsRepository,
            )
            from trading_bot.application.outbox import UnitOfWork
            from trading_bot.application.paper_execution_profile import (
                default_paper_execution_profile_catalog,
                resolve_paper_execution_profile,
            )
            from trading_bot.application.paper_account_initialization import (
                PaperAccountInitializationService,
            )
            from trading_bot.application.paper_account_state import PaperAccountStateService
            from trading_bot.application.paper_runtime_application import (
                PaperDecisionCommand,
                PaperDecisionContext,
                PaperEntryEventCommand,
                PaperMonitorCommand,
                PaperMonitorEvidence,
                PaperRuntimeApplicationError,
            )
            from trading_bot.app import create_app
            from trading_bot.config.settings import AppSettings
            from trading_bot.application.runtime_risk_profile import load_risk_profile
            from trading_bot.application.paper_decision_cycle import (
                DecisionCycleOutcome, PaperDecisionCycle,
            )
            from trading_bot.application.paper_lifecycle import (
                PaperLifecycleConflict, PaperLifecycleCoordinator,
            )
            from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
            from trading_bot.domain.execution.paper import (
                IntentConflict,
                PaperBar,
                PaperExecutionGateway,
            )
            from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot
            from trading_bot.domain.learning.evaluation import (
                EvaluationTrade, build_evaluation_metrics,
            )
            from trading_bot.domain.market.feature_snapshot import (
                FeatureCalculator,
                FeatureSettings,
            )
            from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
            from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
            from trading_bot.domain.risk.engine import RiskEngine, RiskState
            from trading_bot.domain.risk.reservations import ReservationReleaseReason
            from trading_bot.domain.risk.planner import (
                AccountSnapshot, ExchangeRules, PlannerSettings, RiskLimits,
                TradePlanner, TradePlan,
            )
            from trading_bot.domain.strategy.contracts import (
                DecisionSnapshot, FeatureObservation, FeatureQuality, MarketSeries, SeriesQuality,
            )
            from trading_bot.domain.strategy.registry import StrategyRegistry
            from trading_bot.domain.strategy.contracts import StrategyCandidate, StrategySelection

            workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
            feature_snapshot_id = ''
            account_id = None
            url = database_url_from_secret(
                username=os.environ['TRADING_BOT_DATABASE_USER'],
                password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
                host='postgres', database='crypidian',
            )
            engine = create_database_engine(url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(UTC)
            release = StrategyRegistry().get_release('h03-v1')
            base = now - timedelta(hours=220)
            bars = []
            for index in range(220):
                opened = base + timedelta(hours=index)
                closed = opened + timedelta(hours=1)
                current = index == 219
                bars.append(OHLCVBar(
                    symbol='BTCUSDT', timeframe='1h', open_time=opened,
                    close_time=closed, event_at=closed,
                    received_at=closed + timedelta(milliseconds=1),
                    available_at=closed + timedelta(milliseconds=2),
                    open=Decimal('101') if current else Decimal('96'),
                    high=Decimal('102') if current else Decimal('100'),
                    low=Decimal('94') if current else Decimal('95'),
                    close=Decimal('101.5') if current else Decimal('96'),
                    volume=Decimal('6') if current else Decimal('5'),
                    is_closed=True, source='WP02_H03_FIXTURE',
                    source_cursor=f'cycle-bar-{index}',
                    checksum=hashlib.sha256(f'cycle-bar-{index}'.encode()).hexdigest(),
                ))

            async def main():
                async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                    published = await WorkspaceSettingsRepository(
                        uow.session
                    ).load_effective_config(workspace_id=workspace_id)
                    assert published is not None
                    execution_profile = resolve_paper_execution_profile(
                        published, default_paper_execution_profile_catalog(),
                    )
                    account_seed_service = PaperAccountInitializationService()
                    account_result = await account_seed_service.ensure_initialized(
                        uow,
                        published_settings=published,
                        execution_profile=execution_profile,
                        max_notional=Decimal('10000'),
                        evaluated_at=now,
                    )
                    account_retry = await account_seed_service.ensure_initialized(
                        uow,
                        published_settings=published,
                        execution_profile=execution_profile,
                        max_notional=Decimal('10000'),
                        evaluated_at=now,
                    )
                    # Mỗi transaction chỉ mang một bot scope; account thứ hai dùng UoW riêng.
                    async with UnitOfWork(sessions, workspace_id=workspace_id) as race_uow:
                        race_account_result = await PaperAccountInitializationService(
                            account_name='paper-e2e-race-account',
                        ).ensure_initialized(
                            race_uow,
                            published_settings=published,
                            execution_profile=execution_profile,
                            max_notional=Decimal('10000'),
                            evaluated_at=now,
                        )
                        race_genesis_gateway = PaperExecutionGateway(execution_profile.fill_policy)
                        race_genesis_key = f'h03-genesis:{race_account_result.account.id}'
                        await PaperCheckpointRepository().append(
                            race_uow,
                            account_id=race_account_result.account.id,
                            event_key=race_genesis_key,
                            event_hash=hashlib.sha256(race_genesis_key.encode()).hexdigest(),
                            snapshot=race_genesis_gateway.export_state(),
                            occurred_at=now,
                        )
                    assert account_result.created
                    assert not account_retry.created
                    assert account_retry.seed.id == account_result.seed.id
                    account_id = account_result.account.id
                    race_account_id = race_account_result.account.id
                    assert race_account_result.created
                    if account_result.created:
                        genesis_gateway = PaperExecutionGateway(execution_profile.fill_policy)
                        genesis_snapshot = genesis_gateway.export_state()
                        genesis_key = f'h03-genesis:{account_id}'
                        await PaperCheckpointRepository().append(
                            uow,
                            account_id=account_id,
                            event_key=genesis_key,
                            event_hash=hashlib.sha256(genesis_key.encode()).hexdigest(),
                            snapshot=genesis_snapshot,
                            occurred_at=now,
                        )
                    initial_account_state = await PaperAccountStateService(
                        PaperAccountStateRepository(uow.session),
                    ).get_state(workspace_id=workspace_id, account_id=account_id)
                    assert initial_account_state.equity == account_result.seed.initial_equity
                    assert initial_account_state.closed_trade_count == 0
                    market_repository = MarketDataRepository(uow.session)
                    instrument = await market_repository.get_instrument(
                        workspace_id=workspace_id, source='BINANCE_USDM',
                        symbol='BTCUSDT', as_of=now,
                    )
                    assert instrument is not None
                    stream = MarketStream('WP02_H03_FIXTURE', 'BTCUSDT', '1h')
                    for bar in bars:
                        version = MarketRecordVersion(
                            record_id=uuid4(),
                            stream=stream,
                            logical_key=f'BTCUSDT:1h:{bar.open_time.isoformat()}',
                            idempotency_key=f'h03-fixture:{bar.source_cursor}',
                            revision=1,
                            content_checksum=bar.checksum,
                            event_at=bar.event_at,
                            received_at=bar.received_at,
                            available_at=bar.available_at,
                        )
                        await market_repository.append_bar(
                            workspace_id=workspace_id,
                            instrument_id=instrument.id,
                            bar=bar,
                            version=version,
                        )
                    persisted_bars = await market_repository.list_bars(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1h',
                        start=bars[0].open_time,
                        end=bars[-1].open_time + timedelta(hours=1),
                        as_of=bars[-1].available_at, closed_only=True,
                    )
                    assert persisted_bars == bars
                    feature_settings = FeatureSettings(
                        code_hash=hashlib.sha256(b'wp02-h03-feature-fixture-v1').hexdigest(),
                        expected_interval=timedelta(hours=1),
                        max_event_age=timedelta(hours=2),
                    )
                    feature_snapshot = FeatureCalculator(feature_settings).calculate(
                        MarketSnapshot(as_of=bars[-1].available_at, bars=tuple(persisted_bars)),
                        workspace_id=workspace_id,
                        input_event_ids=tuple(bar.source_cursor for bar in persisted_bars),
                    )
                    await FeatureSnapshotRepository(uow.session).append(
                        workspace_id=workspace_id, snapshot=feature_snapshot,
                    )
                    restored_feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                        workspace_id=workspace_id, symbol='BTCUSDT', timeframe='1h',
                        as_of=bars[-1].available_at,
                        schema_hash=feature_snapshot.schema_hash,
                        formula_hash=feature_snapshot.formula_hash,
                        code_hash=feature_snapshot.code_hash,
                    )
                    assert restored_feature is not None
                    assert restored_feature.snapshot_id == feature_snapshot.snapshot_id
                    assert restored_feature.is_usable is True
                    atr_value = restored_feature.features['atr_14']
                    assert atr_value is not None
                    assert restored_feature.input_event_ids == tuple(
                        bar.source_cursor for bar in persisted_bars
                    )
                    feature_snapshot_id = restored_feature.snapshot_id
                    decision_time = bars[-1].available_at + timedelta(seconds=1)
                    evaluated_at = decision_time + timedelta(seconds=1)
                    settings_id = published.settings_version_id
                    assert published.settings.strategy.default_release == 'h03-v1'
                class H03PersistedContextProvider:
                    async def load_context(
                        self, *, uow, command, published_settings, execution_profile, account,
                    ):
                        feature_row = await uow.session.scalar(
                            select(FeatureSnapshotRecord).where(
                                FeatureSnapshotRecord.workspace_id == workspace_id,
                                FeatureSnapshotRecord.snapshot_id == command.feature_snapshot_id,
                            )
                        )
                        assert feature_row is not None and feature_row.is_usable
                        persisted_feature = await FeatureSnapshotRepository(
                            uow.session,
                        ).get_as_of(
                            workspace_id=workspace_id,
                            symbol=feature_row.symbol,
                            timeframe=feature_row.timeframe,
                            as_of=command.evaluated_at,
                            schema_hash=feature_row.schema_hash,
                            formula_hash=feature_row.formula_hash,
                            code_hash=feature_row.code_hash,
                        )
                        assert persisted_feature is not None
                        assert persisted_feature.snapshot_id == command.feature_snapshot_id
                        bars_at_decision = await MarketDataRepository(uow.session).list_bars(
                            workspace_id=workspace_id,
                            symbol=feature_row.symbol,
                            timeframe=feature_row.timeframe,
                            start=bars[0].open_time,
                            end=bars[-1].open_time + timedelta(hours=1),
                            as_of=persisted_feature.as_of,
                            closed_only=True,
                        )
                        assert tuple(bar.source_cursor for bar in bars_at_decision) == tuple(
                            persisted_feature.input_event_ids
                        )
                        decision_time = persisted_feature.as_of + timedelta(seconds=1)
                        atr = persisted_feature.features['atr_14']
                        assert atr is not None
                        snapshot = DecisionSnapshot(
                            symbol=feature_row.symbol,
                            decision_time=decision_time,
                            snapshot_id=persisted_feature.snapshot_id,
                            series=(MarketSeries(
                                timeframe=feature_row.timeframe,
                                bars=tuple(bars_at_decision),
                                quote_volumes=tuple(
                                    float(bar.close * bar.volume) for bar in bars_at_decision
                                ),
                                quality=SeriesQuality.READY,
                                expected_interval=timedelta(hours=1),
                                max_event_age=timedelta(hours=2),
                                backfill_complete=True,
                            ),),
                            features=(FeatureObservation(
                                feature_id=f'{persisted_feature.snapshot_id}:atr-14',
                                name='atr_14', timeframe=feature_row.timeframe,
                                value=atr, previous_value=None,
                                available_at=persisted_feature.as_of,
                                quality=FeatureQuality.READY,
                            ),),
                            position_is_flat=True,
                        )
                        account_state = await PaperAccountStateService(
                            PaperAccountStateRepository(uow.session),
                        ).get_state(workspace_id=workspace_id, account_id=account.account_id)
                        instrument_at_decision = await MarketDataRepository(
                            uow.session,
                        ).get_instrument(
                            workspace_id=workspace_id, source='BINANCE_USDM',
                            symbol=feature_row.symbol, as_of=persisted_feature.as_of,
                        )
                        assert instrument_at_decision is not None
                        risk_profile = await load_risk_profile(
                            uow.session,
                            workspace_id=workspace_id,
                            profile_id=published_settings.settings.risk.profile_version_id,
                        )
                        release = StrategyRegistry().get_release(
                            published_settings.settings.strategy.default_release
                        )
                        planner_settings = PlannerSettings(
                            workspace_id=str(workspace_id),
                            settings_version_id=str(published_settings.settings_version_id),
                            risk_profile_version_id=str(
                                published_settings.settings.risk.profile_version_id
                            ),
                            published=True,
                            active_release_ids=(
                                published_settings.settings.strategy.default_release,
                            ),
                            leverage=risk_profile.limits.max_leverage,
                            exchange=ExchangeRules(
                                instrument_at_decision.tick_size,
                                instrument_at_decision.quantity_step,
                                Decimal('1'),
                                instrument_at_decision.min_notional,
                                risk_profile.limits.max_notional,
                                risk_profile.limits.max_notional / bars_at_decision[-1].close,
                            ),
                            limits=risk_profile.limits,
                            release_content_hashes=((
                                published_settings.settings.strategy.default_release,
                                release.content_hash,
                            ),),
                            settings_hash=published_settings.settings_hash,
                        )
                        account_snapshot = AccountSnapshot(
                            workspace_id=str(workspace_id),
                            account_id=str(account.account_id),
                            settings_version_id=str(published_settings.settings_version_id),
                            captured_at=account_state.as_of,
                            equity=account_state.equity,
                            available_margin=account_state.equity,
                            is_known=True,
                        )
                        risk_repository = RiskReservationRepository(uow.session)
                        totals = await risk_repository._active_totals(
                            workspace_id=workspace_id,
                            account_id=account.account_id,
                            symbol=feature_row.symbol,
                        )
                        assert totals.count == 0 and totals.risk == 0
                        assert totals.notional == 0 and totals.initial_margin == 0
                        open_trade_count = await uow.session.scalar(
                            select(func.count()).select_from(TradeRecord).where(
                                TradeRecord.workspace_id == workspace_id,
                                TradeRecord.account_id == account.account_id,
                                TradeRecord.status == 'OPEN',
                            )
                        )
                        assert open_trade_count == 0
                        risk_state = RiskState(
                            workspace_id=str(workspace_id),
                            account_id=str(account.account_id),
                            settings_version_id=str(published_settings.settings_version_id),
                            risk_profile_version_id=str(
                                published_settings.settings.risk.profile_version_id
                            ),
                            evaluated_at=command.evaluated_at,
                            account_captured_at=account_snapshot.captured_at,
                            market_data_as_of=max(bar.event_at for bar in bars_at_decision),
                            equity=account_snapshot.equity,
                            available_margin=account_snapshot.available_margin,
                            open_exposures=(),
                            realized_loss_today=Decimal('0'),
                            realized_loss_rolling=Decimal('0'),
                            open_position_count=0,
                            is_known=True,
                            pending_reservation_initial_margin=totals.initial_margin,
                        )
                        return PaperDecisionContext(
                            snapshot=snapshot,
                            planner_settings=planner_settings,
                            account=account_snapshot,
                            risk_state=risk_state,
                            account_quote_asset=account_state.quote_asset,
                            open_exposures=(),
                            evidence={
                                'feature_snapshot_id': persisted_feature.snapshot_id,
                                'feature_content_hash': persisted_feature.content_hash,
                                'input_event_ids': list(persisted_feature.input_event_ids),
                            },
                        )

                    async def load_monitor_evidence(
                        self, *, uow, command, published_settings, execution_profile,
                        account, position, pinned_strategy_release_name,
                        pinned_strategy_release_hash, pinned_strategy_settings_version_id,
                        pinned_strategy_parameters,
                    ):
                        feature_row = await uow.session.scalar(
                            select(FeatureSnapshotRecord).where(
                                FeatureSnapshotRecord.workspace_id == workspace_id,
                                FeatureSnapshotRecord.snapshot_id == command.feature_snapshot_id,
                            )
                        )
                        assert feature_row is not None and feature_row.is_usable
                        feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                            workspace_id=workspace_id,
                            symbol=feature_row.symbol,
                            timeframe=feature_row.timeframe,
                            as_of=command.bar.opened_at,
                            schema_hash=feature_row.schema_hash,
                            formula_hash=feature_row.formula_hash,
                            code_hash=feature_row.code_hash,
                        )
                        assert feature is not None
                        assert feature.snapshot_id == command.feature_snapshot_id
                        assert feature.as_of <= command.bar.opened_at
                        monitor_bars = await MarketDataRepository(uow.session).list_bars(
                            workspace_id=workspace_id,
                            symbol=feature_row.symbol,
                            timeframe=feature_row.timeframe,
                            start=bars[0].open_time,
                            end=bars[-1].open_time + timedelta(hours=1),
                            as_of=feature.as_of,
                            closed_only=True,
                        )
                        assert tuple(bar.source_cursor for bar in monitor_bars) == tuple(
                            feature.input_event_ids
                        )
                        atr = feature.features['atr_14']
                        assert atr is not None
                        monitor_snapshot = DecisionSnapshot(
                            symbol=feature_row.symbol,
                            decision_time=feature.as_of + timedelta(seconds=1),
                            snapshot_id=feature.snapshot_id,
                            series=(MarketSeries(
                                timeframe=feature_row.timeframe,
                                bars=tuple(monitor_bars),
                                quote_volumes=tuple(
                                    float(bar.close * bar.volume) for bar in monitor_bars
                                ),
                                quality=SeriesQuality.READY,
                                expected_interval=timedelta(hours=1),
                                max_event_age=timedelta(hours=2),
                                backfill_complete=True,
                            ),),
                            features=(FeatureObservation(
                                feature_id=f'{feature.snapshot_id}:atr-14',
                                name='atr_14', timeframe=feature_row.timeframe,
                                value=atr, previous_value=None,
                                available_at=feature.as_of,
                                quality=FeatureQuality.READY,
                            ),),
                            position_is_flat=False,
                        )
                        release_name = pinned_strategy_release_name
                        assert release_name is not None
                        assert pinned_strategy_release_hash is not None
                        assert pinned_strategy_settings_version_id is not None
                        selection = StrategySelection(
                            settings_version_id=pinned_strategy_settings_version_id,
                            active_release_ids=(release_name,),
                            published=True,
                            parameters=pinned_strategy_parameters,
                        )
                        signal = StrategyRegistry().resolve(
                            release_name, selection
                        ).evaluate(monitor_snapshot, selection)
                        opposing = (
                            isinstance(signal, StrategyCandidate)
                            and signal.direction != position.direction
                        )
                        return PaperMonitorEvidence(
                            workspace_id=workspace_id,
                            account_id=account.account_id,
                            feature_snapshot_id=feature.snapshot_id,
                            symbol=feature.symbol,
                            available_at=feature.as_of,
                            strategy_release_name=release_name,
                            strategy_release_hash=pinned_strategy_release_hash,
                            opposing_signal=opposing,
                            atr=Decimal(str(atr)),
                            strategy_settings_version_id=pinned_strategy_settings_version_id,
                            strategy_parameters=pinned_strategy_parameters,
                        )

                def make_h03_app():
                    return create_app(
                        AppSettings(
                            process_role='trading-runtime',
                            runtime_workspace_id=workspace_id,
                        ),
                        session_factory=sessions,
                        paper_decision_context_provider_factory=(
                            lambda **_: H03PersistedContextProvider()
                        ),
                    )

                paper_app = make_h03_app()
                async with paper_app.router.lifespan_context(paper_app):
                    runtime_bootstrap = paper_app.state.paper_runtime
                    paper_application = paper_app.state.paper_application
                    assert runtime_bootstrap.ready
                    assert paper_app.state.readiness['paper_runtime'] is True
                    assert set(paper_app.state.paper_accounts) == {
                        account_id, race_account_id
                    }
                    assert paper_application is not None
                    assert paper_application.is_ready

                    application_result = await paper_application.evaluate_and_submit(
                        PaperDecisionCommand(
                            account_id=account_id,
                            feature_snapshot_id=feature_snapshot_id,
                            evaluated_at=evaluated_at,
                        )
                    )
                    prepared = application_result.decision
                    assert prepared is not None
                    assert prepared.outcome is DecisionCycleOutcome.RISK_APPROVED
                    assert isinstance(prepared.plan, TradePlan)
                    assert prepared.strategy_result.strategy_release_id == 'h03-v1'
                    assert prepared.plan.settings_hash == published.settings_hash
                    assert prepared.plan.cost_model_version == runtime_bootstrap.active_accounts[
                        account_id
                    ].execution_profile.profile_hash
                    assert prepared.plan.candidate_id == prepared.strategy_result.candidate_id
                    assert prepared.plan.risk_profile_version_id == str(
                        published.settings.risk.profile_version_id
                    )
                    submitted = application_result.submission
                    assert submitted is not None
                    duplicate_result = await paper_application.evaluate_and_submit(
                        PaperDecisionCommand(
                            account_id=account_id,
                            feature_snapshot_id=feature_snapshot_id,
                            evaluated_at=evaluated_at,
                        )
                    )
                    assert duplicate_result.duplicate
                    assert duplicate_result.submission is not None
                    assert duplicate_result.submission.ledger_intent_id == (
                        submitted.ledger_intent_id
                    )
                    assert duplicate_result.idempotency_key == application_result.idempotency_key
                    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                        await uow.bind_account(account_id)
                        persisted_order_count = await uow.session.scalar(
                            select(func.count()).select_from(OrderIntentRecord).where(
                                OrderIntentRecord.workspace_id == workspace_id,
                                OrderIntentRecord.account_id == account_id,
                            )
                        )
                        persisted_reservation_count = await uow.session.scalar(
                            select(func.count()).select_from(RiskReservationRecord).where(
                                RiskReservationRecord.workspace_id == workspace_id,
                                RiskReservationRecord.account_id == account_id,
                            )
                        )
                        assert persisted_order_count == 1
                        assert persisted_reservation_count == 1
                    try:
                        await paper_application.evaluate_and_submit(
                            PaperDecisionCommand(
                                account_id=account_id,
                                feature_snapshot_id=feature_snapshot_id,
                                evaluated_at=evaluated_at + timedelta(seconds=1),
                            )
                        )
                    except PaperRuntimeApplicationError as error:
                        assert error.reason_code == 'PAPER_IDEMPOTENCY_CONFLICT'
                    else:
                        raise AssertionError('Expected persisted idempotency conflict.')
                    runtime_lock_acquired = asyncio.Event()
                    allow_runtime_decision = asyncio.Event()
                    publisher_started = asyncio.Event()
                    publisher_finished = asyncio.Event()

                    class PausingSettingsRepository(WorkspaceSettingsRepository):
                        async def lock_workspace(self, locked_workspace_id):
                            await super().lock_workspace(locked_workspace_id)
                            runtime_lock_acquired.set()
                            await allow_runtime_decision.wait()

                    original_repository_factory = (
                        paper_application._settings_repository_factory
                    )
                    paper_application._settings_repository_factory = PausingSettingsRepository

                    async def publish_settings_after_decision_lock():
                        publisher_started.set()

                        def publish_via_admin_api():
                            import urllib.request

                            request = urllib.request.Request(
                                'http://admin-web:8000/api/v1/settings/drafts/'
                                f"{os.environ['CRYPIDIAN_E2E_SETTINGS_DRAFT_ID']}/publish",
                                data=b'{"expected_revision":1}',
                                headers={
                                    'Content-Type': 'application/json',
                                    'Cookie': os.environ['CRYPIDIAN_E2E_SESSION_COOKIE'],
                                    'X-CSRF-Token': os.environ['CRYPIDIAN_E2E_CSRF_TOKEN'],
                                },
                                method='POST',
                            )
                            with urllib.request.urlopen(request, timeout=20) as response:
                                return response.status, response.read().decode('utf-8')

                        result = await asyncio.to_thread(publish_via_admin_api)
                        publisher_finished.set()
                        return result

                    concurrent_retry = asyncio.create_task(
                        paper_application.evaluate_and_submit(
                            PaperDecisionCommand(
                                account_id=account_id,
                                feature_snapshot_id=feature_snapshot_id,
                                evaluated_at=evaluated_at,
                            )
                        )
                    )
                    await runtime_lock_acquired.wait()
                    concurrent_publish = asyncio.create_task(
                        publish_settings_after_decision_lock()
                    )
                    await publisher_started.wait()
                    await asyncio.sleep(0.05)
                    assert not publisher_finished.is_set()
                    allow_runtime_decision.set()
                    concurrent_retry_result = await concurrent_retry
                    publish_status, publish_body = await concurrent_publish
                    paper_application._settings_repository_factory = original_repository_factory
                    assert concurrent_retry_result.duplicate
                    assert publish_status == 200, publish_body
                    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                        await uow.bind_account(account_id)
                        effective_after_race = await WorkspaceSettingsRepository(
                            uow.session
                        ).load_effective_config(workspace_id=workspace_id)
                        decision_order = await uow.session.scalar(
                            select(OrderIntentRecord).where(
                                OrderIntentRecord.workspace_id == workspace_id,
                                OrderIntentRecord.id == submitted.ledger_intent_id,
                            )
                        )
                        assert effective_after_race is not None
                        assert effective_after_race.settings_version_id != (
                            published.settings_version_id
                        )
                        assert effective_after_race.settings.strategy.parameters[
                            'donchian_lookback'
                        ] == 56
                        assert decision_order is not None
                        assert decision_order.settings_version_id == published.settings_version_id
                    print('runtime_decision_and_settings_publish_are_linearized')

                    def create_publish_first_draft():
                        import urllib.request

                        next_settings = effective_after_race.settings.model_dump(mode='json')
                        next_settings['strategy']['parameters'] = {
                            'donchian_lookback': 56,
                            'volume_median_lookback': 40,
                        }
                        payload = json.dumps({
                            'settings': next_settings,
                            'expected_parent_version_id': str(
                                effective_after_race.settings_version_id
                            ),
                        }).encode('utf-8')
                        request = urllib.request.Request(
                            'http://admin-web:8000/api/v1/settings/drafts',
                            data=payload,
                            headers={
                                'Content-Type': 'application/json',
                                'Cookie': os.environ['CRYPIDIAN_E2E_SESSION_COOKIE'],
                                'X-CSRF-Token': os.environ['CRYPIDIAN_E2E_CSRF_TOKEN'],
                            },
                            method='POST',
                        )
                        with urllib.request.urlopen(request, timeout=20) as response:
                            body = json.loads(response.read())
                        return str(body['draft']['id'])

                    publish_first_draft_id = await asyncio.to_thread(
                        create_publish_first_draft
                    )
                    publish_first_started = asyncio.Event()
                    publish_first_finished = asyncio.Event()

                    async def publish_first_settings():
                        import urllib.request

                        publish_first_started.set()

                        def publish():
                            request = urllib.request.Request(
                                'http://admin-web:8000/api/v1/settings/drafts/'
                                f'{publish_first_draft_id}/publish',
                                data=b'{"expected_revision":1}',
                                headers={
                                    'Content-Type': 'application/json',
                                    'Cookie': os.environ['CRYPIDIAN_E2E_SESSION_COOKIE'],
                                    'X-CSRF-Token': os.environ['CRYPIDIAN_E2E_CSRF_TOKEN'],
                                },
                                method='POST',
                            )
                            with urllib.request.urlopen(request, timeout=30) as response:
                                return response.status, json.loads(response.read())

                        result = await asyncio.to_thread(publish)
                        publish_first_finished.set()
                        return result

                    publish_first_task = asyncio.create_task(publish_first_settings())
                    await publish_first_started.wait()
                    await asyncio.sleep(0.2)
                    fresh_evaluated_at = datetime.now(UTC) + timedelta(seconds=10)
                    fresh_decision = asyncio.create_task(
                        paper_application.evaluate_and_submit(
                            PaperDecisionCommand(
                                account_id=race_account_id,
                                feature_snapshot_id=feature_snapshot_id,
                                evaluated_at=fresh_evaluated_at,
                            )
                        )
                    )
                    await asyncio.sleep(0.1)
                    assert not fresh_decision.done()
                    assert not publish_first_finished.is_set()
                    next_publish_status, next_publish_response = await publish_first_task
                    assert next_publish_status == 200
                    fresh_result = await fresh_decision
                    assert fresh_result.decision is not None
                    assert fresh_result.decision.outcome is DecisionCycleOutcome.RISK_APPROVED
                    assert fresh_result.submission is not None
                    assert fresh_result.decision.plan is not None
                    next_settings_id = next_publish_response['version']['id']
                    assert str(fresh_result.decision.plan.settings_version_id) == str(
                        next_settings_id
                    )
                    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                        await uow.bind_account(race_account_id)
                        new_intent = await uow.session.scalar(
                            select(OrderIntentRecord).where(
                                OrderIntentRecord.workspace_id == workspace_id,
                                OrderIntentRecord.id == fresh_result.submission.ledger_intent_id,
                                OrderIntentRecord.account_id == race_account_id,
                            )
                        )
                        effective_publish_first = await WorkspaceSettingsRepository(
                            uow.session
                        ).load_effective_config(workspace_id=workspace_id)
                        assert new_intent is not None
                        assert str(new_intent.settings_version_id) == next_settings_id
                        assert effective_publish_first is not None
                        assert effective_publish_first.settings_version_id == UUID(
                            next_settings_id
                        )
                        assert effective_publish_first.settings.strategy.parameters == {
                            'donchian_lookback': 56,
                            'volume_median_lookback': 40,
                        }
                    print('settings_publish_first_runtime_entry_uses_new_version')
                    assert paper_application.is_ready
                    plan = prepared.plan
                    risk_decision = prepared.risk_decision
                    assert risk_decision is not None
                    idempotency_key = application_result.idempotency_key
                    assert idempotency_key is not None
                    risk_decision_id = application_result.risk_decision_id
                    single_account_risk_decision_id = (
                        application_result.single_account_risk_decision_id
                    )
                    reservation_id = application_result.risk_reservation_id
                    assert risk_decision_id is not None and reservation_id is not None
                    intent_id = submitted.ledger_intent_id
                    candidate_id = prepared.strategy_result.candidate_id
                    entry_opened = submitted.order.eligible_at
                    entry_closed = entry_opened + timedelta(minutes=1)
                    entry_bar = PaperBar(
                        'h03-paper-entry', plan.symbol, entry_opened, entry_closed,
                        entry_closed, plan.entry_reference_price,
                        plan.entry_reference_price, plan.entry_reference_price,
                        plan.entry_reference_price,
                    )
                    entry_result = await paper_application.apply_entry_event(
                        PaperEntryEventCommand(
                            account_id=account_id,
                            ledger_intent_id=intent_id,
                            paper_intent_id=submitted.order.intent_id,
                            bar=entry_bar,
                        )
                    )
                    assert entry_result.order.status.value == 'FILLED'
                    assert entry_result.persisted_fill_count == 1
                    assert len(entry_result.order.fills) == 1
                    entry_fill = entry_result.order.fills[0]
                    opened_position = entry_result.position
                    assert opened_position is not None
                    assert opened_position.quantity == plan.quantity
                    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                        await uow.bind_account(account_id)
                        intent = await uow.session.scalar(select(OrderIntentRecord).where(
                            OrderIntentRecord.workspace_id == workspace_id,
                            OrderIntentRecord.id == intent_id,
                        ))
                        assert intent is not None and intent.status == 'FILLED'
                        assert intent.plan_hash == plan.plan_hash
                        selected_release = await uow.session.scalar(
                            select(StrategyReleaseRecord).where(
                                StrategyReleaseRecord.workspace_id == workspace_id,
                                StrategyReleaseRecord.name == 'h03-v1',
                                StrategyReleaseRecord.status == 'PUBLISHED',
                                StrategyReleaseRecord.content_hash == StrategyRegistry()
                                .get_release('h03-v1')
                                .content_hash,
                            )
                        )
                        assert selected_release is not None
                        assert intent.strategy_release_id == selected_release.id
                        assert intent.evidence['settings_hash'] == published.settings_hash
                        assert intent.evidence['paper_execution_profile_hash'] == (
                            execution_profile.profile_hash
                        )
                        assert candidate_id == plan.candidate_id == intent.candidate_id
                        assert intent.risk_reservation_id == reservation_id
                        trade = await uow.session.scalar(select(TradeRecord).where(
                            TradeRecord.workspace_id == workspace_id,
                            TradeRecord.account_id == account_id,
                            TradeRecord.intent_id == intent.id,
                        ))
                        assert trade is not None
                recovery_app = make_h03_app()
                async with recovery_app.router.lifespan_context(recovery_app):
                    recovered_runtime = recovery_app.state.paper_runtime
                    recovered_application = recovery_app.state.paper_application
                    assert recovered_runtime.ready
                    assert recovery_app.state.readiness['paper_runtime'] is True
                    assert recovered_application is not None
                    recovered_position = recovery_app.state.paper_accounts[
                        account_id
                    ].gateway.position(submitted.order.intent_id)
                    assert recovered_position is not None
                    assert recovered_position.quantity == plan.quantity
                    assert recovered_position.position_id == opened_position.position_id
                exit_opened = entry_closed + timedelta(minutes=1)
                exit_closed = exit_opened + timedelta(minutes=1)
                stop_bar = PaperBar(
                    'h03-paper-stop', plan.symbol, exit_opened, exit_closed, exit_closed,
                    opened_position.entry_execution_price,
                    max(opened_position.entry_execution_price, plan.stop_price),
                    min(opened_position.entry_execution_price, plan.stop_price),
                    plan.stop_price,
                )
                exit_result = await recovered_application.monitor_position(
                    PaperMonitorCommand(
                        account_id=account_id,
                        ledger_intent_id=intent_id,
                        trade_id=trade.id,
                        paper_position_id=opened_position.position_id,
                        bar=stop_bar,
                        feature_snapshot_id=feature_snapshot_id,
                    )
                )
                closed_position = exit_result.position
                assert closed_position.is_closed
                assert exit_result.monitor_result.exit_reason == 'STOP_LOSS'
                assert exit_result.exit_fill_appended
                assert exit_result.trade_closed
                exit_fill = exit_result.exit_fill
                assert exit_fill is not None
                async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                    await uow.bind_account(account_id)
                    exit_record = await uow.session.scalar(select(FillRecord).where(
                        FillRecord.workspace_id == workspace_id,
                        FillRecord.account_id == account_id,
                        FillRecord.fill_key == exit_fill.fill_id,
                    ))
                    assert exit_record is not None and exit_record.fill_role == 'EXIT'
                    assert exit_record.lineage['paper_execution_profile_hash'] == (
                        execution_profile.profile_hash
                    )
                    entry_record = await uow.session.scalar(select(FillRecord).where(
                        FillRecord.workspace_id == workspace_id,
                        FillRecord.account_id == account_id,
                        FillRecord.fill_key == entry_fill.fill_id,
                    ))
                    assert entry_record is not None and entry_record.fill_role == 'ENTRY'
                    assert entry_record.lineage['paper_execution_profile_hash'] == (
                        execution_profile.profile_hash
                    )
                    closed_trade = await uow.session.scalar(select(TradeRecord).where(
                        TradeRecord.workspace_id == workspace_id,
                        TradeRecord.id == trade.id,
                    ))
                    assert closed_trade is not None
                    persisted_reservation = await uow.session.scalar(
                        select(RiskReservationRecord).where(
                            RiskReservationRecord.workspace_id == workspace_id,
                            RiskReservationRecord.account_id == account_id,
                            RiskReservationRecord.id == reservation_id,
                        )
                    )
                    assert persisted_reservation is not None
                    assert persisted_reservation.status == 'TRANSFERRED'
                    risk_repository = RiskReservationRepository(uow.session)
                    active_totals = await risk_repository._active_totals(
                        workspace_id=workspace_id, account_id=account_id, symbol=plan.symbol,
                    )
                    assert active_totals.count == 0
                    assert active_totals.risk == Decimal('0')
                    assert active_totals.notional == Decimal('0')
                    open_trade_count = await uow.session.scalar(
                        select(func.count()).select_from(TradeRecord).where(
                            TradeRecord.workspace_id == workspace_id,
                            TradeRecord.account_id == account_id,
                            TradeRecord.status == 'OPEN',
                        )
                    )
                    assert open_trade_count == 0
                    next_reservation = await risk_repository.reserve(
                        risk_decision,
                        idempotency_key=f'{idempotency_key}:after-close',
                        risk_decision_id=single_account_risk_decision_id,
                    )
                    assert next_reservation.status.value == 'RESERVED'
                    await risk_repository.release_before_submit(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        reservation_id=next_reservation.reservation_id,
                        reason=ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
                        order_was_submitted=False,
                    )
                    updated_account_state = await PaperAccountStateService(
                        PaperAccountStateRepository(uow.session),
                    ).get_state(workspace_id=workspace_id, account_id=account_id)
                    repeated_account_state = await PaperAccountStateService(
                        PaperAccountStateRepository(uow.session),
                    ).get_state(workspace_id=workspace_id, account_id=account_id)
                    assert updated_account_state.equity == (
                        initial_account_state.equity + closed_trade.net_pnl
                    )
                    assert updated_account_state.closed_trade_count == 1
                    assert updated_account_state.state_hash != initial_account_state.state_hash
                    assert repeated_account_state.state_hash == updated_account_state.state_hash
                    report = build_evaluation_metrics((EvaluationTrade(
                        trade_id=str(closed_trade.id), opened_at=entry_fill.filled_at,
                        closed_at=exit_closed, gross_pnl=closed_position.gross_realized_pnl,
                        fees=closed_trade.fee_total, funding=closed_trade.funding_total,
                        slippage=closed_trade.slippage_total, exposure=plan.notional,
                    ),), observation_count=1, usable_observation_count=1, seed=7)
                    assert closed_trade.status == 'CLOSED'
                    assert closed_trade.net_pnl == closed_position.net_realized_pnl
                    assert report.net_pnl == closed_trade.net_pnl
                    assert closed_trade.candidate_id == candidate_id
                    assert closed_trade.settings_version_id == settings_id
                    assert closed_trade.feature_snapshot_id == feature_snapshot_id
                    persisted_seed = await uow.session.scalar(select(PaperAccountSeed).where(
                        PaperAccountSeed.workspace_id == workspace_id,
                        PaperAccountSeed.trading_account_id == account_id,
                    ))
                    assert persisted_seed is not None
                    assert persisted_seed.quote_asset == execution_profile.quote_asset
                    assert persisted_seed.initial_equity == initial_account_state.equity
                    assert persisted_seed.source_settings_version_id == settings_id
                    assert closed_trade.lineage['paper_execution_profile_hash'] == (
                        execution_profile.profile_hash
                    )
                    print(json.dumps({
                        'strategy_release': plan.strategy_release_id,
                        'candidate_reason': plan.candidate_reason_code,
                        'settings_version_id': str(settings_id),
                        'plan_hash': plan.plan_hash,
                        'risk_decision_id': str(risk_decision_id),
                        'reservation_id': str(reservation_id),
                        'intent_id': str(intent.id), 'status': intent.status,
                        'account_id': str(account_id),
                        'paper_seed_id': str(persisted_seed.id),
                        'trade_id': str(closed_trade.id), 'trade_status': closed_trade.status,
                        'net_pnl': str(closed_trade.net_pnl),
                        'report_net_pnl': str(report.net_pnl),
                    }))
                async with UnitOfWork(sessions, workspace_id=uuid4()) as other_workspace:
                    isolated_seed_count = await other_workspace.session.scalar(
                        select(func.count()).select_from(PaperAccountSeed)
                    )
                    assert isolated_seed_count == 0
                    isolated_checkpoint_count = await other_workspace.session.scalar(
                        select(func.count()).select_from(PaperExecutionCheckpointRecord)
                    )
                    assert isolated_checkpoint_count == 0
                async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                    await uow.bind_account(account_id)
                    checkpoint_repository = PaperCheckpointRepository()
                    latest_revision, latest_snapshot = await checkpoint_repository.load_latest(
                        uow,
                        account_id=account_id,
                    )
                    assert latest_revision is not None and latest_snapshot is not None
                    corrupt_body = json.loads(latest_snapshot.payload)
                    old_key = corrupt_body['orders'][0]['idempotency_key']
                    order_id = corrupt_body['keys'].pop(old_key)
                    corrupt_body['orders'][0]['idempotency_key'] = 'missing-ledger-intent'
                    corrupt_body['keys']['missing-ledger-intent'] = order_id
                    corrupt_payload = json.dumps(
                        corrupt_body, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                    )
                    corrupt_snapshot = PaperExecutionSnapshot(
                        schema_version=latest_snapshot.schema_version,
                        policy_hash=latest_snapshot.policy_hash,
                        payload=corrupt_payload,
                        content_hash=hashlib.sha256(corrupt_payload.encode('utf-8')).hexdigest(),
                    )
                    await PaperCheckpointRepository().append(
                        uow,
                        account_id=account_id,
                        event_key='e2e-ledger-lineage-corruption',
                        event_hash=hashlib.sha256(b'e2e-ledger-lineage-corruption').hexdigest(),
                        snapshot=corrupt_snapshot,
                        occurred_at=exit_closed + timedelta(minutes=1),
                    )
                corrupted_app = make_h03_app()
                async with corrupted_app.router.lifespan_context(corrupted_app):
                    corrupted_runtime = corrupted_app.state.paper_runtime
                    assert corrupted_app.state.readiness['paper_runtime'] is False
                    assert corrupted_app.state.paper_application is None
                assert not corrupted_runtime.ready
                assert corrupted_runtime.error_code == 'PAPER_RECOVERY_CONFLICT'
                assert not corrupted_runtime.active_accounts
                await engine.dispose()

            asyncio.run(main())
            """
        )
        compressed_probe = base64.b64encode(
            zlib.compress(paper_decision_cycle_probe_code.encode("utf-8"), level=9)
        ).decode("ascii")
        paper_decision_cycle_probe_runner = (
            f"import base64,zlib;exec(zlib.decompress(base64.b64decode({compressed_probe!r})))"
        )
        compose(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-U",
            "crypidian_admin",
            "-d",
            "crypidian",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE OR REPLACE FUNCTION e2e_hold_settings_publish() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(2); RETURN NEW; END $$; "
            "DROP TRIGGER IF EXISTS e2e_hold_settings_publish ON workspace_settings_version; "
            "CREATE TRIGGER e2e_hold_settings_publish BEFORE UPDATE "
            "ON workspace_settings_version FOR EACH ROW "
            "WHEN (OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED') "
            "EXECUTE FUNCTION e2e_hold_settings_publish()",
        )
        paper_decision_cycle_probe = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={bootstrap['workspace_id']}",
            "--env",
            f"CRYPIDIAN_E2E_FEATURE_SNAPSHOT_ID={feature_snapshot_id}",
            "--env",
            "CRYPIDIAN_E2E_SESSION_COOKIE",
            "--env",
            "CRYPIDIAN_E2E_CSRF_TOKEN",
            "--env",
            "CRYPIDIAN_E2E_SETTINGS_DRAFT_ID",
            "trading-runtime",
            "python",
            "-c",
            paper_decision_cycle_probe_runner,
        )
        cycle_result = json.loads(
            next(
                line
                for line in reversed(paper_decision_cycle_probe.stdout.splitlines())
                if line.startswith("{")
            )
        )
        assert cycle_result["strategy_release"] == "h03-v1"
        assert cycle_result["candidate_reason"] == "H03_LONG_ENTRY"
        assert cycle_result["status"] == "FILLED"
        assert len(cycle_result["plan_hash"]) == 64
        assert cycle_result["trade_status"] == "CLOSED"
        assert Decimal(cycle_result["net_pnl"]) == Decimal(cycle_result["report_net_pnl"])
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
