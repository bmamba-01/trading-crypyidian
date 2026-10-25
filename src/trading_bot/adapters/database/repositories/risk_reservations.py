"""Repository PostgreSQL cho reservation risk theo workspace và tài khoản."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import case, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.bot_scope import bind_account_bot
from trading_bot.adapters.database.models import (
    FillRecord,
    OrderIntentRecord,
    RiskDecisionRecord,
    RiskReservationRecord,
    TradePlanRecord,
    TradeRecord,
    TradingAccount,
)
from trading_bot.domain.risk.engine import RiskDecision
from trading_bot.domain.risk.planner import TradePlan, calculate_plan_hash
from trading_bot.domain.risk.reservations import (
    ReservationConflict,
    ReservationOutcome,
    ReservationReleaseReason,
    ReservationStatus,
    RiskReservation,
)
from trading_bot.domain.risk.workspace_budget import WorkspaceRiskAggregate

_HELD_STATUSES = (ReservationStatus.RESERVED.value, ReservationStatus.UNKNOWN.value)


@dataclass(frozen=True, slots=True)
class RiskEvaluationRecords:
    """Định danh bản ghi plan và quyết định risk bất biến."""

    trade_plan_id: UUID
    risk_decision_id: UUID
    decision_key: str


class RiskReservationRepository:
    """Ghi giữ risk trong transaction do caller sở hữu.

    Mọi thao tác ghi khóa hàng `trading_account` trước khi đọc hoặc đổi reservation.
    `workspace_id` phải được đặt transaction-local trong `app.workspace_id` bởi
    Unit of Work để PostgreSQL RLS giới hạn tenant.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    async def record_evaluation(
        self, *, plan: TradePlan, decision: RiskDecision
    ) -> RiskEvaluationRecords:
        """Lưu plan và quyết định risk, kể cả bị từ chối, trong transaction hiện tại."""
        workspace_id, account_id = _decision_scope(decision)
        if (
            str(workspace_id) != plan.workspace_id
            or str(account_id) != plan.account_id
            or decision.plan_hash != plan.plan_hash
            or calculate_plan_hash(plan) != plan.plan_hash
        ):
            raise ReservationConflict("Plan và risk decision không cùng scope/hash hợp lệ.")
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        plan_payload = _canonical_value(plan)
        if not isinstance(plan_payload, dict):
            raise ReservationConflict("TradePlan không thể chuyển thành payload chuẩn.")
        existing_plan = await self._session.scalar(
            select(TradePlanRecord).where(
                TradePlanRecord.bot_id == self._session.info.get("bot_id"),
                TradePlanRecord.workspace_id == workspace_id,
                TradePlanRecord.account_id == account_id,
                TradePlanRecord.plan_hash == plan.plan_hash,
            )
        )
        if existing_plan is None:
            existing_plan = TradePlanRecord(
                bot_id=self._session.info.get("bot_id"),
                id=uuid4(),
                workspace_id=workspace_id,
                account_id=account_id,
                plan_hash=plan.plan_hash,
                symbol=plan.symbol,
                notional=plan.notional,
                payload=plan_payload,
            )
            self._session.add(existing_plan)
            await self._session.flush()
        elif existing_plan.payload != plan_payload:
            raise ReservationConflict("TradePlan hash đã có payload khác.")

        decision_payload = _canonical_value(decision)
        if not isinstance(decision_payload, dict):
            raise ReservationConflict("RiskDecision không thể chuyển thành payload chuẩn.")
        decision_key = hashlib.sha256(
            json.dumps(decision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing_decision = await self._session.scalar(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.bot_id == self._session.info.get("bot_id"),
                RiskDecisionRecord.workspace_id == workspace_id,
                RiskDecisionRecord.account_id == account_id,
                RiskDecisionRecord.decision_key == decision_key,
            )
        )
        if existing_decision is None:
            existing_decision = RiskDecisionRecord(
                bot_id=self._session.info.get("bot_id"),
                id=uuid4(),
                workspace_id=workspace_id,
                account_id=account_id,
                decision_key=decision_key,
                plan_hash=plan.plan_hash,
                snapshot_id=decision.snapshot_id,
                approved=decision.approved,
                reason_code=decision.reason_code,
                detail_vi=decision.detail_vi,
                payload=decision_payload,
            )
            self._session.add(existing_decision)
            await self._session.flush()
        elif existing_decision.payload != decision_payload:
            raise ReservationConflict("Risk decision key đã có payload khác.")
        return RiskEvaluationRecords(
            trade_plan_id=existing_plan.id,
            risk_decision_id=existing_decision.id,
            decision_key=decision_key,
        )

    async def reserve(
        self,
        decision: RiskDecision,
        *,
        idempotency_key: str,
        risk_decision_id: UUID,
    ) -> RiskReservation:
        """Giữ hạn mức nếu snapshot còn đồng bộ và tổng risk vẫn dưới hard cap."""
        if not decision.approved:
            raise ReservationConflict("Chỉ quyết định risk đã duyệt mới được reserve.")
        if not idempotency_key.strip():
            raise ReservationConflict("idempotency key không được để trống.")
        if len(idempotency_key) > 240:
            raise ReservationConflict("idempotency key vượt giới hạn 240 ký tự.")
        workspace_id, account_id = _decision_scope(decision)
        _validate_amounts(
            decision.requested_risk,
            decision.open_risk,
            decision.pending_reservation_risk,
            decision.requested_notional,
            decision.open_notional,
            decision.symbol_open_notional,
            decision.max_notional,
            decision.max_symbol_notional,
            decision.pending_reservation_notional,
            decision.pending_symbol_reservation_notional,
            decision.available_margin,
            decision.requested_initial_margin,
            decision.pending_reservation_initial_margin,
            decision.max_aggregate_open_risk,
        )
        if decision.pending_reservation_risk > decision.open_risk:
            raise ReservationConflict("Risk pending không được lớn hơn tổng open risk.")
        if decision.requested_risk <= Decimal("0"):
            raise ReservationConflict("Risk cần reserve phải lớn hơn 0.")
        if decision.requested_initial_margin <= Decimal("0"):
            raise ReservationConflict("Initial margin cần reserve phải lớn hơn 0.")
        if decision.pending_reservation_initial_margin > decision.available_margin:
            raise ReservationConflict("Margin pending vượt margin khả dụng của snapshot.")
        if not decision.symbol.strip() or decision.requested_notional <= Decimal("0"):
            raise ReservationConflict("Symbol và notional cần reserve phải hợp lệ, lớn hơn 0.")
        if decision.pending_reservation_notional > decision.open_notional:
            raise ReservationConflict("Notional pending không được lớn hơn tổng open notional.")
        if decision.pending_symbol_reservation_notional > decision.symbol_open_notional:
            raise ReservationConflict(
                "Notional pending theo symbol vượt tổng symbol open notional."
            )
        if decision.pending_reservation_count < 0 or decision.open_position_count < 0:
            raise ReservationConflict("Số vị thế và reservation không được âm.")
        if decision.max_positions < 1:
            raise ReservationConflict("Giới hạn số vị thế phải lớn hơn 0.")

        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        persisted_decision = await self._session.scalar(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.bot_id == self._session.info.get("bot_id"),
                RiskDecisionRecord.workspace_id == workspace_id,
                RiskDecisionRecord.account_id == account_id,
                RiskDecisionRecord.id == risk_decision_id,
            )
        )
        if (
            persisted_decision is None
            or not persisted_decision.approved
            or persisted_decision.plan_hash != decision.plan_hash
            or persisted_decision.decision_key != _decision_key(decision)
        ):
            raise ReservationConflict("Reservation cần decision đã lưu, đã duyệt và cùng nội dung.")
        existing = await self._session.scalar(
            select(RiskReservationRecord).where(
                RiskReservationRecord.bot_id == self._session.info.get("bot_id"),
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.plan_hash != decision.plan_hash
                or existing.reserved_risk != decision.requested_risk
                or existing.symbol != decision.symbol
                or existing.reserved_notional != decision.requested_notional
                or existing.reserved_initial_margin != decision.requested_initial_margin
                or existing.risk_decision_id != risk_decision_id
            ):
                raise ReservationConflict("idempotency key đã được dùng cho plan khác.")
            now = self._clock()
            _require_aware(now, "clock")
            if now >= existing.expires_at:
                raise ReservationConflict("Reservation đã hết hạn; cần idempotency key mới.")
            if existing.status in {
                ReservationStatus.RELEASED.value,
                ReservationStatus.TRANSFERRED.value,
            }:
                raise ReservationConflict("Reservation đã ở trạng thái terminal.")
            return _to_domain(existing)

        expires_at = decision.candidate_expires_at
        now = self._clock()
        if (
            expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ReservationConflict("Mốc thời gian hết hạn cần có múi giờ.")
        if now >= expires_at:
            raise ReservationConflict("Candidate đã hết hạn trước khi reserve.")

        await self._expire_due_locked(workspace_id=workspace_id, account_id=account_id, now=now)

        totals = await self._active_totals(
            workspace_id=workspace_id, account_id=account_id, symbol=decision.symbol
        )
        if totals.count != decision.pending_reservation_count:
            raise ReservationConflict(
                "Snapshot có số reservation khác trạng thái hiện tại; cần đánh giá risk lại."
            )
        if totals.initial_margin + decision.requested_initial_margin > decision.available_margin:
            raise ReservationConflict(
                "Initial margin của reservation và plan vượt margin khả dụng."
            )
        risk_basis = decision.open_risk - decision.pending_reservation_risk + totals.risk
        if risk_basis + decision.requested_risk > decision.max_aggregate_open_risk:
            raise ReservationConflict("Aggregate open risk vượt giới hạn sau reservation.")
        notional_basis = (
            decision.open_notional - decision.pending_reservation_notional + totals.notional
        )
        if notional_basis + decision.requested_notional > decision.max_notional:
            raise ReservationConflict("Tổng notional vượt giới hạn sau reservation.")
        symbol_notional_basis = (
            decision.symbol_open_notional
            - decision.pending_symbol_reservation_notional
            + totals.symbol_notional
        )
        if symbol_notional_basis + decision.requested_notional > decision.max_symbol_notional:
            raise ReservationConflict("Notional tập trung theo symbol vượt giới hạn.")
        if decision.open_position_count + totals.count + 1 > decision.max_positions:
            raise ReservationConflict("Số vị thế mở và reservation vượt giới hạn.")

        record = RiskReservationRecord(
            bot_id=self._session.info.get("bot_id"),
            id=uuid4(),
            workspace_id=workspace_id,
            account_id=account_id,
            risk_decision_id=risk_decision_id,
            idempotency_key=idempotency_key,
            plan_hash=decision.plan_hash,
            symbol=decision.symbol,
            reserved_risk=decision.requested_risk,
            reserved_notional=decision.requested_notional,
            reserved_initial_margin=decision.requested_initial_margin,
            expires_at=expires_at,
            status=ReservationStatus.RESERVED.value,
        )
        self._session.add(record)
        await self._session.flush()
        return _to_domain(record)

    async def mark_unknown(
        self, *, workspace_id: UUID, account_id: UUID, reservation_id: UUID
    ) -> RiskReservation:
        """Đánh dấu kết quả submit chưa rõ; reservation tiếp tục chiếm ngân sách."""
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        record = await self._get_reservation(
            workspace_id=workspace_id, account_id=account_id, reservation_id=reservation_id
        )
        intent_id = await self._session.scalar(
            select(OrderIntentRecord.id).where(
                OrderIntentRecord.bot_id == self._session.info.get("bot_id"),
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.account_id == account_id,
                OrderIntentRecord.risk_reservation_id == reservation_id,
            )
        )
        if intent_id is None:
            raise ReservationConflict("UNKNOWN cần intent đã được persist trong cùng scope.")
        if record.status == ReservationStatus.UNKNOWN.value:
            return _to_domain(record)
        if record.status != ReservationStatus.RESERVED.value:
            raise ReservationConflict("Chỉ reservation RESERVED mới được chuyển UNKNOWN.")
        record.status = ReservationStatus.UNKNOWN.value
        await self._session.flush()
        return _to_domain(record)

    async def reconcile(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        outcome: ReservationOutcome,
        persist_open_risk: Callable[[], Awaitable[None]] | None = None,
    ) -> RiskReservation:
        """Reconcile UNKNOWN; callback phải ghi exposure cùng transaction hiện tại."""
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        record = await self._get_reservation(
            workspace_id=workspace_id, account_id=account_id, reservation_id=reservation_id
        )
        if record.status in {
            ReservationStatus.RELEASED.value,
            ReservationStatus.TRANSFERRED.value,
        }:
            expected_reason = {
                ReservationOutcome.ORDER_ABSENT: ReservationReleaseReason.ORDER_ABSENT.value,
                ReservationOutcome.OPEN_RISK_RECORDED: (
                    ReservationReleaseReason.OPEN_RISK_RECORDED.value
                ),
            }.get(outcome)
            if expected_reason is not None and record.resolution_reason == expected_reason:
                return _to_domain(record)
            raise ReservationConflict("Kết quả reconcile mâu thuẫn với trạng thái đã ghi.")
        if record.status != ReservationStatus.UNKNOWN.value:
            raise ReservationConflict("Chỉ reservation UNKNOWN mới được reconcile.")
        if outcome is ReservationOutcome.UNRESOLVED:
            return _to_domain(record)
        if outcome is ReservationOutcome.ORDER_ABSENT:
            record.status = ReservationStatus.RELEASED.value
            record.resolution_reason = ReservationReleaseReason.ORDER_ABSENT.value
        elif outcome is ReservationOutcome.OPEN_RISK_RECORDED:
            if persist_open_risk is None:
                raise ReservationConflict(
                    "Cần ghi exposure trong cùng transaction trước khi chuyển reservation."
                )
            await persist_open_risk()
            record.status = ReservationStatus.TRANSFERRED.value
            record.resolution_reason = ReservationReleaseReason.OPEN_RISK_RECORDED.value
        else:
            raise ReservationConflict("Kết quả reconcile không hợp lệ.")
        await self._session.flush()
        return _to_domain(record)

    async def transfer_to_open_trade(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        intent_id: UUID,
        trade_id: UUID,
    ) -> RiskReservation:
        """Chuyển pending risk sang exposure của trade OPEN trong cùng transaction."""
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        record = await self._get_reservation(
            workspace_id=workspace_id, account_id=account_id, reservation_id=reservation_id
        )
        intent = await self._session.scalar(
            select(OrderIntentRecord).where(
                OrderIntentRecord.bot_id == self._session.info.get("bot_id"),
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.account_id == account_id,
                OrderIntentRecord.id == intent_id,
            )
        )
        trade = await self._session.scalar(
            select(TradeRecord).where(
                TradeRecord.bot_id == self._session.info.get("bot_id"),
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.account_id == account_id,
                TradeRecord.id == trade_id,
                TradeRecord.intent_id == intent_id,
            )
        )
        entry_fill = await self._session.scalar(
            select(FillRecord.id).where(
                FillRecord.bot_id == self._session.info.get("bot_id"),
                FillRecord.workspace_id == workspace_id,
                FillRecord.account_id == account_id,
                FillRecord.intent_id == intent_id,
                FillRecord.fill_role == "ENTRY",
            )
        )
        if (
            intent is None
            or trade is None
            or entry_fill is None
            or intent.risk_reservation_id != reservation_id
            or intent.filled_quantity <= 0
            or trade.risk_reservation_id != reservation_id
            or trade.status != "OPEN"
        ):
            raise ReservationConflict(
                "Chỉ reservation gắn với intent có ENTRY fill và trade OPEN mới được transfer."
            )
        if record.status == ReservationStatus.TRANSFERRED.value:
            if record.resolution_reason == ReservationReleaseReason.OPEN_RISK_RECORDED.value:
                return _to_domain(record)
            raise ReservationConflict("Reservation TRANSFERRED có resolution reason sai.")
        if record.status not in {
            ReservationStatus.RESERVED.value,
            ReservationStatus.UNKNOWN.value,
        }:
            raise ReservationConflict("Chỉ reservation còn được giữ mới transfer sang open risk.")
        record.status = ReservationStatus.TRANSFERRED.value
        record.resolution_reason = ReservationReleaseReason.OPEN_RISK_RECORDED.value
        await self._session.flush()
        return _to_domain(record)

    async def release_terminal_no_fill(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        intent_id: UUID,
    ) -> RiskReservation:
        """Giải phóng reservation cho intent CANCELLED/EXPIRED chưa có ENTRY fill."""
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        record = await self._get_reservation(
            workspace_id=workspace_id, account_id=account_id, reservation_id=reservation_id
        )
        intent = await self._session.scalar(
            select(OrderIntentRecord).where(
                OrderIntentRecord.bot_id == self._session.info.get("bot_id"),
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.account_id == account_id,
                OrderIntentRecord.id == intent_id,
            )
        )
        if intent is None or intent.risk_reservation_id != reservation_id:
            raise ReservationConflict("Intent không thuộc reservation/workspace/account.")
        reason = {
            "CANCELLED": ReservationReleaseReason.CANCELLED_NO_FILL,
            "EXPIRED": ReservationReleaseReason.EXPIRED_NO_FILL,
        }.get(intent.status)
        if reason is None or intent.filled_quantity != 0:
            raise ReservationConflict("Chỉ intent terminal chưa fill mới release reservation.")
        if record.status == ReservationStatus.RELEASED.value:
            if record.resolution_reason == reason.value:
                return _to_domain(record)
            raise ReservationConflict("Reservation đã RELEASED với lý do khác.")
        if record.status != ReservationStatus.RESERVED.value:
            raise ReservationConflict("UNKNOWN/TRANSFERRED reservation không tự release.")
        trade = await self._session.scalar(
            select(TradeRecord.id).where(
                TradeRecord.bot_id == self._session.info.get("bot_id"),
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.account_id == account_id,
                TradeRecord.intent_id == intent_id,
            )
        )
        entry_fill = await self._session.scalar(
            select(FillRecord.id).where(
                FillRecord.bot_id == self._session.info.get("bot_id"),
                FillRecord.workspace_id == workspace_id,
                FillRecord.account_id == account_id,
                FillRecord.intent_id == intent_id,
                FillRecord.fill_role == "ENTRY",
            )
        )
        if trade is not None or entry_fill is not None:
            raise ReservationConflict("Intent no-fill lại có trade hoặc ENTRY fill trong ledger.")
        record.status = ReservationStatus.RELEASED.value
        record.resolution_reason = reason.value
        await self._session.flush()
        return _to_domain(record)

    async def release_before_submit(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        reason: ReservationReleaseReason,
        order_was_submitted: bool,
    ) -> RiskReservation:
        """Release reservation chỉ khi đã xác nhận lệnh chưa được gửi tới sàn."""
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        record = await self._get_reservation(
            workspace_id=workspace_id, account_id=account_id, reservation_id=reservation_id
        )
        if order_was_submitted:
            raise ReservationConflict("Không thể release nếu order đã được submit.")
        if record.status == ReservationStatus.RELEASED.value:
            if record.resolution_reason == reason.value:
                return _to_domain(record)
            raise ReservationConflict("Lý do release mâu thuẫn với trạng thái đã ghi.")
        if record.status != ReservationStatus.RESERVED.value:
            raise ReservationConflict("Chỉ reservation RESERVED mới release trước submit.")
        if reason not in {
            ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
            ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
        }:
            raise ReservationConflict("Lý do release trước submit không hợp lệ.")
        if reason is ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT:
            now = self._clock()
            _require_aware(now, "clock")
            if now < record.expires_at:
                raise ReservationConflict("Reservation chưa hết hạn.")
        record.status = ReservationStatus.RELEASED.value
        record.resolution_reason = reason.value
        await self._session.flush()
        return _to_domain(record)

    async def lock_workspace_risk(self, workspace_id: UUID) -> None:
        """Khóa Advisory Lock cấp 1 theo workspace_id (Ruling 10)."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:risk_lock, 0))"),
            {"risk_lock": f"workspace-risk:{workspace_id}"},
        )

    async def lock_account_risk(self, workspace_id: UUID, account_id: UUID) -> None:
        """Khóa Advisory Lock cấp 2 theo workspace_id và account_id (Ruling 10)."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:risk_lock, 0))"),
            {"risk_lock": f"account-risk:{workspace_id}:{account_id}"},
        )

    async def lock_accounts_risk(self, workspace_id: UUID, account_ids: Sequence[UUID]) -> None:
        """Khóa Advisory Lock cấp 2 cho nhiều account sắp xếp UUID tăng dần (Ruling 10)."""
        for acc_id in sorted(set(account_ids)):
            await self.lock_account_risk(workspace_id, acc_id)

    async def get_by_idempotency_key(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        idempotency_key: str,
    ) -> RiskReservationRecord | None:
        """Tìm bản ghi reservation theo scope và idempotency key."""
        return await self._session.scalar(
            select(RiskReservationRecord).where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.bot_id == bot_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.idempotency_key == idempotency_key,
            )
        )

    async def get_decision_by_key(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        decision_key: str,
    ) -> RiskDecisionRecord | None:
        """Tìm bản ghi decision theo scope và decision key."""
        return await self._session.scalar(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.workspace_id == workspace_id,
                RiskDecisionRecord.bot_id == bot_id,
                RiskDecisionRecord.account_id == account_id,
                RiskDecisionRecord.decision_key == decision_key,
            )
        )

    async def get_decision_by_idempotency_key(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        idempotency_key: str,
    ) -> RiskDecisionRecord | None:
        """Tìm bản ghi decision theo scope và idempotency key nằm trong payload."""
        result = await self._session.scalars(
            select(RiskDecisionRecord)
            .where(
                RiskDecisionRecord.workspace_id == workspace_id,
                RiskDecisionRecord.bot_id == bot_id,
                RiskDecisionRecord.account_id == account_id,
            )
            .order_by(RiskDecisionRecord.created_at.desc())
        )
        for record in result.all():
            if (
                isinstance(record.payload, dict)
                and record.payload.get("idempotency_key") == idempotency_key
            ):
                return record
        return None

    async def list_active_reservations(self, *, workspace_id: UUID) -> list[RiskReservationRecord]:
        """Lấy danh sách các reservation đang active (RESERVED hoặc UNKNOWN) trong workspace."""
        result = await self._session.scalars(
            select(RiskReservationRecord).where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.status.in_(_HELD_STATUSES),
            )
        )
        return list(result.all())

    async def expire_overdue(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
        account_ids: Sequence[UUID] | None = None,
    ) -> int:
        """Giải phóng các row RESERVED có expires_at <= as_of sang RELEASED; bỏ qua UNKNOWN."""
        _require_aware(as_of, "as_of")
        stmt = select(RiskReservationRecord).where(
            RiskReservationRecord.workspace_id == workspace_id,
            RiskReservationRecord.status == ReservationStatus.RESERVED.value,
            RiskReservationRecord.expires_at <= as_of,
        )
        if account_ids is not None:
            stmt = stmt.where(RiskReservationRecord.account_id.in_(list(account_ids)))
        stmt = stmt.with_for_update()
        result = await self._session.scalars(stmt)
        due = list(result.all())
        for record in due:
            record.status = ReservationStatus.RELEASED.value
            record.resolution_reason = ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT.value
        if due:
            await self._session.flush()
        return len(due)

    async def get_workspace_risk_aggregate(
        self, *, workspace_id: UUID, as_of: datetime
    ) -> WorkspaceRiskAggregate:
        """Gọi SQL aggregate function get_workspace_risk_aggregate và ánh xạ sang DTO."""
        _require_aware(as_of, "as_of")
        stmt = text(
            "SELECT workspace_id, total_open_risk_usdt, total_pending_risk_usdt, "
            "active_bot_count, has_unknown_exposure "
            "FROM public.get_workspace_risk_aggregate(:workspace_id, :as_of)"
        )
        result = await self._session.execute(stmt, {"workspace_id": workspace_id, "as_of": as_of})
        row = result.one()
        return WorkspaceRiskAggregate(
            workspace_id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
            total_open_risk_usdt=Decimal(str(row[1])),
            total_pending_risk_usdt=Decimal(str(row[2])),
            active_bot_count=int(row[3]),
            has_unknown_exposure=bool(row[4]),
        )

    async def expire_due(
        self, *, workspace_id: UUID, account_id: UUID, now: datetime | None = None
    ) -> int:
        """Giải phóng RESERVED hết hạn chưa gắn với order intent; UNKNOWN được giữ lại."""
        await self._lock_account(workspace_id=workspace_id, account_id=account_id)
        current_time = self._clock() if now is None else now
        _require_aware(current_time, "now")
        return await self._expire_due_locked(
            workspace_id=workspace_id, account_id=account_id, now=current_time
        )

    async def _expire_due_locked(
        self, *, workspace_id: UUID, account_id: UUID, now: datetime
    ) -> int:
        """Account lock serializes expiry with reservation creation and intent writes."""
        unsubmitted_intent = exists(
            select(OrderIntentRecord.id).where(
                OrderIntentRecord.bot_id == self._session.info.get("bot_id"),
                OrderIntentRecord.workspace_id == RiskReservationRecord.workspace_id,
                OrderIntentRecord.account_id == RiskReservationRecord.account_id,
                OrderIntentRecord.risk_reservation_id == RiskReservationRecord.id,
            )
        )
        result = await self._session.scalars(
            select(RiskReservationRecord)
            .where(
                RiskReservationRecord.bot_id == self._session.info.get("bot_id"),
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.status == ReservationStatus.RESERVED.value,
                RiskReservationRecord.expires_at <= now,
                ~unsubmitted_intent,
            )
            .with_for_update()
        )
        due = list(result.all())
        for record in due:
            record.status = ReservationStatus.RELEASED.value
            record.resolution_reason = ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT.value
        if due:
            await self._session.flush()
        return len(due)

    async def _lock_account(self, *, workspace_id: UUID, account_id: UUID) -> None:
        account = await self._session.scalar(
            select(TradingAccount)
            .where(
                TradingAccount.workspace_id == workspace_id,
                TradingAccount.id == account_id,
            )
            .with_for_update()
        )
        if account is None:
            raise ReservationConflict("Không tìm thấy tài khoản trong workspace.")
        try:
            await bind_account_bot(self._session, workspace_id=workspace_id, account_id=account_id)
        except ValueError as error:
            raise ReservationConflict(str(error)) from error

    async def _get_reservation(
        self, *, workspace_id: UUID, account_id: UUID, reservation_id: UUID
    ) -> RiskReservationRecord:
        record = await self._session.scalar(
            select(RiskReservationRecord).where(
                RiskReservationRecord.bot_id == self._session.info.get("bot_id"),
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.id == reservation_id,
            )
        )
        if record is None:
            raise ReservationConflict("Không tìm thấy reservation trong workspace/account.")
        return record

    async def _active_totals(
        self, *, workspace_id: UUID, account_id: UUID, symbol: str
    ) -> _ActiveTotals:
        result = await self._session.execute(
            select(
                func.coalesce(func.sum(RiskReservationRecord.reserved_risk), Decimal("0")),
                func.count(RiskReservationRecord.id),
                func.coalesce(func.sum(RiskReservationRecord.reserved_notional), Decimal("0")),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                RiskReservationRecord.symbol == symbol,
                                RiskReservationRecord.reserved_notional,
                            ),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ),
                func.coalesce(
                    func.sum(RiskReservationRecord.reserved_initial_margin), Decimal("0")
                ),
            ).where(
                RiskReservationRecord.bot_id == self._session.info.get("bot_id"),
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.status.in_(_HELD_STATUSES),
            )
        )
        risk, count, notional, symbol_notional, initial_margin = cast(
            tuple[Decimal, int, Decimal, Decimal, Decimal], result.one()
        )
        return _ActiveTotals(
            risk=Decimal(risk),
            count=int(count),
            notional=Decimal(notional),
            symbol_notional=Decimal(symbol_notional),
            initial_margin=Decimal(initial_margin),
        )


class _ActiveTotals:
    """Tổng risk/notional/margin và số reservation còn giữ dưới account lock."""

    def __init__(
        self,
        *,
        risk: Decimal,
        count: int,
        notional: Decimal,
        symbol_notional: Decimal,
        initial_margin: Decimal,
    ) -> None:
        self.risk = risk
        self.count = count
        self.notional = notional
        self.symbol_notional = symbol_notional
        self.initial_margin = initial_margin


def _decision_scope(decision: RiskDecision) -> tuple[UUID, UUID]:
    try:
        return UUID(decision.workspace_id), UUID(decision.account_id)
    except (ValueError, AttributeError) as error:
        raise ReservationConflict("Workspace/account ID phải là UUID hợp lệ.") from error


def _validate_amounts(*values: Decimal) -> None:
    if any(not value.is_finite() or value < Decimal("0") for value in values):
        raise ReservationConflict("Risk amount phải là Decimal hữu hạn không âm.")


def _canonical_value(value: object) -> object:
    """Chuẩn hóa dataclass/Decimal để payload replay không phụ thuộc định dạng."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ReservationConflict("Plan/decision có kiểu dữ liệu chưa hỗ trợ lưu payload.")


def _decision_key(decision: RiskDecision) -> str:
    payload = _canonical_value(decision)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _to_domain(record: RiskReservationRecord) -> RiskReservation:
    if record.expires_at is None:
        raise ReservationConflict("Reservation không có mốc hết hạn được lưu.")
    _require_aware(record.expires_at, "expires_at")
    try:
        status = ReservationStatus(record.status)
    except ValueError as error:
        raise ReservationConflict("Trạng thái reservation trong DB không hợp lệ.") from error
    try:
        reason = (
            ReservationReleaseReason(record.resolution_reason)
            if record.resolution_reason is not None
            else None
        )
    except ValueError as error:
        raise ReservationConflict("Lý do reservation trong DB không hợp lệ.") from error
    return RiskReservation(
        reservation_id=record.id,
        workspace_id=str(record.workspace_id),
        account_id=str(record.account_id),
        idempotency_key=record.idempotency_key,
        plan_hash=record.plan_hash,
        reserved_risk=Decimal(record.reserved_risk),
        status=status,
        expires_at=record.expires_at,
        symbol=record.symbol,
        reserved_notional=Decimal(record.reserved_notional),
        resolution_reason=reason,
        reserved_initial_margin=Decimal(record.reserved_initial_margin),
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReservationConflict(f"{name} cần có múi giờ.")
