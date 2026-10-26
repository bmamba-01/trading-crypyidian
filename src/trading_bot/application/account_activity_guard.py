"""Guard hoạt động account và resolution incident (MB05.7, ruling 12–14, 30).

- `AccountActivityGuard.inspect_once(account_id)` (runtime, có fence): lấy snapshot qua
  `AccountActivityPort`, phân loại theo state đã biết của gateway bot. Ngoại lai → mở incident
  `EXTERNAL_ACTIVITY_UNRECONCILED` (cùng fingerprint không mở trùng) + state chặn entry + audit;
  adapter lỗi/snapshot không dùng được → `UNVERIFIED` (chặn entry, không incident). Guard không
  có đường ghi ledger, hủy order hay đóng vị thế ngoại lai; exits bảo vệ của vị thế đã biết không
  đi qua guard nên không bị gián đoạn.
- `AccountActivityResolutionService.resolve_incident` (admin-web): chỉ owner/risk_admin + MFA gần
  đây + lý do + mã trong tập cho phép; sau resolution entry vẫn chặn tới khi runtime inspect sạch.
  Resume bot là thao tác riêng (MB05.6).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.bot_runtime_entities import INCIDENT_RESOLUTION_CODES
from trading_bot.adapters.database.repositories.account_activity_incidents import (
    AccountActivityIncidentRepository,
)
from trading_bot.adapters.database.repositories.bot_runtime_states import (
    BotRuntimeStateRepository,
)
from trading_bot.application.bot_runtime_control import bot_is_visible
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.execution.runtime_control import (
    BotReadiness,
    RuntimeActivityStatus,
    RuntimeControlError,
)
from trading_bot.ports.account_activity import (
    AccountActivityError,
    AccountActivitySnapshot,
    ActivityClassification,
    ActivityVerdict,
    KnownAccountState,
    ObservedPosition,
    classify_account_activity,
)

EXTERNAL = "EXTERNAL_ACTIVITY_UNRECONCILED"
UNVERIFIED = "ACCOUNT_ACTIVITY_UNVERIFIED"
_RESOLVER_ROLES = frozenset({Role.OWNER, Role.RISK_ADMIN})


def known_state_from_gateway(
    workspace_id: UUID, account_id: UUID, gateway: Any
) -> KnownAccountState:
    """State có lineage của bot: client order id `paper:<idempotency_key>` + vị thế mở."""
    return KnownAccountState(
        workspace_id=workspace_id,
        account_id=account_id,
        known_client_order_ids=frozenset(str(order.intent_id) for order in gateway.open_orders()),
        open_positions=tuple(
            ObservedPosition(
                symbol=str(position.symbol),
                side=str(position.direction).upper(),
                quantity=position.quantity,
            )
            for position in gateway.open_positions()
        ),
    )


class AccountActivityGuard:
    """Kiểm activity account của bot đang giữ lease; ghi incident/state có fence."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession] | None,
        workspace_id: UUID,
        accounts: Callable[[UUID], Any],
        adapter: Any,
        clock: Callable[[], datetime] | None = None,
        max_snapshot_age: timedelta = timedelta(minutes=2),
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        state_repository_factory: Callable[[Any], Any] = BotRuntimeStateRepository,
        incident_repository_factory: Callable[[Any], Any] = AccountActivityIncidentRepository,
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._accounts = accounts
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_snapshot_age = max_snapshot_age
        self._unit_of_work_factory = unit_of_work_factory
        self._state_repository_factory = state_repository_factory
        self._incident_repository_factory = incident_repository_factory

    async def inspect_once(self, account_id: UUID) -> ActivityClassification:
        account = self._accounts(account_id)
        if account is None:
            return ActivityClassification(
                ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED, "LEASE_NOT_HELD"
            )
        snapshot: AccountActivitySnapshot | None = None
        adapter_error: str | None = None
        try:
            snapshot = await self._adapter.inspect(account_id, None)
        except AccountActivityError as error:
            adapter_error = error.reason_code
        # Đọc đồng hồ SAU khi adapter chụp snapshot: snapshot vừa chụp không bị coi là tương lai.
        now = self._clock()
        known = known_state_from_gateway(self._workspace_id, account_id, account.gateway)
        if adapter_error is not None:
            classification = ActivityClassification(
                ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED, adapter_error
            )
        else:
            classification = classify_account_activity(
                snapshot, known, now=now, max_snapshot_age=self._max_snapshot_age
            )
        return await self._record(account, classification, snapshot, now)

    async def _record(
        self,
        account: Any,
        classification: ActivityClassification,
        snapshot: AccountActivitySnapshot | None,
        now: datetime,
    ) -> ActivityClassification:
        fence = account.fence
        async with self._unit_of_work_factory(
            self._sessions, workspace_id=self._workspace_id, bot_id=account.bot_id
        ) as uow:
            incidents = self._incident_repository_factory(uow.session)
            result = classification
            if classification.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED:
                incident, created = await incidents.open_incident(
                    fence=fence,
                    workspace_id=self._workspace_id,
                    bot_id=account.bot_id,
                    account_id=account.account_id,
                    classification=classification,
                    source=snapshot.source if snapshot is not None else "PAPER_LEDGER",
                    cursor=snapshot.cursor if snapshot is not None else None,
                )
                if created:
                    uow.record_audit(
                        action="bot.activity.external_detected",
                        target_type="account_activity_incident",
                        target_id=incident.id,
                        reason=EXTERNAL,
                        after_state={"fingerprint": classification.fingerprint},
                    )
                reason: str | None = EXTERNAL
                status = RuntimeActivityStatus.VERIFIED
            elif classification.verdict is ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED:
                still_open = await incidents.list_open(
                    workspace_id=self._workspace_id, bot_id=account.bot_id
                )
                reason = EXTERNAL if still_open else UNVERIFIED
                status = RuntimeActivityStatus.UNVERIFIED
            else:
                still_open = await incidents.list_open(
                    workspace_id=self._workspace_id, bot_id=account.bot_id
                )
                status = RuntimeActivityStatus.VERIFIED
                if still_open:
                    # Incident chỉ đóng qua resolution tường minh; snapshot sạch chưa đủ.
                    reason = EXTERNAL
                    result = ActivityClassification(
                        ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED,
                        EXTERNAL,
                        snapshot_hash=classification.snapshot_hash,
                    )
                else:
                    reason = None
            await self._state_repository_factory(uow.session).upsert(
                fence=fence,
                workspace_id=self._workspace_id,
                bot_id=account.bot_id,
                account_id=account.account_id,
                readiness=BotReadiness.READY,
                reason_code=reason,
                heartbeat_at=now,
                activity_cursor=snapshot.cursor if snapshot is not None else None,
                activity_checked_at=now,
                activity_status=status,
            )
        return result


