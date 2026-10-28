"""Vốn đúng thời điểm; mọi dữ kiện và chính sách phải được truyền tường minh."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Context, Decimal, localcontext
from enum import StrEnum

ZERO = Decimal("0")
USDT_QUANTUM = Decimal("0.00000001")


class EquityReasonCode(StrEnum):
    """Mã từ chối ổn định để tầng ngân sách không phải phân tích thông báo."""

    BASIS_UNKNOWN = "BASIS_UNKNOWN"
    SNAPSHOT_UNKNOWN = "SNAPSHOT_UNKNOWN"
    DECIMAL_REQUIRED = "DECIMAL_REQUIRED"
    AMOUNT_INVALID = "AMOUNT_INVALID"
    TIMESTAMP_INVALID = "TIMESTAMP_INVALID"
    OBSERVATION_FUTURE = "OBSERVATION_FUTURE"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    LINEAGE_INVALID = "LINEAGE_INVALID"
    POLICY_INVALID = "POLICY_INVALID"
    EQUITY_STALE = "EQUITY_STALE"
    MARK_MISSING = "MARK_MISSING"
    MARK_STALE = "MARK_STALE"
    POSITION_DUPLICATE = "POSITION_DUPLICATE"
    DIRECTION_INVALID = "DIRECTION_INVALID"
    CONVERSION_MISSING = "CONVERSION_MISSING"
    CONVERSION_UNKNOWN = "CONVERSION_UNKNOWN"
    CONVERSION_REJECTED = "CONVERSION_REJECTED"
    CONVERSION_STALE = "CONVERSION_STALE"
    ROUTE_UNSUPPORTED = "ROUTE_UNSUPPORTED"


class EquityValidationError(ValueError):
    """Lỗi domain có mã máy đọc được và mô tả tiếng Việt."""

    def __init__(self, reason_code: EquityReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(f"Dữ kiện vốn không hợp lệ: {reason_code.value}.")


def _require(condition: bool, code: EquityReasonCode) -> None:
    if not condition:
        raise EquityValidationError(code)


def _amount(value: Decimal, *, signed: bool = False, positive: bool = False) -> None:
    _require(isinstance(value, Decimal), EquityReasonCode.DECIMAL_REQUIRED)
    _require(value.is_finite(), EquityReasonCode.AMOUNT_INVALID)
    _require(
        signed or (value > ZERO if positive else value >= ZERO), EquityReasonCode.AMOUNT_INVALID
    )


def _utc(value: datetime) -> None:
    _require(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0),
        EquityReasonCode.TIMESTAMP_INVALID,
    )


def _age(value: timedelta) -> None:
    _require(
        isinstance(value, timedelta) and value >= timedelta(0), EquityReasonCode.POLICY_INVALID
    )


def _observed(value: datetime, as_of: datetime) -> None:
    _utc(value)
    _require(value <= as_of, EquityReasonCode.OBSERVATION_FUTURE)


def _text(*values: str) -> None:
    _require(
        all(isinstance(value, str) and bool(value.strip()) for value in values),
        EquityReasonCode.LINEAGE_INVALID,
    )


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        # Không dùng normalize(): nó phụ thuộc precision của context bên ngoài.
        text = format(value, "f")
        return "0" if value == ZERO else text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, (datetime, timedelta)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _context(values: Sequence[Decimal]) -> Context:
    # Đủ chữ số cho tổng/tích chính xác, độc lập context Decimal của caller.
    precision = 32 + sum(
        len(value.as_tuple().digits) + abs(int(value.as_tuple().exponent)) for value in values
    )
    return Context(prec=precision)


@dataclass(frozen=True, slots=True, kw_only=True)
class EquityBasis:
    """Nguồn vốn đã xác nhận cùng scope và chính sách tuổi dữ liệu tường minh."""

    workspace_id: str
    bot_id: str
    account_id: str
    asset: str
    as_of: datetime
    captured_at: datetime
    available_at: datetime
    source: str
    source_version: str
    max_mark_age: timedelta
    max_equity_age: timedelta
    known: bool = True
    unknown_reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PaperEquityBasis(EquityBasis):
    """Seed chưa cộng realized; realized là PnL ròng đã gồm phí đã chốt."""

    initial_capital_before_realized_pnl: Decimal
    cumulative_realized_pnl: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class PaperLedgerEquityBasis(EquityBasis):
    """Vốn PAPER do ledger SQL định giá (một phép tính dùng chung với aggregate workspace).

    `open_position_pnl` đã gồm PnL phần exit một phần, UPNL phần còn mở tại mark hợp lệ và
    chi phí đã ghi của trade đang mở (review I3); không còn vị thế/mark rời để tính lại nên
    snapshot loại này không mang `positions`/`mark_refs`.
    """

    initial_capital_before_realized_pnl: Decimal
    cumulative_realized_pnl: Decimal
    open_position_pnl: Decimal
    open_position_count: int
    # Các khóa/hash nguồn được đọc trong cùng snapshot SQL point-in-time.
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueEquityBasis(EquityBasis):
    """Chỉ là hợp đồng kiểu; không kết nối venue hoặc suy đoán ngữ nghĩa API."""

    total_equity_including_realized_and_excluding_unrealized: Decimal


@dataclass(frozen=True, slots=True)
class OpenPositionValuation:
    """Vị thế hợp đồng tuyến tính có quantity dương và hướng riêng biệt."""

    workspace_id: str
    bot_id: str
    account_id: str
    position_id: str
    symbol: str
    asset: str
    direction: str
    quantity: Decimal
    entry_price: Decimal
    contract_multiplier: Decimal
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class MarketMark:
    """Giá đã gắn scope, thời điểm quan sát/khả dụng và nguồn bất biến."""

    workspace_id: str
    bot_id: str
    account_id: str
    symbol: str
    asset: str
    price: Decimal
    as_of: datetime
    available_at: datetime
    source: str
    source_version: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class RiskConversionObservation:
    """Quan sát cặp trực tiếp được policy bên gọi phê duyệt; không tự ghép route."""

    workspace_id: str
    base_asset: str
    quote_asset: str
    rate: Decimal
    as_of: datetime
    available_at: datetime
    source_ref: str
    source_version: str
    source_hash: str
    policy_version_id: str
    route: str
    approved: bool
    quality: str = "ACCEPTED"
    known: bool = True
    unknown_reason: str | None = None

    @property
    def observation_hash(self) -> str:
        """Hash nội dung giữ cả route, policy và nguồn để lưu lineage sau này."""
        return _hash(self)


@dataclass(frozen=True, slots=True)
class AccountEquitySnapshot:
    """Ảnh vốn bất biến; source_hash bao phủ basis, positions và mọi mark đã dùng."""

    workspace_id: str
    bot_id: str
    account_id: str
    asset: str
    equity_amount: Decimal
    equity_basis: str
    seed_capital: Decimal | None
    cumulative_realized_pnl: Decimal | None
    unrealized_pnl: Decimal
    captured_at: datetime
    as_of: datetime
    available_at: datetime
    source: str
    source_version: str
    source_hash: str
    mark_refs: tuple[MarketMark, ...]
    basis: PaperEquityBasis | VenueEquityBasis | PaperLedgerEquityBasis
    positions: tuple[OpenPositionValuation, ...]
    known: bool = True
    unknown_reason: str | None = None
    schema_version: int = 1


def _scope(item: EquityBasis | OpenPositionValuation | MarketMark, basis: EquityBasis) -> None:
    _require(
        (item.workspace_id, item.bot_id, item.account_id)
        == (basis.workspace_id, basis.bot_id, basis.account_id),
        EquityReasonCode.SCOPE_MISMATCH,
    )
    _require(item.asset == basis.asset, EquityReasonCode.CURRENCY_MISMATCH)


def mark_to_market_equity(
    basis: PaperEquityBasis | VenueEquityBasis | PaperLedgerEquityBasis,
    open_positions: Sequence[OpenPositionValuation],
    marks: Mapping[str, MarketMark],
    as_of: datetime,
) -> AccountEquitySnapshot:
    """Tính seed + realized + UPNL; venue total không được cộng realized lần hai."""
    _utc(as_of)
    _require(
        type(basis) in (PaperEquityBasis, VenueEquityBasis, PaperLedgerEquityBasis),
        EquityReasonCode.BASIS_UNKNOWN,
    )
    if isinstance(basis, PaperLedgerEquityBasis):
        return _ledger_equity(basis, open_positions, marks, as_of)
    _require(basis.known is True and basis.unknown_reason is None, EquityReasonCode.BASIS_UNKNOWN)
    _text(
        basis.workspace_id,
        basis.bot_id,
        basis.account_id,
        basis.asset,
        basis.source,
        basis.source_version,
    )
    for stamp in (basis.as_of, basis.captured_at, basis.available_at):
        _observed(stamp, as_of)
    _require(basis.as_of == as_of, EquityReasonCode.AS_OF_MISMATCH)
    _age(basis.max_mark_age)
    _age(basis.max_equity_age)
    _require(basis.captured_at <= basis.available_at, EquityReasonCode.TIMESTAMP_INVALID)
    _require(as_of - basis.captured_at <= basis.max_equity_age, EquityReasonCode.EQUITY_STALE)
    if isinstance(basis, PaperEquityBasis):
        _amount(basis.initial_capital_before_realized_pnl)
        _amount(basis.cumulative_realized_pnl, signed=True)
        seed = basis.initial_capital_before_realized_pnl
        realized: Decimal | None = basis.cumulative_realized_pnl
        values: list[Decimal] = [seed, basis.cumulative_realized_pnl]
    else:
        _amount(basis.total_equity_including_realized_and_excluding_unrealized)
        seed, realized = None, None
        values = [basis.total_equity_including_realized_and_excluding_unrealized]
    positions = tuple(open_positions)
    ids: set[str] = set()
    used_marks: dict[str, MarketMark] = {}
    for position in positions:
        _require(isinstance(position, OpenPositionValuation), EquityReasonCode.LINEAGE_INVALID)
        _scope(position, basis)
        _text(position.position_id, position.symbol)
        _require(position.position_id not in ids, EquityReasonCode.POSITION_DUPLICATE)
        ids.add(position.position_id)
        _require(position.direction in ("long", "short"), EquityReasonCode.DIRECTION_INVALID)
        _observed(position.opened_at, as_of)
        for value in (position.quantity, position.entry_price, position.contract_multiplier):
            _amount(value, positive=True)
        mark = marks.get(position.symbol)
        _require(isinstance(mark, MarketMark), EquityReasonCode.MARK_MISSING)
        assert mark is not None
        _scope(mark, basis)
        _require(mark.symbol == position.symbol, EquityReasonCode.CURRENCY_MISMATCH)
        _text(mark.source, mark.source_version, mark.source_hash)
        _amount(mark.price, positive=True)
        _observed(mark.as_of, as_of)
        _observed(mark.available_at, as_of)
        _require(mark.as_of <= mark.available_at, EquityReasonCode.TIMESTAMP_INVALID)
        _require(as_of - mark.as_of <= basis.max_mark_age, EquityReasonCode.MARK_STALE)
        used_marks[mark.symbol] = mark
        values.extend(
            (position.quantity, position.entry_price, position.contract_multiplier, mark.price)
        )
    with localcontext(_context(values)):
        unrealized = sum(
            (
                (used_marks[item.symbol].price - item.entry_price)
                * item.quantity
                * item.contract_multiplier
                * (1 if item.direction == "long" else -1)
                for item in positions
            ),
            ZERO,
        )
        total = (
            sum(values[:2] if isinstance(basis, PaperEquityBasis) else values[:1], ZERO)
            + unrealized
        )
    _amount(total)
    ordered_positions = tuple(sorted(positions, key=lambda item: item.position_id))
    ordered_marks = tuple(used_marks[key] for key in sorted(used_marks))
    return AccountEquitySnapshot(
        basis.workspace_id,
        basis.bot_id,
        basis.account_id,
        basis.asset,
        total,
        "PAPER" if isinstance(basis, PaperEquityBasis) else "VENUE_TOTAL_EXCLUDING_UNREALIZED",
        seed,
        realized,
        unrealized,
        basis.captured_at,
        as_of,
        max((basis.available_at, *(item.available_at for item in ordered_marks))),
        basis.source,
        basis.source_version,
        _hash((basis, ordered_positions, ordered_marks)),
        ordered_marks,
        basis,
        ordered_positions,
    )


def _ledger_equity(
    basis: PaperLedgerEquityBasis,
    open_positions: Sequence[OpenPositionValuation],
    marks: Mapping[str, MarketMark],
    as_of: datetime,
) -> AccountEquitySnapshot:
    """Seed + realized + PnL vị thế mở đã được ledger SQL định giá; không tính lại từ mark."""
    _require(basis.known is True and basis.unknown_reason is None, EquityReasonCode.BASIS_UNKNOWN)
    _text(
        basis.workspace_id,
        basis.bot_id,
        basis.account_id,
        basis.asset,
        basis.source,
        basis.source_version,
    )
    for stamp in (basis.as_of, basis.captured_at, basis.available_at):
        _observed(stamp, as_of)
    _require(basis.as_of == as_of, EquityReasonCode.AS_OF_MISMATCH)
    _age(basis.max_mark_age)
    _age(basis.max_equity_age)
    _require(basis.captured_at <= basis.available_at, EquityReasonCode.TIMESTAMP_INVALID)
    _require(as_of - basis.captured_at <= basis.max_equity_age, EquityReasonCode.EQUITY_STALE)
    _amount(basis.initial_capital_before_realized_pnl)
    _amount(basis.cumulative_realized_pnl, signed=True)
    _amount(basis.open_position_pnl, signed=True)
    _require(
        isinstance(basis.open_position_count, int)
        and not isinstance(basis.open_position_count, bool)
        and basis.open_position_count >= 0,
        EquityReasonCode.LINEAGE_INVALID,
    )
    # Định giá đã nằm trong ledger SQL: không nhận vị thế/mark rời để tránh hai nguồn sự thật.
    _require(not open_positions and not marks, EquityReasonCode.LINEAGE_INVALID)
    _require(
        basis.open_position_count > 0 or basis.open_position_pnl == ZERO,
        EquityReasonCode.LINEAGE_INVALID,
    )
    values = [
        basis.initial_capital_before_realized_pnl,
        basis.cumulative_realized_pnl,
        basis.open_position_pnl,
    ]
    with localcontext(_context(values)):
        total = values[0] + values[1] + values[2]
    # Vốn âm là không xác định (khớp cờ unknown của SQL), không phải vốn 0.
    _amount(total)
    return AccountEquitySnapshot(
        basis.workspace_id,
        basis.bot_id,
        basis.account_id,
        basis.asset,
        total,
        "PAPER",
        basis.initial_capital_before_realized_pnl,
        basis.cumulative_realized_pnl,
        basis.open_position_pnl,
        basis.captured_at,
        as_of,
        basis.available_at,
        basis.source,
        basis.source_version,
        _hash((basis, (), ())),
        (),
        basis,
        (),
    )


def normalize_equity_to_usdt(
    snapshot: AccountEquitySnapshot,
    conversion: RiskConversionObservation | None,
    as_of: datetime,
    max_age: timedelta,
) -> Decimal:
    """USDT giữ nguyên chính xác; FX trực tiếp làm tròn xuống 8 chữ số thập phân."""
    _utc(as_of)
    _age(max_age)
    _require(type(snapshot) is AccountEquitySnapshot, EquityReasonCode.SNAPSHOT_UNKNOWN)
    _require(
        snapshot.known is True and snapshot.unknown_reason is None,
        EquityReasonCode.SNAPSHOT_UNKNOWN,
    )
    _observed(snapshot.as_of, as_of)
    _require(snapshot.as_of == as_of, EquityReasonCode.AS_OF_MISMATCH)
    _amount(snapshot.equity_amount)
    expected = mark_to_market_equity(
        snapshot.basis,
        snapshot.positions,
        {item.symbol: item for item in snapshot.mark_refs},
        as_of,
    )
    _require(snapshot == expected, EquityReasonCode.LINEAGE_INVALID)
    if snapshot.asset == "USDT":
        return snapshot.equity_amount
    _require(conversion is not None, EquityReasonCode.CONVERSION_MISSING)
    assert conversion is not None
    _require(
        type(conversion) is RiskConversionObservation
        and conversion.known is True
        and conversion.unknown_reason is None,
        EquityReasonCode.CONVERSION_UNKNOWN,
    )
    _require(
        conversion.approved is True and conversion.quality == "ACCEPTED",
        EquityReasonCode.CONVERSION_REJECTED,
    )
    _require(conversion.route == "DIRECT", EquityReasonCode.ROUTE_UNSUPPORTED)
    _require(conversion.workspace_id == snapshot.workspace_id, EquityReasonCode.SCOPE_MISMATCH)
    _require(
        conversion.base_asset == snapshot.asset and conversion.quote_asset == "USDT",
        EquityReasonCode.CURRENCY_MISMATCH,
    )
    _text(
        conversion.source_ref,
        conversion.source_version,
        conversion.source_hash,
        conversion.policy_version_id,
    )
    _amount(conversion.rate, positive=True)
    _observed(conversion.as_of, as_of)
    _observed(conversion.available_at, as_of)
    _require(conversion.as_of <= conversion.available_at, EquityReasonCode.TIMESTAMP_INVALID)
    _require(as_of - conversion.as_of <= max_age, EquityReasonCode.CONVERSION_STALE)
    with localcontext(_context([snapshot.equity_amount, conversion.rate])):
        return (snapshot.equity_amount * conversion.rate).quantize(
            USDT_QUANTUM, rounding=ROUND_FLOOR
        )
