"""Tiêu thụ rights outbox và ghi eligibility invalidation bất biến."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.dataset_manifest_eligibility import (
    DatasetManifestEligibilityRepository,
)
from trading_bot.application.outbox import OutboxRepository

RIGHTS_EVENT_TYPE = "research.rights_changed.v1"
RIGHTS_CONSUMER_NAME = "wp06.dataset-rights-eligibility.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_POLICY_STATES = {"discovered", "rights_pending", "verified", "blocked", "revoked", "expired"}


@dataclass(frozen=True, slots=True)
class RightsChangeEvent:
    """Rights event đã được kiểm schema, tenant, aggregate và thời điểm."""

    workspace_id: UUID
    event_id: UUID
    aggregate_type: str
    occurred_at: datetime
    source_id: str
    change_type: str
    item_id: str | None = None
    revision_id: str | None = None
    policy_sequence: int | None = None
    policy_state: str | None = None

    @classmethod
    def from_outbox(cls, event: Any) -> RightsChangeEvent:
        """Đọc một trong các shape được migration WP05G.2/G.3 phát ra."""
        if (
            getattr(event, "event_type", None) != RIGHTS_EVENT_TYPE
            or getattr(event, "schema_version", None) != 1
        ):
            raise ValueError("rights event type/version không được hỗ trợ")
        workspace_id = getattr(event, "workspace_id", None)
        event_id = getattr(event, "id", None)
        occurred_at = getattr(event, "occurred_at", None)
        aggregate_type = getattr(event, "aggregate_type", None)
        payload = getattr(event, "payload", None)
        if (
            type(workspace_id) is not UUID
            or type(event_id) is not UUID
            or not isinstance(occurred_at, datetime)
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
            or not isinstance(payload, dict)
        ):
            raise ValueError("rights event envelope không hợp lệ")
        try:
            payload_workspace = UUID(payload["workspace_id"])
        except KeyError, TypeError, ValueError:
            raise ValueError("rights event workspace không hợp lệ") from None
        if payload_workspace != workspace_id:
            raise ValueError("rights event workspace không khớp aggregate")
        source_id = payload.get("source_id")
        change_type = payload.get("change_type")
        if not isinstance(source_id, str) or not _IDENTIFIER.fullmatch(source_id):
            raise ValueError("rights event source_id không hợp lệ")

        if aggregate_type == "source_policy_revision" and change_type in {
            "policy_revision",
            "policy_expired",
        }:
            policy_sequence = payload.get("policy_sequence")
            policy_state = payload.get("policy_state")
            if type(policy_sequence) is not int or policy_sequence < 1:
                raise ValueError("rights event policy_sequence không hợp lệ")
            if policy_state not in _POLICY_STATES:
                raise ValueError("rights event policy_state không hợp lệ")
            if change_type == "policy_expired" and policy_state != "verified":
                raise ValueError("rights expiry event phải tham chiếu policy đã verified")
            if not _valid_reason_code(payload.get("reason_code")):
                raise ValueError("rights event reason_code không hợp lệ")
            return cls(
                workspace_id,
                event_id,
                aggregate_type,
                occurred_at,
                source_id,
                change_type,
                policy_sequence=policy_sequence,
                policy_state=policy_state,
            )

        if aggregate_type == "source_item_revision" and change_type in {
            "item_revision",
            "item_tombstoned",
        }:
            item_id = payload.get("item_id")
            revision_id = payload.get("revision_id")
            item_sequence = payload.get("item_sequence")
            if not _valid_identifier(item_id) or not _valid_identifier(revision_id):
                raise ValueError("rights event item/revision không hợp lệ")
            if type(item_sequence) is not int or item_sequence < 1:
                raise ValueError("rights event item_sequence không hợp lệ")
            return cls(
                workspace_id,
                event_id,
                aggregate_type,
                occurred_at,
                source_id,
                change_type,
                item_id=item_id,
                revision_id=revision_id,
            )

        if aggregate_type == "source_item_takedown" and change_type == "item_takedown":
            item_id = payload.get("item_id")
            if not _valid_identifier(item_id) or not _valid_reason_code(payload.get("reason_code")):
                raise ValueError("rights takedown event không hợp lệ")
            return cls(
                workspace_id,
                event_id,
                aggregate_type,
                occurred_at,
                source_id,
                change_type,
                item_id=item_id,
            )
        raise ValueError("rights event aggregate/change_type không khớp contract")

    @property
    def invalidation_reason_code(self) -> str:
        """Mã lý do nội bộ; không sao chép free text từ payload nguồn."""
        return {
            "policy_revision": "source_policy_changed",
            "policy_expired": "source_policy_expired",
            "item_revision": "source_item_superseded",
            "item_tombstoned": "source_item_tombstoned",
            "item_takedown": "source_item_takedown",
        }[self.change_type]


@dataclass(frozen=True, slots=True)
class DatasetRightsChangeBatchResult:
    """Kết quả một lượt xử lý, không gồm payload nhạy cảm của outbox."""

    claimed: int
    processed: int
    invalidated_references: int
    failed: int
    dead_lettered: int


class DatasetRightsChangeConsumer:
    """Ghi invalidation và receipt cùng transaction của caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._outbox = OutboxRepository(session)
        self._eligibility = DatasetManifestEligibilityRepository(session)

    async def process_batch(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        batch_size: int = 20,
        max_attempts: int = 8,
    ) -> DatasetRightsChangeBatchResult:
        """Claim, append invalidations và ghi consumer receipt trong cùng transaction."""
        _validate_batch(workspace_id, worker_id, now, batch_size, max_attempts)
        events = await self._outbox.claim_ready(
            workspace_id=workspace_id,
            worker_id=worker_id,
            now=now,
            lease_duration=timedelta(minutes=5),
            max_attempts=max_attempts,
            limit=batch_size,
            event_types=(RIGHTS_EVENT_TYPE,),
        )
        processed = invalidated = failed = dead_lettered = 0
        for event in events:
            event_id = event.id
            try:
                async with self._session.begin_nested():
                    change = RightsChangeEvent.from_outbox(event)
                    if change.workspace_id != workspace_id:
                        raise ValueError("rights event workspace scope không khớp consumer")
                    event_invalidated = await self._eligibility.append_for_rights_change(
                        workspace_id=change.workspace_id,
                        event_id=change.event_id,
                        occurred_at=change.occurred_at,
                        source_id=change.source_id,
                        item_id=change.item_id,
                        change_type=change.change_type,
                        policy_sequence=change.policy_sequence,
                        reason_code=change.invalidation_reason_code,
                    )
                    await self._outbox.acknowledge(
                        workspace_id=workspace_id,
                        event_id=change.event_id,
                        worker_id=worker_id,
                        consumer_name=RIGHTS_CONSUMER_NAME,
                        now=now,
                    )
                processed += 1
                invalidated += event_invalidated
            except Exception as error:
                failed += 1
                status = await self._outbox.record_failure(
                    workspace_id=workspace_id,
                    event_id=event_id,
                    worker_id=worker_id,
                    now=now,
                    error=error,
                    max_attempts=max_attempts,
                )
                dead_lettered += status == "DEAD"
        return DatasetRightsChangeBatchResult(
            claimed=len(events),
            processed=processed,
            invalidated_references=invalidated,
            failed=failed,
            dead_lettered=dead_lettered,
        )


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _valid_reason_code(value: object) -> bool:
    return isinstance(value, str) and _REASON_CODE.fullmatch(value) is not None


def _validate_batch(
    workspace_id: UUID,
    worker_id: str,
    now: datetime,
    batch_size: int,
    max_attempts: int,
) -> None:
    if (
        type(workspace_id) is not UUID
        or not isinstance(worker_id, str)
        or not worker_id.strip()
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or type(batch_size) is not int
        or not 1 <= batch_size <= 100
        or type(max_attempts) is not int
        or max_attempts < 1
    ):
        raise ValueError("Thông số rights consumer không hợp lệ")
