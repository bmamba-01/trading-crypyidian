"""Worker định kỳ chấm kết quả tín hiệu (CRY-430), chạy cạnh producer trong market-signal-worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from trading_bot.application.signal_outcomes import SignalOutcomeTickResult

logger = logging.getLogger(__name__)


class SignalOutcomeScorerPort(Protocol):
    async def score_once(self, *, workspace_id: UUID, now: datetime) -> SignalOutcomeTickResult: ...


class SignalOutcomeWorker:
    """Gọi scorer theo chu kỳ; lỗi một lượt chỉ ghi mã an toàn rồi thử lại ở lượt sau."""

    def __init__(
        self,
        scorer: SignalOutcomeScorerPort,
        *,
        workspace_id: UUID,
        tick_seconds: float = 60.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not 1 <= tick_seconds <= 3600:
            raise ValueError("tick_seconds phải nằm trong khoảng 1–3600.")
        self._scorer = scorer
        self._workspace_id = workspace_id
        self._tick_seconds = tick_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self.last_tick_succeeded = False
        self.last_tick_at: datetime | None = None
        self.last_error_code: str | None = None
        self.last_result: SignalOutcomeTickResult | None = None

    async def tick(self) -> SignalOutcomeTickResult:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Thời điểm chấm phải có múi giờ.")
        result = await self._scorer.score_once(
            workspace_id=self._workspace_id, now=now.astimezone(UTC)
        )
        self.last_result = result
        return result

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                result = await self.tick()
                self.last_tick_succeeded = True
                self.last_tick_at = self._now()
                self.last_error_code = None
                if result.scored or result.data_gap or result.invalid:
                    logger.info(
                        "Signal outcome tick: scored=%s gap=%s invalid=%s pending=%s",
                        result.scored,
                        result.data_gap,
                        result.invalid,
                        result.pending,
                    )
            except Exception as error:
                self.last_tick_succeeded = False
                self.last_error_code = f"SIGNAL_OUTCOME_TICK_{type(error).__name__.upper()[:80]}"
                logger.warning("Signal outcome tick lỗi; code=%s", self.last_error_code)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue
