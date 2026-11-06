"""Repository opt-in và delivery ledger cho process Telegram signal-worker."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotDecisionRecord,
    FeatureSnapshotRecord,
    TelegramBinding,
    TelegramSignalDelivery,
    TelegramSignalSubscription,
    UserMembership,
)
from trading_bot.application.bot_dispatcher import BotDecision
from trading_bot.application.telegram_signals import TelegramSignalCandidate, TelegramSignalFilters


@dataclass(frozen=True, slots=True)
class SignalSubscriptionTarget:
    subscription_id: UUID
    bot_id: UUID
    chat_id: int
    filters: TelegramSignalFilters = field(default_factory=TelegramSignalFilters)


@dataclass(frozen=True, slots=True)
class SignalDeliveryClaim:
    delivery_id: UUID
    subscription_id: UUID
    bot_id: UUID
    decision_id: UUID
    attempts: int
    chat_id: int
    candidate: TelegramSignalCandidate
    filtered: bool = False


class TelegramSignalRepository:
    """Truy vấn/ghi chỉ trong UoW có workspace và bot scope."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_binding(
        self, *, workspace_id: UUID, membership_id: UUID
    ) -> TelegramBinding | None:
        return await self._session.scalar(
            select(TelegramBinding)
            .where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.membership_id == membership_id,
                TelegramBinding.verified_at.is_not(None),
                TelegramBinding.revoked_at.is_(None),
            )
            .with_for_update(read=True)
        )

    async def get_subscription(
        self, *, workspace_id: UUID, membership_id: UUID, bot_id: UUID
    ) -> TelegramSignalSubscription | None:
        return await self._session.scalar(
            select(TelegramSignalSubscription).where(
                TelegramSignalSubscription.workspace_id == workspace_id,
                TelegramSignalSubscription.membership_id == membership_id,
                TelegramSignalSubscription.bot_id == bot_id,
            )
        )

    async def put_subscription(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        bot_id: UUID,
        binding: TelegramBinding,
        enabled: bool,
        filters: TelegramSignalFilters | None = None,
    ) -> TelegramSignalSubscription:
        try:
            chat_id = int(binding.telegram_user_id)
        except (TypeError, ValueError) as error:
            raise ValueError("Telegram binding không có private chat ID hợp lệ.") from error
        if chat_id <= 0:
            raise ValueError("Telegram binding không có private chat ID hợp lệ.")
        row = await self.get_subscription(
            workspace_id=workspace_id, membership_id=membership_id, bot_id=bot_id
        )
        if row is None:
            row = TelegramSignalSubscription(
                id=uuid4(),
                workspace_id=workspace_id,
                membership_id=membership_id,
                bot_id=bot_id,
                telegram_binding_id=binding.id,
                telegram_chat_id=chat_id,
                enabled=enabled,
                **(filters.as_dict() if filters is not None else TelegramSignalFilters().as_dict()),
            )
            self._session.add(row)
        else:
            row.telegram_binding_id = binding.id
            row.telegram_chat_id = chat_id
            row.enabled = enabled
            if filters is not None:
                for name, values in filters.as_dict().items():
                    setattr(row, name, values)
        await self._session.flush()
        return row

    async def set_bot_subscription_enabled(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        bot_id: UUID,
        enabled: bool,
    ) -> bool:
        result = await self._session.execute(
            update(TelegramSignalSubscription)
            .where(
                TelegramSignalSubscription.workspace_id == workspace_id,
                TelegramSignalSubscription.membership_id == membership_id,
                TelegramSignalSubscription.bot_id == bot_id,
            )
            .values(enabled=enabled)
        )
        return bool(getattr(result, "rowcount", 0))

    async def set_membership_enabled(
        self, *, workspace_id: UUID, membership_id: UUID, enabled: bool
    ) -> int:
        result = await self._session.execute(
            update(TelegramSignalSubscription)
            .where(
                TelegramSignalSubscription.workspace_id == workspace_id,
                TelegramSignalSubscription.membership_id == membership_id,
            )
            .values(enabled=enabled)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def disable_membership(self, *, workspace_id: UUID, membership_id: UUID) -> int:
        return await self.set_membership_enabled(
            workspace_id=workspace_id, membership_id=membership_id, enabled=False
        )

    async def list_enabled_subscriptions(
        self, *, workspace_id: UUID
    ) -> tuple[SignalSubscriptionTarget, ...]:
        rows = await self._session.execute(
            select(
                TelegramSignalSubscription.id,
                TelegramSignalSubscription.bot_id,
                TelegramSignalSubscription.telegram_chat_id,
                TelegramSignalSubscription.symbols,
                TelegramSignalSubscription.timeframes,
                TelegramSignalSubscription.directions,
                TelegramSignalSubscription.strategy_ids,
                TelegramSignalSubscription.setup_codes,
            )
            .join(
                TelegramBinding,
                and_(
                    TelegramBinding.workspace_id == TelegramSignalSubscription.workspace_id,
                    TelegramBinding.id == TelegramSignalSubscription.telegram_binding_id,
                    TelegramBinding.membership_id == TelegramSignalSubscription.membership_id,
                ),
            )
            .join(
                UserMembership,
                and_(
                    UserMembership.workspace_id == TelegramSignalSubscription.workspace_id,
                    UserMembership.id == TelegramSignalSubscription.membership_id,
                ),
            )
            .where(
                TelegramSignalSubscription.workspace_id == workspace_id,
                TelegramSignalSubscription.enabled.is_(True),
                TelegramBinding.verified_at.is_not(None),
                TelegramBinding.revoked_at.is_(None),
                UserMembership.status == "ACTIVE",
            )
        )
        return tuple(
            SignalSubscriptionTarget(
                subscription_id,
                bot_id,
                int(chat_id),
                TelegramSignalFilters(
                    symbols=tuple(symbols),
                    timeframes=tuple(timeframes),
                    directions=tuple(directions),
                    strategy_ids=tuple(strategy_ids),
                    setup_codes=tuple(setup_codes),
                ),
            )
            for (
                subscription_id,
                bot_id,
                chat_id,
                symbols,
                timeframes,
                directions,
                strategy_ids,
                setup_codes,
            ) in rows
        )

    async def recent_candidates(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        created_after: datetime,
        limit: int = 100,
    ) -> tuple[TelegramSignalCandidate, ...]:
        rows = await self._session.execute(
            select(
                BotDecisionRecord.id,
                BotDecisionRecord.workspace_id,
                BotDecisionRecord.bot_id,
                BotDecisionRecord.status,
                BotDecisionRecord.reason_code,
                BotDecisionRecord.decision_hash,
                BotDecisionRecord.payload,
                BotDecisionRecord.created_at,
            )
            .where(
                BotDecisionRecord.workspace_id == workspace_id,
                BotDecisionRecord.bot_id == bot_id,
                BotDecisionRecord.status == "CANDIDATE",
                BotDecisionRecord.created_at >= created_after,
            )
            .order_by(BotDecisionRecord.created_at, BotDecisionRecord.id)
            .limit(limit)
        )
        candidates: list[TelegramSignalCandidate] = []
        for row in rows:
            decision = _verified_decision(row)
            feature_timeframe = await self._session.scalar(
                select(FeatureSnapshotRecord.timeframe).where(
                    FeatureSnapshotRecord.workspace_id == workspace_id,
                    FeatureSnapshotRecord.snapshot_id == decision.feature_snapshot_id,
                )
            )
            if feature_timeframe is None:
                continue
            candidates.append(
                _candidate_from_decision(row, str(feature_timeframe), decision=decision)
            )
        return tuple(candidates)

    async def enqueue_delivery(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        subscription_id: UUID,
        decision_id: UUID,
        available_at: datetime,
    ) -> bool:
        inserted = await self._session.scalar(
            insert(TelegramSignalDelivery)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                bot_id=bot_id,
                subscription_id=subscription_id,
                decision_id=decision_id,
                status="PENDING",
                attempts=0,
                available_at=available_at,
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "subscription_id", "decision_id"]
            )
            .returning(TelegramSignalDelivery.id)
        )
        return inserted is not None

    async def claim_one(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> SignalDeliveryClaim | None:
        query = (
            select(
                TelegramSignalDelivery,
                TelegramSignalSubscription.telegram_chat_id,
                TelegramSignalSubscription.symbols.label("filter_symbols"),
                TelegramSignalSubscription.timeframes.label("filter_timeframes"),
                TelegramSignalSubscription.directions.label("filter_directions"),
                TelegramSignalSubscription.strategy_ids.label("filter_strategy_ids"),
                TelegramSignalSubscription.setup_codes.label("filter_setup_codes"),
                BotDecisionRecord.id.label("decision_record_id"),
                BotDecisionRecord.workspace_id.label("decision_workspace_id"),
                BotDecisionRecord.bot_id.label("decision_bot_id"),
                BotDecisionRecord.status.label("decision_status"),
                BotDecisionRecord.reason_code.label("decision_reason_code"),
                BotDecisionRecord.decision_hash.label("decision_hash"),
                BotDecisionRecord.payload.label("decision_payload"),
                BotDecisionRecord.created_at.label("decision_created_at"),
            )
            .join(
                TelegramSignalSubscription,
                and_(
                    TelegramSignalSubscription.workspace_id == TelegramSignalDelivery.workspace_id,
                    TelegramSignalSubscription.bot_id == TelegramSignalDelivery.bot_id,
                    TelegramSignalSubscription.id == TelegramSignalDelivery.subscription_id,
                ),
            )
            .join(
                BotDecisionRecord,
                and_(
                    BotDecisionRecord.workspace_id == TelegramSignalDelivery.workspace_id,
                    BotDecisionRecord.id == TelegramSignalDelivery.decision_id,
                    BotDecisionRecord.bot_id == TelegramSignalDelivery.bot_id,
                ),
            )
            .join(
                TelegramBinding,
                and_(
                    TelegramBinding.workspace_id == TelegramSignalSubscription.workspace_id,
                    TelegramBinding.id == TelegramSignalSubscription.telegram_binding_id,
                    TelegramBinding.membership_id == TelegramSignalSubscription.membership_id,
                ),
            )
            .join(
                UserMembership,
                and_(
                    UserMembership.workspace_id == TelegramSignalSubscription.workspace_id,
                    UserMembership.id == TelegramSignalSubscription.membership_id,
                ),
            )
            .where(
                TelegramSignalDelivery.workspace_id == workspace_id,
                TelegramSignalDelivery.bot_id == bot_id,
                TelegramSignalSubscription.enabled.is_(True),
                TelegramSignalDelivery.attempts < max_attempts,
                TelegramSignalDelivery.available_at <= now,
                or_(
                    TelegramSignalDelivery.status == "PENDING",
                    and_(
                        TelegramSignalDelivery.status == "PROCESSING",
                        TelegramSignalDelivery.lease_expires_at <= now,
                    ),
                ),
                BotDecisionRecord.status == "CANDIDATE",
                TelegramBinding.verified_at.is_not(None),
                TelegramBinding.revoked_at.is_(None),
                UserMembership.status == "ACTIVE",
            )
            .order_by(TelegramSignalDelivery.available_at, TelegramSignalDelivery.created_at)
            .limit(1)
            .with_for_update(of=TelegramSignalDelivery, skip_locked=True)
        )
        result = (await self._session.execute(query)).first()
        if result is None:
            return None
        delivery = result[0]
        chat_id = int(result.telegram_chat_id)
        decision = _verified_decision(result, prefixed=True)
        timeframe = await self._session.scalar(
            select(FeatureSnapshotRecord.timeframe).where(
                FeatureSnapshotRecord.workspace_id == workspace_id,
                FeatureSnapshotRecord.snapshot_id == decision.feature_snapshot_id,
            )
        )
        if timeframe is None:
            delivery.status = "DEAD"
            delivery.last_error_code = "SIGNAL_TIMEFRAME_UNAVAILABLE"
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            return None
        candidate = _candidate_from_decision(
            result, str(timeframe), prefixed=True, decision=decision
        )
        filters = TelegramSignalFilters(
            symbols=tuple(result.filter_symbols),
            timeframes=tuple(result.filter_timeframes),
            directions=tuple(result.filter_directions),
            strategy_ids=tuple(result.filter_strategy_ids),
            setup_codes=tuple(result.filter_setup_codes),
        )
        if not filters.matches(candidate):
            delivery.status = "FILTERED"
            delivery.last_error_code = "SUBSCRIPTION_FILTER_MISMATCH"
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            return SignalDeliveryClaim(
                delivery_id=delivery.id,
                subscription_id=delivery.subscription_id,
                bot_id=delivery.bot_id,
                decision_id=candidate.decision_id,
                attempts=delivery.attempts,
                chat_id=chat_id,
                candidate=candidate,
                filtered=True,
            )
        delivery.status = "PROCESSING"
        delivery.attempts += 1
        delivery.lease_owner = worker_id
        delivery.lease_expires_at = now + lease_duration
        return SignalDeliveryClaim(
            delivery_id=delivery.id,
            subscription_id=delivery.subscription_id,
            bot_id=delivery.bot_id,
            decision_id=candidate.decision_id,
            attempts=delivery.attempts,
            chat_id=chat_id,
            candidate=candidate,
        )

    async def mark_sent(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        sent_at: datetime,
    ) -> bool:
        result = await self._session.execute(
            update(TelegramSignalDelivery)
            .where(
                TelegramSignalDelivery.workspace_id == workspace_id,
                TelegramSignalDelivery.bot_id == bot_id,
                TelegramSignalDelivery.id == delivery_id,
                TelegramSignalDelivery.status == "PROCESSING",
                TelegramSignalDelivery.lease_owner == worker_id,
            )
            .values(
                status="SENT",
                sent_at=sent_at,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def renew_claim(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Gia hạn lease ngay trước HTTP để tránh replica reclaim delivery trong lúc gửi."""
        result = await self._session.execute(
            update(TelegramSignalDelivery)
            .where(
                TelegramSignalDelivery.workspace_id == workspace_id,
                TelegramSignalDelivery.bot_id == bot_id,
                TelegramSignalDelivery.id == delivery_id,
                TelegramSignalDelivery.status == "PROCESSING",
                TelegramSignalDelivery.lease_owner == worker_id,
            )
            .values(lease_expires_at=lease_expires_at)
        )
        return bool(getattr(result, "rowcount", 0))

    async def mark_failed(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        available_at: datetime,
        error_code: str,
        max_attempts: int,
        attempts: int,
    ) -> bool:
        result = await self._session.execute(
            update(TelegramSignalDelivery)
            .where(
                TelegramSignalDelivery.workspace_id == workspace_id,
                TelegramSignalDelivery.bot_id == bot_id,
                TelegramSignalDelivery.id == delivery_id,
                TelegramSignalDelivery.status == "PROCESSING",
                TelegramSignalDelivery.lease_owner == worker_id,
            )
            .values(
                status="DEAD" if attempts >= max_attempts else "PENDING",
                available_at=available_at,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code[:120],
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def mark_expired(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        error_code: str = "SIGNAL_EXPIRED_BEFORE_DELIVERY",
    ) -> bool:
        result = await self._session.execute(
            update(TelegramSignalDelivery)
            .where(
                TelegramSignalDelivery.workspace_id == workspace_id,
                TelegramSignalDelivery.bot_id == bot_id,
                TelegramSignalDelivery.id == delivery_id,
                TelegramSignalDelivery.status == "PROCESSING",
                TelegramSignalDelivery.lease_owner == worker_id,
            )
            .values(
                status="EXPIRED",
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code,
            )
        )
        return bool(getattr(result, "rowcount", 0))


def _candidate_from_decision(
    row: Any,
    timeframe: str,
    *,
    prefixed: bool = False,
    decision: BotDecision | None = None,
) -> TelegramSignalCandidate:
    """Xác minh decision hash/cột trước khi dựng thông báo; thiếu lineage thì từ chối."""
    decision = decision or _verified_decision(row, prefixed=prefixed)
    if decision.candidate_json is None:
        raise ValueError("Chỉ candidate đã lưu mới có thể thành thông báo tín hiệu.")
    payload = json.loads(decision.candidate_json)
    return TelegramSignalCandidate(
        decision_id=decision.decision_id,
        symbol=str(payload["symbol"]),
        direction=str(payload["direction"]).upper(),
        timeframe=timeframe,
        strategy_id=str(payload["strategy_id"]),
        strategy_release_id=str(payload["strategy_release_id"]),
        reference_price=Decimal(str(payload["reference_price"])),
        signal_at=datetime.fromisoformat(str(payload["signal_at"]).replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")),
        reason_code=str(payload["reason_code"]),
        detail_vi=str(payload["detail_vi"]),
        invalidation=None,
    )


def _verified_decision(row: Any, *, prefixed: bool = False) -> BotDecision:
    """Xác minh hash quyết định và các cột vật lý trước khi đọc candidate payload."""
    payload = row.decision_payload if prefixed else row.payload
    decision_hash = row.decision_hash
    decision = BotDecision.from_payload(payload, decision_hash)
    row_id = row.decision_record_id if prefixed else row.id
    row_workspace_id = row.decision_workspace_id if prefixed else row.workspace_id
    row_bot_id = row.decision_bot_id if prefixed else row.bot_id
    row_status = row.decision_status if prefixed else row.status
    row_reason_code = row.decision_reason_code if prefixed else row.reason_code
    if (
        decision.decision_id != row_id
        or decision.workspace_id != row_workspace_id
        or decision.bot_id != row_bot_id
        or decision.status.value != row_status
        or decision.reason_code != row_reason_code
        or decision.candidate_json is None
    ):
        raise ValueError("Candidate columns không khớp payload/hash quyết định.")
    return decision
