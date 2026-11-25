"""Scheduler lập lịch chạy định kỳ cho replay/backfill job (WP-07C / CRY-365).

Bảo đảm:
- Phân quyền rõ ai được lập lịch / hủy (`owner`, `strategy_admin`).
- Kiểm tra lại quyền dữ liệu hiện hành (`rights_checker`) ở mỗi lượt tick.
- Giới hạn tài nguyên (`max_concurrent_runs`, `min_interval_seconds`, `max_runs_per_day`).
- Không giữ transaction DB dài: mỗi lượt tick là thao tác ngắn hạn, ghi audit đầy đủ.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

ALLOWED_SCHEDULER_ROLES: frozenset[str] = frozenset({"owner", "strategy_admin"})


class ScheduledJobKind(StrEnum):
    REPLAY = "REPLAY"
    BACKFILL = "BACKFILL"


class ScheduleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class SchedulerError(ValueError):
    """Lỗi nghiệp vụ có mã lý do khi tạo, hủy hoặc chạy lịch."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class JobSchedulePolicy:
    """Giới hạn tài nguyên cho scheduler nhằm tránh quá tải worker và DB."""

    max_concurrent_runs: int = 1
    min_interval_seconds: int = 300
    max_runs_per_day: int = 24


@dataclass(frozen=True, slots=True)
class WorkbenchJobSchedule:
    """Cấu hình một lịch chạy định kỳ cho replay hoặc backfill job."""

    schedule_id: UUID
    workspace_id: UUID
    kind: ScheduledJobKind
    target_ref: str
    interval_seconds: int
    status: ScheduleStatus
    created_by: str
    created_role: str
    next_run_at: datetime
    created_at: datetime
    last_run_at: datetime | None = None
    runs_today: int = 0
    cancel_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleAuditEntry:
    """Bản ghi audit cho mọi thao tác tạo, hủy, bỏ qua và dispatch của scheduler."""

    event: str
    schedule_id: UUID
    actor_id: str
    occurred_at: datetime
    detail: str


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    """Kết quả một lượt tick ngắn hạn của scheduler."""

    dispatched_schedule_ids: tuple[UUID, ...]
    skipped_rights_ids: tuple[UUID, ...]
    skipped_limit_ids: tuple[UUID, ...]


