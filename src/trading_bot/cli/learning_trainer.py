"""Entrypoint chạy một attempt đã được admin queue trong WP06."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.config.settings import AppSettings
from trading_bot.workers.learning_training_worker import (
    LearningTrainingAttemptWorker,
    LearningTrainingWorkerFailure,
)

TRAINER_USER = "crypidian_trainer_worker"
TRAINER_PASSWORD_FILE = Path("/run/secrets/database_trainer_worker_password")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chạy một attempt learning đã được cấp.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Claim, train và lưu đúng một attempt.")
    run.add_argument("--workspace-id", required=True, type=UUID)
    run.add_argument("--attempt-id", required=True, type=UUID)
    work_once = commands.add_parser(
        "work-once",
        help="Phục hồi lease và claim, train tối đa một job trong workspace.",
    )
    work_once.add_argument("--workspace-id", required=True, type=UUID)
    work_once.add_argument("--worker-id", required=True)
    return parser


def create_trainer_pool(
    settings: AppSettings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Tạo pool bằng fixed trainer login/secret; không nhận user hoặc file từ argv."""
    if settings.process_role != "trainer-worker":
        raise PermissionError("CLI training chỉ chạy trong process_role=trainer-worker.")
    if settings.trading_mode != "PAPER":
        raise PermissionError("CLI training chỉ chạy trong PAPER.")
    if settings.database_user != TRAINER_USER:
        raise PermissionError("CLI training cần login database trainer riêng.")
    if settings.database_password_file != TRAINER_PASSWORD_FILE:
        raise PermissionError("CLI training cần secret trainer cố định do Compose mount.")
    engine = create_database_engine(
        database_url_from_secret(
            username=TRAINER_USER,
            password_file=TRAINER_PASSWORD_FILE,
            host=settings.database_host,
            database=settings.database_name,
        )
    )
    return engine, create_session_factory(engine)


async def _run_attempt(settings: AppSettings, workspace_id: UUID, attempt_id: UUID) -> int:
    engine, sessions = create_trainer_pool(settings)
    try:
        result = await LearningTrainingAttemptWorker(sessions).run_once(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        print(
            json.dumps(
                {
                    "status": "succeeded",
                    "workspace_id": str(workspace_id),
                    "attempt_id": str(result.attempt_id),
                    "artifact_id": str(result.artifact_id),
                    "artifact_hash": result.artifact_hash,
                    "duplicate": result.duplicate,
                    "elapsed_milliseconds": result.elapsed_milliseconds,
                    "cpu_time_milliseconds": result.cpu_time_milliseconds,
                    "peak_memory_bytes": result.peak_memory_bytes,
                    "actual_operation_units": result.actual_operation_units,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


async def _work_once(settings: AppSettings, workspace_id: UUID, worker_id: str) -> int:
    engine, sessions = create_trainer_pool(settings)
    try:
        result = await LearningTrainingAttemptWorker(sessions).work_once(
            workspace_id=workspace_id,
            worker_id=worker_id,
        )
        payload: dict[str, object] = {
            "status": result.status,
            "workspace_id": str(workspace_id),
            "job_id": str(result.job_id) if result.job_id is not None else None,
            "attempt_id": str(result.attempt_id) if result.attempt_id is not None else None,
            "recovered_jobs": result.recovered_jobs,
            "cancelled_jobs": result.cancelled_jobs,
        }
        if result.training is not None:
            payload.update(
                artifact_id=str(result.training.artifact_id),
                artifact_hash=result.training.artifact_hash,
                duplicate=result.training.duplicate,
                elapsed_milliseconds=result.training.elapsed_milliseconds,
                cpu_time_milliseconds=result.training.cpu_time_milliseconds,
                peak_memory_bytes=result.training.peak_memory_bytes,
                actual_operation_units=result.training.actual_operation_units,
            )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        settings = AppSettings()
        if arguments.command == "run":
            return asyncio.run(_run_attempt(settings, arguments.workspace_id, arguments.attempt_id))
        if arguments.command == "work-once":
            return asyncio.run(_work_once(settings, arguments.workspace_id, arguments.worker_id))
        raise ValueError("CLI command không được hỗ trợ.")
    except LearningTrainingWorkerFailure as error:
        print(f"Attempt trainer failed: {error.reason_code.value}", file=sys.stderr)
        return 1
    except (ValueError, PermissionError, LookupError) as error:
        print(f"Không thể chạy attempt trainer: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"Không thể chạy attempt trainer do lỗi nội bộ ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
