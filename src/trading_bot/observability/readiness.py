"""Đánh giá readiness có mã nguyên nhân (reason-coded) cho các worker (WP-07E / CRY-375)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class WorkerReadinessState(StrEnum):
    """Trạng thái sẵn sàng chuẩn hóa cho worker và producer/sender/backfill."""

    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class WorkerReadinessReport:
    """Báo cáo readiness có mã nguyên nhân, không chứa secret hay dữ liệu nhạy cảm."""

    component: str
    state: WorkerReadinessState
    reason_code: str | None = None
    detail_vi: str | None = None
    last_tick_at: datetime | None = None
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Chuyển sang dict JSON-safe cho `/health/ready` và admin-web."""
        payload: dict[str, Any] = {
            "component": self.component,
            "state": self.state.value,
        }
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        if self.detail_vi is not None:
            payload["detail_vi"] = self.detail_vi
        if self.last_tick_at is not None:
            payload["last_tick_at"] = self.last_tick_at.isoformat()
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def evaluate_worker_readiness(
    *,
    component: str,
    worker: Any,
    task: Any = None,
    require_task: bool = True,
    missing_secret_reason: str | None = None,
    missing_rights_reason: str | None = None,
    max_tick_age: timedelta | None = None,
    now: datetime | None = None,
) -> WorkerReadinessReport:
    """Đánh giá readiness reason-coded từ trạng thái worker, task và quyền/secret."""
    if missing_secret_reason is not None:
        return WorkerReadinessReport(
            component=component,
            state=WorkerReadinessState.NOT_READY,
            reason_code=missing_secret_reason,
            detail_vi="Thiếu secret hoặc thông tin xác thực bắt buộc của worker.",
        )

    if worker is None:
        return WorkerReadinessReport(
            component=component,
            state=WorkerReadinessState.NOT_READY,
            reason_code="WORKER_NOT_STARTED",
            detail_vi="Worker chưa được khởi tạo trong tiến trình.",
        )

    if require_task and (task is None or (callable(getattr(task, "done", None)) and task.done())):
        last_error = getattr(worker, "last_error_code", None)
        return WorkerReadinessReport(
            component=component,
            state=WorkerReadinessState.NOT_READY,
            reason_code=str(last_error) if last_error else "WORKER_TASK_STOPPED",
            detail_vi="Task nền của worker chưa chạy hoặc đã dừng.",
            last_tick_at=_safe_tick_at(worker),
        )

    last_tick_at = _safe_tick_at(worker)
    last_error = getattr(worker, "last_error_code", None)
    last_succeeded = bool(getattr(worker, "last_tick_succeeded", False))

    if not last_succeeded:
        reason = str(last_error) if last_error else "NO_SUCCESSFUL_TICK"
        return WorkerReadinessReport(
            component=component,
            state=WorkerReadinessState.NOT_READY,
            reason_code=reason,
            detail_vi="Worker chưa có chu kỳ chạy thành công gần nhất.",
            last_tick_at=last_tick_at,
        )

    if missing_rights_reason is not None:
        return WorkerReadinessReport(
            component=component,
            state=WorkerReadinessState.DEGRADED,
            reason_code=missing_rights_reason,
            detail_vi="Thiếu quyền nguồn dữ liệu hoặc điều khoản sử dụng hiện hành.",
            last_tick_at=last_tick_at,
        )

    if last_error is not None:
        return WorkerReadinessReport(
            component=component,
            state=WorkerReadinessState.DEGRADED,
            reason_code=str(last_error),
            detail_vi="Worker đang chạy nhưng có cảnh báo ở chu kỳ gần nhất.",
            last_tick_at=last_tick_at,
        )

    if max_tick_age is not None and last_tick_at is not None:
        current = now or datetime.now(UTC)
        if current - last_tick_at > max_tick_age:
            return WorkerReadinessReport(
                component=component,
                state=WorkerReadinessState.DEGRADED,
                reason_code="TICK_STALE",
                detail_vi="Chu kỳ thành công gần nhất đã quá ngưỡng thời gian.",
                last_tick_at=last_tick_at,
            )

    return WorkerReadinessReport(
        component=component,
        state=WorkerReadinessState.READY,
        last_tick_at=last_tick_at,
    )


def _safe_tick_at(worker: Any) -> datetime | None:
    value = getattr(worker, "last_tick_at", None)
    return value if isinstance(value, datetime) else None
