from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from textwrap import dedent
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pyotp
import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

RUNTIME_HEALTH_PROBE_CODE = dedent(
    """\
    import json
    import sys
    import urllib.error
    import urllib.request

    url = 'http://127.0.0.1:8000' + sys.argv[1]
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = response.status
            raw_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw_body = error.read()
    try:
        body = json.loads(raw_body or b'{}')
    except json.JSONDecodeError:
        body = {'raw': raw_body.decode(errors='replace')}
    print(json.dumps({'status': status, 'body': body}))
    """
)
compile(RUNTIME_HEALTH_PROBE_CODE, "<runtime-health-probe>", "exec")

PAPER_SEED_CODE = dedent(
    """\
    import asyncio
    import hashlib
    import json
    import os
    from datetime import UTC, datetime
    from decimal import Decimal
    from pathlib import Path
    from uuid import UUID
    from trading_bot.adapters.database.engine import (
        create_database_engine, create_session_factory, database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointRepository,
    )
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_account_initialization import (
        PaperAccountInitializationService,
    )
    from trading_bot.application.paper_execution_profile import (
        default_paper_execution_profile_catalog, resolve_paper_execution_profile,
    )
    from trading_bot.domain.execution.paper import PaperExecutionGateway

    async def main():
        workspace_id = UUID(os.environ['CRYPIDIAN_E2E_WORKSPACE_ID'])
        now = datetime.now(UTC)
        url = database_url_from_secret(
            username=os.environ['TRADING_BOT_DATABASE_USER'],
            password_file=Path(os.environ['TRADING_BOT_DATABASE_PASSWORD_FILE']),
            host='postgres', database='crypidian',
        )
        engine = create_database_engine(url)
        sessions = create_session_factory(engine)
        account_ids = {}
        async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
            published = await WorkspaceSettingsRepository(
                uow.session
            ).load_effective_config(workspace_id=workspace_id, as_of=now)
            assert published is not None
            profile = resolve_paper_execution_profile(
                published, default_paper_execution_profile_catalog(),
            )
            if os.environ.get('CRYPIDIAN_E2E_CREATE_NO_CHECKPOINT') == 'true':
                accounts_to_create = [('runtime-no-checkpoint', False)]
            else:
                accounts_to_create = [('runtime-good', True)]
            for name, create_checkpoint in accounts_to_create:
                initialized = await PaperAccountInitializationService(
                    account_name=name,
                ).ensure_initialized(
                    uow,
                    published_settings=published,
                    execution_profile=profile,
                    max_notional=Decimal('10000'),
                    evaluated_at=now,
                )
                account = initialized.account
                account_ids[name] = str(account.id)
                if create_checkpoint and initialized.created:
                    snapshot = PaperExecutionGateway(
                        profile.fill_policy,
                    ).export_state()
                    await PaperCheckpointRepository().append(
                        uow,
                        account_id=account.id,
                        event_key=f'e2e-genesis:{account.id}',
                        event_hash=hashlib.sha256(
                            f'e2e-genesis:{account.id}'.encode(),
                        ).hexdigest(),
                        snapshot=snapshot,
                        occurred_at=now,
                    )
        await engine.dispose()
        print(json.dumps(account_ids))

    asyncio.run(main())
    """
)
compile(PAPER_SEED_CODE, "<paper-seed-code>", "exec")