class WorkbenchJobScheduler:
    """Bộ lập lịch định kỳ kiểm quyền theo từng lượt và tôn trọng giới hạn tài nguyên."""

    def __init__(self, *, policy: JobSchedulePolicy | None = None) -> None:
        self._policy = policy or JobSchedulePolicy()
        self._schedules: dict[UUID, WorkbenchJobSchedule] = {}
        self._dispatched_slots: set[tuple[UUID, str]] = set()
        self._audit_log: list[ScheduleAuditEntry] = []

    @property
    def audit_log(self) -> tuple[ScheduleAuditEntry, ...]:
        return tuple(self._audit_log)

    def create_schedule(
        self,
        *,
        workspace_id: UUID,
        kind: ScheduledJobKind,
        target_ref: str,
        interval_seconds: int,
        actor_id: str,
        actor_role: str,
        start_at: datetime,
    ) -> WorkbenchJobSchedule:
        """Tạo lịch mới; chỉ cho phép vai trò `owner` hoặc `strategy_admin`."""
        role = actor_role.lower().strip()
        if role not in ALLOWED_SCHEDULER_ROLES:
            raise SchedulerError(
                "SCHEDULER_PERMISSION_DENIED",
                f"Vai trò {actor_role} không được phép lập lịch job.",
            )
        if interval_seconds < self._policy.min_interval_seconds:
            raise SchedulerError(
                "SCHEDULER_INTERVAL_TOO_SHORT",
                f"Chu kỳ tối thiểu là {self._policy.min_interval_seconds} giây.",
            )
        if not target_ref.strip():
            raise SchedulerError("SCHEDULER_TARGET_EMPTY", "target_ref không được rỗng.")

        start_utc = start_at.astimezone(UTC)
        sched = WorkbenchJobSchedule(
            schedule_id=uuid4(),
            workspace_id=workspace_id,
            kind=kind,
            target_ref=target_ref.strip(),
            interval_seconds=interval_seconds,
            status=ScheduleStatus.ACTIVE,
            created_by=actor_id,
            created_role=role,
            next_run_at=start_utc,
            created_at=start_utc,
        )
        self._schedules[sched.schedule_id] = sched
        self._audit_log.append(
            ScheduleAuditEntry(
                event="schedule.created",
                schedule_id=sched.schedule_id,
                actor_id=actor_id,
                occurred_at=start_utc,
                detail=f"{kind.value}:{target_ref} mỗi {interval_seconds}s",
            )
        )
        return sched

    def cancel_schedule(
        self,
        schedule_id: UUID,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
        now: datetime,
    ) -> WorkbenchJobSchedule:
        """Hủy lịch chạy và ghi audit đầy đủ."""
        if actor_role.lower().strip() not in ALLOWED_SCHEDULER_ROLES:
            raise SchedulerError(
                "SCHEDULER_PERMISSION_DENIED",
                f"Vai trò {actor_role} không được phép hủy lịch.",
            )
        if schedule_id not in self._schedules:
            raise SchedulerError("SCHEDULE_NOT_FOUND", f"Không tìm thấy lịch {schedule_id}.")
        current = self._schedules[schedule_id]
        cancelled = replace(current, status=ScheduleStatus.CANCELLED, cancel_reason=reason)
        self._schedules[schedule_id] = cancelled
        self._audit_log.append(
            ScheduleAuditEntry(
                event="schedule.cancelled",
                schedule_id=schedule_id,
                actor_id=actor_id,
                occurred_at=now.astimezone(UTC),
                detail=reason,
            )
        )
        return cancelled

    def tick(
        self,
        *,
        now: datetime,
        active_runs: int,
        rights_checker: Callable[[WorkbenchJobSchedule], bool],
        dispatcher: Callable[[WorkbenchJobSchedule, str], None],
    ) -> SchedulerTickResult:
        """Thực thi một lượt kiểm tra ngắn hạn; luôn kiểm lại quyền hiện hành trước khi dispatch."""
        now_utc = now.astimezone(UTC)
        dispatched: list[UUID] = []
        skipped_rights: list[UUID] = []
        skipped_limit: list[UUID] = []
        running = active_runs

        for sid, sched in list(self._schedules.items()):
            if sched.status is not ScheduleStatus.ACTIVE:
                continue
            if now_utc < sched.next_run_at:
                continue

            if (
                running >= self._policy.max_concurrent_runs
                or sched.runs_today >= self._policy.max_runs_per_day
            ):
                skipped_limit.append(sid)
                self._audit_log.append(
                    ScheduleAuditEntry(
                        event="schedule.skipped_resource_limit",
                        schedule_id=sid,
                        actor_id="scheduler-worker",
                        occurred_at=now_utc,
                        detail=f"active_runs={running}, runs_today={sched.runs_today}",
                    )
                )
                continue

            if not rights_checker(sched):
                skipped_rights.append(sid)
                self._audit_log.append(
                    ScheduleAuditEntry(
                        event="schedule.skipped_rights_denied",
                        schedule_id=sid,
                        actor_id="scheduler-worker",
                        occurred_at=now_utc,
                        detail="Thiếu quyền dữ liệu hiện hành tại thời điểm tick.",
                    )
                )
                continue

            slot_key = sched.next_run_at.isoformat()
            if (sid, slot_key) in self._dispatched_slots:
                continue

            dispatcher(sched, slot_key)
            self._dispatched_slots.add((sid, slot_key))
            running += 1
            dispatched.append(sid)
            self._schedules[sid] = replace(
                sched,
                last_run_at=now_utc,
                next_run_at=now_utc + timedelta(seconds=sched.interval_seconds),
                runs_today=sched.runs_today + 1,
            )
            self._audit_log.append(
                ScheduleAuditEntry(
                    event="schedule.dispatched",
                    schedule_id=sid,
                    actor_id="scheduler-worker",
                    occurred_at=now_utc,
                    detail=f"slot={slot_key}",
                )
            )

        return SchedulerTickResult(
            dispatched_schedule_ids=tuple(dispatched),
            skipped_rights_ids=tuple(skipped_rights),
            skipped_limit_ids=tuple(skipped_limit),
        )
