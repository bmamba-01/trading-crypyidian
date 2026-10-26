"""Điều phối vòng đời vị thế PAPER giữa gateway và ledger bền vững."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from trading_bot.application.paper_recovery import PaperRecoveryConflict
from trading_bot.domain.execution.paper import (
    IntentConflict,
    OrderStatus,
    PaperBar,
    PaperExecutionGateway,
    PaperFill,
    PaperOrderIntent,
    PaperPosition,
)
from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot
from trading_bot.domain.execution.position_monitor import PositionMonitorResult
from trading_bot.domain.execution.runtime_control import FencingToken
from trading_bot.domain.risk.planner import TradePlan

if TYPE_CHECKING:
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointRepository,
    )
    from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerRepository
    from trading_bot.application.outbox import UnitOfWork


class PaperLifecycleConflict(RuntimeError):
    """Bất nhất giữa trạng thái PAPER trong bộ nhớ và dữ liệu cần ghi ledger."""


@dataclass(frozen=True, slots=True)
class EntryLifecycleResult:
    """Kết quả ghi nhận một market event cho order entry PAPER."""

    order: PaperOrderIntent
    position: PaperPosition | None
    trade_id: UUID | None
    persisted_fill_count: int


@dataclass(frozen=True, slots=True)
class PaperSubmissionResult:
    """Kết quả tạo PAPER intent và checkpoint trong cùng transaction."""

    order: PaperOrderIntent
    ledger_intent_id: UUID
    duplicate: bool


@dataclass(frozen=True, slots=True)
class PositionLifecycleResult:
    """Kết quả monitor vị thế và đồng bộ fill/transition sang ledger."""

    position: PaperPosition
    exit_fill: PaperFill | None
    exit_fill_appended: bool
    trade_closed: bool
    monitor_result: PositionMonitorResult


class PaperLifecycleCoordinator:
    """Nối state machine PAPER với ledger, không tự quản lý transaction."""

    def __init__(
        self,
        gateway: PaperExecutionGateway,
        ledger: PaperLedgerRepository,
        checkpoints: PaperCheckpointRepository | None = None,
    ) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self._checkpoints = checkpoints

    async def hydrate_account(self, uow: UnitOfWork, *, account_id: UUID) -> None:
        """Nạp snapshot account sau khi transaction đọc đã commit thành công."""
        if self._checkpoints is None:
            raise PaperLifecycleConflict("Chưa cấu hình PAPER checkpoint repository.")
        await self._checkpoints.lock_account(uow, account_id=account_id)
        latest = await self._checkpoints.load_latest(uow, account_id=account_id)
        if latest is None:
            raise PaperLifecycleConflict("Không có checkpoint PAPER để hydrate.")
        _, snapshot = latest
        restored = await self._restore_verified(uow, account_id=account_id, snapshot=snapshot)
        uow.after_commit(lambda: self._gateway.replace_state(restored.export_state()))

    async def _restore_verified(
        self, uow: UnitOfWork, *, account_id: UUID, snapshot: PaperExecutionSnapshot
    ) -> PaperExecutionGateway:
        """Kiểm schema/policy và đối soát ledger trước khi tạo candidate gateway."""
        if self._checkpoints is None:
            raise PaperLifecycleConflict("Chưa cấu hình PAPER checkpoint repository.")
        try:
            restored = PaperExecutionGateway.restore(self._gateway.policy, snapshot)
        except IntentConflict as error:
            raise PaperLifecycleConflict("Checkpoint PAPER sai policy hoặc schema.") from error
        try:
            await self._checkpoints.verify_ledger_consistency(
                uow, account_id=account_id, snapshot=snapshot
            )
        except PaperRecoveryConflict as error:
            raise PaperLifecycleConflict(
                "Checkpoint PAPER không khớp ledger; giữ account ở trạng thái fail-closed."
            ) from error
        return restored

    async def submit_approved(
        self,
        uow: UnitOfWork,
        *,
        account_id: UUID,
        result: object,
        evaluated_at: datetime,
        idempotency_key: str,
        strategy_release_id: UUID,
        feature_snapshot_id: str,
        risk_decision_id: UUID,
        risk_reservation_id: UUID,
        quote_asset: str,
        evidence: dict[str, object],
        fencing_token: FencingToken | None = None,
    ) -> PaperSubmissionResult:
        """Ghi intent/checkpoint nguyên tử; state ứng viên chỉ công bố sau commit."""
        if self._checkpoints is None:
            raise PaperLifecycleConflict("Chưa cấu hình PAPER checkpoint repository.")
        plan = getattr(result, "plan", None)
        strategy_result = getattr(result, "strategy_result", None)
        risk_decision = getattr(result, "risk_decision", None)
        outcome = getattr(getattr(result, "outcome", None), "value", None)
        if (
            outcome != "RISK_APPROVED"
            or not isinstance(plan, TradePlan)
            or not bool(getattr(risk_decision, "approved", False))
            or strategy_result is None
            or not idempotency_key.strip()
        ):
            raise PaperLifecycleConflict("Chỉ risk-approved plan hợp lệ mới được submit PAPER.")
        await self._checkpoints.lock_account(uow, account_id=account_id)
        latest = await self._checkpoints.load_latest(uow, account_id=account_id)
        if latest is None:
            candidate = PaperExecutionGateway(self._gateway.policy)
            if self._gateway.export_state() != candidate.export_state():
                raise PaperLifecycleConflict(
                    "Không có checkpoint nhưng gateway có state; từ chối ghi đè lineage."
                )
        else:
            _, latest_snapshot = latest
            candidate = await self._restore_verified(
                uow, account_id=account_id, snapshot=latest_snapshot
            )
        order = candidate.submit(
            plan,
            idempotency_key,
            submitted_at=evaluated_at,
            eligible_after=getattr(strategy_result, "eligible_after", None),
        )
        intent = await self._ledger.create_intent(
            uow,
            account_id=account_id,
            idempotency_key=idempotency_key,
            plan_hash=plan.plan_hash,
            settings_version_id=UUID(str(plan.settings_version_id)),
            strategy_release_id=strategy_release_id,
            strategy_release_hash=str(plan.release_content_hash),
            feature_snapshot_id=feature_snapshot_id,
            candidate_id=str(plan.candidate_id),
            risk_decision_id=risk_decision_id,
            risk_reservation_id=risk_reservation_id,
            symbol=plan.symbol,
            side=plan.direction.upper(),
            quote_asset=quote_asset,
            quantity=plan.quantity,
            created_at=evaluated_at,
            evidence=evidence,
        )
        snapshot = candidate.export_state()
        event_hash = hashlib.sha256(
            f"{idempotency_key}:{plan.plan_hash}:{evaluated_at.isoformat()}".encode()
        ).hexdigest()
        append_result = await self._checkpoints.append(
            uow,
            account_id=account_id,
            event_key=f"submit:{idempotency_key}",
            event_hash=event_hash,
            snapshot=snapshot,
            occurred_at=evaluated_at,
            event_scope=None,
            fencing_token=fencing_token,
        )
        uow.after_commit(lambda: self._gateway.replace_state(append_result.snapshot))
        return PaperSubmissionResult(order, intent.intent_id, intent.duplicate)

    async def apply_entry_market_event(
        self,
        uow: UnitOfWork,
        *,
        paper_intent_id: str,
        ledger_intent_id: UUID,
        bar: PaperBar,
        available_quantity: Decimal | None = None,
        account_id: UUID | None = None,
        fencing_token: FencingToken | None = None,
    ) -> EntryLifecycleResult:
        """Khớp lệnh PAPER và ghi mọi fill/position event theo khóa ổn định.

        Caller phải bao toàn bộ thao tác bằng cùng một UnitOfWork để fill,
        transition và trade cùng commit hoặc rollback.
        """
        if self._checkpoints is not None:
            if account_id is None:
                raise PaperLifecycleConflict("Checkpoint lifecycle cần account_id.")
            await self._checkpoints.lock_account(uow, account_id=account_id)
            latest = await self._checkpoints.load_latest(uow, account_id=account_id)
            if latest is None:
                raise PaperLifecycleConflict("PAPER account chưa có checkpoint để tiếp tục.")
            _, current_snapshot = latest
            candidate = await self._restore_verified(
                uow, account_id=account_id, snapshot=current_snapshot
            )
            candidate_coordinator = PaperLifecycleCoordinator(candidate, self._ledger)
            result = await candidate_coordinator.apply_entry_market_event(
                uow,
                paper_intent_id=paper_intent_id,
                ledger_intent_id=ledger_intent_id,
                bar=bar,
                available_quantity=available_quantity,
            )
            snapshot = candidate.export_state()
            append_result = await self._checkpoints.append(
                uow,
                account_id=account_id,
                event_key=_market_event_key("market", paper_intent_id, bar),
                event_hash=_market_bar_hash(bar),
                snapshot=snapshot,
                occurred_at=bar.opened_at,
                event_scope=_market_event_scope(paper_intent_id, bar.symbol),
                fencing_token=fencing_token,
            )
            uow.after_commit(lambda: self._gateway.replace_state(append_result.snapshot))
            return result

        self._gateway.order(paper_intent_id)
        orders = self._gateway.apply_market_event(
            bar, available_quantity=available_quantity, intent_id=paper_intent_id
        )
        order = next((item for item in orders if item.intent_id == paper_intent_id), None)
        if order is None:
            raise PaperLifecycleConflict("Market event không trả về paper intent được yêu cầu.")
        if order.status not in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }:
            raise PaperLifecycleConflict("Paper order có trạng thái không được hỗ trợ.")

        persisted_count = 0
        trade_id: UUID | None = None
        prior_quantity = Decimal(0)
        first_fill_time: datetime | None = None
        for fill_index, fill in enumerate(order.fills):
            first_fill_time = first_fill_time or fill.filled_at
            inserted = await self._ledger.append_fill(
                uow,
                intent_id=ledger_intent_id,
                fill_key=fill.fill_id,
                bar_key=fill.bar_key,
                fill_policy_version=fill.fill_policy_version,
                quantity=fill.quantity,
                price=fill.execution_price,
                fee_amount=fill.fee_cost,
                slippage_amount=fill.slippage_cost,
                occurred_at=fill.filled_at,
            )
            persisted_count += int(inserted)
            prior_quantity += fill.quantity
            await self._ledger.append_position_event(
                uow,
                intent_id=ledger_intent_id,
                event_key=f"entry-fill:{fill.fill_id}",
                event_type="OPENED" if fill_index == 0 else "INCREASED",
                quantity=prior_quantity,
                occurred_at=fill.filled_at,
                payload={
                    "bar_key": fill.bar_key,
                    "fill_key": fill.fill_id,
                    "fill_policy_version": fill.fill_policy_version,
                },
            )
        if first_fill_time is not None:
            trade = await self._ledger.open_trade(
                uow, intent_id=ledger_intent_id, opened_at=first_fill_time
            )
            trade_id = trade.trade_id
        if order.status in {
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }:
            await self._ledger.record_order_transition(
                uow,
                intent_id=ledger_intent_id,
                status=order.status.value,
                occurred_at=bar.closed_at,
            )
        if trade_id is not None and persisted_count > 0:
            await self._ledger.transfer_reservation_to_open_trade(
                uow, intent_id=ledger_intent_id, trade_id=trade_id
            )
        elif order.status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
            await self._ledger.release_reservation_for_terminal_no_fill(
                uow, intent_id=ledger_intent_id
            )
        elif order.status is OrderStatus.UNKNOWN:
            await self._ledger.mark_reservation_unknown(uow, intent_id=ledger_intent_id)
        return EntryLifecycleResult(
            order=order,
            position=self._gateway.position(paper_intent_id),
            trade_id=trade_id,
            persisted_fill_count=persisted_count,
        )

    async def monitor_market_event(
        self,
        uow: UnitOfWork,
        *,
        paper_position_id: str,
        ledger_intent_id: UUID,
        trade_id: UUID,
        position: PaperPosition,
        bar: PaperBar,
        opposing_signal: bool = False,
        atr: Decimal | None = None,
        account_id: UUID | None = None,
        fencing_token: FencingToken | None = None,
    ) -> PositionLifecycleResult:
        """Monitor stop/target/trailing và ghi EXIT fill, transition, đóng trade.

        `position` là snapshot trước event; caller giữ snapshot này cho retry cùng
        bar để gateway tái tạo chính xác fingerprint của close idempotent.
        """
        if self._checkpoints is not None:
            if account_id is None:
                raise PaperLifecycleConflict("Checkpoint lifecycle cần account_id.")
            await self._checkpoints.lock_account(uow, account_id=account_id)
            latest = await self._checkpoints.load_latest(uow, account_id=account_id)
            if latest is None:
                raise PaperLifecycleConflict("PAPER account chưa có checkpoint để tiếp tục.")
            _, current_snapshot = latest
            candidate = await self._restore_verified(
                uow, account_id=account_id, snapshot=current_snapshot
            )
            candidate_coordinator = PaperLifecycleCoordinator(candidate, self._ledger)
            candidate_position = candidate.position(paper_position_id)
            if candidate_position is None:
                raise PaperLifecycleConflict("Checkpoint không có position cần giám sát.")
            candidate_result = await candidate_coordinator.monitor_market_event(
                uow,
                paper_position_id=paper_position_id,
                ledger_intent_id=ledger_intent_id,
                trade_id=trade_id,
                position=candidate_position,
                bar=bar,
                opposing_signal=opposing_signal,
                atr=atr,
            )
            snapshot = candidate.export_state()
            append_result = await self._checkpoints.append(
                uow,
                account_id=account_id,
                event_key=_market_event_key("monitor", paper_position_id, bar),
                event_hash=_market_bar_hash(bar),
                snapshot=snapshot,
                occurred_at=bar.opened_at,
                event_scope=_market_event_scope(paper_position_id, bar.symbol),
                fencing_token=fencing_token,
            )
            uow.after_commit(lambda: self._gateway.replace_state(append_result.snapshot))
            return candidate_result

        if position.position_id != paper_position_id:
            raise PaperLifecycleConflict("Position ID không khớp snapshot được cung cấp.")
        result = self._gateway.monitor_position(
            position, bar, opposing_signal=opposing_signal, atr=atr
        )
        if result.exit_reason is None:
            if result.position.stop_price != position.stop_price:
                await self._ledger.append_position_event(
                    uow,
                    intent_id=ledger_intent_id,
                    event_key=f"stop-update:{position.position_id}:{bar.bar_key}",
                    event_type="STOP_UPDATED",
                    quantity=result.position.quantity,
                    occurred_at=bar.closed_at,
                    payload={
                        "bar_key": bar.bar_key,
                        "stop_price": str(result.position.stop_price),
                    },
                )
            return PositionLifecycleResult(result.position, None, False, False, result)

        if result.exit_reference_price is None:
            raise PaperLifecycleConflict("Exit reason cần giá tham chiếu để tạo fill.")
        close_key = f"exit:{position.position_id}:{bar.bar_key}:{result.exit_reason}"
        receipt = self._gateway.close_position(
            position.position_id,
            position.quantity,
            reference_price=result.exit_reference_price,
            closed_at=bar.closed_at,
            idempotency_key=close_key,
        )
        fill = receipt.fill
        if fill is None:
            raise PaperLifecycleConflict("Gateway đóng vị thế nhưng không tạo exit fill.")
        appended = await self._ledger.append_exit_fill(
            uow,
            intent_id=ledger_intent_id,
            fill_key=fill.fill_id,
            bar_key=bar.bar_key,
            fill_policy_version=fill.fill_policy_version,
            quantity=fill.quantity,
            price=fill.execution_price,
            fee_amount=fill.fee_cost,
            slippage_amount=fill.slippage_cost,
            occurred_at=fill.filled_at,
        )
        trade_closed = False
        if appended and receipt.position.is_closed:
            await self._ledger.close_trade(
                uow,
                trade_id=trade_id,
                gross_pnl=receipt.position.gross_realized_pnl,
                closed_at=bar.closed_at,
            )
            trade_closed = True
        return PositionLifecycleResult(result.position, fill, appended, trade_closed, result)


def _market_bar_hash(bar: PaperBar) -> str:
    """Tạo hash xác định từ payload nến để bảo vệ khóa retry."""
    payload = {
        "symbol": bar.symbol,
        "bar_key": bar.bar_key,
        "opened_at": bar.opened_at.isoformat(),
        "closed_at": bar.closed_at.isoformat(),
        "available_at": bar.available_at.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _market_event_scope(intent_id: str, symbol: str) -> str:
    """Tạo khóa scope ngắn, ổn định cho cursor theo intent và symbol."""
    return hashlib.sha256(f"{intent_id}:{symbol}".encode()).hexdigest()


def _market_event_key(kind: str, intent_id: str, bar: PaperBar) -> str:
    """Tạo event key ngắn từ tuple đầy đủ để không vượt giới hạn DB."""
    identity = json.dumps([kind, intent_id, bar.symbol, bar.bar_key], separators=(",", ":"))
    return f"paper:v1:{kind}:{hashlib.sha256(identity.encode()).hexdigest()}"
