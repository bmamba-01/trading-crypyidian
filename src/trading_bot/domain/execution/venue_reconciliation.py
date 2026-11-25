"""Đối soát startup/reconnect venue và chuyển đổi sang `AccountActivitySnapshot` (WP-03D / CRY-294).

Hợp đồng kết quả tường minh theo WBS WP-03 Task 7:
`MATCHED`, `ORDER_ABSENT`, `OPEN_EXPOSURE_RECORDED`, `FOREIGN_ACTIVITY`,
`PROTECTION_MISSING`, `MISMATCH`, `STALE_INCOMPLETE`.
Mọi trạng thái thiếu, cũ, ở tương lai, không đầy đủ hoặc không khép được stream gap đều fail-closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trading_bot.domain.execution.live_preflight import (
    VenueOrderSummary,
    VenuePositionSnapshot,
)
from trading_bot.ports.account_activity import (
    AccountActivitySnapshot,
    ActivityClassification,
    ActivityVerdict,
    KnownAccountState,
    ObservedOrder,
    ObservedPosition,
    classify_account_activity,
)

ZERO = Decimal("0")


class VenueReconcileOutcome(StrEnum):
    """Bảy kết quả đối soát tường minh của WP-03D Task 7."""

    MATCHED = "MATCHED"
    ORDER_ABSENT = "ORDER_ABSENT"
    OPEN_EXPOSURE_RECORDED = "OPEN_EXPOSURE_RECORDED"
    FOREIGN_ACTIVITY = "FOREIGN_ACTIVITY"
    PROTECTION_MISSING = "PROTECTION_MISSING"
    MISMATCH = "MISMATCH"
    STALE_INCOMPLETE = "STALE_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class VenueFillEvent:
    """Một lần khớp lệnh gần đây từ REST userTrades dùng để khép khoảng trống và dedupe."""

    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class VenueAuthoritativeSnapshot:
    """Snapshot có thẩm quyền từ sàn gồm balances, positions, normal/algo orders và recent fills.

    Basis vốn: `wallet_balance` đã bao gồm realized PnL/phí/funding đã chốt;
    `venue_equity = wallet_balance + unrealized_pnl` (không cộng trùng realized PnL).
    """

    workspace_id: UUID
    account_id: UUID
    environment: str
    observed_at: datetime
    complete: bool
    cursor: str | None
    wallet_balance: Decimal | None
    unrealized_pnl: Decimal | None
    positions: tuple[VenuePositionSnapshot, ...] = ()
    open_orders: tuple[VenueOrderSummary, ...] = ()
    algo_orders: tuple[VenueOrderSummary, ...] = ()
    recent_fills: tuple[VenueFillEvent, ...] = ()
    history_coverage_from: datetime | None = None
    history_coverage_to: datetime | None = None

    @property
    def venue_equity(self) -> Decimal | None:
        if self.wallet_balance is None or self.unrealized_pnl is None:
            return None
        return self.wallet_balance + self.unrealized_pnl

    def deduped_fills(self) -> tuple[VenueFillEvent, ...]:
        """Khử trùng lặp recent fills theo `trade_id`, giữ thứ tự thời gian."""
        seen: set[str] = set()
        unique: list[VenueFillEvent] = []
        for fill in sorted(self.recent_fills, key=lambda f: (f.occurred_at, f.trade_id)):
            if fill.trade_id in seen:
                continue
            seen.add(fill.trade_id)
            unique.append(fill)
        return tuple(unique)

    def to_account_activity_snapshot(self) -> AccountActivitySnapshot:
        """Chuyển đổi sang `AccountActivitySnapshot(source='VENUE')` để chạy classifier MB05."""
        obs_orders = tuple(
            ObservedOrder(
                client_order_id=o.client_order_id,
                symbol=o.symbol,
                side=o.side,
                quantity=o.orig_qty,
                status=o.status,
            )
            for o in (*self.open_orders, *self.algo_orders)
            if o.orig_qty > ZERO
        )
        obs_positions = tuple(
            ObservedPosition(
                symbol=p.symbol,
                side="LONG" if p.quantity > ZERO else "SHORT",
                quantity=abs(p.quantity),
            )
            for p in self.positions
            if p.quantity != ZERO
        )
        return AccountActivitySnapshot(
            workspace_id=self.workspace_id,
            account_id=self.account_id,
            source="VENUE",
            cursor=self.cursor,
            observed_at=self.observed_at.astimezone(UTC),
            complete=self.complete,
            orders=obs_orders,
            positions=obs_positions,
        )


@dataclass(frozen=True, slots=True)
class PendingUnknownIntent:
    """Intent đang ở trạng thái UNKNOWN cần đối soát bằng snapshot + recent fills."""

    intent_id: UUID
    client_order_id: str
    symbol: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VenueReconcileDecision:
    """Kết quả đối soát startup/reconnect kèm quyền chuyển trạng thái reservation."""

    outcome: VenueReconcileOutcome
    entry_allowed: bool
    can_transition_reservation: bool
    reason_code: str
    detail_vi: str
    deduped_fill_count: int = 0
    activity_classification: ActivityClassification | None = None


def reconcile_venue_startup(
    snapshot: VenueAuthoritativeSnapshot | None,
    *,
    expected_workspace_id: UUID,
    expected_account_id: UUID,
    expected_environment: str,
    known_state: KnownAccountState,
    protected_symbols: frozenset[str],
    unknown_intents: Sequence[PendingUnknownIntent] = (),
    as_of: datetime,
    max_age: timedelta = timedelta(seconds=30),
) -> VenueReconcileDecision:
    """Đối soát đầy đủ tại thời điểm khởi động (startup gate đóng mặc định)."""
    now_utc = as_of.astimezone(UTC)

    # 1. Thiếu snapshot, incomplete, stale, future hoặc thiếu equity basis -> STALE_INCOMPLETE
    if snapshot is None:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="VENUE_SNAPSHOT_MISSING",
            detail_vi="Thiếu authoritative snapshot từ sàn.",
        )
    if (
        snapshot.workspace_id != expected_workspace_id
        or snapshot.account_id != expected_account_id
        or snapshot.environment.upper() != expected_environment.upper()
    ):
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="VENUE_SCOPE_MISMATCH",
            detail_vi="Snapshot không khớp workspace_id, account_id hoặc environment.",
        )
    obs_utc = snapshot.observed_at.astimezone(UTC)
    if not snapshot.complete or snapshot.venue_equity is None:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="VENUE_SNAPSHOT_INCOMPLETE",
            detail_vi="Snapshot chưa đầy đủ hoặc thiếu dữ kiện equity (không gán bằng 0).",
        )
    if obs_utc > now_utc:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="VENUE_SNAPSHOT_FUTURE",
            detail_vi="Thời điểm quan sát snapshot nằm ở tương lai.",
        )
    if (now_utc - obs_utc) > max_age:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="VENUE_SNAPSHOT_STALE",
            detail_vi="Snapshot sàn đã quá hạn cho phép.",
        )

    fills = snapshot.deduped_fills()

    # 2. Phân loại hoạt động ngoại lai qua classifier MB05 dùng chung
    activity_snap = snapshot.to_account_activity_snapshot()
    classification = classify_account_activity(
        snapshot=activity_snap,
        known_state=known_state,
        now=now_utc,
        max_snapshot_age=max_age,
    )
    if classification.verdict is ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code=classification.reason_code or "ACCOUNT_ACTIVITY_UNVERIFIED",
            detail_vi="Không thể xác minh độ tin cậy của activity snapshot.",
            deduped_fill_count=len(fills),
            activity_classification=classification,
        )

    # 3. Kiểm tra quantity mismatch trên các symbol đã biết trước khi kết luận foreign
    known_by_sym_side = {(p.symbol, p.side): p.quantity for p in known_state.open_positions}
    active_positions = [p for p in snapshot.positions if p.quantity != ZERO]
    for p in active_positions:
        side = "LONG" if p.quantity > ZERO else "SHORT"
        qty = abs(p.quantity)
        if (p.symbol, side) in known_by_sym_side and known_by_sym_side[(p.symbol, side)] != qty:
            return VenueReconcileDecision(
                outcome=VenueReconcileOutcome.MISMATCH,
                entry_allowed=False,
                can_transition_reservation=False,
                reason_code="VENUE_POSITION_QUANTITY_MISMATCH",
                detail_vi=f"Sai lệch khối lượng vị thế {p.symbol} giữa sàn và sổ cái.",
                deduped_fill_count=len(fills),
                activity_classification=classification,
            )

    if classification.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.FOREIGN_ACTIVITY,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="EXTERNAL_ACTIVITY_UNRECONCILED",
            detail_vi="Phát hiện lệnh hoặc vị thế ngoại lai; tạo incident bền, không tự sửa.",
            deduped_fill_count=len(fills),
            activity_classification=classification,
        )

    # 4. Kiểm tra lệnh bảo vệ cho mọi vị thế đang mở
    all_algo_symbols = {a.symbol for a in snapshot.algo_orders}
    for p in active_positions:
        if p.symbol not in protected_symbols and p.symbol not in all_algo_symbols:
            return VenueReconcileDecision(
                outcome=VenueReconcileOutcome.PROTECTION_MISSING,
                entry_allowed=False,
                can_transition_reservation=False,
                reason_code="VENUE_PROTECTION_MISSING",
                detail_vi=f"Vị thế mở {p.symbol} thiếu lệnh bảo vệ cắt lỗ / Algo order.",
                deduped_fill_count=len(fills),
                activity_classification=classification,
            )

    # 5. Đối soát các intent UNKNOWN dựa trên open_orders, algo_orders và deduped recent_fills
    if unknown_intents:
        open_cids = {o.client_order_id for o in (*snapshot.open_orders, *snapshot.algo_orders)}
        fill_cids = {f.client_order_id for f in fills}
        for pending in unknown_intents:
            if pending.client_order_id in open_cids or pending.client_order_id in fill_cids:
                return VenueReconcileDecision(
                    outcome=VenueReconcileOutcome.OPEN_EXPOSURE_RECORDED,
                    entry_allowed=False,
                    can_transition_reservation=True,
                    reason_code="UNKNOWN_INTENT_FOUND_ON_VENUE",
                    detail_vi=f"Đã tìm thấy bằng chứng khớp/mở của {pending.client_order_id}.",
                    deduped_fill_count=len(fills),
                    activity_classification=classification,
                )
            if snapshot.history_coverage_from is None or snapshot.history_coverage_from.astimezone(
                UTC
            ) > pending.created_at.astimezone(UTC):
                return VenueReconcileDecision(
                    outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
                    entry_allowed=False,
                    can_transition_reservation=False,
                    reason_code="FILL_HISTORY_GAP_UNCOVERED",
                    detail_vi="Lịch sử REST không phủ tới mốc tạo intent UNKNOWN; giữ nguyên.",
                    deduped_fill_count=len(fills),
                    activity_classification=classification,
                )
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.ORDER_ABSENT,
            entry_allowed=False,
            can_transition_reservation=True,
            reason_code="UNKNOWN_INTENT_CONFIRMED_ABSENT",
            detail_vi="Đã xác minh lệnh UNKNOWN vắng mặt trên sàn và trong lịch sử khớp.",
            deduped_fill_count=len(fills),
            activity_classification=classification,
        )

    return VenueReconcileDecision(
        outcome=VenueReconcileOutcome.MATCHED,
        entry_allowed=True,
        can_transition_reservation=False,
        reason_code="VENUE_RECONCILE_MATCHED",
        detail_vi="Đối soát startup/reconnect khớp hoàn toàn.",
        deduped_fill_count=len(fills),
        activity_classification=classification,
    )


def reconcile_venue_reconnect(
    snapshot: VenueAuthoritativeSnapshot | None,
    *,
    last_stream_event_at: datetime,
    expected_workspace_id: UUID,
    expected_account_id: UUID,
    expected_environment: str,
    known_state: KnownAccountState,
    protected_symbols: frozenset[str],
    unknown_intents: Sequence[PendingUnknownIntent] = (),
    as_of: datetime,
    max_age: timedelta = timedelta(seconds=30),
) -> VenueReconcileDecision:
    """Phục hồi sau reconnect stream: giữ gate đóng nếu lịch sử REST không khép được gap."""
    if snapshot is None:
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="RECONNECT_SNAPSHOT_MISSING",
            detail_vi="Thiếu snapshot khi phục hồi reconnect.",
        )
    if snapshot.history_coverage_from is None or snapshot.history_coverage_from.astimezone(
        UTC
    ) > last_stream_event_at.astimezone(UTC):
        return VenueReconcileDecision(
            outcome=VenueReconcileOutcome.STALE_INCOMPLETE,
            entry_allowed=False,
            can_transition_reservation=False,
            reason_code="STREAM_GAP_UNCLOSABLE",
            detail_vi="Lịch sử REST không khép được khoảng trống stream; giữ cổng entry đóng.",
        )
    return reconcile_venue_startup(
        snapshot,
        expected_workspace_id=expected_workspace_id,
        expected_account_id=expected_account_id,
        expected_environment=expected_environment,
        known_state=known_state,
        protected_symbols=protected_symbols,
        unknown_intents=unknown_intents,
        as_of=as_of,
        max_age=max_age,
    )
