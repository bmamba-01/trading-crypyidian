"""Domain logic cho preflight và đối soát trạng thái sàn chỉ đọc (OPS07F / Task 5).

Cung cấp các quy tắc đối soát fail-closed giữa snapshot sàn (authoritative venue snapshot)
và trạng thái cục bộ của hệ thống (intents, ledger, reservations, known positions, protection).
Tuyệt đối không gọi hay phụ thuộc bất kỳ API đặt, sửa hoặc hủy lệnh nào.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

ZERO = Decimal("0")


class PreflightReasonCode(StrEnum):
    """Mã lý do chuẩn hóa cho kết quả preflight và đối soát."""

    PASSED = "PREFLIGHT_PASSED"
    ACCOUNT_MISMATCH = "PREFLIGHT_ACCOUNT_MISMATCH"
    ENVIRONMENT_MISMATCH = "PREFLIGHT_ENVIRONMENT_MISMATCH"
    SNAPSHOT_STALE = "PREFLIGHT_SNAPSHOT_STALE"
    CLOCK_DRIFT_EXCESSIVE = "PREFLIGHT_CLOCK_DRIFT_EXCESSIVE"
    API_PERMISSIONS_INSUFFICIENT = "PREFLIGHT_API_PERMISSIONS_INSUFFICIENT"
    API_WITHDRAWAL_ENABLED = "PREFLIGHT_API_WITHDRAWAL_ENABLED"
    STREAM_GAP_DETECTED = "PREFLIGHT_STREAM_GAP_DETECTED"
    FOREIGN_POSITION = "PREFLIGHT_FOREIGN_POSITION"
    FOREIGN_ORDER = "PREFLIGHT_FOREIGN_ORDER"
    PROTECTION_MISSING = "PREFLIGHT_PROTECTION_MISSING"
    EXPOSURE_MISMATCH = "PREFLIGHT_EXPOSURE_MISMATCH"
    UNKNOWN_STATE_HELD = "PREFLIGHT_UNKNOWN_STATE_HELD"
    ENDPOINT_FAILURE = "PREFLIGHT_ENDPOINT_FAILURE"


@dataclass(frozen=True, slots=True)
class VenuePositionSnapshot:
    """Ảnh chụp vị thế hiện hành trên sàn."""

    symbol: str
    position_side: str  # BOTH, LONG, SHORT
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class VenueOrderSummary:
    """Tóm tắt lệnh đang mở trên sàn (regular hoặc algo protective order)."""

    symbol: str
    order_id: str
    client_order_id: str
    side: str  # BUY, SELL
    type: str  # LIMIT, MARKET, STOP_MARKET, TAKE_PROFIT_MARKET, etc.
    orig_qty: Decimal
    executed_qty: Decimal
    price: Decimal
    status: str
    is_algo: bool = False
    stop_price: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class LivePreflightSnapshot:
    """Authoritative read-only snapshot thu thập từ sàn giao dịch."""

    account_id: str
    environment: str
    observed_at: datetime
    server_time: datetime
    api_permissions: frozenset[str]
    balances: dict[str, Decimal]
    positions: list[VenuePositionSnapshot]
    open_orders: list[VenueOrderSummary]
    algo_orders: list[VenueOrderSummary]
    recent_activity: list[dict[str, Any]] = field(default_factory=list)
    raw_reference: str | None = None

    def compute_hash(self) -> str:
        """Tính mã băm SHA-256 bảo đảm tính toàn vẹn của snapshot."""
        components = {
            "account_id": self.account_id,
            "environment": self.environment,
            "observed_at": self.observed_at.isoformat(),
            "server_time": self.server_time.isoformat(),
            "api_permissions": sorted(self.api_permissions),
            "balances": {k: str(v) for k, v in sorted(self.balances.items())},
            "positions": [
                {
                    "symbol": p.symbol,
                    "position_side": p.position_side,
                    "quantity": str(p.quantity),
                    "entry_price": str(p.entry_price),
                    "mark_price": str(p.mark_price),
                }
                for p in sorted(self.positions, key=lambda x: (x.symbol, x.position_side))
            ],
            "open_orders": [
                {
                    "symbol": o.symbol,
                    "order_id": o.order_id,
                    "client_order_id": o.client_order_id,
                    "side": o.side,
                    "type": o.type,
                    "orig_qty": str(o.orig_qty),
                }
                for o in sorted(self.open_orders, key=lambda x: x.order_id)
            ],
            "algo_orders": [
                {
                    "symbol": a.symbol,
                    "order_id": a.order_id,
                    "client_order_id": a.client_order_id,
                    "side": a.side,
                    "type": a.type,
                    "orig_qty": str(a.orig_qty),
                }
                for a in sorted(self.algo_orders, key=lambda x: x.order_id)
            ],
        }
        digest = hashlib.sha256(
            json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest


@dataclass(frozen=True, slots=True)
class LocalReconciliationState:
    """Trạng thái đã biết của bot / workspace cục bộ."""

    account_id: str
    environment: str
    known_client_order_ids: frozenset[str]
    known_positions: dict[tuple[str, str], Decimal]  # (symbol, position_side) -> quantity
    active_protection_symbols: frozenset[str]  # symbol đã có stop loss / algo protective order
    unknown_intent_ids: list[UUID] = field(default_factory=list)
    last_processed_stream_seq: int | None = None


@dataclass(frozen=True, slots=True)
class LivePreflightVerdict:
    """Kết luận đối soát và preflight có thể kiểm toán được."""

    passed: bool
    reason_code: PreflightReasonCode
    detail_vi: str
    evidence_hash: str
    checked_at: datetime
    hold_reservations: bool = True
    incidents: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason_code": self.reason_code.value,
            "detail_vi": self.detail_vi,
            "evidence_hash": self.evidence_hash,
            "checked_at": self.checked_at.isoformat(),
            "hold_reservations": self.hold_reservations,
            "incidents": self.incidents,
        }


def evaluate_live_preflight(
    snapshot: LivePreflightSnapshot,
    local_state: LocalReconciliationState,
    *,
    now: datetime | None = None,
    max_snapshot_age: timedelta = timedelta(seconds=15),
    max_clock_drift: timedelta = timedelta(milliseconds=1000),
) -> LivePreflightVerdict:
    """Đánh giá đối soát preflight hoàn toàn độc lập, fail-closed.

    Không bao giờ giải phóng reservation khi có trạng thái UNKNOWN, foreign hay discrepancy.
    """
    check_time = now or datetime.now(UTC)
    snapshot_hash = snapshot.compute_hash()

    def _verdict(
        passed: bool,
        code: PreflightReasonCode,
        detail_vi: str,
        *,
        hold: bool = True,
        incidents: list[dict[str, Any]] | None = None,
    ) -> LivePreflightVerdict:
        verdict_data = {
            "passed": passed,
            "code": code.value,
            "snapshot_hash": snapshot_hash,
            "checked_at": check_time.isoformat(),
        }
        evidence_hash = hashlib.sha256(
            json.dumps(verdict_data, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return LivePreflightVerdict(
            passed=passed,
            reason_code=code,
            detail_vi=detail_vi,
            evidence_hash=evidence_hash,
            checked_at=check_time,
            hold_reservations=hold,
            incidents=incidents or [],
        )

    # 1. Kiểm tra account & environment binding
    if snapshot.account_id != local_state.account_id:
        return _verdict(
            False,
            PreflightReasonCode.ACCOUNT_MISMATCH,
            f"Tài khoản ({snapshot.account_id}) không khớp bot ({local_state.account_id}).",
        )

    if snapshot.environment.upper() != local_state.environment.upper():
        return _verdict(
            False,
            PreflightReasonCode.ENVIRONMENT_MISMATCH,
            f"Môi trường ({snapshot.environment}) không khớp cấu hình ({local_state.environment}).",
        )

    # 2. Kiểm tra độ tươi của snapshot và độ lệch đồng hồ (clock drift)
    snapshot_age = abs(check_time - snapshot.observed_at)
    if snapshot_age > max_snapshot_age:
        max_s = max_snapshot_age.total_seconds()
        return _verdict(
            False,
            PreflightReasonCode.SNAPSHOT_STALE,
            f"Snapshot sàn đã quá cũ ({snapshot_age.total_seconds():.1f}s > {max_s:.1f}s).",
        )

    clock_drift = abs(snapshot.server_time - snapshot.observed_at)
    if clock_drift > max_clock_drift:
        drift_ms = clock_drift.total_seconds() * 1000
        max_ms = max_clock_drift.total_seconds() * 1000
        return _verdict(
            False,
            PreflightReasonCode.CLOCK_DRIFT_EXCESSIVE,
            f"Độ lệch thời gian sàn ({drift_ms:.0f}ms) vượt dung sai cho phép ({max_ms:.0f}ms).",
        )

    # 3. Kiểm tra quyền API (phải có READ/TRADE, cấm WITHDRAWAL/TRANSFER)
    upper_perms = {p.upper() for p in snapshot.api_permissions}
    dangerous_perms = {"WITHDRAW", "WITHDRAWAL", "TRANSFER", "INTERNAL_TRANSFER"}
    found_dangerous = upper_perms.intersection(dangerous_perms)
    if found_dangerous:
        return _verdict(
            False,
            PreflightReasonCode.API_WITHDRAWAL_ENABLED,
            f"API key có quyền nguy hiểm không được phép: {', '.join(sorted(found_dangerous))}.",
        )

    if "READ" not in upper_perms:
        return _verdict(
            False,
            PreflightReasonCode.API_PERMISSIONS_INSUFFICIENT,
            "API key thiếu quyền đọc (READ).",
        )

    # 4. Kiểm tra trạng thái UNKNOWN đang tồn đọng cục bộ
    if local_state.unknown_intent_ids:
        count_u = len(local_state.unknown_intent_ids)
        return _verdict(
            False,
            PreflightReasonCode.UNKNOWN_STATE_HELD,
            f"Có {count_u} intent đang ở trạng thái UNKNOWN cần được xử lý dứt điểm trước khi mở.",
            hold=True,
        )

    # 5. Kiểm tra stream gap trong recent_activity (nếu có sequence)
    if local_state.last_processed_stream_seq is not None and snapshot.recent_activity:
        for ev in snapshot.recent_activity:
            seq = ev.get("stream_seq")
            if seq is not None and seq > local_state.last_processed_stream_seq + 1:
                last_s = local_state.last_processed_stream_seq
                return _verdict(
                    False,
                    PreflightReasonCode.STREAM_GAP_DETECTED,
                    f"Phát hiện lỗ hổng chuỗi sự kiện stream (nhảy từ {last_s} sang {seq}).",
                )

    incidents: list[dict[str, Any]] = []

    # 6. Kiểm tra vị thế ngoại lai (Foreign Position) & sai lệch exposure
    active_venue_positions = [p for p in snapshot.positions if abs(p.quantity) > ZERO]
    for p in active_venue_positions:
        key = (p.symbol, p.position_side)
        if key not in local_state.known_positions:
            incidents.append(
                {
                    "kind": "FOREIGN_POSITION",
                    "symbol": p.symbol,
                    "position_side": p.position_side,
                    "quantity": str(p.quantity),
                }
            )
        else:
            known_qty = local_state.known_positions[key]
            if abs(p.quantity - known_qty) > Decimal("0.00000001"):
                incidents.append(
                    {
                        "kind": "EXPOSURE_MISMATCH",
                        "symbol": p.symbol,
                        "position_side": p.position_side,
                        "venue_qty": str(p.quantity),
                        "known_qty": str(known_qty),
                    }
                )

    if any(i["kind"] == "FOREIGN_POSITION" for i in incidents):
        return _verdict(
            False,
            PreflightReasonCode.FOREIGN_POSITION,
            "Phát hiện vị thế mở ngoại lai trên sàn không thuộc quản lý của bot.",
            incidents=incidents,
        )

    if any(i["kind"] == "EXPOSURE_MISMATCH" for i in incidents):
        return _verdict(
            False,
            PreflightReasonCode.EXPOSURE_MISMATCH,
            "Khối lượng vị thế trên sàn sai lệch so với dữ liệu sổ cái nội bộ.",
            incidents=incidents,
        )

    # 7. Kiểm tra thiếu lệnh bảo vệ (Protection Missing)
    # Nếu vị thế mở mà symbol không có active protection (stop loss / algo)
    for p in active_venue_positions:
        if p.symbol not in local_state.active_protection_symbols:
            incidents.append(
                {
                    "kind": "PROTECTION_MISSING",
                    "symbol": p.symbol,
                    "position_side": p.position_side,
                }
            )

    if any(i["kind"] == "PROTECTION_MISSING" for i in incidents):
        return _verdict(
            False,
            PreflightReasonCode.PROTECTION_MISSING,
            "Có vị thế đang mở trên sàn nhưng thiếu lệnh cắt lỗ / bảo vệ rủi ro.",
            incidents=incidents,
        )

    # 8. Kiểm tra lệnh mở ngoại lai (Foreign Orders)
    # Lệnh mở trên sàn mà client_order_id không nằm trong known_client_order_ids
    all_venue_orders = snapshot.open_orders + snapshot.algo_orders
    for ord_item in all_venue_orders:
        if ord_item.client_order_id not in local_state.known_client_order_ids:
            incidents.append(
                {
                    "kind": "FOREIGN_ORDER",
                    "symbol": ord_item.symbol,
                    "order_id": ord_item.order_id,
                    "client_order_id": ord_item.client_order_id,
                }
            )

    if any(i["kind"] == "FOREIGN_ORDER" for i in incidents):
        return _verdict(
            False,
            PreflightReasonCode.FOREIGN_ORDER,
            "Phát hiện lệnh đang mở ngoại lai trên sàn không có trong sổ cái bot.",
            incidents=incidents,
        )

    # 9. Tất cả kiểm tra đều đạt
    return _verdict(
        True,
        PreflightReasonCode.PASSED,
        "Đối soát trạng thái sàn thành công, mọi vị thế và bảo vệ khớp hoàn toàn.",
        hold=False,
    )
