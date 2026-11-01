"""Điều phối chấm điểm AI qua cổng model giới hạn, không có quyền execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter_ns
from typing import Protocol

from trading_bot.domain.learning.scoring import (
    ModelInvocationStatus,
    ScoreReason,
    ScoreRequest,
    ScoreResult,
    ScoringPolicy,
    score_candidate,
)


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    """Phản hồi thuần từ model adapter; không chứa lệnh hoặc quyền thực thi."""

    status: ModelInvocationStatus
    response: object


class ScoreModelPort(Protocol):
    """Cổng gọi model chỉ nhận request/context và policy đã đóng băng."""

    # Adapter phải đặt hard timeout ở transport và dọn kết nối khi coroutine bị hủy.
    # Task supervision giới hạn thời gian chờ của service nhưng không thu hồi được
    # tài nguyên mà adapter không hợp tác giữ lại.
    async def invoke(self, request: ScoreRequest, policy: ScoringPolicy) -> ModelInvocation: ...


class LearningScoringService:
    """Gọi model trong hạn mức policy và chuyển mọi lỗi thành kết quả domain fail-closed."""

    def __init__(self, model_port: ScoreModelPort) -> None:
        self._model_port = model_port

    async def score(self, *, request: ScoreRequest, policy: ScoringPolicy) -> ScoreResult:
        """Chấm snapshot/context do caller cung cấp theo policy cụ thể.

        Service không tự tải feature, evidence hoặc policy và không cung cấp API
        đặt lệnh. Model adapter phải là coroutine không chặn event loop, có hard
        timeout ở transport và hoàn tất cleanup khi nhận cancellation.
        """
        preflight = self._resolve(
            request,
            policy,
            ModelInvocationStatus.UNAVAILABLE,
            None,
            timedelta(0),
        )
        if preflight.reason_codes != (ScoreReason.MODEL_UNAVAILABLE,):
            return preflight

        started_at = perf_counter_ns()
        try:
            invocation_task = asyncio.create_task(self._model_port.invoke(request, policy))
        except Exception:
            return self._resolve(
                request,
                policy,
                ModelInvocationStatus.FAILED,
                None,
                self._elapsed(started_at),
            )

        try:
            done, _ = await asyncio.wait(
                {invocation_task},
                timeout=policy.max_model_latency.total_seconds(),
            )
        except asyncio.CancelledError:
            invocation_task.cancel()
            invocation_task.add_done_callback(_consume_task_outcome)
            raise

        latency = self._elapsed(started_at)
        if not done:
            invocation_task.cancel()
            invocation_task.add_done_callback(_consume_task_outcome)
            return self._resolve(
                request,
                policy,
                ModelInvocationStatus.TIMEOUT,
                None,
                latency,
            )

        try:
            invocation = invocation_task.result()
        except TimeoutError:
            return self._resolve(
                request,
                policy,
                ModelInvocationStatus.TIMEOUT,
                None,
                latency,
            )
        except Exception:
            return self._resolve(
                request,
                policy,
                ModelInvocationStatus.FAILED,
                None,
                latency,
            )

        if (
            not isinstance(invocation, ModelInvocation)
            or type(invocation.status) is not ModelInvocationStatus
        ):
            return self._resolve(
                request,
                policy,
                ModelInvocationStatus.FAILED,
                None,
                latency,
            )
        return self._resolve(request, policy, invocation.status, invocation.response, latency)

    @staticmethod
    def _resolve(
        request: ScoreRequest,
        policy: ScoringPolicy,
        status: ModelInvocationStatus,
        response: object,
        latency: timedelta,
    ) -> ScoreResult:
        try:
            return score_candidate(
                request,
                policy,
                invocation_status=status,
                model_response=response,
                model_latency=latency,
            )
        except Exception:
            # Contract đã được caller dựng; lỗi bất ngờ khi xử lý phản hồi model
            # được quy về reason ổn định thay vì làm rò lỗi hoặc duyệt candidate.
            return score_candidate(
                request,
                policy,
                invocation_status=ModelInvocationStatus.FAILED,
                model_response=None,
                model_latency=latency,
            )

    @staticmethod
    def _elapsed(started_at: int) -> timedelta:
        return timedelta(microseconds=max(0, perf_counter_ns() - started_at) / 1_000)


def _consume_task_outcome(task: asyncio.Task[object]) -> None:
    """Đọc kết quả muộn để tránh lỗi task không được quan sát sau timeout."""
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
