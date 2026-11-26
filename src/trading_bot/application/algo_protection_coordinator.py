"""Điều phối lệnh bảo vệ qua Binance USDⓈ-M Algo Service (WP-03C.2 / CRY-293).

Đảm bảo:
- Mọi lượng vị thế đã fill xác nhận từ sàn đều được phủ bởi lệnh bảo vệ đã được sàn ACK `NEW`.
- Thay thế bảo vệ khi có successive fill theo trình tự bảo thủ (conservative replace):
  đặt lệnh mới và xác nhận ACK trước khi hủy lệnh cũ để không tạo khoảng trống không được bảo vệ.
- Nếu đặt lệnh bảo vệ bị reject/thất bại hoặc nhận `ALGO_UPDATE.REJECTED`: đóng ngay cổng entry
  và phát sự kiện leo thang `ProtectionEscalation`.
- Dọn dẹp algo mồ côi chỉ khi vị thế xác nhận về 0 VÀ đã có bằng chứng chắc chắn không còn
  fill/trigger đang mở (`no_open_trigger_confirmed=True`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from trading_bot.adapters.binance_usdm.algo_rest import (
    AlgoOrderAccepted,
    AlgoOrderCanceled,
    AlgoOrderPort,
    AlgoSubmissionFailed,
)
from trading_bot.adapters.binance_usdm.order_schemas import (
    AlgoOrder,
    AlgoOrderStatus,
    AlgoUpdate,
    ConditionalAlgoOrderType,
)
from trading_bot.domain.execution.algo_protection import (
    ZERO,
    AlgoOrderParams,
    AlgoProtectionLink,
    PositionMode,
    PositionSide,
    ProtectionCoverageAssessment,
    WorkingType,
    assess_protection_coverage,
)


@dataclass(frozen=True, slots=True)
class ProtectionEscalation:
    """Sự kiện leo thang khẩn cấp khi thiếu hoặc bị từ chối lệnh bảo vệ."""

    symbol: str
    position_side: PositionSide
    reason_code: str
    detail: str
    occurred_at: datetime
    entry_intent_id: UUID | None = None
    algo_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProtectionSyncResult:
    """Kết quả mỗi bước đồng bộ bảo vệ của coordinator."""

    assessment: ProtectionCoverageAssessment
    entry_gate_open: bool
    escalations: tuple[ProtectionEscalation, ...] = ()
    placed_algo_id: int | None = None
    canceled_algo_ids: tuple[int, ...] = ()


class AlgoProtectionCoordinator:
    """Điều phối quan hệ bền vững entry intent ↔ algo order ↔ triggered order ↔ fills."""

    def __init__(
        self,
        *,
        algo_port: AlgoOrderPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._algo_port = algo_port
        self._clock = clock or (lambda: datetime.now(UTC))
        self._links_by_client_id: dict[str, AlgoProtectionLink] = {}
        self._client_id_by_algo_id: dict[int, str] = {}
        self._client_id_by_triggered_order_id: dict[str, str] = {}
        self._escalations: list[ProtectionEscalation] = []
        self._sequence: int = 0
        self._entry_gate_open: bool = True

    @property
    def entry_gate_open(self) -> bool:
        """Trạng thái cổng cho phép mở lệnh entry mới (đóng ngay khi bảo vệ có sự cố)."""
        return self._entry_gate_open

    @property
    def escalations(self) -> tuple[ProtectionEscalation, ...]:
        """Danh sách các sự kiện leo thang đã phát sinh."""
        return tuple(self._escalations)

    def links_for(
        self, *, symbol: str, position_side: PositionSide = PositionSide.BOTH
    ) -> tuple[AlgoProtectionLink, ...]:
        """Lấy toàn bộ liên kết bảo vệ theo symbol và position_side."""
        return tuple(
            link
            for link in self._links_by_client_id.values()
            if link.symbol == symbol and link.position_side is position_side
        )

    def on_confirmed_entry_fill(
        self,
        *,
        entry_intent_id: UUID,
        symbol: str,
        protective_side: str,
        confirmed_position_qty: Decimal,
        trigger_price: Decimal,
        order_type: ConditionalAlgoOrderType = ConditionalAlgoOrderType.STOP_MARKET,
        working_type: WorkingType = WorkingType.MARK_PRICE,
        position_mode: PositionMode = PositionMode.ONE_WAY,
        position_side: PositionSide = PositionSide.BOTH,
        use_close_position: bool = False,
        limit_price: Decimal | None = None,
        callback_rate: Decimal | None = None,
        activate_price: Decimal | None = None,
    ) -> ProtectionSyncResult:
        """Đồng bộ lệnh bảo vệ khi có lượng fill xác nhận từ sàn.

        - Nếu `confirmed_position_qty == 0`: không đặt lệnh bảo vệ mới.
        - Nếu chưa có bảo vệ phủ đủ `confirmed_position_qty`:
          1. Đặt lệnh algo bảo vệ mới cho tổng `confirmed_position_qty` (với `reduce_only=True`
             ở One-Way hoặc `close_position=True` khi hợp lệ).
          2. Chỉ sau khi sàn ACK lệnh mới ở trạng thái `NEW`, mới hủy các lệnh algo cũ
             (conservative replace — không tạo khoảng trống không có bảo vệ).
          3. Nếu đặt thất bại hoặc sàn trả trạng thái không phải `NEW`: đóng cổng entry
             và ghi `ProtectionEscalation`.
        """
        now = self._clock()
        existing = self.links_for(symbol=symbol, position_side=position_side)
        if confirmed_position_qty == ZERO:
            assessment = assess_protection_coverage(
                symbol=symbol,
                position_side=position_side,
                confirmed_position_qty=ZERO,
                algo_links=existing,
            )
            return ProtectionSyncResult(
                assessment=assessment,
                entry_gate_open=self._entry_gate_open and assessment.entry_gate_open,
            )

        current_assessment = assess_protection_coverage(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            algo_links=existing,
        )
        if current_assessment.uncovered_quantity == ZERO and current_assessment.entry_gate_open:
            return ProtectionSyncResult(
                assessment=current_assessment,
                entry_gate_open=self._entry_gate_open,
            )

        old_active_links = [link for link in existing if link.is_confirmed_active]
        client_algo_id = self._next_client_algo_id(entry_intent_id)
        reduce_only = (position_mode is PositionMode.ONE_WAY) and (not use_close_position)

        params = AlgoOrderParams(
            symbol=symbol,
            side=protective_side,
            order_type=order_type,
            client_algo_id=client_algo_id,
            working_type=working_type,
            position_mode=position_mode,
            position_side=position_side,
            trigger_price=trigger_price
            if order_type is not ConditionalAlgoOrderType.TRAILING_STOP_MARKET
            else None,
            price=limit_price,
            quantity=None if use_close_position else confirmed_position_qty,
            reduce_only=reduce_only,
            close_position=use_close_position,
            callback_rate=callback_rate,
            activate_price=activate_price,
        )

        # Bước 1: Đặt lệnh bảo vệ mới TRƯỚC khi hủy lệnh cũ (conservative replace)
        place_result = self._algo_port.place_algo_order(params)
        if isinstance(place_result, AlgoSubmissionFailed):
            self._entry_gate_open = False
            reason = (
                "PROTECTION_REJECTED"
                if place_result.failure.outcome.value == "DEFINITIVE_REJECTION"
                else "PROTECTION_MISSING"
            )
            esc = ProtectionEscalation(
                symbol=symbol,
                position_side=position_side,
                reason_code=reason,
                detail=f"Đặt lệnh bảo vệ thất bại ({place_result.failure.kind.value}).",
                occurred_at=now,
                entry_intent_id=entry_intent_id,
            )
            self._escalations.append(esc)
            assessment = assess_protection_coverage(
                symbol=symbol,
                position_side=position_side,
                confirmed_position_qty=confirmed_position_qty,
                algo_links=self.links_for(symbol=symbol, position_side=position_side),
            )
            return ProtectionSyncResult(
                assessment=assessment,
                entry_gate_open=False,
                escalations=(esc,),
            )

        assert isinstance(place_result, AlgoOrderAccepted)
        acked = place_result.order
        confirmed_qty = confirmed_position_qty if acked.status is AlgoOrderStatus.NEW else ZERO
        new_link = AlgoProtectionLink(
            entry_intent_id=entry_intent_id,
            symbol=symbol,
            protective_side=protective_side,
            position_side=position_side,
            order_type=order_type,
            client_algo_id=acked.client_algo_id,
            working_type=working_type,
            requested_quantity=confirmed_position_qty,
            reduce_only=reduce_only,
            close_position=use_close_position,
            algo_id=acked.algo_id,
            algo_status=acked.status,
            confirmed_quantity=confirmed_qty,
            triggered_order_id=acked.actual_order_id,
            updated_at=now,
        )
        self._store_link(new_link)

        new_escalations: list[ProtectionEscalation] = []
        canceled_ids: list[int] = []

        # Bước 2: Chỉ hủy lệnh bảo vệ cũ sau khi lệnh mới đã ACK NEW
        if new_link.is_confirmed_active:
            for old_link in old_active_links:
                if old_link.algo_id is None or old_link.algo_id == acked.algo_id:
                    continue
                cancel_res = self._algo_port.cancel_algo_order(
                    symbol=symbol,
                    algo_id=old_link.algo_id,
                    client_algo_id=old_link.client_algo_id,
                )
                if isinstance(cancel_res, AlgoOrderCanceled):
                    updated_old = replace(
                        old_link,
                        algo_status=AlgoOrderStatus.CANCELED,
                        confirmed_quantity=ZERO,
                        updated_at=now,
                    )
                    self._store_link(updated_old)
                    canceled_ids.append(old_link.algo_id)
                else:
                    # Hủy lệnh cũ thất bại (có thể lệnh cũ vừa trigger trong lúc replace):
                    # kiểm tra lại trạng thái lệnh cũ từ sàn để không bỏ sót trigger.
                    query_res = self._algo_port.query_algo_order(algo_id=old_link.algo_id)
                    if not isinstance(query_res, AlgoSubmissionFailed):
                        queried = query_res.order
                        updated_old = replace(
                            old_link,
                            algo_status=queried.status,
                            triggered_order_id=queried.actual_order_id
                            or old_link.triggered_order_id,
                            triggered_quantity=queried.actual_quantity
                            or old_link.triggered_quantity,
                            confirmed_quantity=old_link.confirmed_quantity
                            if queried.status is AlgoOrderStatus.NEW
                            else ZERO,
                            updated_at=now,
                        )
                        self._store_link(updated_old)
        else:
            self._entry_gate_open = False
            esc = ProtectionEscalation(
                symbol=symbol,
                position_side=position_side,
                reason_code="PROTECTION_REJECTED"
                if acked.status is AlgoOrderStatus.REJECTED
                else "PROTECTION_MISSING",
                detail=f"Lệnh bảo vệ algoId={acked.algo_id} ở trạng thái {acked.status.value}.",
                occurred_at=now,
                entry_intent_id=entry_intent_id,
                algo_id=acked.algo_id,
            )
            self._escalations.append(esc)
            new_escalations.append(esc)

        final_assessment = assess_protection_coverage(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            algo_links=self.links_for(symbol=symbol, position_side=position_side),
        )
        if not final_assessment.entry_gate_open:
            self._entry_gate_open = False
        return ProtectionSyncResult(
            assessment=final_assessment,
            entry_gate_open=self._entry_gate_open and final_assessment.entry_gate_open,
            escalations=tuple(new_escalations),
            placed_algo_id=acked.algo_id,
            canceled_algo_ids=tuple(canceled_ids),
        )

    def on_algo_update(
        self,
        update: AlgoUpdate,
        *,
        confirmed_position_qty: Decimal,
        rejection_reason: str | None = None,
    ) -> ProtectionSyncResult:
        """Xử lý sự kiện `ALGO_UPDATE` từ User Data Stream (không phụ thuộc event deprecated)."""
        now = self._clock()
        algo = update.order
        client_id = self._client_id_by_algo_id.get(algo.algo_id, algo.client_algo_id)
        existing = self._links_by_client_id.get(client_id)

        if existing is None:
            # Không tự tạo bảo vệ từ algo lạ; chỉ đánh dấu đóng cổng nếu bị reject
            if algo.status is AlgoOrderStatus.REJECTED:
                self._entry_gate_open = False
            assessment = assess_protection_coverage(
                symbol=algo.symbol,
                position_side=PositionSide.BOTH,
                confirmed_position_qty=confirmed_position_qty,
                algo_links=self.links_for(symbol=algo.symbol, position_side=PositionSide.BOTH),
            )
            return ProtectionSyncResult(
                assessment=assessment,
                entry_gate_open=self._entry_gate_open and assessment.entry_gate_open,
            )

        updated = replace(
            existing,
            algo_id=algo.algo_id,
            algo_status=algo.status,
            confirmed_quantity=existing.requested_quantity
            if algo.status is AlgoOrderStatus.NEW
            else ZERO,
            triggered_order_id=algo.actual_order_id or existing.triggered_order_id,
            triggered_quantity=algo.actual_quantity
            if algo.actual_quantity is not None
            else existing.triggered_quantity,
            rejection_reason=rejection_reason
            if algo.status is AlgoOrderStatus.REJECTED
            else existing.rejection_reason,
            updated_at=now,
        )
        self._store_link(updated)

        new_escalations: list[ProtectionEscalation] = []
        if algo.status is AlgoOrderStatus.REJECTED:
            self._entry_gate_open = False
            esc = ProtectionEscalation(
                symbol=existing.symbol,
                position_side=existing.position_side,
                reason_code="PROTECTION_REJECTED",
                detail=(
                    f"ALGO_UPDATE báo REJECTED cho algoId={algo.algo_id}"
                    + (f": {rejection_reason}" if rejection_reason else ".")
                ),
                occurred_at=now,
                entry_intent_id=existing.entry_intent_id,
                algo_id=algo.algo_id,
            )
            self._escalations.append(esc)
            new_escalations.append(esc)

        assessment = assess_protection_coverage(
            symbol=existing.symbol,
            position_side=existing.position_side,
            confirmed_position_qty=confirmed_position_qty,
            algo_links=self.links_for(symbol=existing.symbol, position_side=existing.position_side),
        )
        if not assessment.entry_gate_open:
            self._entry_gate_open = False

        return ProtectionSyncResult(
            assessment=assessment,
            entry_gate_open=self._entry_gate_open and assessment.entry_gate_open,
            escalations=tuple(new_escalations),
        )

    def on_triggered_order_fill(
        self,
        *,
        actual_order_id: str,
        trade_id: str,
        filled_qty: Decimal,
    ) -> AlgoProtectionLink | None:
        """Gắn fill của lệnh thường sinh sau khi Algo trigger vào `AlgoProtectionLink`."""
        client_id = self._client_id_by_triggered_order_id.get(actual_order_id)
        if client_id is None:
            return None
        link = self._links_by_client_id[client_id]
        if trade_id in link.linked_fill_ids:
            return link
        updated = replace(
            link,
            triggered_quantity=link.triggered_quantity + filled_qty,
            linked_fill_ids=(*link.linked_fill_ids, trade_id),
            updated_at=self._clock(),
        )
        self._store_link(updated)
        return updated

    def cleanup_orphan_protections(
        self,
        *,
        symbol: str,
        position_side: PositionSide = PositionSide.BOTH,
        confirmed_position_qty: Decimal,
        no_open_trigger_confirmed: bool,
    ) -> ProtectionSyncResult:
        """Dọn dẹp lệnh algo mồ côi CHỈ khi vị thế đã về 0 và xác nhận không còn trigger mở."""
        now = self._clock()
        links = self.links_for(symbol=symbol, position_side=position_side)
        if confirmed_position_qty > ZERO or not no_open_trigger_confirmed:
            assessment = assess_protection_coverage(
                symbol=symbol,
                position_side=position_side,
                confirmed_position_qty=confirmed_position_qty,
                algo_links=links,
            )
            return ProtectionSyncResult(
                assessment=assessment,
                entry_gate_open=self._entry_gate_open and assessment.entry_gate_open,
            )

        canceled_ids: list[int] = []
        for link in links:
            if not link.is_open_on_exchange or link.algo_id is None:
                continue
            cancel_res = self._algo_port.cancel_algo_order(
                symbol=symbol,
                algo_id=link.algo_id,
                client_algo_id=link.client_algo_id,
            )
            if isinstance(cancel_res, AlgoOrderCanceled):
                updated = replace(
                    link,
                    algo_status=AlgoOrderStatus.CANCELED,
                    confirmed_quantity=ZERO,
                    updated_at=now,
                )
                self._store_link(updated)
                canceled_ids.append(link.algo_id)

        final_assessment = assess_protection_coverage(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=ZERO,
            algo_links=self.links_for(symbol=symbol, position_side=position_side),
        )
        return ProtectionSyncResult(
            assessment=final_assessment,
            entry_gate_open=self._entry_gate_open and final_assessment.entry_gate_open,
            canceled_algo_ids=tuple(canceled_ids),
        )

    def reconcile_with_exchange_state(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        confirmed_position_qty: Decimal,
        open_algo_orders: tuple[AlgoOrder, ...],
    ) -> ProtectionCoverageAssessment:
        """Đối soát lại độ phủ bảo vệ từ danh sách `openAlgoOrders` thật trên sàn."""
        now = self._clock()
        open_by_id = {order.algo_id: order for order in open_algo_orders if order.symbol == symbol}
        for link in self.links_for(symbol=symbol, position_side=position_side):
            if link.algo_id is None:
                continue
            if link.algo_id in open_by_id:
                venue_order = open_by_id[link.algo_id]
                updated = replace(
                    link,
                    algo_status=venue_order.status,
                    confirmed_quantity=link.requested_quantity
                    if venue_order.status is AlgoOrderStatus.NEW
                    else ZERO,
                    triggered_order_id=venue_order.actual_order_id or link.triggered_order_id,
                    updated_at=now,
                )
                self._store_link(updated)
            elif link.is_confirmed_active:
                # Không còn trong openAlgoOrders trên sàn → không được tin là vẫn đang bảo vệ
                updated = replace(
                    link,
                    algo_status=AlgoOrderStatus.UNKNOWN,
                    confirmed_quantity=ZERO,
                    updated_at=now,
                )
                self._store_link(updated)

        assessment = assess_protection_coverage(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            algo_links=self.links_for(symbol=symbol, position_side=position_side),
        )
        if not assessment.entry_gate_open:
            self._entry_gate_open = False
        return assessment

    def clear_gate_after_operator_resolution(
        self,
        *,
        symbol: str,
        position_side: PositionSide,
        confirmed_position_qty: Decimal,
    ) -> bool:
        """Chỉ mở lại cổng entry sau khi sự cố được xử lý và trạng thái sàn đã phủ đủ bảo vệ."""
        # Loại bỏ trạng thái REJECTED cũ đã được operator xử lý bằng bảo vệ mới hợp lệ
        links = self.links_for(symbol=symbol, position_side=position_side)
        cleaned = tuple(link for link in links if link.algo_status is not AlgoOrderStatus.REJECTED)
        assessment = assess_protection_coverage(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            algo_links=cleaned,
        )
        if assessment.entry_gate_open:
            self._entry_gate_open = True
            return True
        return False

    def _store_link(self, link: AlgoProtectionLink) -> None:
        self._links_by_client_id[link.client_algo_id] = link
        if link.algo_id is not None:
            self._client_id_by_algo_id[link.algo_id] = link.client_algo_id
        if link.triggered_order_id is not None:
            self._client_id_by_triggered_order_id[link.triggered_order_id] = link.client_algo_id

    def _next_client_algo_id(self, entry_intent_id: UUID) -> str:
        self._sequence += 1
        return f"prot-{entry_intent_id.hex[:12]}-{self._sequence}"
