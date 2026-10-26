"""Ngân sách rủi ro Multi-Bot, reservation và breach; dữ kiện và chính sách truyền tường minh."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, localcontext
from enum import StrEnum
from uuid import UUID, uuid4

from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    EquityValidationError,
    RiskConversionObservation,
    normalize_equity_to_usdt,
)

ZERO = Decimal("0")
USDT_QUANTUM = Decimal("0.00000001")


class BudgetReasonCode(StrEnum):
    """Mã kết quả và lý do từ chối ổn định của tầng ngân sách rủi ro."""

    APPROVED = "APPROVED"
    WORKSPACE_CAP_EXCEEDED = "WORKSPACE_CAP_EXCEEDED"
    WORKSPACE_EXPOSURE_UNKNOWN = "WORKSPACE_EXPOSURE_UNKNOWN"
    WORKSPACE_EQUITY_UNKNOWN = "WORKSPACE_EQUITY_UNKNOWN"
    WORKSPACE_SCOPE_INCOMPLETE = "WORKSPACE_SCOPE_INCOMPLETE"
    ACCOUNT_CAP_EXCEEDED = "ACCOUNT_CAP_EXCEEDED"
    BOT_CAP_EXCEEDED = "BOT_CAP_EXCEEDED"
    CAPACITY_BREACH_AFTER_TIGHTENING = "CAPACITY_BREACH_AFTER_TIGHTENING"
    DENOMINATOR_UNUSABLE = "DENOMINATOR_UNUSABLE"
    BOT_DISABLED = "BOT_DISABLED"
    EQUITY_UNKNOWN = "EQUITY_UNKNOWN"
    EQUITY_STALE = "EQUITY_STALE"
    CONVERSION_UNKNOWN = "CONVERSION_UNKNOWN"
    CONVERSION_STALE = "CONVERSION_STALE"
    OBSERVATION_FUTURE = "OBSERVATION_FUTURE"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DECIMAL_REQUIRED = "DECIMAL_REQUIRED"
    AMOUNT_INVALID = "AMOUNT_INVALID"
    FRACTION_INVALID = "FRACTION_INVALID"
    TIMESTAMP_INVALID = "TIMESTAMP_INVALID"
    LINEAGE_INVALID = "LINEAGE_INVALID"


class BudgetDecisionResult(StrEnum):
    """Trạng thái quyết định ngân sách: được duyệt hoặc từ chối vào lệnh mới."""

    APPROVED = "APPROVED"
    DENY_NEW_ENTRY = "DENY_NEW_ENTRY"


class BudgetOperation(StrEnum):
    """Loại hành động ngân sách: vào lệnh mới, quản trị bảo vệ hoặc vòng đời reservation."""

    NEW_ENTRY = "NEW_ENTRY"
    PROTECTIVE_EXIT = "PROTECTIVE_EXIT"
    PROTECTIVE_MONITORING = "PROTECTIVE_MONITORING"
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    TRANSFER = "TRANSFER"


class ReservationStatus(StrEnum):
    """Trạng thái giữ ngân sách cho một lệnh intent."""

    RESERVED = "RESERVED"
    UNKNOWN = "UNKNOWN"
    RELEASED = "RELEASED"
    TRANSFERRED = "TRANSFERRED"


class BudgetValidationError(ValueError):
    """Lỗi validation dữ kiện ngân sách rủi ro có reason code máy đọc được."""

    def __init__(self, reason_code: BudgetReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(f"Dữ kiện ngân sách không hợp lệ: {reason_code.value}.")


class BudgetConflictError(RuntimeError):
    """Yêu cầu giữ ngân sách trùng idempotency key nhưng khác payload."""


def _require(condition: bool, code: BudgetReasonCode) -> None:
    if not condition:
        raise BudgetValidationError(code)


def _amount(value: Decimal, *, signed: bool = False, positive: bool = False) -> None:
    _require(isinstance(value, Decimal), BudgetReasonCode.DECIMAL_REQUIRED)
    _require(value.is_finite(), BudgetReasonCode.AMOUNT_INVALID)
    _require(
        signed or (value > ZERO if positive else value >= ZERO),
        BudgetReasonCode.AMOUNT_INVALID,
    )


def _fraction(value: Decimal) -> None:
    _require(isinstance(value, Decimal), BudgetReasonCode.DECIMAL_REQUIRED)
    _require(value.is_finite(), BudgetReasonCode.FRACTION_INVALID)
    _require(ZERO < value <= Decimal("1"), BudgetReasonCode.FRACTION_INVALID)


def _utc(value: datetime) -> None:
    _require(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0),
        BudgetReasonCode.TIMESTAMP_INVALID,
    )


def _text(*values: str) -> None:
    _require(
        all(isinstance(v, str) and bool(v.strip()) for v in values),
        BudgetReasonCode.LINEAGE_INVALID,
    )


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        text = format(value, "f")
        return "0" if value == ZERO else text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, (datetime, timedelta)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _exact_context(values: Sequence[Decimal], rounding: str) -> Context:
    """Context Decimal cục bộ, độc lập context ambient của caller.

    Precision đủ lớn để phép cộng/trừ/nhân hữu hạn của miền này là chính xác; `rounding` là chốt
    an toàn có hướng: trần (cap) và phần còn lại làm tròn xuống, exposure làm tròn lên.
    """
    precision = 32 + sum(
        len(value.as_tuple().digits) + abs(int(value.as_tuple().exponent))
        for value in values
        if value.is_finite()
    )
    return Context(prec=precision, rounding=rounding)


def _floor_product(fraction: Decimal, amount: Decimal) -> Decimal:
    """Cap = fraction × vốn, làm tròn xuống (không bao giờ nới trần do làm tròn)."""
    with localcontext(_exact_context((fraction, amount), ROUND_FLOOR)):
        return fraction * amount


def _ceiling_sum(values: Sequence[Decimal]) -> Decimal:
    """Tổng exposure làm tròn lên (không bao giờ đếm thiếu rủi ro do làm tròn)."""
    with localcontext(_exact_context(values, ROUND_CEILING)):
        total = ZERO
        for value in values:
            total += value
        return total


def _floor_sum(values: Sequence[Decimal]) -> Decimal:
    """Tổng vốn (mẫu số của cap) làm tròn xuống."""
    with localcontext(_exact_context(values, ROUND_FLOOR)):
        total = ZERO
        for value in values:
            total += value
        return total


def _floor_difference(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    """Phần còn lại = cap − exposure, làm tròn xuống."""
    with localcontext(_exact_context((minuend, subtrahend), ROUND_FLOOR)):
        return minuend - subtrahend


def effective_bot_open_risk_fraction(
    *,
    bot_fraction: Decimal | None,
    account_fraction: Decimal,
    workspace_fraction: Decimal,
) -> Decimal | None:
    """Cap bot hiệu lực: bot chỉ được siết, không nới account/workspace (contract §3, §5).

    Overlay đã publish hợp lệ có thể trở nên lỏng hơn khi root/profile siết sau đó; khi resolve
    runtime, cap bot được kẹp về min(bot, account, workspace) thay vì làm bot không resolve được
    (review I4). Kiểm tra chặt (từ chối overlay lỏng hơn) vẫn áp ở bước validate/publish overlay.
    """
    if bot_fraction is None:
        return None
    return min(bot_fraction, account_fraction, workspace_fraction)


@dataclass(frozen=True, slots=True)
class PositionRisk:
    """Rủi ro lỗ tối đa tại stop bảo vệ của vị thế đang mở."""

    workspace_id: str
    bot_id: str
    account_id: str
    position_id: str
    symbol: str
    worst_case_loss_usdt: Decimal

    def __post_init__(self) -> None:
        _text(self.workspace_id, self.bot_id, self.account_id, self.position_id, self.symbol)
        _amount(self.worst_case_loss_usdt)


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Bản ghi giữ ngân sách cho một lệnh intent."""

    reservation_id: str
    workspace_id: str
    bot_id: str
    account_id: str
    idempotency_key: str
    reserved_risk_usdt: Decimal
    status: ReservationStatus
    expires_at: datetime
    plan_hash: str = ""

    def __post_init__(self) -> None:
        _text(
            self.reservation_id,
            self.workspace_id,
            self.bot_id,
            self.account_id,
            self.idempotency_key,
        )
        _amount(self.reserved_risk_usdt)
        _utc(self.expires_at)


