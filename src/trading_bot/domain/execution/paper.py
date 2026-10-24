"""Mô phỏng lệnh PAPER thuần, xác định và không kết nối sàn."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from trading_bot.domain.risk.planner import TradePlan
from trading_bot.domain.strategy.recipes import ExitRecipe

if TYPE_CHECKING:
    from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot
    from trading_bot.domain.execution.position_monitor import PositionMonitorResult

ZERO = Decimal("0")
ONE = Decimal("1")


class IntentConflict(RuntimeError):
    """Lệnh PAPER không hợp lệ hoặc khóa idempotency bị dùng sai."""


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PaperExecutionPolicy:
    """Bộ giả định khớp lệnh bất biến, có version và chỉ dành cho PAPER."""

    version_id: str
    latency: timedelta
    spread_rate: Decimal
    slippage_rate: Decimal
    fee_rate: Decimal
    max_fill_quantity_per_event: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.version_id.strip() or self.latency < timedelta(0):
            raise ValueError("Fill policy cần version và latency không âm.")
        values = (self.spread_rate, self.slippage_rate, self.fee_rate)
        if any(not value.is_finite() or value < ZERO for value in values):
            raise ValueError("Spread, slippage và fee phải hữu hạn, không âm.")
        if self.spread_rate + self.slippage_rate >= ONE:
            raise ValueError("Spread cộng slippage phải nhỏ hơn 100%.")
        if self.max_fill_quantity_per_event is not None and (
            not self.max_fill_quantity_per_event.is_finite()
            or self.max_fill_quantity_per_event <= ZERO
        ):
            raise ValueError("Giới hạn khớp mỗi event phải hữu hạn và dương.")


@dataclass(frozen=True, slots=True)
class PaperBar:
    """Nến OHLC đã biết đầy đủ tại available_at; dùng cho PAPER/replay."""

    bar_key: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        for value in (self.opened_at, self.closed_at, self.available_at):
            _aware(value)
        if not self.bar_key.strip() or not self.symbol.strip():
            raise ValueError("Bar cần natural key và symbol.")
        if not self.opened_at < self.closed_at or self.available_at < self.closed_at:
            raise ValueError("Bar phải đóng và có sẵn trước khi dùng làm event.")
        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= ZERO for value in prices):
            raise ValueError("OHLC phải là Decimal hữu hạn dương.")
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close, self.high
        ):
            raise ValueError("OHLC không tạo thành một nến hợp lệ.")


@dataclass(frozen=True, slots=True)
class PaperFill:
    """Một lần khớp duy nhất, giữ giá tham chiếu và chi phí thành phần."""

    fill_id: str
    fill_policy_version: str
    bar_key: str
    quantity: Decimal
    reference_price: Decimal
    execution_price: Decimal
    fee_cost: Decimal
    slippage_cost: Decimal
    filled_at: datetime


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    """Lệnh giả lập và các fill đã nhận; phần chưa khớp có thể bị hủy."""

    intent_id: str
    idempotency_key: str
    plan_hash: str
    fill_policy_version: str
    symbol: str
    direction: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    eligible_at: datetime
    expires_at: datetime
    stop_price: Decimal
    target_price: Decimal | None
    status: OrderStatus
    fills: tuple[PaperFill, ...] = ()
    seen_bar_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PaperPosition:
    """Vị thế PAPER có stop coverage, chi phí tích lũy và kết quả đã hiện thực."""

    position_id: str
    intent_id: str
    symbol: str
    direction: str
    quantity: Decimal
    entry_reference_price: Decimal
    entry_execution_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    recipe: ExitRecipe
    contract_multiplier: Decimal
    protective_stop_quantity: Decimal
    entry_bar_key: str
    fill_policy_version: str
    bars_held: int = 0
    fee_cost: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    funding_cost: Decimal = ZERO
    funding_event_ids: tuple[str, ...] = ()
    gross_realized_pnl: Decimal = ZERO
    is_closed: bool = False
    seen_bar_keys: tuple[str, ...] = ()
    exit_reason: str | None = None
    exit_reference_price: Decimal | None = None

    @property
    def net_realized_pnl(self) -> Decimal:
        """PnL thuần; slippage đã tách khỏi gross tham chiếu đúng một lần."""
        return self.gross_realized_pnl - self.fee_cost - self.slippage_cost - self.funding_cost


@dataclass(frozen=True, slots=True)
class ReduceOnlyReceipt:
    """Kết quả đóng chỉ giảm vị thế, không thể mở vị thế ngược."""

    idempotency_key: str
    filled_quantity: Decimal
    reduce_only: bool
    position: PaperPosition
    fill: PaperFill | None = None
    request_fingerprint: tuple[str, ...] = ()


class PaperExecutionGateway:
    """State machine trong bộ nhớ; không import credentials, HTTP hoặc adapter sàn."""

    def __init__(self, policy: PaperExecutionPolicy) -> None:
        self._policy = policy
        self._orders: dict[str, PaperOrderIntent] = {}
        self._keys: dict[str, str] = {}
        self._positions: dict[str, PaperPosition] = {}
        self._plans: dict[str, TradePlan] = {}
        self._close_keys: dict[str, ReduceOnlyReceipt] = {}
        self._entries_paused = False
        self._market_event_hashes: dict[tuple[str, str, str], str] = {}
        self._last_market_event: dict[tuple[str, str], tuple[datetime, str]] = {}

    @property
    def policy(self) -> PaperExecutionPolicy:
        """Policy bất biến đã được ghim vào order/fill trong phiên PAPER."""
        return self._policy

    def export_state(self) -> PaperExecutionSnapshot:
        """Xuất snapshot đầy đủ để checkpoint; serialization đặt ngoài domain state machine."""
        from trading_bot.domain.execution.paper_state import create_snapshot

        return create_snapshot(
            policy=self._policy,
            orders=self._orders,
            keys=self._keys,
            positions=self._positions,
            plans=self._plans,
            close_keys=self._close_keys,
            entries_paused=self._entries_paused,
            market_event_hashes=self._market_event_hashes,
            last_market_event=self._last_market_event,
        )

    @classmethod
    def restore(
        cls, policy: PaperExecutionPolicy, snapshot: PaperExecutionSnapshot
    ) -> PaperExecutionGateway:
        """Tạo gateway mới từ snapshot đầy đủ; mismatch phải fail-closed."""
        from trading_bot.domain.execution.paper_state import (
            PaperExecutionSnapshot,
            PaperSnapshotError,
            restore_snapshot,
        )

        if not isinstance(snapshot, PaperExecutionSnapshot):
            raise IntentConflict("Paper snapshot không đúng kiểu dữ liệu.")
        gateway = cls(policy)
        try:
            (
                gateway._orders,
                gateway._keys,
                gateway._positions,
                gateway._plans,
                gateway._close_keys,
                gateway._entries_paused,
                gateway._market_event_hashes,
                gateway._last_market_event,
            ) = restore_snapshot(policy, snapshot)
        except PaperSnapshotError as error:
            raise IntentConflict(str(error)) from error
        return gateway

    def replace_state(self, snapshot: PaperExecutionSnapshot) -> None:
        """Thay state từ snapshot đã xác thực, phục vụ publish sau DB commit."""
        restored = self.restore(self._policy, snapshot)
        self._orders = restored._orders
        self._keys = restored._keys
        self._positions = restored._positions
        self._plans = restored._plans
        self._close_keys = restored._close_keys
        self._entries_paused = restored._entries_paused
        self._market_event_hashes = restored._market_event_hashes
        self._last_market_event = restored._last_market_event

    def submit(
        self,
        plan: TradePlan,
        idempotency_key: str,
        *,
        submitted_at: datetime,
        eligible_after: datetime | None = None,
    ) -> PaperOrderIntent:
        """Tạo intent một lần; lần retry cùng key/hash trả lại intent hiện có."""
        _aware(submitted_at)
        if not idempotency_key.strip():
            raise IntentConflict("idempotency key không được để trống.")
        existing_id = self._keys.get(idempotency_key)
        if existing_id is not None:
            existing = self._orders[existing_id]
            if existing.plan_hash != plan.plan_hash:
                raise IntentConflict("idempotency key đã gắn với plan khác.")
            return existing
        if self._entries_paused:
            raise IntentConflict("PAPER entries đang paused.")
        if plan.quantity <= ZERO or not plan.quantity.is_finite():
            raise IntentConflict("Plan cần quantity hữu hạn và dương.")
        if submitted_at >= plan.expires_at:
            raise IntentConflict("Plan đã hết hạn trước khi tạo intent.")
        ready_at = eligible_after or plan.source_signal_at + timedelta(microseconds=1)
        _aware(ready_at)
        eligible_at = max(ready_at, submitted_at) + self.policy.latency
        intent_id = f"paper:{idempotency_key}"
        order = PaperOrderIntent(
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            plan_hash=plan.plan_hash,
            fill_policy_version=self.policy.version_id,
            symbol=plan.symbol,
            direction=plan.direction,
            requested_quantity=plan.quantity,
            filled_quantity=ZERO,
            remaining_quantity=plan.quantity,
            eligible_at=eligible_at,
            expires_at=plan.expires_at,
            stop_price=plan.stop_price,
            target_price=plan.target_price,
            status=OrderStatus.ACCEPTED,
        )
        self._keys[idempotency_key] = intent_id
        self._orders[intent_id] = order
        self._plans[intent_id] = plan
        return order

    def apply_market_event(
        self,
        bar: PaperBar,
        *,
        available_quantity: Decimal | None = None,
        intent_id: str | None = None,
    ) -> tuple[PaperOrderIntent, ...]:
        """Khớp lệnh đủ điều kiện ở giá mở nến, không dùng dữ liệu đóng nến làm giá vào."""
        event_scope = intent_id or "*"
        if intent_id is not None:
            self._get_order(intent_id)
        self._accept_market_event(bar, event_scope=event_scope)
        results: list[PaperOrderIntent] = []
        for intent_id, order in tuple(self._orders.items()):
            if event_scope != "*" and intent_id != event_scope:
                continue
            if order.symbol != bar.symbol or order.status not in {
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
            }:
                continue
            if bar.bar_key in order.seen_bar_keys:
                results.append(order)
                continue
            seen_order = replace(order, seen_bar_keys=(*order.seen_bar_keys, bar.bar_key))
            if bar.opened_at >= order.expires_at:
                seen_order = replace(seen_order, status=OrderStatus.EXPIRED)
            elif bar.opened_at >= order.eligible_at:
                fill_limit = order.remaining_quantity
                if self.policy.max_fill_quantity_per_event is not None:
                    fill_limit = min(fill_limit, self.policy.max_fill_quantity_per_event)
                if available_quantity is not None:
                    if not available_quantity.is_finite() or available_quantity < ZERO:
                        raise ValueError("available_quantity phải hữu hạn, không âm.")
                    fill_limit = min(fill_limit, available_quantity)
                if fill_limit > ZERO:
                    fill = self._create_fill(order, bar, fill_limit)
                    new_filled = order.filled_quantity + fill.quantity
                    remaining = order.requested_quantity - new_filled
                    status = (
                        OrderStatus.FILLED if remaining == ZERO else OrderStatus.PARTIALLY_FILLED
                    )
                    seen_order = replace(
                        seen_order,
                        filled_quantity=new_filled,
                        remaining_quantity=remaining,
                        status=status,
                        fills=(*order.fills, fill),
                    )
                    self._add_to_position(seen_order, fill)
            self._orders[intent_id] = seen_order
            results.append(seen_order)
        return tuple(results)

    def cancel_unfilled(self, intent_id: str) -> PaperOrderIntent:
        """Hủy remainder; fill/position đã có vẫn mở và giữ nguyên stop coverage."""
        order = self._get_order(intent_id)
        if order.status is OrderStatus.UNKNOWN:
            raise IntentConflict("Không thể hủy intent UNKNOWN trước khi reconcile.")
        if order.status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
            return order
        if order.status is OrderStatus.FILLED:
            return order
        updated = replace(order, status=OrderStatus.CANCELLED)
        self._orders[intent_id] = updated
        return updated

    def mark_unknown(self, intent_id: str) -> PaperOrderIntent:
        """Giữ intent chưa rõ kết quả; không khớp hoặc giải phóng phần còn lại."""
        order = self._get_order(intent_id)
        if order.status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
            raise IntentConflict("Intent terminal không thể chuyển UNKNOWN.")
        updated = replace(order, status=OrderStatus.UNKNOWN)
        self._orders[intent_id] = updated
        return updated

    def resolve_unknown(self, intent_id: str, *, order_absent: bool) -> PaperOrderIntent:
        """Chỉ kết thúc UNKNOWN khi nguồn đối soát xác nhận order vắng mặt."""
        order = self._get_order(intent_id)
        if order.status is not OrderStatus.UNKNOWN:
            raise IntentConflict("Chỉ intent UNKNOWN mới được reconcile.")
        if not order_absent:
            return order
        updated = replace(order, status=OrderStatus.CANCELLED)
        self._orders[intent_id] = updated
        return updated

    def pause_entries(self) -> None:
        """Chặn intent mới/hủy phần entry chưa khớp; giữ nguyên vị thế đã có và stop."""
        self._entries_paused = True
        for intent_id, order in tuple(self._orders.items()):
            if order.status in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
                self._orders[intent_id] = replace(order, status=OrderStatus.CANCELLED)

    def resume_entries(self) -> None:
        """Cho phép tạo intent PAPER mới sau khi được mở lại."""
        self._entries_paused = False

    def position(self, intent_id: str) -> PaperPosition | None:
        """Trả snapshot vị thế đang gắn với order intent."""
        return self._positions.get(intent_id)

    def order(self, intent_id: str) -> PaperOrderIntent:
        """Trả snapshot order để retry/reconcile có thể kiểm tra trạng thái."""
        return self._get_order(intent_id)

    def monitor_position(
        self,
        position: PaperPosition,
        bar: PaperBar,
        *,
        opposing_signal: bool = False,
        atr: Decimal | None = None,
    ) -> PositionMonitorResult:
        """Theo dõi stop/target/trailing và thời điểm; pause entry không áp dụng ở đây."""
        from trading_bot.domain.execution.position_monitor import (
            PositionMonitorResult,
            monitor_position,
        )

        self._accept_market_event(bar, event_scope=position.intent_id)
        current = self._positions.get(position.intent_id, position)
        if current.is_closed and bar.bar_key in current.seen_bar_keys:
            return PositionMonitorResult(current, current.exit_reason, current.exit_reference_price)
        result = monitor_position(current, bar, opposing_signal=opposing_signal, atr=atr)
        if result.exit_reason is None or result.exit_reference_price is None:
            self._positions[position.intent_id] = result.position
            return result
        receipt = self.close_position(
            current.position_id,
            current.quantity,
            reference_price=result.exit_reference_price,
            closed_at=bar.closed_at,
            idempotency_key=f"exit:{current.position_id}:{bar.bar_key}:{result.exit_reason}",
        )
        closed = replace(
            receipt.position,
            bars_held=result.position.bars_held,
            seen_bar_keys=result.position.seen_bar_keys,
            exit_reason=result.exit_reason,
            exit_reference_price=result.exit_reference_price,
        )
        self._positions[position.intent_id] = closed
        return replace(result, position=closed)

    def apply_funding_event(
        self, position_id: str, event_id: str, amount: Decimal
    ) -> PaperPosition:
        """Ghi dòng tiền funding có dấu; số âm là khoản nhận được."""
        position = self._get_position(position_id)
        if position.is_closed:
            raise IntentConflict("Không thể ghi funding cho vị thế đã đóng.")
        if not event_id.strip() or not amount.is_finite():
            raise ValueError("Funding event cần ID và dòng tiền hữu hạn.")
        if event_id in position.funding_event_ids:
            return position
        updated = replace(
            position,
            funding_cost=position.funding_cost + amount,
            funding_event_ids=(*position.funding_event_ids, event_id),
        )
        self._positions[position.intent_id] = updated
        return updated

    def close_position(
        self,
        position_id: str,
        requested_quantity: Decimal,
        *,
        reference_price: Decimal,
        closed_at: datetime,
        idempotency_key: str,
    ) -> ReduceOnlyReceipt:
        """Đóng tối đa lượng đang mở; quantity dư bị cắt, không thể đảo chiều."""
        _aware(closed_at)
        if not idempotency_key.strip():
            raise IntentConflict("Close idempotency key không được để trống.")
        fingerprint = (
            position_id,
            str(requested_quantity),
            str(reference_price),
            closed_at.isoformat(),
        )
        previous = self._close_keys.get(idempotency_key)
        if previous is not None:
            if previous.request_fingerprint != fingerprint:
                raise IntentConflict("Close idempotency key đã gắn với yêu cầu khác.")
            return previous
        if not requested_quantity.is_finite() or requested_quantity <= ZERO:
            raise ValueError("Close quantity phải hữu hạn và dương.")
        if not reference_price.is_finite() or reference_price <= ZERO:
            raise ValueError("Close price phải hữu hạn và dương.")
        position = self._get_position(position_id)
        fill_quantity = min(requested_quantity, position.quantity)
        if position.is_closed or fill_quantity == ZERO:
            receipt = ReduceOnlyReceipt(
                idempotency_key,
                ZERO,
                True,
                position,
                request_fingerprint=fingerprint,
            )
            self._close_keys[idempotency_key] = receipt
            return receipt
        fill = self._close_fill(
            position, fill_quantity, reference_price, closed_at, idempotency_key
        )
        sign = ONE if position.direction == "long" else -ONE
        gross = (reference_price - position.entry_reference_price) * fill_quantity
        gross *= sign * position.contract_multiplier
        quantity_left = position.quantity - fill_quantity
        updated = replace(
            position,
            quantity=quantity_left,
            protective_stop_quantity=quantity_left,
            fee_cost=position.fee_cost + fill.fee_cost,
            slippage_cost=position.slippage_cost + fill.slippage_cost,
            gross_realized_pnl=position.gross_realized_pnl + gross,
            is_closed=quantity_left == ZERO,
            exit_reason="REDUCE_ONLY" if quantity_left == ZERO else position.exit_reason,
            exit_reference_price=reference_price
            if quantity_left == ZERO
            else position.exit_reference_price,
        )
        self._positions[position.intent_id] = updated
        receipt = ReduceOnlyReceipt(
            idempotency_key,
            fill_quantity,
            True,
            updated,
            fill,
            fingerprint,
        )
        self._close_keys[idempotency_key] = receipt
        return receipt

    def _create_fill(self, order: PaperOrderIntent, bar: PaperBar, quantity: Decimal) -> PaperFill:
        side_sign = ONE if order.direction == "long" else -ONE
        adverse_rate = self.policy.spread_rate + self.policy.slippage_rate
        raw_price = bar.open * (ONE + adverse_rate * side_sign)
        rounding = ROUND_CEILING if side_sign > ZERO else ROUND_FLOOR
        plan = self._plans[order.intent_id]
        execution_price = _round_price(raw_price, plan.exchange_rules.tick_size, rounding)
        multiplier = plan.exchange_rules.contract_multiplier
        fee = execution_price * quantity * multiplier * self.policy.fee_rate
        slippage = abs(execution_price - bar.open) * quantity * multiplier
        sequence = len(order.fills) + 1
        return PaperFill(
            f"{order.intent_id}:{bar.bar_key}:{sequence}",
            order.fill_policy_version,
            bar.bar_key,
            quantity,
            bar.open,
            execution_price,
            fee,
            slippage,
            bar.opened_at,
        )

    def _add_to_position(self, order: PaperOrderIntent, fill: PaperFill) -> None:
        plan = self._plans[order.intent_id]
        position = self._positions.get(order.intent_id)
        old_quantity = position.quantity if position else ZERO
        total = old_quantity + fill.quantity
        if position is None:
            position = PaperPosition(
                position_id=order.intent_id,
                intent_id=order.intent_id,
                symbol=order.symbol,
                direction=order.direction,
                quantity=ZERO,
                entry_reference_price=fill.reference_price,
                entry_execution_price=fill.execution_price,
                stop_price=order.stop_price,
                target_price=order.target_price,
                recipe=plan.exit_recipe,
                contract_multiplier=plan.exchange_rules.contract_multiplier,
                protective_stop_quantity=ZERO,
                entry_bar_key=fill.bar_key,
                fill_policy_version=fill.fill_policy_version,
            )
        avg_reference = (
            position.entry_reference_price * old_quantity + fill.reference_price * fill.quantity
        ) / total
        avg_execution = (
            position.entry_execution_price * old_quantity + fill.execution_price * fill.quantity
        ) / total
        self._positions[order.intent_id] = replace(
            position,
            quantity=total,
            protective_stop_quantity=total,
            entry_reference_price=avg_reference,
            entry_execution_price=avg_execution,
            fee_cost=position.fee_cost + fill.fee_cost,
            slippage_cost=position.slippage_cost + fill.slippage_cost,
        )

    def _close_fill(
        self,
        position: PaperPosition,
        quantity: Decimal,
        reference_price: Decimal,
        closed_at: datetime,
        event_id: str,
    ) -> PaperFill:
        side_sign = -ONE if position.direction == "long" else ONE
        adverse_rate = self.policy.spread_rate + self.policy.slippage_rate
        raw_price = reference_price * (ONE + adverse_rate * side_sign)
        rounding = ROUND_FLOOR if side_sign < ZERO else ROUND_CEILING
        execution_price = _round_price(
            raw_price,
            self._plans[position.intent_id].exchange_rules.tick_size,
            rounding,
        )
        multiplier = position.contract_multiplier
        fee = execution_price * quantity * multiplier * self.policy.fee_rate
        slippage = abs(execution_price - reference_price) * quantity * multiplier
        return PaperFill(
            f"{event_id}:close",
            self.policy.version_id,
            event_id,
            quantity,
            reference_price,
            execution_price,
            fee,
            slippage,
            closed_at,
        )

    def _get_order(self, intent_id: str) -> PaperOrderIntent:
        try:
            return self._orders[intent_id]
        except KeyError as error:
            raise IntentConflict("Không tìm thấy PAPER intent.") from error

    def _accept_market_event(self, bar: PaperBar, *, event_scope: str) -> None:
        key = (event_scope, bar.symbol, bar.bar_key)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "opened_at": bar.opened_at.isoformat(),
                    "closed_at": bar.closed_at.isoformat(),
                    "available_at": bar.available_at.isoformat(),
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        previous_hash = self._market_event_hashes.get(key)
        if previous_hash is not None:
            if previous_hash != fingerprint:
                raise IntentConflict("Market event key đã được dùng với payload khác.")
            return
        cursor_key = (event_scope, bar.symbol)
        previous_cursor = self._last_market_event.get(cursor_key)
        if previous_cursor is not None and bar.opened_at <= previous_cursor[0]:
            raise IntentConflict("Market event mới phải có thứ tự thời gian tăng dần theo symbol.")
        self._market_event_hashes[key] = fingerprint
        self._last_market_event[cursor_key] = (bar.opened_at, bar.bar_key)

    def _get_position(self, position_id: str) -> PaperPosition:
        try:
            return self._positions[position_id]
        except KeyError as error:
            raise IntentConflict("Không tìm thấy PAPER position.") from error


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Thời gian PAPER phải có múi giờ.")


def _round_price(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick
