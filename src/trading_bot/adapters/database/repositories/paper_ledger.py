"""Ghi ledger PAPER trong transaction do UnitOfWork quản lý."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from trading_bot.adapters.database.models.entities import StrategyRelease, TradingAccount
from trading_bot.adapters.database.models.paper_entities import (
    ExchangeOrderRecord,
    FillRecord,
    OrderIntentRecord,
    PositionEventRecord,
    TradeCostRecord,
    TradeRecord,
)
from trading_bot.adapters.database.models.risk_entities import (
    RiskDecisionRecord,
    RiskReservationRecord,
    TradePlanRecord,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.events import EventEnvelope
from trading_bot.domain.risk.reservations import RiskReservation


class PaperLedgerConflict(RuntimeError):
    """Từ chối dữ liệu PAPER sai phạm vi, khác payload hoặc sai trạng thái."""


class PaperLedgerRepository:
    """Lưu bản ghi có thể phát lại; không tự commit và không gọi mạng/sàn."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create_intent(
        self,
        uow: UnitOfWork,
        *,
        account_id: UUID,
        idempotency_key: str,
        plan_hash: str,
        settings_version_id: UUID,
        strategy_release_id: UUID,
        strategy_release_hash: str,
        feature_snapshot_id: str,
        candidate_id: str,
        risk_decision_id: UUID,
        risk_reservation_id: UUID,
        symbol: str,
        side: str,
        quote_asset: str,
        quantity: Decimal,
        created_at: datetime,
        evidence: dict[str, Any] | None = None,
    ) -> IntentAppendResult:
        """Lock PAPER account và append intent; cùng idempotency key được nhận lại."""
        if not isinstance(uow.workspace_id, UUID):
            raise PaperLedgerConflict("UnitOfWork cần workspace hợp lệ.")
        _validate_intent_fields(
            idempotency_key=idempotency_key,
            plan_hash=plan_hash,
            strategy_release_hash=strategy_release_hash,
            feature_snapshot_id=feature_snapshot_id,
            candidate_id=candidate_id,
            symbol=symbol,
            side=side,
            quote_asset=quote_asset,
            quantity=quantity,
            created_at=created_at,
        )
        account = await uow.session.scalar(
            select(TradingAccount)
            .where(
                TradingAccount.workspace_id == uow.workspace_id,
                TradingAccount.id == account_id,
            )
            .with_for_update()
        )
        if account is None or account.workspace_id != uow.workspace_id:
            raise PaperLedgerConflict("Trading account không thuộc workspace hiện tại.")
        if account.mode != "PAPER":
            raise PaperLedgerConflict("Ledger chỉ nhận tài khoản PAPER.")

        existing = await uow.session.scalar(
            select(OrderIntentRecord).where(
                OrderIntentRecord.workspace_id == uow.workspace_id,
                OrderIntentRecord.account_id == account_id,
                OrderIntentRecord.idempotency_key == idempotency_key,
            )
        )
        values = {
            "plan_hash": plan_hash,
            "settings_version_id": settings_version_id,
            "strategy_release_id": strategy_release_id,
            "strategy_release_hash": strategy_release_hash,
            "feature_snapshot_id": feature_snapshot_id,
            "candidate_id": candidate_id,
            "risk_decision_id": risk_decision_id,
            "risk_reservation_id": risk_reservation_id,
            "symbol": symbol,
            "quote_asset": quote_asset,
            "side": side,
            "quantity": quantity,
        }
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in values.items()) or (
                existing.created_at != created_at or existing.evidence != (evidence or {})
            ):
                raise PaperLedgerConflict("Idempotency key đã được dùng với payload khác.")
            return IntentAppendResult(existing.id, duplicate=True)

        decision = await uow.session.scalar(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.workspace_id == uow.workspace_id,
                RiskDecisionRecord.account_id == account_id,
                RiskDecisionRecord.id == risk_decision_id,
            )
        )
        if decision is None or not decision.approved or decision.plan_hash != plan_hash:
            raise PaperLedgerConflict("Intent cần risk decision đã duyệt và khớp plan hash.")
        reservation = await uow.session.scalar(
            select(RiskReservationRecord)
            .where(
                RiskReservationRecord.workspace_id == uow.workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.id == risk_reservation_id,
            )
            .with_for_update()
        )
        if (
            reservation is None
            or reservation.status != "RESERVED"
            or reservation.risk_decision_id != risk_decision_id
            or reservation.plan_hash != plan_hash
        ):
            raise PaperLedgerConflict("Risk reservation phải RESERVED và khớp decision/plan.")
        now = self._clock()
        _validate_aware(now)
        expires_at = getattr(reservation, "expires_at", None)
        if not isinstance(expires_at, datetime):
            raise PaperLedgerConflict("Risk reservation thiếu thời điểm hết hạn đã lưu.")
        _validate_aware(expires_at)
        if now >= expires_at or created_at >= expires_at:
            raise PaperLedgerConflict("Risk reservation đã hết hạn trước khi tạo intent.")
        decision_payload = getattr(decision, "payload", None)
        candidate_expiry = (
            decision_payload.get("candidate_expires_at")
            if isinstance(decision_payload, dict)
            else None
        )
        if not isinstance(candidate_expiry, str):
            raise PaperLedgerConflict("Risk decision thiếu candidate expiry đã lưu.")
        try:
            decision_expiry = datetime.fromisoformat(candidate_expiry)
        except ValueError as error:
            raise PaperLedgerConflict(
                "Candidate expiry trong risk decision không hợp lệ."
            ) from error
        _validate_aware(decision_expiry)
        if decision_expiry != expires_at:
            raise PaperLedgerConflict("Risk decision và reservation khác thời điểm hết hạn.")

        plan = await uow.session.scalar(
            select(TradePlanRecord).where(
                TradePlanRecord.workspace_id == uow.workspace_id,
                TradePlanRecord.account_id == account_id,
                TradePlanRecord.plan_hash == plan_hash,
            )
        )
        release = await uow.session.scalar(
            select(StrategyRelease).where(
                StrategyRelease.workspace_id == uow.workspace_id,
                StrategyRelease.id == strategy_release_id,
            )
        )
        plan_payload = plan.payload if plan is not None else {}
        plan_mismatches: list[str] = []
        if plan is None:
            plan_mismatches.append("trade_plan_missing")
        if release is None:
            plan_mismatches.append("strategy_release_missing")
        elif release.status != "PUBLISHED":
            plan_mismatches.append("strategy_release_not_published")
        elif release.name != plan_payload.get("strategy_release_id"):
            plan_mismatches.append("strategy_release_id")
        if release is not None and release.content_hash != strategy_release_hash:
            plan_mismatches.append("strategy_release_hash")
        if plan is not None:
            if plan.symbol != symbol:
                plan_mismatches.append("symbol")
            if plan_payload.get("quantity") != str(quantity):
                plan_mismatches.append("quantity")
            if str(plan_payload.get("direction", "")).upper() != side:
                plan_mismatches.append("direction")
            if plan_payload.get("settings_version_id") != str(settings_version_id):
                plan_mismatches.append("settings_version_id")
            if "settings_hash" in plan_payload and plan_payload.get("settings_hash") != (
                evidence or {}
            ).get("settings_hash"):
                plan_mismatches.append("settings_hash")
            if "candidate_id" in plan_payload and plan_payload.get("candidate_id") != candidate_id:
                plan_mismatches.append("candidate_id")
            if plan_payload.get("release_content_hash") != strategy_release_hash:
                plan_mismatches.append("plan_release_hash")
        if plan_mismatches:
            fields = ", ".join(plan_mismatches)
            raise PaperLedgerConflict(
                "Intent khác quantity/symbol/lineage của TradePlan đã duyệt "
                f"(trường không khớp: {fields})."
            )

        intent_id = uuid4()
        row = OrderIntentRecord(
            id=intent_id,
            workspace_id=uow.workspace_id,
            account_id=account_id,
            mode="PAPER",
            idempotency_key=idempotency_key,
            created_at=created_at,
            filled_quantity=Decimal(0),
            status="ACCEPTED",
            evidence=evidence or {},
            **values,
        )
        uow.session.add(row)
        await uow.session.flush()
        uow.session.add(
            ExchangeOrderRecord(
                workspace_id=uow.workspace_id,
                account_id=account_id,
                mode="PAPER",
                intent_id=intent_id,
                client_order_id=f"paper-{intent_id}",
                exchange_order_id=None,
                status="NEW",
                raw_reference=None,
            )
        )
        uow.enqueue(
            EventEnvelope(
                event_type="paper.order_intent.accepted",
                workspace_id=uow.workspace_id,
                aggregate_type="order_intent",
                aggregate_id=intent_id,
                occurred_at=created_at,
                correlation_id=uow.request_id,
                idempotency_key=f"intent:{idempotency_key}",
                schema_version=1,
                payload={"account_id": str(account_id), "symbol": symbol, "side": side},
            )
        )
        await uow.session.flush()
        return IntentAppendResult(intent_id, duplicate=False)

    async def cancel_remainder(
        self, uow: UnitOfWork, *, intent_id: UUID, occurred_at: datetime
    ) -> bool:
        """Hủy phần chưa khớp và đồng bộ synthetic order trong một UoW."""
        intent = await self._owned_intent(uow, intent_id)
        _validate_aware(occurred_at)
        if intent.status == "CANCELLED":
            return False
        if intent.status not in {"ACCEPTED", "PARTIALLY_FILLED"}:
            raise PaperLedgerConflict("Chỉ intent còn mở mới được hủy phần dư.")
        order = await uow.session.scalar(
            select(ExchangeOrderRecord)
            .where(
                ExchangeOrderRecord.workspace_id == uow.workspace_id,
                ExchangeOrderRecord.account_id == intent.account_id,
                ExchangeOrderRecord.intent_id == intent_id,
            )
            .with_for_update()
        )
        if order is None or order.status not in {"NEW", "PARTIALLY_FILLED", "UNKNOWN"}:
            raise PaperLedgerConflict("Synthetic exchange order không ở trạng thái có thể hủy.")
        intent.status = "CANCELLED"
        order.status = "CANCELLED"
        await uow.session.flush()
        uow.enqueue(
            _paper_event(
                uow,
                "paper.order_intent.cancelled",
                intent_id,
                f"cancel:{intent_id}",
                occurred_at,
                {"filled_quantity": str(intent.filled_quantity)},
            )
        )
        return True

    async def record_order_transition(
        self,
        uow: UnitOfWork,
        *,
        intent_id: UUID,
        status: str,
        occurred_at: datetime,
    ) -> bool:
        """Persist trạng thái PAPER terminal/unknown của intent và synthetic order."""
        if status not in {"CANCELLED", "EXPIRED", "UNKNOWN"}:
            raise PaperLedgerConflict("Trạng thái order transition không được hỗ trợ.")
        _validate_aware(occurred_at)
        intent = await self._owned_intent(uow, intent_id)
        order = await uow.session.scalar(
            select(ExchangeOrderRecord)
            .where(
                ExchangeOrderRecord.workspace_id == uow.workspace_id,
                ExchangeOrderRecord.account_id == intent.account_id,
                ExchangeOrderRecord.intent_id == intent_id,
            )
            .with_for_update()
        )
        if order is None:
            raise PaperLedgerConflict("Không tìm thấy synthetic order của intent.")
        if intent.status == status and order.status == status:
            return False
        if intent.status == "UNKNOWN" or order.status == "UNKNOWN":
            raise PaperLedgerConflict("UNKNOWN chỉ được thay đổi qua reconcile có chứng cứ.")
        if intent.status not in {"ACCEPTED", "PARTIALLY_FILLED"}:
            raise PaperLedgerConflict("Intent không còn ở trạng thái có thể chuyển.")
        if order.status not in {"NEW", "PARTIALLY_FILLED"}:
            raise PaperLedgerConflict("Synthetic order không còn ở trạng thái có thể chuyển.")
        intent.status = status
        order.status = status
        await uow.session.flush()
        uow.enqueue(
            _paper_event(
                uow,
                f"paper.order_intent.{status.lower()}",
                intent_id,
                f"transition:{status.lower()}:{intent_id}",
                occurred_at,
                {"filled_quantity": str(intent.filled_quantity)},
            )
        )
        return True

    async def transfer_reservation_to_open_trade(
        self,
        uow: UnitOfWork,
        *,
        intent_id: UUID,
        trade_id: UUID,
    ) -> RiskReservation:
        """Chuyển reservation sang exposure OPEN trong cùng UnitOfWork."""
        from trading_bot.adapters.database.repositories.risk_reservations import (
            RiskReservationRepository,
        )

        intent = await self._owned_intent(uow, intent_id)
        return await RiskReservationRepository(uow.session).transfer_to_open_trade(
            workspace_id=uow.workspace_id,
            account_id=intent.account_id,
            reservation_id=intent.risk_reservation_id,
            intent_id=intent.id,
            trade_id=trade_id,
        )

    async def release_reservation_for_terminal_no_fill(
        self, uow: UnitOfWork, *, intent_id: UUID
    ) -> RiskReservation:
        """Giải phóng quota cho intent đã CANCELLED/EXPIRED mà không có fill."""
        from trading_bot.adapters.database.repositories.risk_reservations import (
            RiskReservationRepository,
        )

        intent = await self._owned_intent(uow, intent_id)
        return await RiskReservationRepository(uow.session).release_terminal_no_fill(
            workspace_id=uow.workspace_id,
            account_id=intent.account_id,
            reservation_id=intent.risk_reservation_id,
            intent_id=intent.id,
        )

    async def mark_reservation_unknown(
        self, uow: UnitOfWork, *, intent_id: UUID
    ) -> RiskReservation:
        """Giữ quota khi PAPER order có trạng thái chưa xác định."""
        from trading_bot.adapters.database.repositories.risk_reservations import (
            RiskReservationRepository,
        )

        intent = await self._owned_intent(uow, intent_id)
        return await RiskReservationRepository(uow.session).mark_unknown(
            workspace_id=uow.workspace_id,
            account_id=intent.account_id,
            reservation_id=intent.risk_reservation_id,
        )

    async def append_fill(
        self,
        uow: UnitOfWork,
        *,
        intent_id: UUID,
        fill_key: str,
        bar_key: str,
        fill_policy_version: str,
        quantity: Decimal,
        price: Decimal,
        fee_amount: Decimal,
        slippage_amount: Decimal,
        occurred_at: datetime,
    ) -> bool:
        """Append fill once and advance intent's filled quantity monotonically."""
        intent = await self._owned_intent(uow, intent_id)
        if not fill_key.strip() or not bar_key.strip() or not fill_policy_version.strip():
            raise PaperLedgerConflict("Fill cần khóa, bar và phiên bản fill policy.")
        if any(not value.is_finite() for value in (quantity, price, fee_amount, slippage_amount)):
            raise PaperLedgerConflict("Fill chứa giá trị Decimal không hữu hạn.")
        if quantity <= 0 or price <= 0 or fee_amount < 0 or slippage_amount < 0:
            raise PaperLedgerConflict("Giá/khối lượng phải dương; fee và slippage không âm.")
        _validate_aware(occurred_at)
        previous = await uow.session.scalar(
            select(FillRecord).where(
                FillRecord.workspace_id == uow.workspace_id,
                FillRecord.account_id == intent.account_id,
                FillRecord.fill_key == fill_key,
            )
        )
        if previous is not None:
            expected = (
                intent.id,
                bar_key,
                fill_policy_version,
                quantity,
                price,
                fee_amount,
                slippage_amount,
                occurred_at,
                _intent_lineage(intent),
            )
            actual = (
                previous.intent_id,
                previous.bar_key,
                previous.fill_policy_version,
                previous.quantity,
                previous.price,
                previous.fee_amount,
                previous.slippage_amount,
                previous.occurred_at,
                previous.lineage,
            )
            if actual != expected:
                raise PaperLedgerConflict("Fill key đã được dùng với payload khác.")
            return False
        new_filled = intent.filled_quantity + quantity
        if new_filled > intent.quantity or intent.status not in {"ACCEPTED", "PARTIALLY_FILLED"}:
            raise PaperLedgerConflict("Fill vượt khối lượng còn lại hoặc intent không mở.")
        order = await uow.session.scalar(
            select(ExchangeOrderRecord)
            .where(
                ExchangeOrderRecord.workspace_id == uow.workspace_id,
                ExchangeOrderRecord.account_id == intent.account_id,
                ExchangeOrderRecord.intent_id == intent_id,
            )
            .with_for_update()
        )
        if order is None or order.status not in {"NEW", "PARTIALLY_FILLED"}:
            raise PaperLedgerConflict("Synthetic exchange order không ở trạng thái có thể fill.")
        uow.session.add(
            FillRecord(
                workspace_id=uow.workspace_id,
                account_id=intent.account_id,
                mode="PAPER",
                intent_id=intent_id,
                fill_role="ENTRY",
                fill_key=fill_key,
                bar_key=bar_key,
                fill_policy_version=fill_policy_version,
                quantity=quantity,
                price=price,
                fee_amount=fee_amount,
                slippage_amount=slippage_amount,
                occurred_at=occurred_at,
                lineage=_intent_lineage(intent),
            )
        )
        intent.filled_quantity = new_filled
        intent.status = "FILLED" if new_filled == intent.quantity else "PARTIALLY_FILLED"
        order.status = intent.status
        await uow.session.flush()
        existing_trade = await uow.session.scalar(
            select(TradeRecord).where(
                TradeRecord.workspace_id == uow.workspace_id,
                TradeRecord.account_id == intent.account_id,
                TradeRecord.intent_id == intent_id,
            )
        )
        opened = await self.open_trade(
            uow,
            intent_id=intent_id,
            opened_at=existing_trade.opened_at if existing_trade is not None else occurred_at,
        )
        # Cột cost trên fill lưu chứng cứ đầu vào; trade_cost là nguồn duy nhất để cộng tổng.
        await self.append_trade_cost(
            uow,
            trade_id=opened.trade_id,
            cost_key=f"fill:{fill_key}:fee",
            cost_type="FEE",
            amount=fee_amount,
            currency=intent.quote_asset,
            source_event_id=fill_key,
            occurred_at=occurred_at,
        )
        await self.append_trade_cost(
            uow,
            trade_id=opened.trade_id,
            cost_key=f"fill:{fill_key}:slippage",
            cost_type="SLIPPAGE",
            amount=slippage_amount,
            currency=intent.quote_asset,
            source_event_id=fill_key,
            occurred_at=occurred_at,
        )
        uow.enqueue(
            _paper_event(
                uow,
                "paper.fill.recorded",
                intent_id,
                fill_key,
                occurred_at,
                {"quantity": str(quantity), "price": str(price), "bar_key": bar_key},
            )
        )
        return True

    async def append_exit_fill(
        self,
        uow: UnitOfWork,
        *,
        intent_id: UUID,
        fill_key: str,
        bar_key: str,
        fill_policy_version: str,
        quantity: Decimal,
        price: Decimal,
        fee_amount: Decimal,
        slippage_amount: Decimal,
        occurred_at: datetime,
    ) -> bool:
        """Ghi exit fill vào trade đang mở mà không sửa khối lượng entry của intent."""
        intent = await self._owned_intent(uow, intent_id)
        if not fill_key.strip() or not bar_key.strip() or not fill_policy_version.strip():
            raise PaperLedgerConflict("Exit fill cần khóa, bar và phiên bản fill policy.")
        if any(not value.is_finite() for value in (quantity, price, fee_amount, slippage_amount)):
            raise PaperLedgerConflict("Exit fill chứa giá trị Decimal không hữu hạn.")
        if quantity <= 0 or price <= 0 or fee_amount < 0 or slippage_amount < 0:
            raise PaperLedgerConflict("Giá/khối lượng phải dương; fee và slippage không âm.")
        _validate_aware(occurred_at)
        previous = await uow.session.scalar(
            select(FillRecord).where(
                FillRecord.workspace_id == uow.workspace_id,
                FillRecord.account_id == intent.account_id,
                FillRecord.fill_key == fill_key,
            )
        )
        if previous is not None:
            expected = (
                intent.id,
                "EXIT",
                bar_key,
                fill_policy_version,
                quantity,
                price,
                fee_amount,
                slippage_amount,
                occurred_at,
                _intent_lineage(intent),
            )
            actual = (
                previous.intent_id,
                previous.fill_role,
                previous.bar_key,
                previous.fill_policy_version,
                previous.quantity,
                previous.price,
                previous.fee_amount,
                previous.slippage_amount,
                previous.occurred_at,
                previous.lineage,
            )
            if actual != expected:
                raise PaperLedgerConflict("Fill key đã được dùng với payload khác.")
            return False

        trade = await uow.session.scalar(
            select(TradeRecord)
            .where(
                TradeRecord.workspace_id == uow.workspace_id,
                TradeRecord.account_id == intent.account_id,
                TradeRecord.intent_id == intent_id,
            )
            .with_for_update()
        )
        if trade is None or trade.status != "OPEN":
            raise PaperLedgerConflict("Exit fill cần trade PAPER đang mở.")
        fills_result = await uow.session.scalars(
            select(FillRecord).where(
                FillRecord.workspace_id == uow.workspace_id,
                FillRecord.account_id == intent.account_id,
                FillRecord.intent_id == intent_id,
            )
        )
        fills = list(fills_result.all())
        remaining_quantity = sum(
            (fill.quantity if fill.fill_role == "ENTRY" else -fill.quantity for fill in fills),
            Decimal("0"),
        )
        if quantity > remaining_quantity:
            raise PaperLedgerConflict("Exit fill vượt lượng vị thế còn mở.")
        new_remaining = remaining_quantity - quantity
        uow.session.add(
            FillRecord(
                workspace_id=uow.workspace_id,
                account_id=intent.account_id,
                mode="PAPER",
                intent_id=intent_id,
                fill_role="EXIT",
                fill_key=fill_key,
                bar_key=bar_key,
                fill_policy_version=fill_policy_version,
                quantity=quantity,
                price=price,
                fee_amount=fee_amount,
                slippage_amount=slippage_amount,
                occurred_at=occurred_at,
                lineage=_intent_lineage(intent),
            )
        )
        await uow.session.flush()
        await self.append_trade_cost(
            uow,
            trade_id=trade.id,
            cost_key=f"fill:{fill_key}:fee",
            cost_type="FEE",
            amount=fee_amount,
            currency=intent.quote_asset,
            source_event_id=fill_key,
            occurred_at=occurred_at,
        )
        await self.append_trade_cost(
            uow,
            trade_id=trade.id,
            cost_key=f"fill:{fill_key}:slippage",
            cost_type="SLIPPAGE",
            amount=slippage_amount,
            currency=intent.quote_asset,
            source_event_id=fill_key,
            occurred_at=occurred_at,
        )
        await self.append_position_event(
            uow,
            intent_id=intent_id,
            event_key=f"exit-fill:{fill_key}",
            event_type="CLOSED" if new_remaining == 0 else "REDUCED",
            quantity=new_remaining,
            occurred_at=occurred_at,
            payload={"fill_key": fill_key, "closed_quantity": str(quantity)},
        )
        uow.enqueue(
            _paper_event(
                uow,
                "paper.exit_fill.recorded",
                intent_id,
                fill_key,
                occurred_at,
                {"quantity": str(quantity), "price": str(price), "bar_key": bar_key},
            )
        )
        return True

    async def append_position_event(
        self,
        uow: UnitOfWork,
        *,
        intent_id: UUID,
        event_key: str,
        event_type: str,
        quantity: Decimal,
        occurred_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Append transition event by its idempotency key."""
        intent = await self._owned_intent(uow, intent_id)
        allowed = {"OPENED", "INCREASED", "REDUCED", "CLOSED", "STOP_UPDATED"}
        if (
            event_type not in allowed
            or not event_key.strip()
            or not quantity.is_finite()
            or quantity < 0
        ):
            raise PaperLedgerConflict("Position event không hợp lệ.")
        _validate_aware(occurred_at)
        existing = await uow.session.scalar(
            select(PositionEventRecord).where(
                PositionEventRecord.workspace_id == uow.workspace_id,
                PositionEventRecord.account_id == intent.account_id,
                PositionEventRecord.event_key == event_key,
            )
        )
        if existing is not None:
            if (
                existing.intent_id,
                existing.event_type,
                existing.quantity,
                existing.occurred_at,
                existing.payload,
                existing.lineage,
            ) != (
                intent_id,
                event_type,
                quantity,
                occurred_at,
                payload or {},
                _intent_lineage(intent),
            ):
                raise PaperLedgerConflict("Position event key đã có nội dung khác.")
            return False
        uow.session.add(
            PositionEventRecord(
                workspace_id=uow.workspace_id,
                account_id=intent.account_id,
                mode="PAPER",
                intent_id=intent_id,
                event_key=event_key,
                event_type=event_type,
                quantity=quantity,
                occurred_at=occurred_at,
                payload=payload or {},
                lineage=_intent_lineage(intent),
            )
        )
        await uow.session.flush()
        uow.enqueue(
            _paper_event(
                uow,
                "paper.position.transition",
                intent_id,
                event_key,
                occurred_at,
                {"type": event_type},
            )
        )
        return True

    async def open_trade(
        self, uow: UnitOfWork, *, intent_id: UUID, opened_at: datetime
    ) -> TradeAppendResult:
        """Tạo một trade cho intent đã có fill, sao chép lineage nguyên vẹn."""
        intent = await self._owned_intent(uow, intent_id)
        _validate_aware(opened_at)
        if intent.filled_quantity <= 0:
            raise PaperLedgerConflict("Không mở trade khi intent chưa có fill.")
        existing = await uow.session.scalar(
            select(TradeRecord).where(
                TradeRecord.workspace_id == uow.workspace_id,
                TradeRecord.account_id == intent.account_id,
                TradeRecord.intent_id == intent_id,
            )
        )
        if existing is not None:
            if existing.opened_at != opened_at or existing.lineage != _intent_lineage(intent):
                raise PaperLedgerConflict("Trade intent đã được mở với thời điểm/lineage khác.")
            return TradeAppendResult(existing.id, duplicate=True)
        trade_id = uuid4()
        row = TradeRecord(
            id=trade_id,
            workspace_id=uow.workspace_id,
            account_id=intent.account_id,
            mode="PAPER",
            intent_id=intent.id,
            status="OPEN",
            symbol=intent.symbol,
            quote_asset=intent.quote_asset,
            side=intent.side,
            settings_version_id=intent.settings_version_id,
            strategy_release_id=intent.strategy_release_id,
            strategy_release_hash=intent.strategy_release_hash,
            feature_snapshot_id=intent.feature_snapshot_id,
            candidate_id=intent.candidate_id,
            risk_decision_id=intent.risk_decision_id,
            risk_reservation_id=intent.risk_reservation_id,
            gross_pnl=Decimal(0),
            fee_total=Decimal(0),
            funding_total=Decimal(0),
            slippage_total=Decimal(0),
            total_cost=Decimal(0),
            net_pnl=Decimal(0),
            opened_at=opened_at,
            closed_at=None,
            lineage=_intent_lineage(intent),
        )
        uow.session.add(row)
        await uow.session.flush()
        return TradeAppendResult(trade_id, duplicate=False)

    async def append_trade_cost(
        self,
        uow: UnitOfWork,
        *,
        trade_id: UUID,
        cost_key: str,
        cost_type: str,
        amount: Decimal,
        currency: str,
        source_event_id: str,
        occurred_at: datetime,
    ) -> bool:
        """Append cost once; negative amount is allowed only for funding credit."""
        trade = await uow.session.scalar(
            select(TradeRecord)
            .where(
                TradeRecord.workspace_id == uow.workspace_id,
                TradeRecord.id == trade_id,
            )
            .with_for_update()
        )
        if trade is None:
            raise PaperLedgerConflict("Không tìm thấy trade trong workspace.")
        if trade.status != "OPEN":
            raise PaperLedgerConflict("Không thể ghi chi phí sau khi trade đóng.")
        if currency != trade.quote_asset:
            raise PaperLedgerConflict("Chi phí phải chuẩn hóa sang quote asset của trade.")
        if cost_type not in {"FEE", "FUNDING", "SLIPPAGE"} or not amount.is_finite():
            raise PaperLedgerConflict("Loại chi phí hoặc số tiền không hợp lệ.")
        if amount < 0 and cost_type != "FUNDING":
            raise PaperLedgerConflict("Chỉ funding credit mới được ghi số âm.")
        if not all(value.strip() for value in (cost_key, currency, source_event_id)):
            raise PaperLedgerConflict("Chi phí phải có khóa, tiền tệ và nguồn.")
        _validate_aware(occurred_at)
        existing = await uow.session.scalar(
            select(TradeCostRecord).where(
                TradeCostRecord.workspace_id == uow.workspace_id,
                TradeCostRecord.account_id == trade.account_id,
                TradeCostRecord.cost_key == cost_key,
            )
        )
        if existing is not None:
            expected = (
                trade_id,
                cost_type,
                amount,
                currency,
                source_event_id,
                occurred_at,
                dict(trade.lineage),
            )
            actual = (
                existing.trade_id,
                existing.cost_type,
                existing.amount,
                existing.currency,
                existing.source_event_id,
                existing.occurred_at,
                existing.lineage,
            )
            if actual != expected:
                raise PaperLedgerConflict("Cost key đã được dùng với payload khác.")
            return False
        uow.session.add(
            TradeCostRecord(
                workspace_id=uow.workspace_id,
                account_id=trade.account_id,
                mode="PAPER",
                trade_id=trade_id,
                cost_key=cost_key,
                cost_type=cost_type,
                amount=amount,
                currency=currency,
                occurred_at=occurred_at,
                source_event_id=source_event_id,
                lineage=dict(trade.lineage),
            )
        )
        await uow.session.flush()
        return True

    async def close_trade(
        self,
        uow: UnitOfWork,
        *,
        trade_id: UUID,
        gross_pnl: Decimal,
        closed_at: datetime,
    ) -> TradeRecord:
        """Tính cost theo thành phần persisted rồi đóng trade đúng một lần."""
        trade = await uow.session.scalar(
            select(TradeRecord)
            .where(
                TradeRecord.workspace_id == uow.workspace_id,
                TradeRecord.id == trade_id,
            )
            .with_for_update()
        )
        if trade is None or trade.status != "OPEN":
            raise PaperLedgerConflict("Trade không tồn tại hoặc đã đóng.")
        if not gross_pnl.is_finite():
            raise PaperLedgerConflict("Gross PnL phải hữu hạn.")
        _validate_aware(closed_at)
        costs_result = await uow.session.scalars(
            select(TradeCostRecord).where(
                TradeCostRecord.workspace_id == uow.workspace_id,
                TradeCostRecord.account_id == trade.account_id,
                TradeCostRecord.trade_id == trade_id,
            )
        )
        costs = list(costs_result.all())
        fee, funding, slippage = sum_cost_components(costs, quote_asset=trade.quote_asset)
        total = fee + funding + slippage
        trade.gross_pnl = gross_pnl
        trade.fee_total = fee
        trade.funding_total = funding
        trade.slippage_total = slippage
        trade.total_cost = total
        trade.net_pnl = gross_pnl - total
        trade.closed_at = closed_at
        trade.status = "CLOSED"
        await uow.session.flush()
        uow.enqueue(
            _paper_event(
                uow,
                "paper.trade.closed",
                trade_id,
                f"trade-close:{trade_id}",
                closed_at,
                {
                    "gross_pnl": str(gross_pnl),
                    "total_cost": str(total),
                    "net_pnl": str(gross_pnl - total),
                },
            )
        )
        return trade

    async def _owned_intent(self, uow: UnitOfWork, intent_id: UUID) -> OrderIntentRecord:
        intent = await uow.session.scalar(
            select(OrderIntentRecord)
            .where(
                OrderIntentRecord.workspace_id == uow.workspace_id,
                OrderIntentRecord.id == intent_id,
            )
            .with_for_update()
        )
        if intent is None:
            raise PaperLedgerConflict("Intent không tồn tại trong workspace.")
        return intent


@dataclass(frozen=True, slots=True)
class IntentAppendResult:
    """Định danh intent và chỉ báo lần ghi có phải retry hay không."""

    intent_id: UUID
    duplicate: bool


@dataclass(frozen=True, slots=True)
class TradeAppendResult:
    """Định danh trade PAPER và trạng thái retry."""

    trade_id: UUID
    duplicate: bool


def _intent_lineage(intent: OrderIntentRecord) -> dict[str, Any]:
    lineage = {
        "settings_version_id": str(intent.settings_version_id),
        "strategy_release_id": str(intent.strategy_release_id),
        "strategy_release_hash": intent.strategy_release_hash,
        "feature_snapshot_id": intent.feature_snapshot_id,
        "candidate_id": intent.candidate_id,
        "quote_asset": intent.quote_asset,
        "risk_decision_id": str(intent.risk_decision_id),
        "risk_reservation_id": str(intent.risk_reservation_id),
        "plan_hash": intent.plan_hash,
    }
    profile_hash = intent.evidence.get("paper_execution_profile_hash")
    if isinstance(profile_hash, str) and profile_hash:
        lineage["paper_execution_profile_hash"] = profile_hash
    return lineage


def _paper_event(
    uow: UnitOfWork,
    event_type: str,
    aggregate_id: UUID,
    idempotency_key: str,
    occurred_at: datetime,
    payload: dict[str, Any],
) -> EventEnvelope:
    return EventEnvelope(
        event_type=event_type,
        workspace_id=uow.workspace_id,
        aggregate_type="paper_ledger",
        aggregate_id=aggregate_id,
        occurred_at=occurred_at,
        correlation_id=uow.request_id,
        idempotency_key=idempotency_key,
        schema_version=1,
        payload=payload,
    )


def _validate_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PaperLedgerConflict("Mốc thời gian cần có timezone.")


def sum_cost_components(
    costs: list[TradeCostRecord], *, quote_asset: str
) -> tuple[Decimal, Decimal, Decimal]:
    """Cộng đúng một lần từng loại cost; funding âm biểu thị khoản nhận được."""
    totals = {"FEE": Decimal(0), "FUNDING": Decimal(0), "SLIPPAGE": Decimal(0)}
    for cost in costs:
        if cost.cost_type not in totals or not cost.amount.is_finite():
            raise PaperLedgerConflict("Ledger có loại hoặc số tiền chi phí không hợp lệ.")
        if cost.amount < 0 and cost.cost_type != "FUNDING":
            raise PaperLedgerConflict("Chỉ funding credit mới được âm.")
        if cost.currency != quote_asset:
            raise PaperLedgerConflict("Không thể cộng chi phí có quote currency khác nhau.")
        totals[cost.cost_type] += cost.amount
    return totals["FEE"], totals["FUNDING"], totals["SLIPPAGE"]


def _validate_intent_fields(
    *,
    idempotency_key: str,
    plan_hash: str,
    strategy_release_hash: str,
    feature_snapshot_id: str,
    candidate_id: str,
    symbol: str,
    side: str,
    quote_asset: str,
    quantity: Decimal,
    created_at: datetime,
) -> None:
    if not idempotency_key.strip() or len(idempotency_key) > 240:
        raise PaperLedgerConflict("Idempotency key trống hoặc vượt giới hạn.")
    if any(
        len(value) != 64
        for value in (plan_hash, strategy_release_hash, feature_snapshot_id, candidate_id)
    ):
        raise PaperLedgerConflict("Các hash plan/strategy/feature/candidate phải đủ 64 ký tự.")
    if not symbol.strip() or side not in {"LONG", "SHORT"}:
        raise PaperLedgerConflict("Symbol hoặc hướng vị thế không hợp lệ.")
    if not quote_asset.isalnum() or not quote_asset.isupper() or len(quote_asset) > 16:
        raise PaperLedgerConflict("Quote asset cần là mã viết hoa hợp lệ.")
    if not quantity.is_finite() or quantity <= 0:
        raise PaperLedgerConflict("Khối lượng intent phải dương và hữu hạn.")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise PaperLedgerConflict("Thời điểm intent phải có timezone.")
