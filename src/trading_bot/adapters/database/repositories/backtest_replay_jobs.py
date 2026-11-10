"""Claim, tiến độ và kết thúc job replay có lease trong transaction caller."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord

if TYPE_CHECKING:
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission

_MAX_ATTEMPTS = 3


class BacktestReplayJobRepository:
    """Mỗi phương thức dùng chung transaction/workspace đã mở bởi UnitOfWork."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        workspace_id: UUID,
        requested_by: UUID,
        request: BacktestReplayJobSubmission,
    ) -> BacktestReplayJobRecord:
        """Append payload canonical để worker không nhận cấu hình đã bị sửa."""
        payload = request.to_payload()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = BacktestReplayJobRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            requested_by=requested_by,
            bundle_hash=request.bundle_hash,
            strategy_release_id=request.strategy_release_id,
            execution_interval=request.execution_interval,
            request_payload=payload,
            canonical_request=canonical,
            status="QUEUED",
            stage="queued",
            progress=0,
            cancel_requested=False,
            attempt_count=0,
            lease_generation=0,
            result_run_ids=[],
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_recent(
        self, *, workspace_id: UUID, limit: int = 30
    ) -> list[BacktestReplayJobRecord]:
        """Giới hạn lịch sử trả về để trang admin không tải vô hạn."""
        if not 1 <= limit <= 100:
            raise ValueError("Giới hạn job phải nằm trong [1, 100].")
        result = await self._session.scalars(
            select(BacktestReplayJobRecord)
            .where(BacktestReplayJobRecord.workspace_id == workspace_id)
            .order_by(BacktestReplayJobRecord.created_at.desc(), BacktestReplayJobRecord.id.desc())
            .limit(limit)
        )
        return list(result.all())

    async def request_cancel(
        self, *, workspace_id: UUID, job_id: UUID, requested_by: UUID
    ) -> BacktestReplayJobRecord | None:
        """Chỉ bật cờ hủy; trạng thái cuối do worker xác nhận tại điểm an toàn."""
        row = await self._session.scalar(
            select(BacktestReplayJobRecord)
            .where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                BacktestReplayJobRecord.id == job_id,
            )
            .with_for_update()
        )
        if row is None:
            return None
        can_cancel = row.status == "QUEUED" or (
            row.status == "RUNNING" and row.stage in {"load", "replay"}
        )
        if can_cancel and not row.cancel_requested:
            row.cancel_requested = True
            row.cancel_requested_at = datetime.now(UTC)
            row.cancel_requested_by = requested_by
            row.updated_at = datetime.now(UTC)
            await self._session.flush()
        return row

    async def claim_next(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> BacktestReplayJobRecord | None:
        """Claim một job bằng SKIP LOCKED; transaction phải commit trước runtime."""
        if now.tzinfo is None or now.utcoffset() is None or lease_duration <= timedelta(0):
            raise ValueError("Thời điểm hoặc thời hạn lease không hợp lệ.")
        row = await self._session.scalar(
            select(BacktestReplayJobRecord)
            .where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                or_(
                    BacktestReplayJobRecord.status == "QUEUED",
                    and_(
                        BacktestReplayJobRecord.status == "RUNNING",
                        BacktestReplayJobRecord.lease_until <= now,
                    ),
                ),
            )
            .order_by(BacktestReplayJobRecord.created_at, BacktestReplayJobRecord.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        if row.cancel_requested:
            row.status = "CANCELLED"
            row.stage = "cancelled"
            row.progress = 100
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = now
            await self._session.flush()
            return row
        if row.attempt_count >= _MAX_ATTEMPTS:
            row.status = "FAILED"
            row.stage = "failed"
            row.progress = 100
            row.error_code = "WORKER_ATTEMPTS_EXHAUSTED"
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = now
            await self._session.flush()
            return row
        row.status = "RUNNING"
        row.stage = "load"
        row.progress = max(row.progress, 5)
        row.attempt_count += 1
        row.lease_generation += 1
        row.lease_owner = worker_id
        row.lease_until = now + lease_duration
        row.updated_at = now
        await self._session.flush()
        return row

    async def refresh_lease(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> bool:
        """Gia hạn duy nhất lease hiện hành; worker cũ bị fence khi claim lại."""
        result = await self._session.execute(
            update(BacktestReplayJobRecord)
            .where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                BacktestReplayJobRecord.id == job_id,
                BacktestReplayJobRecord.status == "RUNNING",
                BacktestReplayJobRecord.lease_owner == worker_id,
                BacktestReplayJobRecord.lease_generation == generation,
                BacktestReplayJobRecord.lease_until > now,
            )
            .values(lease_until=now + lease_duration, updated_at=now)
        )
        rowcount: int | None = cast(Any, result).rowcount
        return rowcount == 1

    async def update_progress(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        stage: str,
        progress: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> bool:
        """Cập nhật tiến độ đơn điệu khi token lease còn hiện hành."""
        if stage not in {"load", "replay", "persist", "complete", "failed", "cancelled"}:
            raise ValueError("Giai đoạn job không hợp lệ.")
        if type(progress) is not int or not 0 <= progress <= 100:
            raise ValueError("Tiến độ phải là số nguyên trong [0, 100].")
        statement = update(BacktestReplayJobRecord).where(
            BacktestReplayJobRecord.workspace_id == workspace_id,
            BacktestReplayJobRecord.id == job_id,
            BacktestReplayJobRecord.status == "RUNNING",
            BacktestReplayJobRecord.lease_owner == worker_id,
            BacktestReplayJobRecord.lease_generation == generation,
            BacktestReplayJobRecord.lease_until > now,
            BacktestReplayJobRecord.progress <= progress,
        )
        if stage == "persist":
            statement = statement.where(BacktestReplayJobRecord.cancel_requested.is_(False))
        result = await self._session.execute(
            statement.values(
                stage=stage,
                progress=progress,
                lease_until=now + lease_duration,
                updated_at=now,
            )
        )
        rowcount: int | None = cast(Any, result).rowcount
        return rowcount == 1

    async def cancel_requested(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        now: datetime,
    ) -> bool:
        """Đọc cờ hủy chỉ khi caller vẫn giữ generation lease hiện hành."""
        row = await self._session.scalar(
            select(BacktestReplayJobRecord).where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                BacktestReplayJobRecord.id == job_id,
                BacktestReplayJobRecord.status == "RUNNING",
                BacktestReplayJobRecord.lease_owner == worker_id,
                BacktestReplayJobRecord.lease_generation == generation,
                BacktestReplayJobRecord.lease_until > now,
            )
        )
        return True if row is None else row.cancel_requested

    async def finish(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        status: str,
        stage: str,
        progress: int,
        now: datetime,
        result_run_ids: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> bool:
        """Ghi terminal state bằng lease hiện hành, không nhận kết quả worker cũ."""
        if status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError("Trạng thái kết thúc job không hợp lệ.")
        if (status, stage, progress) not in {
            ("SUCCEEDED", "complete", 100),
            ("FAILED", "failed", 100),
            ("CANCELLED", "cancelled", 100),
        }:
            raise ValueError("Trạng thái cuối và giai đoạn không khớp.")
        result = await self._session.execute(
            update(BacktestReplayJobRecord)
            .where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                BacktestReplayJobRecord.id == job_id,
                BacktestReplayJobRecord.status == "RUNNING",
                BacktestReplayJobRecord.lease_owner == worker_id,
                BacktestReplayJobRecord.lease_generation == generation,
                BacktestReplayJobRecord.lease_until > now,
            )
            .values(
                status=status,
                stage=stage,
                progress=progress,
                result_run_ids=list(result_run_ids),
                error_code=error_code,
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )
        rowcount: int | None = cast(Any, result).rowcount
        return rowcount == 1
