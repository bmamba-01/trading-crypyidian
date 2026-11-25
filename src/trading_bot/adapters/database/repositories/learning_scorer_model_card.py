"""Đọc latency scorer qua aggregate gateway, không truy cập ledger thô."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.domain.learning.scorer_model_card import (
    ScorerActionLatencySummary,
    scorer_action_latency_summary_from_rows,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOW_CODES = frozenset({"24h", "7d", "30d"})


class LearningScorerActionLatencyUnavailable(ValueError):
    """Không thể xác minh quyền hoặc đọc aggregate PAPER latency an toàn."""


class LearningScorerModelCardRepository:
    """Gọi duy nhất SQL gateway aggregate-only cho latency scorer PAPER."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_action_latency(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        report_id: UUID,
        artifact_hash: str,
        report_hash: str,
        base_artifact_hash: str,
        policy_hash: str,
        window_code: str,
    ) -> ScorerActionLatencySummary:
        """Đọc snapshot cho lineage đã kiểm từ report hiện hành."""
        if any(type(value) is not UUID for value in (workspace_id, bot_id, report_id)):
            raise ValueError("Workspace, bot và report phải là UUID.")
        if any(
            type(value) is not str or _SHA256.fullmatch(value) is None
            for value in (artifact_hash, report_hash, base_artifact_hash, policy_hash)
        ):
            raise ValueError("Hash lineage phải là SHA-256 dạng hex.")
        if type(window_code) is not str or window_code not in _WINDOW_CODES:
            raise ValueError("Mã cửa sổ latency không hợp lệ.")

        statement = text(
            "SELECT * FROM public.wp06_aggregate_scorer_paper_action_latency("
            "CAST(:workspace_id AS uuid), CAST(:bot_id AS uuid), "
            "CAST(:report_id AS uuid), :artifact_hash, :report_hash, "
            ":base_artifact_hash, :policy_hash, :window_code)"
        )
        try:
            result = await self._session.execute(
                statement,
                {
                    "workspace_id": str(workspace_id),
                    "bot_id": str(bot_id),
                    "report_id": str(report_id),
                    "artifact_hash": artifact_hash,
                    "report_hash": report_hash,
                    "base_artifact_hash": base_artifact_hash,
                    "policy_hash": policy_hash,
                    "window_code": window_code,
                },
            )
            rows = result.mappings().all()
        except SQLAlchemyError as error:
            raise LearningScorerActionLatencyUnavailable(
                "Chưa thể xác minh quyền hiện hành hoặc đọc latency PAPER an toàn."
            ) from error

        try:
            normalized_rows = tuple(_strict_mapping(row) for row in rows)
            return scorer_action_latency_summary_from_rows(
                bot_id=bot_id,
                window_code=window_code,
                rows=normalized_rows,
            )
        except ValueError as error:
            raise LearningScorerActionLatencyUnavailable(
                "Projection latency PAPER không đúng hợp đồng aggregate."
            ) from error


def _strict_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Mỗi dòng latency phải là mapping aggregate.")
    return {str(key): _strict_value(item) for key, item in value.items()}


def _strict_value(value: Any) -> object:
    if value is None or type(value) in {str, int}:
        return value
    from datetime import datetime

    if isinstance(value, datetime):
        return value
    raise ValueError("Projection latency chứa kiểu dữ liệu không được phép.")


__all__ = [
    "LearningScorerActionLatencyUnavailable",
    "LearningScorerModelCardRepository",
]