@dataclass(frozen=True, slots=True)
class AccountScopeInput:
    """Dữ liệu đầu vào cho một tài khoản và bot liên kết trong workspace."""

    workspace_id: str
    bot_id: str
    account_id: str
    enabled: bool
    account_max_open_risk_fraction: Decimal
    bot_max_open_risk_fraction: Decimal | None = None
    snapshot: AccountEquitySnapshot | None = None
    conversion: RiskConversionObservation | None = None
    risk_profile_version_id: str = ""
    bot_config_version_id: str = ""
    bot_config_hash: str = ""

    def __post_init__(self) -> None:
        _text(self.workspace_id, self.bot_id, self.account_id)
        _fraction(self.account_max_open_risk_fraction)
        if self.bot_max_open_risk_fraction is not None:
            _fraction(self.bot_max_open_risk_fraction)
            _require(
                self.bot_max_open_risk_fraction <= self.account_max_open_risk_fraction,
                BudgetReasonCode.FRACTION_INVALID,
            )


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    """Chính sách ngân sách root và thời hạn dữ liệu khả dụng."""

    workspace_id: str
    root_workspace_max_open_risk_fraction: Decimal
    root_settings_version_id: str
    root_settings_hash: str
    max_equity_age: timedelta
    max_conversion_age: timedelta
    risk_profile_version_id: str = ""
    max_mark_age: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        _text(self.workspace_id, self.root_settings_version_id, self.root_settings_hash)
        if self.risk_profile_version_id:
            _text(self.risk_profile_version_id)
        _fraction(self.root_workspace_max_open_risk_fraction)
        _require(
            isinstance(self.max_equity_age, timedelta) and self.max_equity_age >= timedelta(0),
            BudgetReasonCode.LINEAGE_INVALID,
        )
        _require(
            isinstance(self.max_conversion_age, timedelta)
            and self.max_conversion_age >= timedelta(0),
            BudgetReasonCode.LINEAGE_INVALID,
        )
        _require(
            isinstance(self.max_mark_age, timedelta) and self.max_mark_age > timedelta(0),
            BudgetReasonCode.LINEAGE_INVALID,
        )


