"""Port quan sát hoạt động account và classifier thuần (MB05.1, ruling 12–13).

Classifier chỉ phân loại: order/position có lineage của bot đi qua, phần còn lại là ngoại lai
(`EXTERNAL_ACTIVITY_UNRECONCILED`); snapshot thiếu/cũ/không đầy đủ/sai scope là
`ACCOUNT_ACTIVITY_UNVERIFIED`. Không có hành động adopt/hủy/đóng state ngoại lai nào ở đây.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

ACTIVITY_SOURCES = frozenset({"PAPER_LEDGER", "SCRIPTED", "VENUE"})


class AccountActivityError(RuntimeError):
    """Lỗi adapter/snapshot activity mang reason code an toàn."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _text(value: object, *, limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AccountActivityError("ACTIVITY_VALUE_INVALID")
    return value


def _quantity(value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise AccountActivityError("ACTIVITY_QUANTITY_INVALID")
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True, slots=True, order=True)
class ObservedOrder:
    """Order quan sát được trên account; chỉ chứa dữ liệu cần để đối chiếu (không secret)."""

    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    status: str

    def __post_init__(self) -> None:
        _text(self.client_order_id, limit=160)
        _text(self.symbol, limit=64)
        _text(self.side, limit=16)
        _quantity(self.quantity)
        _text(self.status, limit=32)
        object.__setattr__(self, "side", self.side.upper())

    def canonical(self) -> dict[str, str]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": _decimal_text(self.quantity),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, order=True)
class ObservedPosition:
    """Vị thế quan sát được; position "đã biết" khi khớp symbol/side/quantity của bot."""

    symbol: str
    side: str
    quantity: Decimal

    def __post_init__(self) -> None:
        _text(self.symbol, limit=64)
        _text(self.side, limit=16)
        _quantity(self.quantity)
        object.__setattr__(self, "side", self.side.upper())

    def canonical(self) -> dict[str, str]:
        return {"symbol": self.symbol, "side": self.side, "quantity": _decimal_text(self.quantity)}


