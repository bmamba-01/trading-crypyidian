"""Incident activity ngoại lai (MB05.3, ruling 13): ghi có fence, resolution OPEN -> RESOLVED.

Repository không bao giờ ghi ledger/order/position cho state ngoại lai; JSONB chỉ chứa client
order id/symbol/side/quantity/status (không secret). Incident OPEN duy nhất theo fingerprint
bot-scoped; resolution chỉ dành cho admin-web (grant cột + RLS owner/risk_admin).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import AccountActivityIncidentRecord
from trading_bot.adapters.database.models.bot_runtime_entities import INCIDENT_RESOLUTION_CODES
from trading_bot.adapters.database.repositories.bot_runtime_leases import (
    RuntimeFenceGuard,
    runtime_db_reason,
)
from trading_bot.domain.execution.runtime_control import RuntimeControlError, RuntimeFence
from trading_bot.ports.account_activity import (
    ACTIVITY_SOURCES,
    ActivityClassification,
    ActivityVerdict,
)


class AccountActivityIncidentRepository:
    """Ghi/đọc incident trong scope RLS bot của transaction hiện tại; không tự commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_open(
        self, *, workspace_id: UUID, bot_id: UUID
    ) -> list[AccountActivityIncidentRecord]:
        rows = await self._session.scalars(
            select(AccountActivityIncidentRecord)
            .where(
                AccountActivityIncidentRecord.workspace_id == workspace_id,
                AccountActivityIncidentRecord.bot_id == bot_id,
                AccountActivityIncidentRecord.status == "OPEN",
            )
            .order_by(AccountActivityIncidentRecord.detected_at)
        )
        return list(rows.all())

    async def open_incident(
        self,
        *,
        fence: RuntimeFence,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        classification: ActivityClassification,
        source: str,
        cursor: str | None,
    ) -> tuple[AccountActivityIncidentRecord, bool]:
        """Mở incident cho state ngoại lai; cùng fingerprint còn OPEN thì trả incident cũ."""
        if (
            classification.verdict is not ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED
            or classification.fingerprint is None
            or classification.snapshot_hash is None
        ):
            raise RuntimeControlError("INCIDENT_CLASSIFICATION_INVALID")
        if source not in ACTIVITY_SOURCES:
            raise RuntimeControlError("INCIDENT_SOURCE_INVALID")
        await RuntimeFenceGuard(self._session).require(
            workspace_id=workspace_id, bot_id=bot_id, account_id=account_id, fence=fence
        )
        existing = await self._session.scalar(
            select(AccountActivityIncidentRecord).where(
                AccountActivityIncidentRecord.workspace_id == workspace_id,
                AccountActivityIncidentRecord.bot_id == bot_id,
                AccountActivityIncidentRecord.fingerprint == classification.fingerprint,
                AccountActivityIncidentRecord.status == "OPEN",
            )
        )
        if existing is not None:
            return existing, False
        record = AccountActivityIncidentRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            status="OPEN",
            source=source,
            snapshot_hash=classification.snapshot_hash,
            cursor=cursor,
            foreign_orders=[order.canonical() for order in classification.foreign_orders],
            foreign_positions=[
                position.canonical() for position in classification.foreign_positions
            ],
            fingerprint=classification.fingerprint,
            fencing_token=int(fence.fencing_token),
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise RuntimeControlError(runtime_db_reason(error, "INCIDENT_ALREADY_OPEN")) from None
        except DBAPIError as error:
            raise RuntimeControlError(runtime_db_reason(error, "INCIDENT_WRITE_REJECTED")) from None
        return record, True

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        incident_id: UUID,
        resolution_code: str,
        reason: str,
        resolved_by: UUID,
        resolved_at: datetime,
    ) -> AccountActivityIncidentRecord:
        """Resolution tường minh có actor/lý do; trigger DB đặt `resolved_at` theo đồng hồ DB.

        Quyền (owner/risk_admin + MFA gần đây) do application service MB05.7 kiểm trước.
        """
        if resolution_code not in INCIDENT_RESOLUTION_CODES:
            raise RuntimeControlError("INCIDENT_RESOLUTION_CODE_INVALID")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise RuntimeControlError("INCIDENT_RESOLUTION_REASON_REQUIRED")
        record = await self._session.scalar(
            select(AccountActivityIncidentRecord)
            .where(
                AccountActivityIncidentRecord.workspace_id == workspace_id,
                AccountActivityIncidentRecord.bot_id == bot_id,
                AccountActivityIncidentRecord.id == incident_id,
            )
            .with_for_update()
        )
        if record is None:
            raise RuntimeControlError("INCIDENT_NOT_FOUND")
        if record.status != "OPEN":
            raise RuntimeControlError("INCIDENT_NOT_OPEN")
        record.status = "RESOLVED"
        record.resolution_code = resolution_code
        record.resolution_reason = reason
        record.resolved_by = resolved_by
        record.resolved_at = resolved_at
        try:
            await self._session.flush()
        except DBAPIError as error:
            raise RuntimeControlError(
                runtime_db_reason(error, "INCIDENT_RESOLUTION_REJECTED")
            ) from None
        return record