@dataclass(frozen=True, slots=True)
class ScopeBudget:
    """Ngân sách rủi ro tính toán cho một scope (workspace, account, hoặc bot)."""

    scope_name: str
    cap_usdt: Decimal
    open_risk_usdt: Decimal
    pending_risk_usdt: Decimal
    requested_risk_usdt: Decimal
    remaining_risk_usdt: Decimal
    is_breached: bool


@dataclass(frozen=True, slots=True)
class CapacityBreachState:
    """Trạng thái vi phạm hạn mức sau khi siết cap dưới mức rủi ro hiện có."""

    workspace_id: str
    bot_id: str | None
    account_id: str | None
    scope_name: str
    cap_usdt: Decimal
    current_exposure_usdt: Decimal
    excess_usdt: Decimal
    reason_code: BudgetReasonCode = BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING


@dataclass(frozen=True, slots=True)
class WorkspaceRiskAggregate:
    """Tổng hợp rủi ro toàn workspace qua SQL aggregate tối thiểu."""

    workspace_id: UUID
    total_equity_usdt: Decimal
    total_open_risk_usdt: Decimal
    total_pending_risk_usdt: Decimal
    active_bot_count: int
    has_unknown_exposure: bool
    has_unknown_equity: bool


@dataclass(frozen=True, slots=True)
class WorkspaceRiskDecision:
    """Quyết định bất biến kèm lineage đầy đủ sau khi kiểm tra ngân sách 3 scope."""

    decision_id: str
    candidate_id: str
    workspace_id: str
    bot_id: str
    account_id: str
    operation: BudgetOperation
    idempotency_key: str
    result: BudgetDecisionResult
    approved: bool
    reason_code: BudgetReasonCode
    detail_vi: str
    as_of: datetime
    workspace_equity_usdt: Decimal
    account_equity_usdt: Decimal
    workspace_budget: ScopeBudget
    account_budget: ScopeBudget
    bot_budget: ScopeBudget
    breaches: tuple[CapacityBreachState, ...]
    root_settings_version_id: str
    root_settings_hash: str
    bot_config_version_id: str
    bot_config_hash: str
    risk_profile_version_id: str
    snapshot_ids: tuple[str, ...]
    conversion_ids: tuple[str, ...]
    plan_hash: str = ""


