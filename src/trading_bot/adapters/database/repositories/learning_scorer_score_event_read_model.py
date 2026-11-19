"""Truy vấn score event theo workspace và chiếu đúng trường hiển thị an toàn."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_score_event_entities import (
    LearningScoreEventRecord,
)
from trading_bot.adapters.database.models.learning_scorer_paper_entry_block_entities import (
    LearningPaperEntryBlockRecord,
)
from trading_bot.domain.learning.scorer_score_event_summary import (
    ScorerEventDecision,
    ScorerEventMode,
    ScorerPaperEntryBlockAuditSummary,
    ScorerScoreEventPage,
    ScorerScoreEventSummary,
)

_DISPLAY_COLUMNS = (
    LearningScoreEventRecord.id,
    LearningScoreEventRecord.created_at,
    LearningScoreEventRecord.mode,
    LearningScoreEventRecord.decision,
    LearningScoreEventRecord.reason_code,
    LearningScoreEventRecord.probability,
    LearningScoreEventRecord.confidence,
    LearningScoreEventRecord.uncertainty,
    LearningScoreEventRecord.bot_id,
    LearningScoreEventRecord.bot_decision_id,
    LearningPaperEntryBlockRecord.actor_user_id.label("operator_block_actor_user_id"),
    LearningPaperEntryBlockRecord.mfa_verified_at.label("operator_block_mfa_verified_at"),
    LearningPaperEntryBlockRecord.reason.label("operator_block_reason"),
    LearningPaperEntryBlockRecord.created_at.label("operator_block_created_at"),
)


class LearningScorerScoreEventReadRepository:
    """Đọc trang metadata scorer; không lấy canonical hoặc event payload."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_page(
        self,
        *,
        workspace_id: UUID,
        page: int,
        page_size: int = 20,
    ) -> ScorerScoreEventPage:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if type(page) is not int or page < 1:
            raise ValueError("page phải là số nguyên dương.")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("page_size phải nằm trong [1, 100].")

        statement = (
            select(*_DISPLAY_COLUMNS)
            .outerjoin(
                LearningPaperEntryBlockRecord,
                (
                    LearningPaperEntryBlockRecord.workspace_id
                    == LearningScoreEventRecord.workspace_id
                )
                & (LearningPaperEntryBlockRecord.score_event_id == LearningScoreEventRecord.id)
                & (
                    LearningPaperEntryBlockRecord.bot_decision_id
                    == LearningScoreEventRecord.bot_decision_id
                ),
            )
            .where(LearningScoreEventRecord.workspace_id == workspace_id)
            .order_by(
                LearningScoreEventRecord.created_at.desc(),
                LearningScoreEventRecord.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        )
        result = await self._session.execute(statement)
        rows = result.mappings().all()
        has_next = len(rows) > page_size
        visible_rows = rows[:page_size]
        items = tuple(_summary_from_row(row) for row in visible_rows)
        return ScorerScoreEventPage(items=items, page=page, has_next=has_next)

    async def get_blockable_decision_id(
        self, *, workspace_id: UUID, score_event_id: UUID
    ) -> UUID | None:
        """Lấy định danh ứng viên PAPER_GATE APPROVE trong đúng workspace."""
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID hợp lệ.")
        if type(score_event_id) is not UUID or score_event_id.int == 0:
            raise ValueError("score_event_id phải là UUID hợp lệ.")
        statement = select(LearningScoreEventRecord.bot_decision_id).where(
            LearningScoreEventRecord.workspace_id == workspace_id,
            LearningScoreEventRecord.id == score_event_id,
            LearningScoreEventRecord.mode == "PAPER_GATE",
            LearningScoreEventRecord.decision == "APPROVE",
            LearningScoreEventRecord.bot_id.is_not(None),
            LearningScoreEventRecord.bot_decision_id.is_not(None),
        )
        result = await self._session.execute(statement)
        value = result.scalar_one_or_none()
        return _strict_uuid(value, "bot_decision_id") if value is not None else None


def _summary_from_row(row: object) -> ScorerScoreEventSummary:
    if not isinstance(row, Mapping):
        raise ValueError("Projection score event không hợp lệ.")
    values = cast(Mapping[str, object], row)
    mode = _strict_text(values["mode"], "mode")
    if mode not in {"SHADOW", "PAPER_GATE"}:
        raise ValueError("mode score event không hợp lệ.")
    decision = _strict_text(values["decision"], "decision")
    if decision not in {"APPROVE", "VETO", "ABSTAIN"}:
        raise ValueError("decision score event không hợp lệ.")
    return ScorerScoreEventSummary(
        event_id=_strict_uuid(values["id"], "id"),
        created_at=_strict_datetime(values["created_at"]),
        mode=cast(ScorerEventMode, mode),
        decision=cast(ScorerEventDecision, decision),
        reason_code=_optional_text(values["reason_code"], "reason_code"),
        probability=_optional_score(values["probability"], "probability"),
        confidence=_optional_score(values["confidence"], "confidence"),
        uncertainty=_optional_score(values["uncertainty"], "uncertainty"),
        bot_id=_optional_uuid(values["bot_id"], "bot_id"),
        bot_decision_id=_optional_uuid(values["bot_decision_id"], "bot_decision_id"),
        paper_entry_block=_paper_entry_block_from_row(values),
    )


def _paper_entry_block_from_row(
    row: Mapping[str, object],
) -> ScorerPaperEntryBlockAuditSummary | None:
    values = (
        row["operator_block_actor_user_id"],
        row["operator_block_mfa_verified_at"],
        row["operator_block_reason"],
        row["operator_block_created_at"],
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("Audit chặn PAPER lưu thiếu trường.")
    return ScorerPaperEntryBlockAuditSummary(
        actor_user_id=_strict_uuid(values[0], "operator_block_actor_user_id"),
        mfa_verified_at=_strict_datetime(values[1]),
        reason=_strict_text(values[2], "operator_block_reason"),
        created_at=_strict_datetime(values[3]),
    )


def _strict_uuid(value: object, name: str) -> UUID:
    if type(value) is not UUID:
        raise ValueError(f"{name} score event phải là UUID.")
    return value


def _optional_uuid(value: object, name: str) -> UUID | None:
    if value is None:
        return None
    return _strict_uuid(value, name)


def _strict_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("created_at score event phải có múi giờ.")
    return value


def _strict_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} score event phải là chuỗi không rỗng.")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _strict_text(value, name)


def _optional_score(value: object, name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"{name} score event phải nằm trong [0, 1].")
    return float(value)


__all__ = ["LearningScorerScoreEventReadRepository"]
