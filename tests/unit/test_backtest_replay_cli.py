from __future__ import annotations

import getpass as getpass_module
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.application.backtest_replay_jobs import (
    BacktestReplayBundleChoice,
    BacktestReplayJob,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import AuthenticationFailure, AuthService, LoginResult
from trading_bot.cli import backtest_replay as cli
from trading_bot.config.settings import AppSettings


def _job() -> BacktestReplayJob:
    now = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
    return BacktestReplayJob(
        id=UUID("00000000-0000-0000-0000-000000000101"),
        status="QUEUED",
        stage="queued",
        progress=0,
        bundle_hash="a" * 64,
        strategy_release_id="h01-v1",
        execution_interval="1h",
        created_at=now,
        updated_at=now,
        error_code=None,
        result_run_ids=(),
        cancel_requested=False,
        cancel_requested_at=None,
        cancel_requested_by=None,
    )


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _Service:
    def __init__(self, sessions: object) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def list(self, principal: Principal) -> tuple[BacktestReplayJob, ...]:
        self.calls.append(("list", (principal,)))
        return (_job(),)

    async def list_bundles(self, principal: Principal) -> tuple[BacktestReplayBundleChoice, ...]:
        self.calls.append(("bundles", (principal,)))
        return (
            BacktestReplayBundleChoice(
                bundle_hash="b" * 64,
                symbol="BTCUSDT",
                evaluation_start=datetime(2026, 11, 1, tzinfo=UTC),
                range_end=datetime(2026, 11, 2, tzinfo=UTC),
                intervals=("1h", "4h"),
            ),
        )

    async def submit(self, principal: Principal, request: object) -> BacktestReplayJob:
        self.calls.append(("submit", (principal, request)))
        return _job()

    async def cancel(self, principal: Principal, job_id: UUID) -> BacktestReplayJob:
        self.calls.append(("cancel", (principal, job_id)))
        return _job()


class _Auth:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.login_calls = 0
        self.login_arguments_match = False
        self.logout_calls = 0
        self.logout_cookie_match = False

    async def login(
        self, *, workspace_slug: str, email: str, password: str, code: str
    ) -> LoginResult:
        self.login_calls += 1
        self.login_arguments_match = (
            workspace_slug == "paper-workspace"
            and email == "owner@example.test"
            and password == "password-never-print"
            and code == "987654"
        )
        return LoginResult(
            cookie_value="session-token-never-print",
            csrf_token="csrf-never-print",
            principal=self.principal,
            email="owner@example.test",
        )

    async def logout(self, cookie_value: str | None) -> None:
        self.logout_calls += 1
        self.logout_cookie_match = cookie_value == "session-token-never-print"


def _principal(roles: frozenset[Role] | None = None) -> Principal:
    return Principal(
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
        workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
        roles=roles if roles is not None else frozenset({Role.OWNER}),
        mfa_verified_at=datetime(2026, 11, 4, 3, 0, tzinfo=UTC),
    )


def _settings(role: str = "admin-web") -> SimpleNamespace:
    return SimpleNamespace(
        process_role=role,
        database_user="crypidian_admin_web",
        database_password_file=Path("/unused/secret"),
        database_host="postgres",
        database_name="crypidian",
        mfa_encryption_key_file=Path("/unused/mfa-key"),
        mfa_secret_directory=Path("/unused/mfa-secrets"),
    )


def _arguments(command: str) -> list[str]:
    base = [command, "--workspace-slug", "paper-workspace"]
    if command == "enqueue":
        base.extend(
            [
                "--bundle-hash",
                "a" * 64,
                "--strategy-release-id",
                "h01-v1",
                "--execution-interval",
                "1h",
                "--starting-equity",
                "10000",
                "--per-trade-risk-fraction",
                "0.01",
                "--fee-rate",
                "0.0004",
                "--slippage-rate",
                "0.0002",
                "--seed",
                "7",
            ]
        )
    elif command == "cancel":
        base.extend(["--job-id", str(UUID(int=101))])
    return base


def _wire_admin_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth: _Auth,
    service: _Service,
    engine: _Engine,
    settings: SimpleNamespace | None = None,
) -> None:
    monkeypatch.setattr(cli, "AppSettings", lambda: settings or _settings())
    monkeypatch.setattr(cli, "_admin_sessions", lambda value: (engine, object()))
    monkeypatch.setattr(cli, "_auth_service", lambda value, sessions: auth)
    monkeypatch.setattr(cli, "BacktestReplayJobService", lambda sessions: service)
    monkeypatch.setattr(
        cli,
        "_prompt_login_credentials",
        lambda: (
            "owner@example.test",
            "password-never-print",
            "987654",
        ),
    )


def test_help_and_admin_arguments_never_accept_identity_or_credentials() -> None:
    parser = cli._parser()
    worker_arguments = parser.parse_args(["worker", "--once"])
    admin_arguments = parser.parse_args(["list", "--workspace-slug", "paper-workspace"])

    assert "Quản lý replay PAPER" in parser.format_help()
    assert worker_arguments.once is True
    assert admin_arguments.workspace_slug == "paper-workspace"
    assert not hasattr(admin_arguments, "workspace_id")
    assert not hasattr(admin_arguments, "membership_id")
    assert not hasattr(admin_arguments, "password")
    assert not hasattr(admin_arguments, "code")