class WorkspaceBudgetEngine:
    """Động cơ tính toán ngân sách thuần và xác định breach theo 3 cấp phạm vi."""

    def evaluate(
        self,
        *,
        policy: WorkspacePolicy,
        accounts: Sequence[AccountScopeInput],
        positions: Sequence[PositionRisk],
        reservations: Sequence[BudgetReservation],
        target_bot_id: str,
        requested_risk_usdt: Decimal,
        as_of: datetime,
        workspace_aggregate: WorkspaceRiskAggregate,
        reservation_cutoff: datetime | None = None,
        operation: BudgetOperation = BudgetOperation.NEW_ENTRY,
        candidate_id: str = "",
        idempotency_key: str = "",
        plan_hash: str = "",
    ) -> WorkspaceRiskDecision:
        """Đánh giá ngân sách rủi ro 3 scope tại điểm thời gian as_of tường minh.

        `workspace_aggregate` là bắt buộc: thiếu aggregate thì không có căn cứ workspace (không
        còn chế độ local-only âm thầm). `reservation_cutoff` (mặc định `as_of`) là mốc đồng hồ
        thật để xét RESERVED còn hiệu lực; caller truyền min(as_of, now) để as_of tương lai không
        làm rơi reservation còn hạn (review C3c).
        """
        _utc(as_of)
        _amount(requested_risk_usdt)
        _require(
            isinstance(workspace_aggregate, WorkspaceRiskAggregate),
            BudgetReasonCode.LINEAGE_INVALID,
        )
        cutoff = as_of if reservation_cutoff is None else reservation_cutoff
        _utc(cutoff)
        _require(cutoff <= as_of, BudgetReasonCode.TIMESTAMP_INVALID)

        # 1. Tìm target account gắn với target_bot_id
        target_acc = next((acc for acc in accounts if acc.bot_id == target_bot_id), None)
        _require(target_acc is not None, BudgetReasonCode.SCOPE_MISMATCH)
        assert target_acc is not None
        if target_acc.workspace_id != policy.workspace_id:
            _require(False, BudgetReasonCode.SCOPE_MISMATCH)
        if (
            len({account.account_id for account in accounts}) != len(accounts)
            or len({account.bot_id for account in accounts}) != len(accounts)
            or any(account.workspace_id != policy.workspace_id for account in accounts)
        ):
            _require(False, BudgetReasonCode.SCOPE_MISMATCH)

        # 2. Xử lý trường hợp quản trị bảo vệ (Protective operations không bị từ chối)
        is_protective = operation in (
            BudgetOperation.PROTECTIVE_EXIT,
            BudgetOperation.PROTECTIVE_MONITORING,
        )

        # 3. Tính toán equity cho từng account và mẫu số workspace
        workspace_equity_usdt = ZERO
        account_equities: dict[str, Decimal] = {}
        data_error_code: BudgetReasonCode | None = None

        for acc in accounts:
            if not acc.enabled:
                # Bot disabled không đóng góp vào mẫu số vốn mới
                if acc.snapshot is not None and acc.snapshot.known:
                    try:
                        eq = normalize_equity_to_usdt(
                            acc.snapshot, acc.conversion, as_of, policy.max_conversion_age
                        )
                        account_equities[acc.account_id] = eq
                    except Exception:
                        account_equities[acc.account_id] = ZERO
                else:
                    account_equities[acc.account_id] = ZERO
                continue

            # Với account enabled: snapshot bắt buộc phải có và hợp lệ
            if acc.snapshot is None:
                data_error_code = BudgetReasonCode.DENOMINATOR_UNUSABLE
                break

            snap = acc.snapshot
            if (snap.workspace_id, snap.bot_id, snap.account_id) != (
                acc.workspace_id,
                acc.bot_id,
                acc.account_id,
            ):
                data_error_code = BudgetReasonCode.SCOPE_MISMATCH
                break
            if not snap.known:
                data_error_code = BudgetReasonCode.EQUITY_UNKNOWN
                break

            if snap.captured_at > as_of or snap.available_at > as_of or snap.as_of > as_of:
                data_error_code = BudgetReasonCode.OBSERVATION_FUTURE
                break

            if snap.as_of != as_of:
                data_error_code = BudgetReasonCode.AS_OF_MISMATCH
                break

            if as_of - snap.captured_at > policy.max_equity_age:
                data_error_code = BudgetReasonCode.EQUITY_STALE
                break

            if snap.asset != "USDT":
                if acc.conversion is None or not acc.conversion.known:
                    data_error_code = BudgetReasonCode.CONVERSION_UNKNOWN
                    break
                if acc.conversion.workspace_id != acc.workspace_id:
                    data_error_code = BudgetReasonCode.SCOPE_MISMATCH
                    break
                if acc.conversion.as_of > as_of or acc.conversion.available_at > as_of:
                    data_error_code = BudgetReasonCode.OBSERVATION_FUTURE
                    break
                if as_of - acc.conversion.as_of > policy.max_conversion_age:
                    data_error_code = BudgetReasonCode.CONVERSION_STALE
                    break

            try:
                eq = normalize_equity_to_usdt(
                    snap, acc.conversion, as_of, policy.max_conversion_age
                )
                account_equities[acc.account_id] = eq
                workspace_equity_usdt = _floor_sum((workspace_equity_usdt, eq))
            except EquityValidationError as err:
                mapped_code = getattr(
                    BudgetReasonCode, err.reason_code.name, BudgetReasonCode.DENOMINATOR_UNUSABLE
                )
                data_error_code = mapped_code
                break

        if data_error_code is None:
            if str(workspace_aggregate.workspace_id) != policy.workspace_id:
                data_error_code = BudgetReasonCode.SCOPE_MISMATCH
            elif workspace_aggregate.has_unknown_exposure:
                data_error_code = BudgetReasonCode.WORKSPACE_EXPOSURE_UNKNOWN
            else:
                try:
                    _amount(workspace_aggregate.total_equity_usdt)
                    _amount(workspace_aggregate.total_open_risk_usdt)
                    _amount(workspace_aggregate.total_pending_risk_usdt)
                    _require(
                        workspace_aggregate.total_equity_usdt > ZERO,
                        BudgetReasonCode.DENOMINATOR_UNUSABLE,
                    )
                    _require(
                        isinstance(workspace_aggregate.active_bot_count, int)
                        and not isinstance(workspace_aggregate.active_bot_count, bool)
                        and workspace_aggregate.active_bot_count >= 0,
                        BudgetReasonCode.AMOUNT_INVALID,
                    )
                    _require(
                        isinstance(workspace_aggregate.has_unknown_exposure, bool),
                        BudgetReasonCode.AMOUNT_INVALID,
                    )
                    _require(
                        isinstance(workspace_aggregate.has_unknown_equity, bool),
                        BudgetReasonCode.AMOUNT_INVALID,
                    )
                    _require(
                        not workspace_aggregate.has_unknown_equity,
                        BudgetReasonCode.WORKSPACE_EQUITY_UNKNOWN,
                    )
                    enabled_scope_count = sum(1 for account in accounts if account.enabled)
                    _require(
                        workspace_aggregate.active_bot_count > 0
                        and workspace_aggregate.active_bot_count >= enabled_scope_count,
                        BudgetReasonCode.WORKSPACE_SCOPE_INCOMPLETE,
                    )
                except BudgetValidationError as error:
                    data_error_code = error.reason_code
                if data_error_code is None:
                    workspace_equity_usdt = workspace_aggregate.total_equity_usdt

        # Nếu lỗi dữ liệu mẫu số và là lệnh vào mới -> Fail-closed từ chối ngay
        if data_error_code is not None and not is_protective:
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.DENY_NEW_ENTRY,
                approved=False,
                reason_code=data_error_code,
                detail_vi=(
                    f"Từ chối entry mới do dữ liệu risk không đủ tin cậy: {data_error_code.value}."
                ),
                as_of=as_of,
                workspace_equity_usdt=ZERO,
                account_equity_usdt=ZERO,
                workspace_budget=ScopeBudget(
                    "WORKSPACE", ZERO, ZERO, ZERO, requested_risk_usdt, ZERO, False
                ),
                account_budget=ScopeBudget(
                    "ACCOUNT", ZERO, ZERO, ZERO, requested_risk_usdt, ZERO, False
                ),
                bot_budget=ScopeBudget("BOT", ZERO, ZERO, ZERO, requested_risk_usdt, ZERO, False),
                breaches=(),
                plan_hash=plan_hash,
            )

        target_acc_equity = account_equities.get(target_acc.account_id, ZERO)

        # 4. Tính toán rủi ro mở và reservation đang giữ cho từng scope
        # Rủi ro mở và reservation của MỌI bot (kể cả disabled) đều tính vào workspace!
        ws_open_items: list[Decimal] = []
        ws_pending_items: list[Decimal] = []
        acc_open_items: list[Decimal] = []
        acc_pending_items: list[Decimal] = []
        bot_open_items: list[Decimal] = []
        bot_pending_items: list[Decimal] = []

        for pos in positions:
            ws_open_items.append(pos.worst_case_loss_usdt)
            if pos.account_id == target_acc.account_id:
                acc_open_items.append(pos.worst_case_loss_usdt)
            if pos.bot_id == target_bot_id:
                bot_open_items.append(pos.worst_case_loss_usdt)

        for res in reservations:
            # RELEASED và TRANSFERRED không giữ budget
            if res.status in (ReservationStatus.RELEASED, ReservationStatus.TRANSFERRED):
                continue

            # RESERVED quá hạn theo đồng hồ thật không giữ budget; UNKNOWN giữ bất kể hạn.
            if res.status == ReservationStatus.RESERVED and res.expires_at <= cutoff:
                continue

            ws_pending_items.append(res.reserved_risk_usdt)
            if res.account_id == target_acc.account_id:
                acc_pending_items.append(res.reserved_risk_usdt)
            if res.bot_id == target_bot_id:
                bot_pending_items.append(res.reserved_risk_usdt)

        ws_open_risk = max(_ceiling_sum(ws_open_items), workspace_aggregate.total_open_risk_usdt)
        ws_pending_risk = max(
            _ceiling_sum(ws_pending_items), workspace_aggregate.total_pending_risk_usdt
        )
        acc_open_risk = _ceiling_sum(acc_open_items)
        acc_pending_risk = _ceiling_sum(acc_pending_items)
        bot_open_risk = _ceiling_sum(bot_open_items)
        bot_pending_risk = _ceiling_sum(bot_pending_items)

        # 5. Tính trần (Cap) từng scope: làm tròn xuống, không phụ thuộc context ambient.
        workspace_cap = _floor_product(
            policy.root_workspace_max_open_risk_fraction, workspace_equity_usdt
        )
        account_cap = _floor_product(target_acc.account_max_open_risk_fraction, target_acc_equity)
        if target_acc.bot_max_open_risk_fraction is not None:
            bot_cap = min(
                account_cap,
                _floor_product(target_acc.bot_max_open_risk_fraction, target_acc_equity),
            )
        else:
            bot_cap = account_cap

        # 6. Kiểm tra Breach do siết Cap (Cap tightening breach); exposure làm tròn lên.
        ws_exposure = _ceiling_sum((ws_open_risk, ws_pending_risk))
        acc_exposure = _ceiling_sum((acc_open_risk, acc_pending_risk))
        bot_exposure = _ceiling_sum((bot_open_risk, bot_pending_risk))

        breaches: list[CapacityBreachState] = []
        if ws_exposure > workspace_cap:
            breaches.append(
                CapacityBreachState(
                    workspace_id=policy.workspace_id,
                    bot_id=None,
                    account_id=None,
                    scope_name="WORKSPACE",
                    cap_usdt=workspace_cap,
                    current_exposure_usdt=ws_exposure,
                    excess_usdt=-_floor_difference(workspace_cap, ws_exposure),
                )
            )
        if acc_exposure > account_cap:
            breaches.append(
                CapacityBreachState(
                    workspace_id=policy.workspace_id,
                    bot_id=None,
                    account_id=target_acc.account_id,
                    scope_name="ACCOUNT",
                    cap_usdt=account_cap,
                    current_exposure_usdt=acc_exposure,
                    excess_usdt=-_floor_difference(account_cap, acc_exposure),
                )
            )
        if target_acc.bot_max_open_risk_fraction is not None and bot_exposure > bot_cap:
            breaches.append(
                CapacityBreachState(
                    workspace_id=policy.workspace_id,
                    bot_id=target_bot_id,
                    account_id=target_acc.account_id,
                    scope_name="BOT",
                    cap_usdt=bot_cap,
                    current_exposure_usdt=bot_exposure,
                    excess_usdt=-_floor_difference(bot_cap, bot_exposure),
                )
            )

        ws_remaining = max(ZERO, _floor_difference(workspace_cap, ws_exposure))
        acc_remaining = max(ZERO, _floor_difference(account_cap, acc_exposure))
        bot_remaining = max(ZERO, _floor_difference(bot_cap, bot_exposure))

        ws_budget = ScopeBudget(
            "WORKSPACE",
            workspace_cap,
            ws_open_risk,
            ws_pending_risk,
            requested_risk_usdt,
            ws_remaining,
            ws_exposure > workspace_cap,
        )
        acc_budget = ScopeBudget(
            "ACCOUNT",
            account_cap,
            acc_open_risk,
            acc_pending_risk,
            requested_risk_usdt,
            acc_remaining,
            acc_exposure > account_cap,
        )
        bot_budget = ScopeBudget(
            "BOT",
            bot_cap,
            bot_open_risk,
            bot_pending_risk,
            requested_risk_usdt,
            bot_remaining,
            bot_exposure > bot_cap,
        )

        # 7. Quyết định phê duyệt
        if is_protective:
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.APPROVED,
                approved=True,
                reason_code=BudgetReasonCode.APPROVED,
                detail_vi="Phê duyệt hành động quản trị bảo vệ (protective action).",
                as_of=as_of,
                workspace_equity_usdt=workspace_equity_usdt,
                account_equity_usdt=target_acc_equity,
                workspace_budget=ws_budget,
                account_budget=acc_budget,
                bot_budget=bot_budget,
                breaches=tuple(breaches),
                plan_hash=plan_hash,
            )

        if not target_acc.enabled:
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.DENY_NEW_ENTRY,
                approved=False,
                reason_code=BudgetReasonCode.BOT_DISABLED,
                detail_vi="Từ chối entry mới vì bot đã bị vô hiệu hóa.",
                as_of=as_of,
                workspace_equity_usdt=workspace_equity_usdt,
                account_equity_usdt=target_acc_equity,
                workspace_budget=ws_budget,
                account_budget=acc_budget,
                bot_budget=bot_budget,
                breaches=tuple(breaches),
                plan_hash=plan_hash,
            )

        if breaches:
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.DENY_NEW_ENTRY,
                approved=False,
                reason_code=BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING,
                detail_vi="Từ chối entry mới do vi phạm hạn mức sau khi siết trần rủi ro.",
                as_of=as_of,
                workspace_equity_usdt=workspace_equity_usdt,
                account_equity_usdt=target_acc_equity,
                workspace_budget=ws_budget,
                account_budget=acc_budget,
                bot_budget=bot_budget,
                breaches=tuple(breaches),
                plan_hash=plan_hash,
            )

        # Kiểm tra giới hạn nhỏ nhất áp dụng cho requested_risk
        # Boundary: requested_risk <= remaining là APPROVED, > remaining là REJECTED
        # Bot overlay chỉ áp dụng nếu có mặt (bot_max_open_risk_fraction is not None)
        if (
            target_acc.bot_max_open_risk_fraction is not None
            and requested_risk_usdt > bot_remaining
        ):
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.DENY_NEW_ENTRY,
                approved=False,
                reason_code=BudgetReasonCode.BOT_CAP_EXCEEDED,
                detail_vi=(
                    f"Rủi ro yêu cầu ({requested_risk_usdt}) vượt trần bot overlay "
                    f"(còn lại: {bot_remaining})."
                ),
                as_of=as_of,
                workspace_equity_usdt=workspace_equity_usdt,
                account_equity_usdt=target_acc_equity,
                workspace_budget=ws_budget,
                account_budget=acc_budget,
                bot_budget=bot_budget,
                breaches=(),
                plan_hash=plan_hash,
            )

        if requested_risk_usdt > acc_remaining:
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.DENY_NEW_ENTRY,
                approved=False,
                reason_code=BudgetReasonCode.ACCOUNT_CAP_EXCEEDED,
                detail_vi=(
                    f"Rủi ro yêu cầu ({requested_risk_usdt}) vượt trần account "
                    f"(còn lại: {acc_remaining})."
                ),
                as_of=as_of,
                workspace_equity_usdt=workspace_equity_usdt,
                account_equity_usdt=target_acc_equity,
                workspace_budget=ws_budget,
                account_budget=acc_budget,
                bot_budget=bot_budget,
                breaches=(),
                plan_hash=plan_hash,
            )

        if requested_risk_usdt > ws_remaining:
            return self._build_decision(
                candidate_id=candidate_id or str(uuid4()),
                policy=policy,
                target_acc=target_acc,
                operation=operation,
                idempotency_key=idempotency_key,
                result=BudgetDecisionResult.DENY_NEW_ENTRY,
                approved=False,
                reason_code=BudgetReasonCode.WORKSPACE_CAP_EXCEEDED,
                detail_vi=(
                    f"Rủi ro yêu cầu ({requested_risk_usdt}) vượt trần workspace "
                    f"(còn lại: {ws_remaining})."
                ),
                as_of=as_of,
                workspace_equity_usdt=workspace_equity_usdt,
                account_equity_usdt=target_acc_equity,
                workspace_budget=ws_budget,
                account_budget=acc_budget,
                bot_budget=bot_budget,
                breaches=(),
                plan_hash=plan_hash,
            )

        return self._build_decision(
            candidate_id=candidate_id or str(uuid4()),
            policy=policy,
            target_acc=target_acc,
            operation=operation,
            idempotency_key=idempotency_key,
            result=BudgetDecisionResult.APPROVED,
            approved=True,
            reason_code=BudgetReasonCode.APPROVED,
            detail_vi="Phê duyệt phân bổ ngân sách rủi ro thành công.",
            as_of=as_of,
            workspace_equity_usdt=workspace_equity_usdt,
            account_equity_usdt=target_acc_equity,
            workspace_budget=ws_budget,
            account_budget=acc_budget,
            bot_budget=bot_budget,
            breaches=(),
            plan_hash=plan_hash,
        )

    def _build_decision(
        self,
        *,
        candidate_id: str,
        policy: WorkspacePolicy,
        target_acc: AccountScopeInput,
        operation: BudgetOperation,
        idempotency_key: str,
        result: BudgetDecisionResult,
        approved: bool,
        reason_code: BudgetReasonCode,
        detail_vi: str,
        as_of: datetime,
        workspace_equity_usdt: Decimal,
        account_equity_usdt: Decimal,
        workspace_budget: ScopeBudget,
        account_budget: ScopeBudget,
        bot_budget: ScopeBudget,
        breaches: tuple[CapacityBreachState, ...],
        plan_hash: str,
    ) -> WorkspaceRiskDecision:
        snapshot_id = (
            f"{target_acc.snapshot.workspace_id}:{target_acc.snapshot.bot_id}:{target_acc.snapshot.source_hash}"
            if target_acc.snapshot is not None
            else ""
        )
        conversion_id = (
            f"{target_acc.conversion.source_ref}:{target_acc.conversion.observation_hash}"
            if target_acc.conversion is not None
            else ""
        )
        return WorkspaceRiskDecision(
            decision_id=str(uuid4()),
            candidate_id=candidate_id,
            workspace_id=policy.workspace_id,
            bot_id=target_acc.bot_id,
            account_id=target_acc.account_id,
            operation=operation,
            idempotency_key=idempotency_key,
            result=result,
            approved=approved,
            reason_code=reason_code,
            detail_vi=detail_vi,
            as_of=as_of,
            workspace_equity_usdt=workspace_equity_usdt,
            account_equity_usdt=account_equity_usdt,
            workspace_budget=workspace_budget,
            account_budget=account_budget,
            bot_budget=bot_budget,
            breaches=breaches,
            root_settings_version_id=policy.root_settings_version_id,
            root_settings_hash=policy.root_settings_hash,
            bot_config_version_id=target_acc.bot_config_version_id,
            bot_config_hash=target_acc.bot_config_hash,
            risk_profile_version_id=(
                target_acc.risk_profile_version_id or policy.risk_profile_version_id
            ),
            snapshot_ids=(snapshot_id,) if snapshot_id else (),
            conversion_ids=(conversion_id,) if conversion_id else (),
            plan_hash=plan_hash,
        )


