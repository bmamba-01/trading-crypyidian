"""CLI chạy một lượt hoặc supervisor observer learning trong workspace deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.application.learning_shadow import LearningShadowApplication
from trading_bot.config.settings import AppSettings
from trading_bot.workers.learning_shadow_observer import LearningShadowObserverSupervisor

OBSERVER_USER = "crypidian_learning_shadow_observer"
OBSERVER_PASSWORD_FILE = Path("/run/secrets/database_learning_shadow_observer_password")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Theo dõi quyết định PAPER và ghi challenger ABSTAIN an toàn."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("once", help="Quan sát tối đa một batch rồi thoát.")
    commands.add_parser("run", help="Chạy supervisor cho tới khi nhận tín hiệu dừng.")
    return parser


def create_observer_pool(
    settings: AppSettings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Tạo pool bằng login observer cố định; CLI không nhận DSN hoặc secret từ argv."""
    if settings.process_role != "learning-shadow-observer":
        raise PermissionError("CLI chỉ chạy với process_role=learning-shadow-observer.")
    if settings.trading_mode != "PAPER":
        raise PermissionError("CLI observer chỉ chạy trong PAPER.")
    if settings.database_user != OBSERVER_USER:
        raise PermissionError("CLI observer cần login database observer riêng.")
    if settings.database_password_file != OBSERVER_PASSWORD_FILE:
        raise PermissionError("CLI observer cần secret database observer cố định.")
    if settings.learning_shadow_observer_workspace_id is None:
        raise PermissionError("CLI observer cần workspace deployment đã cấu hình.")
    engine = create_database_engine(
        database_url_from_secret(
            username=OBSERVER_USER,
            password_file=OBSERVER_PASSWORD_FILE,
            host=settings.database_host,
            database=settings.database_name,
        )
    )
    return engine, create_session_factory(engine)


async def _observe_once(settings: AppSettings) -> int:
    workspace_id = settings.learning_shadow_observer_workspace_id
    if workspace_id is None:
        raise PermissionError("learning-shadow-observer cần workspace deployment.")
    engine, sessions = create_observer_pool(settings)
    try:
        supervisor = LearningShadowObserverSupervisor(
            application=LearningShadowApplication(sessions),
            workspace_id=workspace_id,
            poll_seconds=settings.learning_shadow_observer_poll_seconds,
            max_backoff_seconds=settings.learning_shadow_observer_max_backoff_seconds,
        )
        batch = await supervisor.run_once()
        print(
            json.dumps(
                {
                    "workspace_id": str(workspace_id),
                    "scanned_count": batch.scanned_count,
                    "created_count": batch.created_count,
                    "skipped_count": batch.skipped_count,
                    "status": "succeeded",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


async def _run_supervisor(settings: AppSettings) -> int:
    workspace_id = settings.learning_shadow_observer_workspace_id
    if workspace_id is None:
        raise PermissionError("learning-shadow-observer cần workspace deployment.")
    engine, sessions = create_observer_pool(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    try:
        for current_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(current_signal, stopping.set)
            installed.append(current_signal)
        supervisor = LearningShadowObserverSupervisor(
            application=LearningShadowApplication(sessions),
            workspace_id=workspace_id,
            poll_seconds=settings.learning_shadow_observer_poll_seconds,
            max_backoff_seconds=settings.learning_shadow_observer_max_backoff_seconds,
        )
        await supervisor.run(stopping)
        return 0
    finally:
        stopping.set()
        for current_signal in installed:
            loop.remove_signal_handler(current_signal)
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        settings = AppSettings()
        if arguments.command == "once":
            return asyncio.run(_observe_once(settings))
        if arguments.command == "run":
            return asyncio.run(_run_supervisor(settings))
        raise ValueError("CLI command không được hỗ trợ.")
    except (ValueError, PermissionError, LookupError) as error:
        print(f"Không thể chạy learning shadow observer: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"Learning shadow observer gặp lỗi nội bộ ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
