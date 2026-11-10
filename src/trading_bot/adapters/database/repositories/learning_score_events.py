"""Repository append-only cho score event runtime metadata-only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_score_event_entities import (
    LearningScoreEventRecord,
)
from trading_bot.domain.learning.score_event import LearningScoreEvent

_FORBIDDEN_KEYS = frozenset(
    {"features", "feature_values", "labels", "sample_ids", "test_payload", "raw_source"}
)


class LearningScoreEventConflict(RuntimeError):
    """Score event đã dùng cùng key nhưng khác canonical metadata."""


@dataclass(frozen=True, slots=True)
class LearningScoreEventWriteResult:
    event: LearningScoreEvent
    is_duplicate: bool


class LearningScoreEventRepository:
    """Append/retry score event trong transaction workspace do caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: LearningScoreEvent) -> LearningScoreEventWriteResult:
        if type(event) is not LearningScoreEvent:
            raise ValueError("event phải là LearningScoreEvent.")
        existing = await self._find(event.workspace_id, event.idempotency_key)
        if existing is not None:
            self._assert_same(existing, event)
            return LearningScoreEventWriteResult(event, True)

        record = LearningScoreEventRecord.from_domain(event)
        self.validate_record(record)
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._find(event.workspace_id, event.idempotency_key)
            if existing is None:
                raise LearningScoreEventConflict(
                    "Không thể ghi score event do xung đột ràng buộc khác."
                ) from error
            self._assert_same(existing, event)
            return LearningScoreEventWriteResult(event, True)
        return LearningScoreEventWriteResult(event, False)

    async def _find(self, workspace_id: UUID, idempotency_key: str) -> RowMapping | None:
        result = await self._session.execute(
            select(
                LearningScoreEventRecord.workspace_id,
                LearningScoreEventRecord.bot_id,
                LearningScoreEventRecord.bot_decision_id,
                LearningScoreEventRecord.idempotency_key,
                LearningScoreEventRecord.event_hash,
                LearningScoreEventRecord.canonical_event,
            ).where(
                LearningScoreEventRecord.workspace_id == workspace_id,
                LearningScoreEventRecord.idempotency_key == idempotency_key,
            )
        )
        return result.mappings().one_or_none()

    @staticmethod
    def _assert_same(record: RowMapping, event: LearningScoreEvent) -> None:
        canonical_event = record["canonical_event"]
        event_hash = record["event_hash"]
        if (
            record["idempotency_key"] != event.idempotency_key
            or record["workspace_id"] != event.workspace_id
            or record["bot_id"] != event.bot_id
            or record["bot_decision_id"] != event.bot_decision_id
            or event_hash != event.event_hash
            or canonical_event != event.canonical_json
            or not isinstance(canonical_event, str)
            or not isinstance(event_hash, str)
            or hashlib.sha256(canonical_event.encode("utf-8")).hexdigest() != event_hash
        ):
            raise LearningScoreEventConflict(
                "Score event idempotency key đã được dùng với metadata khác."
            )

    @staticmethod
    def validate_record(record: LearningScoreEventRecord) -> None:
        if type(record) is not LearningScoreEventRecord:
            raise LearningScoreEventConflict("Record score event không đúng kiểu.")
        try:
            payload = json.loads(record.canonical_event)
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except TypeError, ValueError, json.JSONDecodeError:
            raise LearningScoreEventConflict("Canonical score event không đọc được.") from None
        if (
            canonical != record.canonical_event
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != record.event_hash
            or payload != record.event_payload
            or payload.get("schema_version") != "crypidian.learning.score-event.v1"
            or _FORBIDDEN_KEYS.intersection(payload)
            or payload.get("idempotency_key") != record.idempotency_key
            or payload.get("workspace_id") != str(record.workspace_id)
            or payload.get("bot_id") != str(record.bot_id)
            or payload.get("bot_decision_id") != str(record.bot_decision_id)
            or payload.get("bot_decision_hash") != record.bot_decision_hash
            or payload.get("candidate_id") != record.candidate_id
            or payload.get("candidate_hash") != record.candidate_hash
            or payload.get("vector_hash") != record.vector_hash
            or payload.get("source_rights_fingerprint") != record.source_rights_fingerprint
            or payload.get("deployment_event_id") != str(record.deployment_event_id)
            or payload.get("scorer_artifact_id") != str(record.scorer_artifact_id)
            or payload.get("evaluation_report_id") != str(record.evaluation_report_id)
            or payload.get("proof_id") != str(record.proof_id)
            or payload.get("policy_hash") != record.policy_hash
        ):
            raise LearningScoreEventConflict(
                "Score event canonical bytes/hash/denormalized columns không nhất quán."
            )


__all__ = [
    "LearningScoreEventConflict",
    "LearningScoreEventRepository",
    "LearningScoreEventWriteResult",
]