class CapacityStatus(StrEnum):
    """Kết quả đánh giá capacity khi publish thay đổi root/profile risk (contract §6)."""

    WITHIN_CAP = "WITHIN_CAP"
    CAPACITY_BREACH_AFTER_TIGHTENING = "CAPACITY_BREACH_AFTER_TIGHTENING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AccountRiskState:
    """Scalar vốn/exposure của một account do ledger SQL tính (cùng nguồn với aggregate)."""

    bot_id: str
    account_id: str
    enabled: bool
    equity_usdt: Decimal | None
    equity_known: bool
    open_risk_usdt: Decimal
    pending_risk_usdt: Decimal
    exposure_known: bool

    def __post_init__(self) -> None:
        _text(self.bot_id, self.account_id)
        _amount(self.open_risk_usdt)
        _amount(self.pending_risk_usdt)
        if self.equity_usdt is not None:
            _amount(self.equity_usdt, signed=True)


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    """Đánh giá exposure hiện hành so với cap mới, đọc dưới workspace risk lock."""

    status: CapacityStatus
    breached_scopes: tuple[str, ...]
    workspace_equity_usdt: Decimal | None
    workspace_cap_usdt: Decimal | None
    workspace_exposure_usdt: Decimal
    breached_account_count: int
    unknown_reasons: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        """Payload JSON ổn định cho audit/outbox/API; không chứa ID bot/account."""
        value = _canonical(self)
        assert isinstance(value, dict)
        return value

    def summary(self) -> str:
        """Lý do audit máy đọc được (scope, exposure, cap) — không chứa định danh bot."""
        cap = "-" if self.workspace_cap_usdt is None else _canonical(self.workspace_cap_usdt)
        return (
            f"{self.status.value}; scopes={','.join(self.breached_scopes) or '-'}; "
            f"workspace_exposure_usdt={_canonical(self.workspace_exposure_usdt)}; "
            f"workspace_cap_usdt={cap}; "
            f"breached_accounts={self.breached_account_count}; "
            f"unknown={','.join(self.unknown_reasons) or '-'}"
        )


