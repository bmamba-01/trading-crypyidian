"""Chạy lõi trainer trong tiến trình riêng với trần CPU/bộ nhớ theo attempt."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection
from threading import Event

from trading_bot.application.learning_cpu_trainer import train_authorized_batch
from trading_bot.application.learning_scorer_artifact import build_calibrated_scorer_artifact
from trading_bot.application.learning_training_batch import AuthorizedTrainerBatch


class LearningTrainingProcessFailureCode(StrEnum):
    """Mã lỗi tiến trình trainer đã làm sạch để parent ghi ledger an toàn."""

    CPU_LIMIT = "cpu_limit"
    MEMORY_LIMIT = "memory_limit"
    RUNTIME_LIMIT = "runtime_limit"
    RESOURCE_LIMIT_UNAVAILABLE = "resource_limit_unavailable"
    CHILD_FAILED = "child_failed"
    CHILD_RESULT_INVALID = "child_result_invalid"
    CANCELLED = "cancelled"


class LearningTrainingProcessFailure(RuntimeError):
    """Lỗi worker con không kèm traceback hay dữ liệu nhạy cảm."""

    def __init__(
        self,
        code: LearningTrainingProcessFailureCode,
        *,
        diagnostic_type: str | None = None,
        process_id: int | None = None,
    ) -> None:
        self.code = code
        self.diagnostic_type = diagnostic_type
        self.process_id = process_id
        super().__init__(f"Tiến trình trainer thất bại: {code.value}.")


@dataclass(frozen=True, slots=True)
class LearningCpuTrainingProcessResult:
    """Artifact v1/v2 cùng thời gian CPU, RSS cực đại và PID tiến trình con."""

    artifact_json: str
    artifact_hash: str
    elapsed_milliseconds: int
    cpu_time_milliseconds: int
    peak_memory_bytes: int
    process_id: int
    scorer_artifact_json: str | None = None
    scorer_artifact_hash: str | None = None


class LearningCpuTrainingProcessRunner:
    """Tạo process `spawn`, chỉ chuyển batch đã cấp quyền và nhận artifact canonical."""

    def train(
        self,
        batch: AuthorizedTrainerBatch,
        *,
        cancel_event: Event | None = None,
    ) -> LearningCpuTrainingProcessResult:
        """Fit một batch; timeout parent buộc dừng process nếu child không tự thoát."""
        if type(batch) is not AuthorizedTrainerBatch:
            raise ValueError("Process runner chỉ nhận AuthorizedTrainerBatch.")
        if cancel_event is not None and cancel_event.is_set():
            raise LearningTrainingProcessFailure(LearningTrainingProcessFailureCode.CANCELLED)
        if not sys.platform.startswith("linux"):
            raise LearningTrainingProcessFailure(
                LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
            )
        if not hasattr(os, "getpid") or not hasattr(multiprocessing, "get_context"):
            raise LearningTrainingProcessFailure(
                LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
            )
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        child = context.Process(
            target=_train_in_limited_child,
            args=(
                batch.resource_request.max_runtime_seconds,
                batch.resource_request.memory_limit_bytes,
                child_connection,
            ),
            name=f"learning-trainer-{batch.attempt_id}",
        )
        started_at = time.monotonic()
        try:
            child.start()
            child_connection.close()
            remaining_seconds = batch.resource_request.max_runtime_seconds - (
                time.monotonic() - started_at
            )
            while remaining_seconds > 0 and not parent_connection.poll(min(0.1, remaining_seconds)):
                if cancel_event is not None and cancel_event.is_set():
                    _stop_child(child)
                    raise LearningTrainingProcessFailure(
                        LearningTrainingProcessFailureCode.CANCELLED,
                        process_id=child.pid,
                    )
                remaining_seconds = batch.resource_request.max_runtime_seconds - (
                    time.monotonic() - started_at
                )
            if remaining_seconds <= 0:
                _stop_child(child)
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.RUNTIME_LIMIT
                )
            readiness = parent_connection.recv()
            if type(readiness) is not tuple or not readiness:
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                )
            if readiness[0] != "ready":
                if readiness[0] != "error" or len(readiness) < 2:
                    raise LearningTrainingProcessFailure(
                        LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                    )
                try:
                    failure_code = LearningTrainingProcessFailureCode(readiness[1])
                except IndexError, ValueError:
                    failure_code = LearningTrainingProcessFailureCode.CHILD_FAILED
                diagnostic_type = (
                    readiness[2] if len(readiness) > 2 and isinstance(readiness[2], str) else None
                )
                raise LearningTrainingProcessFailure(failure_code, diagnostic_type=diagnostic_type)
            if cancel_event is not None and cancel_event.is_set():
                _stop_child(child)
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CANCELLED,
                    process_id=child.pid,
                )
            parent_connection.send(batch)
            deadline = started_at + batch.resource_request.max_runtime_seconds
            while child.is_alive() and time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    _stop_child(child)
                    raise LearningTrainingProcessFailure(
                        LearningTrainingProcessFailureCode.CANCELLED,
                        process_id=child.pid,
                    )
                child.join(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            if child.is_alive():
                _stop_child(child)
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.RUNTIME_LIMIT
                )
            elapsed_milliseconds = max(0, round((time.monotonic() - started_at) * 1000))
            if not parent_connection.poll():
                if child.exitcode == -getattr(signal, "SIGXCPU", signal.SIGTERM):
                    code = LearningTrainingProcessFailureCode.CPU_LIMIT
                else:
                    code = LearningTrainingProcessFailureCode.CHILD_FAILED
                raise LearningTrainingProcessFailure(code, diagnostic_type=f"exit_{child.exitcode}")
            result = parent_connection.recv()
            if type(result) is not tuple or not result:
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                )
            if result[0] != "ok":
                try:
                    code = LearningTrainingProcessFailureCode(result[1])
                except IndexError, ValueError:
                    code = LearningTrainingProcessFailureCode.CHILD_FAILED
                diagnostic_type = (
                    result[2] if len(result) > 2 and isinstance(result[2], str) else None
                )
                raise LearningTrainingProcessFailure(code, diagnostic_type=diagnostic_type)
            if len(result) != 7:
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                )
            (
                artifact_json,
                artifact_hash,
                scorer_artifact_json,
                scorer_artifact_hash,
                cpu_time_ms,
                peak_memory_bytes,
            ) = result[1:]
            if (
                not isinstance(artifact_json, str)
                or not isinstance(artifact_hash, str)
                or not isinstance(scorer_artifact_json, str)
                or not isinstance(scorer_artifact_hash, str)
                or type(cpu_time_ms) is not int
                or type(peak_memory_bytes) is not int
                or cpu_time_ms < 0
                or peak_memory_bytes <= 0
            ):
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                )
            expected_hash = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
            if artifact_hash != expected_hash:
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                )
            expected_scorer_hash = hashlib.sha256(scorer_artifact_json.encode("utf-8")).hexdigest()
            if scorer_artifact_hash != expected_scorer_hash:
                raise LearningTrainingProcessFailure(
                    LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID
                )
            return LearningCpuTrainingProcessResult(
                artifact_json=artifact_json,
                artifact_hash=artifact_hash,
                elapsed_milliseconds=elapsed_milliseconds,
                cpu_time_milliseconds=cpu_time_ms,
                peak_memory_bytes=peak_memory_bytes,
                process_id=child.pid or 0,
                scorer_artifact_json=scorer_artifact_json,
                scorer_artifact_hash=scorer_artifact_hash,
            )
        except LearningTrainingProcessFailure:
            raise
        except (OSError, ValueError, EOFError) as error:
            raise LearningTrainingProcessFailure(
                LearningTrainingProcessFailureCode.CHILD_FAILED
            ) from error
        finally:
            if child.is_alive():
                _stop_child(child)
            if child.pid is not None:
                child.close()
            parent_connection.close()
            child_connection.close()


def _train_in_limited_child(
    cpu_seconds: int,
    address_space_bytes: int,
    connection: Connection,
) -> None:
    """Áp giới hạn trước khi nhận và giải mã batch từ tiến trình cha."""
    try:
        _apply_process_limits(
            cpu_seconds=cpu_seconds,
            address_space_bytes=address_space_bytes,
        )
    except LearningTrainingProcessFailure as error:
        _send_failure(connection, error.code)
        connection.close()
        return
    except ImportError, OSError, ValueError:
        _send_failure(connection, LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE)
        connection.close()
        return

    try:
        _remove_database_environment()
        connection.send(("ready",))
        batch = connection.recv()
        if (
            type(batch) is not AuthorizedTrainerBatch
            or batch.resource_request.max_runtime_seconds != cpu_seconds
            or batch.resource_request.memory_limit_bytes != address_space_bytes
        ):
            _send_failure(connection, LearningTrainingProcessFailureCode.CHILD_RESULT_INVALID)
            return
        artifact = train_authorized_batch(batch)
        scorer_artifact = build_calibrated_scorer_artifact(batch, artifact)
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        peak_memory_bytes = _peak_rss_bytes(usage.ru_maxrss)
        cpu_time_milliseconds = max(0, round((usage.ru_utime + usage.ru_stime) * 1000))
        connection.send(
            (
                "ok",
                artifact.canonical_json,
                artifact.artifact_hash,
                scorer_artifact.canonical_json,
                scorer_artifact.artifact_hash,
                cpu_time_milliseconds,
                peak_memory_bytes,
            )
        )
    except MemoryError:
        _send_failure(connection, LearningTrainingProcessFailureCode.MEMORY_LIMIT)
    except TimeoutError:
        _send_failure(connection, LearningTrainingProcessFailureCode.RUNTIME_LIMIT)
    except LearningTrainingProcessFailure as error:
        _send_failure(connection, error.code)
    except Exception as error:
        _send_failure(
            connection,
            LearningTrainingProcessFailureCode.CHILD_FAILED,
            diagnostic_type=type(error).__name__,
        )
    finally:
        connection.close()


def _apply_process_limits(*, cpu_seconds: int, address_space_bytes: int) -> None:
    """Thiết lập soft=hard; thiếu giới hạn hoặc host cap thấp thì từ chối chạy."""
    import resource

    if not hasattr(resource, "RLIMIT_CPU") or not hasattr(resource, "RLIMIT_AS"):
        raise LearningTrainingProcessFailure(
            LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
        )
    _, cpu_hard_limit = resource.getrlimit(resource.RLIMIT_CPU)
    cpu_soft = cpu_seconds
    cpu_hard = cpu_seconds + 1
    if cpu_hard_limit != resource.RLIM_INFINITY and cpu_hard > cpu_hard_limit:
        raise LearningTrainingProcessFailure(
            LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
        )
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_hard))

    _, memory_hard_limit = resource.getrlimit(resource.RLIMIT_AS)
    if memory_hard_limit != resource.RLIM_INFINITY and address_space_bytes > memory_hard_limit:
        raise LearningTrainingProcessFailure(
            LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
        )
    resource.setrlimit(resource.RLIMIT_AS, (address_space_bytes, address_space_bytes))
    if hasattr(resource, "RLIMIT_CORE"):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _peak_rss_bytes(maximum_resident_set_size: int) -> int:
    """Chuẩn hóa `ru_maxrss`: byte trên macOS, KiB trên Linux."""
    if type(maximum_resident_set_size) is not int or maximum_resident_set_size <= 0:
        raise ValueError("RSS đo được không hợp lệ.")
    if sys.platform == "darwin":
        return maximum_resident_set_size
    if sys.platform.startswith("linux"):
        return maximum_resident_set_size * 1024
    raise LearningTrainingProcessFailure(
        LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
    )


def _remove_database_environment() -> None:
    """Không để child dùng lại biến môi trường kết nối DB của worker cha."""
    for name in tuple(os.environ):
        if name.startswith("TRADING_BOT_DATABASE_") or name in {
            "DATABASE_URL",
            "PGPASSWORD",
            "PGSERVICEFILE",
        }:
            os.environ.pop(name, None)


def _send_failure(
    connection: Connection,
    code: LearningTrainingProcessFailureCode,
    *,
    diagnostic_type: str | None = None,
) -> None:
    try:
        connection.send(("error", code.value, diagnostic_type))
    except BrokenPipeError, OSError:
        pass


def _stop_child(child: multiprocessing.Process) -> None:
    """Yêu cầu thoát, sau đó SIGKILL nếu không dừng trong thời gian ngắn."""
    child.terminate()
    child.join(timeout=0.2)
    if child.is_alive():
        child.kill()
        child.join()


__all__ = [
    "LearningCpuTrainingProcessResult",
    "LearningCpuTrainingProcessRunner",
    "LearningTrainingProcessFailure",
    "LearningTrainingProcessFailureCode",
]