class ActivityResolutionError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class AccountActivityResolutionService:
    """Resolution incident activity ở admin-web (grant cột + RLS owner/risk_admin ở DB)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        *,
        clock: Callable[[], datetime] | None = None,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        incident_repository_factory: Callable[[Any], Any] = AccountActivityIncidentRepository,
        bot_visibility: Callable[..., Awaitable[bool]] = bot_is_visible,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._unit_of_work_factory = unit_of_work_factory
        self._incident_repository_factory = incident_repository_factory
        self._bot_visibility = bot_visibility

    async def resolve_incident(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        incident_id: UUID,
        resolution_code: str,
        reason: str,
    ) -> Any:
        correlation_id = uuid4()
        try:
            async with self._uow(principal, bot_id, correlation_id) as uow:
                if not principal.roles & _RESOLVER_ROLES:
                    raise ActivityResolutionError("CONTROL_PERMISSION_DENIED")
                if not await self._bot_visibility(
                    uow.session, workspace_id=principal.workspace_id, bot_id=bot_id
                ):
                    raise ActivityResolutionError("BOT_NOT_FOUND")
                now = self._clock()
                if (
                    principal.mfa_verified_at > now
                    or now - principal.mfa_verified_at > RECENT_MFA_LIFETIME
                ):
                    raise ActivityResolutionError("RECENT_MFA_REQUIRED")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                    raise ActivityResolutionError("INCIDENT_RESOLUTION_REASON_REQUIRED")
                if resolution_code not in INCIDENT_RESOLUTION_CODES:
                    raise ActivityResolutionError("INCIDENT_RESOLUTION_CODE_INVALID")
                record = await self._incident_repository_factory(uow.session).resolve(
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    incident_id=incident_id,
                    resolution_code=resolution_code,
                    reason=reason,
                    resolved_by=principal.membership_id,
                    resolved_at=now,
                )
                uow.record_audit(
                    action="bot.activity.resolved",
                    target_type="account_activity_incident",
                    target_id=incident_id,
                    reason=reason,
                    after_state={"resolution_code": resolution_code},
                )
                return record
        except (ActivityResolutionError, RuntimeControlError) as error:
            async with self._uow(principal, bot_id, correlation_id) as uow:
                uow.record_audit(
                    action="bot.runtime.control_denied",
                    target_type="account_activity_incident",
                    target_id=incident_id,
                    reason=error.reason_code,
                )
            raise ActivityResolutionError(error.reason_code) from None

    def _uow(self, principal: Principal, bot_id: UUID, correlation_id: UUID) -> Any:
        return self._unit_of_work_factory(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            request_id=correlation_id,
        )