def assess_capacity_after_risk_change(
    *,
    workspace_fraction: Decimal,
    account_fraction: Decimal,
    states: Sequence[AccountRiskState],
) -> CapacityAssessment:
    """So exposure hiện hành (vị thế mở + reservation còn giữ) với cap root/profile mới.

    Exposure của mọi bot (kể cả disabled) tính vào workspace; cap account chỉ xét account
    enabled có vốn đã biết. Siết dưới exposure -> `CAPACITY_BREACH_AFTER_TIGHTENING` (publish vẫn
    hiệu lực, entry mới bị chặn ở reserve, không auto-flatten). Thiếu dữ kiện -> `UNKNOWN`.
    """
    _fraction(workspace_fraction)
    _fraction(account_fraction)
    unknown: list[str] = []
    enabled = [state for state in states if state.enabled]
    exposure = _ceiling_sum(
        [value for state in states for value in (state.open_risk_usdt, state.pending_risk_usdt)]
    )
    if any(not state.exposure_known for state in states):
        unknown.append("EXPOSURE_UNKNOWN")
    if not enabled:
        unknown.append("NO_ENABLED_ACCOUNT")

    def known_equity(state: AccountRiskState) -> Decimal | None:
        if not state.equity_known or state.equity_usdt is None or state.equity_usdt < ZERO:
            return None
        return state.equity_usdt

    if any(known_equity(state) is None for state in enabled):
        unknown.append("EQUITY_UNKNOWN")
    breached: list[str] = []
    workspace_equity: Decimal | None = None
    workspace_cap: Decimal | None = None
    if enabled and "EQUITY_UNKNOWN" not in unknown:
        workspace_equity = _floor_sum([known_equity(state) or ZERO for state in enabled])
        workspace_cap = _floor_product(workspace_fraction, workspace_equity)
        if exposure > workspace_cap:
            breached.append("WORKSPACE")
    account_breaches = 0
    for state in enabled:
        equity = known_equity(state)
        if equity is None:
            continue
        account_exposure = _ceiling_sum((state.open_risk_usdt, state.pending_risk_usdt))
        if account_exposure > _floor_product(account_fraction, equity):
            account_breaches += 1
    if account_breaches:
        breached.append("ACCOUNT")
    if breached:
        status = CapacityStatus.CAPACITY_BREACH_AFTER_TIGHTENING
    elif unknown:
        status = CapacityStatus.UNKNOWN
    else:
        status = CapacityStatus.WITHIN_CAP
    return CapacityAssessment(
        status=status,
        breached_scopes=tuple(breached),
        workspace_equity_usdt=workspace_equity,
        workspace_cap_usdt=workspace_cap,
        workspace_exposure_usdt=exposure,
        breached_account_count=account_breaches,
        unknown_reasons=tuple(unknown),
    )