def test_prompt_uses_getpass_and_rejects_noninteractive_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Terminal:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(sys, "stderr", Terminal())
    prompts: list[str] = []
    values = iter(("owner@example.test", "password-never-print", "987654"))

    def getpass(prompt: str) -> str:
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr(getpass_module, "getpass", getpass)

    assert cli._prompt_login_credentials() == (
        "owner@example.test",
        "password-never-print",
        "987654",
    )
    assert len(prompts) == 3
    assert all(
        "password" not in prompt.lower() or "mật khẩu" in prompt.lower() for prompt in prompts
    )

    class Pipe:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", Pipe())
    with pytest.raises(ValueError, match="terminal tương tác"):
        cli._prompt_login_credentials()


@pytest.mark.parametrize(
    ("command", "method"),
    [("list", "list"), ("bundles", "bundles"), ("enqueue", "submit"), ("cancel", "cancel")],
)
def test_admin_commands_use_authenticated_principal_and_revoke_cli_session(
    command: str,
    method: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    principal = _principal()
    auth = _Auth(principal)
    engine = _Engine()
    service = _Service(object())
    _wire_admin_mocks(monkeypatch, auth=auth, service=service, engine=engine)

    assert cli.main(_arguments(command)) == 0

    output = json.loads(capsys.readouterr().out)
    assert service.calls[0][0] == method
    assert service.calls[0][1][0] is principal
    assert auth.login_calls == auth.logout_calls == 1
    assert auth.login_arguments_match
    assert auth.logout_cookie_match
    assert engine.disposed
    if command in {"list", "enqueue", "cancel"}:
        expected = output[0] if command == "list" else output
        assert expected["id"] == str(_job().id)
        assert expected["progress"] == 0
    else:
        assert output[0]["symbol"] == "BTCUSDT"
    captured = capsys.readouterr()
    assert "password-never-print" not in captured.out + captured.err
    assert "987654" not in captured.out + captured.err
    assert "session-token-never-print" not in captured.out + captured.err


def test_non_owner_login_cannot_dispatch_owner_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth = _Auth(_principal(frozenset({Role.OPERATOR})))
    engine = _Engine()
    service = _Service(object())
    _wire_admin_mocks(monkeypatch, auth=auth, service=service, engine=engine)

    assert cli.main(_arguments("list")) == 2
    assert service.calls == []
    assert auth.logout_calls == 1
    assert engine.disposed
    output = capsys.readouterr()
    assert "password-never-print" not in output.out + output.err
    assert "987654" not in output.out + output.err


def test_failed_login_does_not_dispatch_or_leak_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailedAuth(_Auth):
        async def login(
            self, *, workspace_slug: str, email: str, password: str, code: str
        ) -> LoginResult:
            raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    auth = FailedAuth(_principal())
    engine = _Engine()
    service = _Service(object())
    _wire_admin_mocks(monkeypatch, auth=auth, service=service, engine=engine)

    assert cli.main(_arguments("list")) == 2
    output = capsys.readouterr()
    assert service.calls == []
    assert "password-never-print" not in output.out + output.err
    assert "987654" not in output.out + output.err
    assert engine.disposed


def test_auth_service_uses_local_mfa_provider_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    typed_settings = AppSettings(
        mfa_encryption_key_file=settings.mfa_encryption_key_file,
        mfa_secret_directory=settings.mfa_secret_directory,
    )
    sessions = cast(async_sessionmaker[Any], object())
    provider = object()
    captured: dict[str, object] = {}

    def create_provider(*, key_file: Path, storage_directory: Path) -> object:
        captured["key_file"] = key_file
        captured["storage_directory"] = storage_directory
        return provider

    def auth_factory(received_sessions: object, received_provider: object) -> AuthService:
        captured["sessions"] = received_sessions
        captured["provider"] = received_provider
        return cast(AuthService, provider)

    monkeypatch.setattr(cli, "LocalMfaSecretProvider", create_provider)
    monkeypatch.setattr(cli, "AuthService", auth_factory)

    assert cli._auth_service(typed_settings, sessions) is provider
    assert captured == {
        "key_file": settings.mfa_encryption_key_file,
        "storage_directory": settings.mfa_secret_directory,
        "sessions": sessions,
        "provider": provider,
    }


def test_unexpected_service_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth = _Auth(_principal())
    engine = _Engine()

    class BrokenService(_Service):
        async def list(self, principal: Principal) -> tuple[BacktestReplayJob, ...]:
            raise RuntimeError("database password=do-not-print")

    service = BrokenService(object())
    _wire_admin_mocks(monkeypatch, auth=auth, service=service, engine=engine)

    assert cli.main(_arguments("list")) == 2
    error = capsys.readouterr().err
    assert "Không thể xử lý job replay" in error
    assert "do-not-print" not in error
    assert "password-never-print" not in error
    assert "987654" not in error
    assert auth.logout_calls == 1
    assert engine.disposed
