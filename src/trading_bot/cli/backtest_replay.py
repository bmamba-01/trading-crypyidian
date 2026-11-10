"""CLI tạo, xem, hủy và chạy job replay từ dữ liệu đã lưu."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.application.backtest_replay_jobs import (
    BacktestReplayBundleChoice,
    BacktestReplayJob,
    BacktestReplayJobService,
    BacktestReplayJobSubmission,
)
from trading_bot.auth.policy import Role
from trading_bot.auth.service import AuthenticationFailure, AuthService, LoginResult
from trading_bot.config.settings import AppSettings
from trading_bot.workers.backtest_replay_worker import run_backtest_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quản lý replay PAPER trên dataset đã lưu.")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker", help="Chạy worker job replay cho một workspace.")
    worker.add_argument(
        "--once",
        action="store_true",
        help="Nhận và xử lý tối đa một job rồi thoát.",
    )
    for name in ("list", "bundles"):
        command = commands.add_parser(name, help="Xem job hoặc bundle của workspace.")
        command.add_argument("--workspace-slug", required=True)
    enqueue = commands.add_parser("enqueue", help="Tạo job replay mới.")
    _add_admin_scope(enqueue)
    enqueue.add_argument("--bundle-hash", required=True)
    enqueue.add_argument("--strategy-release-id", required=True)
    enqueue.add_argument("--execution-interval", required=True)
    enqueue.add_argument("--starting-equity", required=True, type=Decimal)
    enqueue.add_argument("--per-trade-risk-fraction", required=True, type=Decimal)
    enqueue.add_argument("--fee-rate", required=True, type=Decimal)
    enqueue.add_argument("--slippage-rate", required=True, type=Decimal)
    enqueue.add_argument("--seed", required=True, type=int)
    cancel = commands.add_parser("cancel", help="Yêu cầu worker dừng job tại điểm an toàn.")
    _add_admin_scope(cancel)
    cancel.add_argument("--job-id", required=True, type=UUID)
    return parser


def _add_admin_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-slug", required=True)


def _auth_service(settings: AppSettings, sessions: async_sessionmaker[Any]) -> AuthService:
    """Dựng AuthService bằng kho bí mật MFA local đang cấu hình cho admin-web."""
    key_file = settings.mfa_encryption_key_file
    secret_directory = settings.mfa_secret_directory
    if key_file is None or secret_directory is None:
        raise ValueError("Thiếu cấu hình khóa và thư mục bí mật MFA của admin-web.")
    secret_provider = LocalMfaSecretProvider(
        key_file=key_file,
        storage_directory=secret_directory,
    )
    return AuthService(sessions, secret_provider)


def _prompt_login_credentials() -> tuple[str, str, str]:
    """Nhận thông tin đăng nhập qua terminal ẩn, không nhận credential từ argv/env."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError(
            "Lệnh quản trị cần terminal tương tác; không truyền thông tin đăng nhập qua argv/env."
        )
    email = getpass.getpass("Email tài khoản: ").strip()
    password = getpass.getpass("Mật khẩu: ")
    code = getpass.getpass("Mã TOTP: ").strip()
    if not email or not password or not code:
        raise ValueError("Email, mật khẩu và mã TOTP không được để trống.")
    return email, password, code


def _admin_sessions(
    settings: AppSettings,
) -> tuple[AsyncEngine, async_sessionmaker[Any]]:
    if settings.process_role != "admin-web" or settings.database_user != "crypidian_admin_web":
        raise ValueError("Lệnh quản trị cần process_role=admin-web và login admin_web.")
    password_file = settings.database_password_file
    if password_file is None:
        raise ValueError("Thiếu file credential database admin_web.")
    url = database_url_from_secret(
        username=settings.database_user,
        password_file=Path(password_file),
        host=settings.database_host,
        database=settings.database_name,
    )
    engine = create_database_engine(url)
    return engine, create_session_factory(engine)


def _serialize_job(job: BacktestReplayJob) -> dict[str, object]:
    return {
        "id": str(job.id),
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "bundle_hash": job.bundle_hash,
        "strategy_release_id": job.strategy_release_id,
        "execution_interval": job.execution_interval,
        "learning_intent": job.learning_intent,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "cancel_requested": job.cancel_requested,
        "cancel_requested_at": (
            job.cancel_requested_at.isoformat() if job.cancel_requested_at else None
        ),
        "error_code": job.error_code,
        "result_run_ids": list(job.result_run_ids),
    }


async def _run_admin_command(arguments: argparse.Namespace, settings: AppSettings) -> int:
    engine, sessions = _admin_sessions(settings)
    auth_service: AuthService | None = None
    login_result: LoginResult | None = None
    try:
        service = BacktestReplayJobService(sessions)
        auth_service = _auth_service(settings, sessions)
        email, password, code = _prompt_login_credentials()
        try:
            login_result = await auth_service.login(
                workspace_slug=arguments.workspace_slug,
                email=email,
                password=password,
                code=code,
            )
        finally:
            del email, password, code
        principal = login_result.principal
        if Role.OWNER not in principal.roles:
            raise PermissionError("Chỉ owner workspace được quản lý replay job.")
        if arguments.command == "list":
            rows: tuple[BacktestReplayJob, ...] = await service.list(principal)
            print(json.dumps([_serialize_job(item) for item in rows], ensure_ascii=False, indent=2))
        elif arguments.command == "bundles":
            bundles: tuple[BacktestReplayBundleChoice, ...] = await service.list_bundles(principal)
            payload = [
                {
                    "bundle_hash": item.bundle_hash,
                    "symbol": item.symbol,
                    "evaluation_start": item.evaluation_start.isoformat(),
                    "range_end": item.range_end.isoformat(),
                    "intervals": list(item.intervals),
                }
                for item in bundles
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif arguments.command == "enqueue":
            request = BacktestReplayJobSubmission(
                bundle_hash=arguments.bundle_hash,
                strategy_release_id=arguments.strategy_release_id,
                execution_interval=arguments.execution_interval,
                starting_equity=arguments.starting_equity,
                per_trade_risk_fraction=arguments.per_trade_risk_fraction,
                fee_rate=arguments.fee_rate,
                slippage_rate=arguments.slippage_rate,
                seed=arguments.seed,
            )
            print(
                json.dumps(
                    _serialize_job(await service.submit(principal, request)),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif arguments.command == "cancel":
            job = await service.cancel(principal, arguments.job_id)
            print(json.dumps(_serialize_job(job), ensure_ascii=False, indent=2))
        else:
            raise ValueError("Lệnh quản trị không được hỗ trợ.")
        return 0
    finally:
        try:
            if auth_service is not None and login_result is not None:
                await auth_service.logout(login_result.cookie_value)
        finally:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        settings = AppSettings()
        if arguments.command == "worker":
            if settings.process_role != "backtest-worker":
                raise PermissionError("Worker cần TRADING_BOT_PROCESS_ROLE=backtest-worker.")
            asyncio.run(run_backtest_worker(settings, once=arguments.once))
            return 0
        return asyncio.run(_run_admin_command(arguments, settings))
    except (ValueError, PermissionError, LookupError) as error:
        print(f"Không thể xử lý job replay: {error}", file=sys.stderr)
        return 2
    except AuthenticationFailure:
        print(
            "Đăng nhập không thành công; kiểm tra workspace, email, mật khẩu và mã TOTP.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            "Không thể xử lý job replay do lỗi nội bộ; kiểm tra nhật ký vận hành.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