def _sha256(value: object) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AccountActivitySnapshot:
    """Ảnh chụp bất biến activity account tại `observed_at` (UTC); hash không phụ thuộc thứ tự."""

    workspace_id: UUID
    account_id: UUID
    source: str
    cursor: str | None
    observed_at: datetime
    complete: bool
    orders: tuple[ObservedOrder, ...] = ()
    positions: tuple[ObservedPosition, ...] = ()
    snapshot_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.account_id, UUID):
            raise AccountActivityError("ACTIVITY_SCOPE_INVALID")
        if self.source not in ACTIVITY_SOURCES:
            raise AccountActivityError("ACTIVITY_SOURCE_INVALID")
        if self.cursor is not None:
            _text(self.cursor, limit=240)
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != timedelta(0)
        ):
            raise AccountActivityError("ACTIVITY_TIME_NOT_UTC")
        if not isinstance(self.complete, bool):
            raise AccountActivityError("ACTIVITY_VALUE_INVALID")
        if not all(isinstance(order, ObservedOrder) for order in self.orders) or not all(
            isinstance(position, ObservedPosition) for position in self.positions
        ):
            raise AccountActivityError("ACTIVITY_VALUE_INVALID")
        object.__setattr__(self, "orders", tuple(sorted(self.orders)))
        object.__setattr__(self, "positions", tuple(sorted(self.positions)))
        object.__setattr__(
            self,
            "snapshot_hash",
            _sha256(
                {
                    "workspace_id": str(self.workspace_id),
                    "account_id": str(self.account_id),
                    "source": self.source,
                    "cursor": self.cursor,
                    "observed_at": self.observed_at.isoformat(),
                    "complete": self.complete,
                    "orders": [order.canonical() for order in self.orders],
                    "positions": [position.canonical() for position in self.positions],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class KnownAccountState:
    """State có lineage của bot: client order id `paper:<idempotency_key>` và vị thế mở."""

    workspace_id: UUID
    account_id: UUID
    known_client_order_ids: frozenset[str]
    open_positions: tuple[ObservedPosition, ...]


class ActivityVerdict(StrEnum):
    CLEAN = "CLEAN"
    EXTERNAL_ACTIVITY_UNRECONCILED = "EXTERNAL_ACTIVITY_UNRECONCILED"
    ACCOUNT_ACTIVITY_UNVERIFIED = "ACCOUNT_ACTIVITY_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class ActivityClassification:
    """Kết quả phân loại thuần; chỉ mô tả, không mang hành động xử lý state ngoại lai."""

    verdict: ActivityVerdict
    reason_code: str | None
    foreign_orders: tuple[ObservedOrder, ...] = ()
    foreign_positions: tuple[ObservedPosition, ...] = ()
    fingerprint: str | None = None
    snapshot_hash: str | None = None


@runtime_checkable
class AccountActivityPort(Protocol):
    """Nguồn activity account; lỗi phải raise `AccountActivityError` (sẽ thành UNVERIFIED)."""

    async def inspect(self, account_id: UUID, cursor: str | None) -> AccountActivitySnapshot: ...


def _unverified(reason_code: str, snapshot_hash: str | None = None) -> ActivityClassification:
    return ActivityClassification(
        verdict=ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED,
        reason_code=reason_code,
        snapshot_hash=snapshot_hash,
    )


def classify_account_activity(
    snapshot: AccountActivitySnapshot | None,
    known_state: KnownAccountState,
    *,
    now: datetime,
    max_snapshot_age: timedelta,
) -> ActivityClassification:
    """Phân loại snapshot theo state đã biết; `now` do caller truyền (domain không đọc đồng hồ)."""
    if snapshot is None:
        return _unverified("ACTIVITY_SNAPSHOT_MISSING")
    if (
        snapshot.workspace_id != known_state.workspace_id
        or snapshot.account_id != known_state.account_id
    ):
        return _unverified("ACTIVITY_SCOPE_MISMATCH", snapshot.snapshot_hash)
    if not snapshot.complete:
        return _unverified("ACTIVITY_SNAPSHOT_INCOMPLETE", snapshot.snapshot_hash)
    if snapshot.observed_at > now:
        return _unverified("ACTIVITY_SNAPSHOT_FUTURE", snapshot.snapshot_hash)
    if now - snapshot.observed_at > max_snapshot_age:
        return _unverified("ACTIVITY_SNAPSHOT_STALE", snapshot.snapshot_hash)

    foreign_orders = tuple(
        order
        for order in snapshot.orders
        if order.client_order_id not in known_state.known_client_order_ids
    )
    remaining = list(known_state.open_positions)
    foreign_positions: list[ObservedPosition] = []
    for position in snapshot.positions:
        if position in remaining:
            remaining.remove(position)
        else:
            foreign_positions.append(position)
    if not foreign_orders and not foreign_positions:
        return ActivityClassification(
            verdict=ActivityVerdict.CLEAN,
            reason_code=None,
            snapshot_hash=snapshot.snapshot_hash,
        )
    # Fingerprint bỏ status/cursor/thời điểm: cùng state lạ quan sát lại không mở incident trùng.
    fingerprint = _sha256(
        {
            "workspace_id": str(snapshot.workspace_id),
            "account_id": str(snapshot.account_id),
            "orders": [
                {key: value for key, value in order.canonical().items() if key != "status"}
                for order in sorted(foreign_orders)
            ],
            "positions": [position.canonical() for position in sorted(foreign_positions)],
        }
    )
    return ActivityClassification(
        verdict=ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED,
        reason_code="EXTERNAL_ACTIVITY_UNRECONCILED",
        foreign_orders=tuple(sorted(foreign_orders)),
        foreign_positions=tuple(sorted(foreign_positions)),
        fingerprint=fingerprint,
        snapshot_hash=snapshot.snapshot_hash,
    )
