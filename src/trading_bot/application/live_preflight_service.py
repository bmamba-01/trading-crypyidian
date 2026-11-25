"""Application service điều phối preflight và đối soát trạng thái sàn chỉ đọc (OPS07F / Task 5).

Chỉ thực hiện đọc authoritative snapshot qua transport an toàn, không có bất kỳ
đường dẫn nào gọi endpoint tạo, sửa hay hủy lệnh trên sàn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LivePreflightVerdict,
    LocalReconciliationState,
    PreflightReasonCode,
    evaluate_live_preflight,
)

logger = logging.getLogger(__name__)


class ReadOnlyVenueGateway(Protocol):
    """Cổng giao tiếp sàn CHỈ ĐỌC: chỉ hỗ trợ lấy snapshot, không có hàm đặt/hủy lệnh."""

    async def fetch_authoritative_snapshot(self) -> LivePreflightSnapshot: ...


class LocalReconciliationStateProvider(Protocol):
    """Cung cấp trạng thái sổ cái và bảo vệ nội bộ của bot."""

    async def load_local_state(
        self, *, workspace_id: UUID, bot_id: UUID, account_id: UUID
    ) -> LocalReconciliationState: ...


class PreflightIncidentRecorder(Protocol):
    """Ghi nhận sự cố bất thường phát hiện được trong quá trình preflight."""

    async def record_incident(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        verdict: LivePreflightVerdict,
    ) -> None: ...


class LiveReadOnlyPreflightService:
    """Service thực hiện preflight và đối soát an toàn trước khi kích hoạt entry gate."""

    def __init__(
        self,
        *,
        venue_gateway: ReadOnlyVenueGateway,
        state_provider: LocalReconciliationStateProvider,
        incident_recorder: PreflightIncidentRecorder | None = None,
        clock: Callable[[], datetime] | None = None,
        max_snapshot_age: timedelta = timedelta(seconds=15),
        max_clock_drift: timedelta = timedelta(milliseconds=1000),
        max_retries: int = 3,
        retry_backoff_base_s: float = 0.5,
    ) -> None:
        self._venue_gateway = venue_gateway
        self._state_provider = state_provider
        self._incident_recorder = incident_recorder
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_snapshot_age = max_snapshot_age
        self._max_clock_drift = max_clock_drift
        self._max_retries = max(1, max_retries)
        self._retry_backoff_base_s = max(0.05, retry_backoff_base_s)

    async def run_preflight(
        self, *, workspace_id: UUID, bot_id: UUID, account_id: UUID
    ) -> LivePreflightVerdict:
        """Thực hiện chu trình preflight chỉ đọc với retry an toàn."""
        # 1. Tải trạng thái cục bộ
        local_state = await self._state_provider.load_local_state(
            workspace_id=workspace_id, bot_id=bot_id, account_id=account_id
        )

        # 2. Thu thập snapshot sàn qua retry chỉ đọc
        snapshot: LivePreflightSnapshot | None = None
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                snapshot = await self._venue_gateway.fetch_authoritative_snapshot()
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Lỗi khi tải snapshot sàn chỉ đọc (lần thử %d/%d): %s",
                    attempt,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries:
                    backoff = self._retry_backoff_base_s * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)

        # Nếu hoàn toàn không thể lấy được snapshot sau retry
        if snapshot is None:
            now = self._clock()
            verdict = LivePreflightVerdict(
                passed=False,
                reason_code=PreflightReasonCode.ENDPOINT_FAILURE,
                detail_vi=f"Không thể lấy authoritative snapshot từ sàn: {last_error}",
                evidence_hash="0" * 64,
                checked_at=now,
                hold_reservations=True,
                incidents=[{"kind": "ENDPOINT_FAILURE", "error": str(last_error)}],
            )
            if self._incident_recorder is not None:
                await self._incident_recorder.record_incident(
                    workspace_id=workspace_id,
                    bot_id=bot_id,
                    account_id=account_id,
                    verdict=verdict,
                )
            return verdict

        # 3. Đánh giá đối soát fail-closed
        verdict = evaluate_live_preflight(
            snapshot,
            local_state,
            now=self._clock(),
            max_snapshot_age=self._max_snapshot_age,
            max_clock_drift=self._max_clock_drift,
        )

        # 4. Ghi nhận incident nếu phát hiện vi phạm
        if not verdict.passed and self._incident_recorder is not None:
            await self._incident_recorder.record_incident(
                workspace_id=workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                verdict=verdict,
            )

        return verdict