def test_runtime_process_hydrates_and_fails_closed_without_checkpoint() -> None:
    repository_root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    project_name = f"crypidian-runtime-e2e-{uuid.uuid4().hex[:10]}"
    image_reference = build_image_reference(repository_root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        admin_port = listener.getsockname()[1]
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image_reference,
        "TRADING_BOT_WEB_PORT": str(admin_port),
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
        pytest.fail(f"Docker daemon cần chạy để xác minh runtime E2E Compose: {error}")
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
            pytest.fail(f"Compose vượt timeout cho {' '.join(arguments)}:\n{output[-4000:]}")
        if result.returncode:
            pytest.fail(
                f"Compose thất bại: {' '.join(arguments)}\n"
                f"stdout:\n{result.stdout.strip()}\n"
                f"stderr:\n{result.stderr.strip()}"
            )
        return result

    def api_request(
        client: object,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        request = Request(
            f"http://127.0.0.1:{admin_port}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers=request_headers,
            method=method,
        )
        try:
            with client.open(request, timeout=10) as response:  # type: ignore[attr-defined]
                body = response.read()
                return response.status, json.loads(body) if body else {}
        except HTTPError as error:
            body = error.read()
            return error.code, json.loads(body) if body else {}

    def start_runtime(workspace_id: str) -> str:
        result = compose(
            "run",
            "--detach",
            "--no-deps",
            "--env",
            f"TRADING_BOT_RUNTIME_WORKSPACE_ID={workspace_id}",
            "trading-runtime",
        )
        return result.stdout.strip().splitlines()[-1]

    def probe_runtime_health(container_id: str, path: str) -> tuple[int, dict[str, object]]:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "python",
                "-c",
                RUNTIME_HEALTH_PROBE_CODE,
                path,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode:
            diagnostic = _redact_runtime_logs(result.stderr or result.stdout)[-2000:]
            raise RuntimeError(
                f"docker exec health probe failed ({result.returncode}): {diagnostic}"
            )
        try:
            response = json.loads(result.stdout.strip().splitlines()[-1])
            return int(response["status"]), response["body"]
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            diagnostic = _redact_runtime_logs(result.stdout + result.stderr)[-2000:]
            raise RuntimeError(f"Invalid docker exec health response: {diagnostic}") from error

    def runtime_diagnostics(container_id: str, path: str) -> str:
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}|{{json .NetworkSettings.Ports}}",
                container_id,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        logs = subprocess.run(
            ["docker", "logs", "--tail", "100", container_id],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        try:
            probe_status, probe_body = probe_runtime_health(container_id, path)
            in_container_probe = json.dumps({"status": probe_status, "body": probe_body})
            in_container_exit_code = 0
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            in_container_probe = f"{type(error).__name__}: {error}"
            in_container_exit_code = 1
        safe_logs = _redact_runtime_logs(logs.stdout + logs.stderr)[-6000:]
        return (
            f"container_id={container_id}\n"
            f"inspect_exit_code={inspect.returncode}\n"
            f"inspect={inspect.stdout.strip() or inspect.stderr.strip()}\n"
            f"logs_exit_code={logs.returncode}\n"
            f"logs={safe_logs}\n"
            f"in_container_probe_exit_code={in_container_exit_code}\n"
            f"in_container_probe={in_container_probe}"
        )

    def wait_for_runtime_health(
        path: str, *, expected_status: int, container_id: str
    ) -> dict[str, object]:
        deadline = time.monotonic() + 45
        last_error = "chưa nhận response"
        while time.monotonic() < deadline:
            try:
                status, body = probe_runtime_health(container_id, path)
                if status == expected_status:
                    return body
                last_error = f"HTTP {status}: {body}"
            except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                last_error = type(error).__name__
            time.sleep(0.5)
        diagnostics = runtime_diagnostics(container_id, path)
        pytest.fail(
            f"Runtime không đạt trạng thái mong đợi tại {path}: {last_error}\n"
            f"Chẩn đoán container:\n{diagnostics}"
        )

    def stop_runtime(container_id: str) -> None:
        stop = subprocess.run(
            ["docker", "stop", container_id],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        remove = subprocess.run(
            ["docker", "rm", container_id],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        for operation in (stop, remove):
            if operation.returncode and "No such container" not in operation.stderr:
                pytest.fail(f"Không dừng/xóa được runtime E2E: {operation.stderr.strip()}")

    runtime_container_ids: list[str] = []
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180")
        with urlopen(f"http://127.0.0.1:{admin_port}/health/live", timeout=5) as response:
            assert json.load(response) == {
                "status": "ok",
                "mode": "PAPER",
                "role": "admin-web",
            }

        client = build_opener(HTTPCookieProcessor(CookieJar()))
        bootstrap_payload = {
            "workspace_name": "Runtime E2E workspace",
            "workspace_slug": f"runtime-{uuid.uuid4().hex[:10]}",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "mode": "PAPER",
        }
        bootstrap_status, bootstrap = api_request(
            client,
            "POST",
            "/api/v1/auth/bootstrap",
            bootstrap_payload,
            {"X-Bootstrap-Token": (repository_root / ".secrets/bootstrap_token").read_text()},
        )
        assert bootstrap_status == 201
        workspace_id = str(bootstrap["workspace_id"])
        setup_uri = str(bootstrap["mfa_setup_uri"])
        totp_secret = parse_qs(urlparse(setup_uri).query)["secret"][0]
        previous_code = pyotp.TOTP(totp_secret).at(datetime.now(UTC) - timedelta(seconds=30))
        verify_status, _ = api_request(
            client,
            "POST",
            "/api/v1/auth/bootstrap/verify",
            {
                "workspace_slug": bootstrap_payload["workspace_slug"],
                "email": bootstrap_payload["email"],
                "code": previous_code,
            },
        )
        assert verify_status == 200
        login_status, login = api_request(
            client,
            "POST",
            "/api/v1/auth/login",
            {
                "workspace_slug": bootstrap_payload["workspace_slug"],
                "email": bootstrap_payload["email"],
                "password": bootstrap_payload["password"],
                "code": pyotp.TOTP(totp_secret).now(),
            },
        )
        assert login_status == 200
        csrf_token = str(login["csrf_token"])
        headers = {"X-CSRF-Token": csrf_token}
        risk_status, risk_profile = api_request(
            client,
            "POST",
            "/api/v1/risk-profiles",
            {
                "name": "Runtime E2E risk",
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
            headers,
        )
        assert risk_status == 201
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
        draft_status, draft_result = api_request(
            client,
            "POST",
            "/api/v1/settings/drafts",
            {"settings": root_settings, "expected_parent_version_id": None},
            headers,
        )
        assert draft_status == 201
        draft_id = str(draft_result["draft"]["id"])
        validation_status, validation = api_request(
            client,
            "POST",
            f"/api/v1/settings/drafts/{draft_id}/validate",
            headers=headers,
        )
        assert validation_status == 200
        assert validation["valid"] is True
        publish_status, published = api_request(
            client,
            "POST",
            f"/api/v1/settings/drafts/{draft_id}/publish",
            {"expected_revision": 1},
            headers,
        )
        assert publish_status == 200

        seed_result = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "trading-runtime",
            "python",
            "-c",
            PAPER_SEED_CODE,
        )
        account_ids = json.loads(seed_result.stdout.strip().splitlines()[-1])
        assert set(account_ids) == {"runtime-good"}
        foreign_workspace_id = uuid.uuid4()
        foreign_account_id = uuid.uuid4()
        foreign_scope_seed = compose(
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
            "INSERT INTO workspace(id,name,slug,mode,status) VALUES "
            f"('{foreign_workspace_id}','Foreign runtime scope','foreign-"
            f"{foreign_workspace_id.hex[:12]}','PAPER','ACTIVE'); "
            "INSERT INTO trading_account(id,workspace_id,name,venue,mode,environment,status,"
            "max_notional) VALUES "
            f"('{foreign_account_id}','{foreign_workspace_id}','foreign-paper-account',"
            "'binance_usdm','PAPER','local','ENABLED',10000)",
        )
        assert "INSERT 0 1" in foreign_scope_seed.stdout

        successful_container = start_runtime(workspace_id)
        runtime_container_ids.append(successful_container)
        live = wait_for_runtime_health(
            "/health/live", expected_status=200, container_id=successful_container
        )
        assert live == {"status": "ok", "mode": "PAPER", "role": "trading-runtime"}
        ready = wait_for_runtime_health(
            "/health/ready", expected_status=200, container_id=successful_container
        )
        assert ready["status"] == "ready"
        assert ready["workspace_control"] == "active"
        assert ready["paper_accounts"] == {
            "total": 1,
            "ready": 1,
            "recovery_required": 0,
            "standby": 0,
            "paused": 0,
            "activity_blocked": 0,
            "blocked_by_workspace": 0,
        }
        assert ready["execution_capabilities"] == {
            "paper_recovery": "ready",
            "market_dispatch": "not_configured",
            "exchange_execution": "disabled",
        }
        assert workspace_id not in json.dumps(ready)
        assert account_ids["runtime-good"] not in json.dumps(ready)
        assert str(foreign_workspace_id) not in json.dumps(ready)
        assert str(foreign_account_id) not in json.dumps(ready)
        stop_runtime(successful_container)
        runtime_container_ids.remove(successful_container)

        extra_account_result = compose(
            "exec",
            "--no-TTY",
            "--env",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "--env",
            "CRYPIDIAN_E2E_CREATE_NO_CHECKPOINT=true",
            "trading-runtime",
            "python",
            "-c",
            PAPER_SEED_CODE,
        )
        account_ids.update(json.loads(extra_account_result.stdout.strip().splitlines()[-1]))

        failed_container = start_runtime(workspace_id)
        runtime_container_ids.append(failed_container)
        live_after_recovery_failure = wait_for_runtime_health(
            "/health/live", expected_status=200, container_id=failed_container
        )
        assert live_after_recovery_failure == {
            "status": "ok",
            "mode": "PAPER",
            "role": "trading-runtime",
        }
        # MB05.5 ruling 2/26 (thay đổi có chủ đích): account thiếu checkpoint chỉ làm bot đó
        # RECOVERY_REQUIRED; sibling vẫn READY; process vẫn phục vụ -> 200 `degraded`.
        deadline = time.monotonic() + 45
        degraded: dict[str, object] = {}
        while time.monotonic() < deadline:
            status, degraded = probe_runtime_health(failed_container, "/health/ready")
            counts = degraded.get("paper_accounts", {})
            if status == 200 and isinstance(counts, dict) and counts.get("ready") == 1:
                break
            time.sleep(0.5)
        assert degraded["status"] == "degraded", degraded
        assert degraded["paper_accounts"] == {
            "total": 2,
            "ready": 1,
            "recovery_required": 1,
            "standby": 0,
            "paused": 0,
            "activity_blocked": 0,
            "blocked_by_workspace": 0,
        }
        response_text = json.dumps(degraded)
        assert workspace_id not in response_text
        assert account_ids["runtime-good"] not in response_text
        assert account_ids["runtime-no-checkpoint"] not in response_text
    finally:
        cleanup_errors: list[str] = []
        for container_id in runtime_container_ids:
            try:
                remove = subprocess.run(
                    ["docker", "rm", "--force", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                if remove.returncode and "No such container" not in remove.stderr:
                    cleanup_errors.append(remove.stderr.strip())
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append(
                    f"Không xóa được runtime {container_id}: {type(error).__name__}"
                )
        try:
            project_cleanup = subprocess.run(
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
            if project_cleanup.returncode:
                cleanup_errors.append(project_cleanup.stderr.strip())
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup_errors.append(f"Compose cleanup thất bại: {type(error).__name__}")
        if cleanup_errors and sys.exc_info()[0] is None:
            pytest.fail("E2E teardown không sạch: " + "; ".join(cleanup_errors))


def _redact_runtime_logs(logs: str) -> str:
    """Ẩn dạng credential phổ biến trước khi đưa log container vào lỗi test."""
    redacted = re.sub(
        r"(?i)(password|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        logs,
    )
    return re.sub(
        r"(?i)(postgres(?:ql)?(?:\+[^:/\s]+)?://)[^:/\s]+:[^@/\s]+@",
        r"\1[REDACTED]@",
        redacted,
    )