class WorkspaceBudgetBook:
    """Kho domain lưu trữ và đảm bảo tính idempotent theo scope và key."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str, str], tuple[str, WorkspaceRiskDecision]] = {}

    def record(self, decision: WorkspaceRiskDecision) -> None:
        """Ghi nhận quyết định có kèm plan_hash vào sổ lưu idempotency."""
        key = (
            decision.workspace_id,
            decision.bot_id,
            decision.account_id,
            decision.operation.value,
            decision.idempotency_key,
        )
        self._records[key] = (decision.plan_hash, decision)

    def get_or_evaluate(
        self,
        *,
        key: tuple[str, str, str, str | BudgetOperation, str],
        payload_hash: str,
    ) -> WorkspaceRiskDecision | None:
        """Kiểm tra idempotency: trùng key + cùng payload trả kết quả cũ, khác payload conflict."""
        normalized_key = (
            key[0],
            key[1],
            key[2],
            key[3].value if isinstance(key[3], BudgetOperation) else str(key[3]),
            key[4],
        )
        if normalized_key not in self._records:
            return None

        saved_hash, decision = self._records[normalized_key]
        if saved_hash != payload_hash:
            raise BudgetConflictError(
                f"Trùng idempotency key '{normalized_key}' nhưng payload khác nhau (conflict)."
            )
        return decision
