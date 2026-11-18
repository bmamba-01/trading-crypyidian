"""Phân nhóm tương quan symbol và trần rủi ro/notional theo nhóm (RISK-1 / CRY-31).

Các tài sản tiền mã hóa cùng hệ số beta cao với BTC (ví dụ BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT)
thường dịch chuyển cùng chiều khi thị trường biến động mạnh. Nếu chỉ giới hạn theo từng symbol
đơn lẻ, danh mục có thể mở đồng thời nhiều vị thế tương quan cao và tích tụ rủi ro thực tế vượt
mức cho phép như một vị thế lớn duy nhất.

Module này định nghĩa:
- Bảng phân nhóm tương quan mặc định bảo thủ (`CRYPTO_L1_MAJORS`, `CRYPTO_L2_DEFI`,
  `CRYPTO_MEME`) cùng khả năng cấu hình ánh xạ symbol → nhóm và trần riêng theo workspace/policy.
- Hàm tính tổng rủi ro (risk amount), tổng giá trị danh nghĩa (notional) và số vị thế theo nhóm
  từ `open_exposures`, các khoản giữ chỗ (pending reservations) và kế hoạch lệnh mới (`TradePlan`).
- Hàm đánh giá trần nhóm (`evaluate_correlation_group_cap`) tích hợp trực tiếp vào `RiskEngine`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_bot.domain.risk.planner import OpenExposure, RiskLimits, TradePlan

if TYPE_CHECKING:
    from trading_bot.domain.risk.engine import RiskState

ZERO = Decimal("0")
ONE = Decimal("1")

CRYPTO_L1_MAJORS = "CRYPTO_L1_MAJORS"
CRYPTO_L2_DEFI = "CRYPTO_L2_DEFI"
CRYPTO_MEME = "CRYPTO_MEME"

CORRELATION_GROUP_RISK_EXCEEDED = "CORRELATION_GROUP_RISK_EXCEEDED"
CORRELATION_GROUP_NOTIONAL_EXCEEDED = "CORRELATION_GROUP_NOTIONAL_EXCEEDED"
CORRELATION_GROUP_POSITIONS_EXCEEDED = "CORRELATION_GROUP_POSITIONS_EXCEEDED"

# Tỷ lệ trần mặc định bảo thủ trên ngân sách tổng khi nhóm không khai báo trần tuyệt đối riêng.
# Với lệnh đơn lẻ đầu tiên trong nhóm, trần mặc định luôn tối thiểu bằng trần đơn lệnh/đơn symbol
# hiện hành để tương thích ngược 100% với cấu hình 1 vị thế hoặc cấu hình symbol tập trung.
DEFAULT_GROUP_MAX_RISK_FRACTION = Decimal("0.60")
DEFAULT_GROUP_MAX_NOTIONAL_FRACTION = Decimal("0.35")


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _validate_positive_decimal(value: Decimal | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise ValueError(f"Giới hạn {field_name} phải là Decimal hữu hạn dương.")


def _validate_fraction(value: Decimal | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or not ZERO < value <= ONE:
        raise ValueError(f"Tỷ lệ {field_name} phải là Decimal hữu hạn trong (0, 1].")


@dataclass(frozen=True, slots=True)
class CorrelationGroupSpec:
    """Đặc tả một nhóm tương quan symbol kèm các trần rủi ro/notional tùy chọn."""

    group_id: str
    symbols: tuple[str, ...]
    label_vi: str = ""
    max_risk_amount: Decimal | None = None
    max_risk_fraction: Decimal | None = None
    max_equity_risk_fraction: Decimal | None = None
    max_notional: Decimal | None = None
    max_notional_fraction: Decimal | None = None
    max_equity_notional_fraction: Decimal | None = None
    max_positions: int | None = None

    def __post_init__(self) -> None:
        cleaned_id = self.group_id.strip()
        if not cleaned_id:
            raise ValueError("group_id của nhóm tương quan không được để trống.")
        object.__setattr__(self, "group_id", cleaned_id)

        normalized_symbols = tuple(dict.fromkeys(_normalize_symbol(s) for s in self.symbols))
        if not normalized_symbols or any(not s for s in normalized_symbols):
            raise ValueError("Danh sách symbols của nhóm tương quan không được rỗng.")
        object.__setattr__(self, "symbols", normalized_symbols)

        _validate_positive_decimal(self.max_risk_amount, "max_risk_amount")
        _validate_fraction(self.max_risk_fraction, "max_risk_fraction")
        _validate_fraction(self.max_equity_risk_fraction, "max_equity_risk_fraction")
        _validate_positive_decimal(self.max_notional, "max_notional")
        _validate_fraction(self.max_notional_fraction, "max_notional_fraction")
        _validate_fraction(self.max_equity_notional_fraction, "max_equity_notional_fraction")

        if self.max_positions is not None and (
            type(self.max_positions) is not int or self.max_positions < 1
        ):
            raise ValueError("Giới hạn max_positions của nhóm phải là số nguyên dương.")

    @property
    def has_explicit_risk_cap(self) -> bool:
        """Nhóm có khai báo trần rủi ro riêng hay không."""
        return (
            self.max_risk_amount is not None
            or self.max_risk_fraction is not None
            or self.max_equity_risk_fraction is not None
        )

    @property
    def has_explicit_notional_cap(self) -> bool:
        """Nhóm có khai báo trần notional riêng hay không."""
        return (
            self.max_notional is not None
            or self.max_notional_fraction is not None
            or self.max_equity_notional_fraction is not None
        )


DEFAULT_CORRELATION_GROUPS: tuple[CorrelationGroupSpec, ...] = (
    CorrelationGroupSpec(
        group_id=CRYPTO_L1_MAJORS,
        label_vi="Nhóm tiền mã hóa nền tảng vốn hóa lớn hệ số beta BTC cao (L1 Majors)",
        symbols=(
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "BNBUSDT",
            "AVAXUSDT",
            "ADAUSDT",
            "XRPUSDT",
            "DOTUSDT",
            "NEARUSDT",
            "SUIUSDT",
            "TONUSDT",
            "APTUSDT",
            "LTCUSDT",
            "BCHUSDT",
            "LINKUSDT",
            "ETCUSDT",
            "ATOMUSDT",
        ),
    ),
    CorrelationGroupSpec(
        group_id=CRYPTO_L2_DEFI,
        label_vi="Nhóm hạ tầng Layer-2 và giao thức DeFi tương quan cao",
        symbols=(
            "ARBUSDT",
            "OPUSDT",
            "MATICUSDT",
            "POLUSDT",
            "STRKUSDT",
            "ZKUSDT",
            "TIAUSDT",
            "UNIUSDT",
            "AAVEUSDT",
            "MKRUSDT",
            "CRVUSDT",
            "LDOUSDT",
            "PENDLEUSDT",
            "SNXUSDT",
            "INJUSDT",
        ),
    ),
    CorrelationGroupSpec(
        group_id=CRYPTO_MEME,
        label_vi="Nhóm tài sản đầu cơ biến động theo chu kỳ Meme beta cao",
        symbols=(
            "DOGEUSDT",
            "SHIBUSDT",
            "PEPEUSDT",
            "WIFUSDT",
            "BONKUSDT",
            "FLOKIUSDT",
            "BOMEUSDT",
            "1000PEPEUSDT",
            "1000SHIBUSDT",
            "1000BONKUSDT",
            "1000FLOKIUSDT",
        ),
    ),
)

DEFAULT_SYMBOL_CORRELATION_MAP: dict[str, str] = {
    symbol: spec.group_id for spec in DEFAULT_CORRELATION_GROUPS for symbol in spec.symbols
}


@dataclass(frozen=True, slots=True)
class CorrelationGroupPolicy:
    """Chính sách phân nhóm tương quan và trần rủi ro/notional cấp nhóm."""

    groups: tuple[CorrelationGroupSpec, ...] = DEFAULT_CORRELATION_GROUPS
    custom_symbol_groups: tuple[tuple[str, str], ...] = ()
    default_max_risk_fraction: Decimal = DEFAULT_GROUP_MAX_RISK_FRACTION
    default_max_notional_fraction: Decimal = DEFAULT_GROUP_MAX_NOTIONAL_FRACTION
    default_max_positions: int | None = None
    fallback_group_id: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        _validate_fraction(self.default_max_risk_fraction, "default_max_risk_fraction")
        _validate_fraction(self.default_max_notional_fraction, "default_max_notional_fraction")
        if self.default_max_positions is not None and (
            type(self.default_max_positions) is not int or self.default_max_positions < 1
        ):
            raise ValueError("default_max_positions phải là số nguyên dương.")
        if self.fallback_group_id is not None:
            cleaned_fallback = self.fallback_group_id.strip()
            if not cleaned_fallback:
                raise ValueError("fallback_group_id không được là chuỗi trắng.")
            object.__setattr__(self, "fallback_group_id", cleaned_fallback)

        seen_group_ids: set[str] = set()
        seen_symbols: dict[str, str] = {}
        for spec in self.groups:
            if spec.group_id in seen_group_ids:
                raise ValueError(f"group_id '{spec.group_id}' bị trùng lặp trong policy.")
            seen_group_ids.add(spec.group_id)
            for symbol in spec.symbols:
                previous = seen_symbols.get(symbol)
                if previous is not None and previous != spec.group_id:
                    raise ValueError(
                        f"Symbol '{symbol}' bị gán trùng vào hai nhóm '{previous}' và "
                        f"'{spec.group_id}'."
                    )
                seen_symbols[symbol] = spec.group_id

        normalized_custom: list[tuple[str, str]] = []
        seen_custom: dict[str, str] = {}
        for raw_symbol, raw_group in self.custom_symbol_groups:
            sym = _normalize_symbol(raw_symbol)
            grp = raw_group.strip()
            if not sym or not grp:
                raise ValueError("Ánh xạ custom_symbol_groups cần symbol và group_id hợp lệ.")
            prev_custom = seen_custom.get(sym)
            if prev_custom is not None and prev_custom != grp:
                raise ValueError(f"Symbol '{sym}' bị gán trùng nhiều nhóm trong custom mapping.")
            seen_custom[sym] = grp
            normalized_custom.append((sym, grp))
        object.__setattr__(self, "custom_symbol_groups", tuple(normalized_custom))

    @classmethod
    def from_mapping(
        cls,
        symbol_to_group: Mapping[str, str] | Iterable[tuple[str, str]],
        *,
        group_specs: Iterable[CorrelationGroupSpec] = (),
        include_defaults: bool = True,
        default_max_risk_fraction: Decimal = DEFAULT_GROUP_MAX_RISK_FRACTION,
        default_max_notional_fraction: Decimal = DEFAULT_GROUP_MAX_NOTIONAL_FRACTION,
        default_max_positions: int | None = None,
        fallback_group_id: str | None = None,
        enabled: bool = True,
    ) -> CorrelationGroupPolicy:
        """Tạo chính sách nhóm tương quan từ bảng ánh xạ symbol -> group_id tùy chỉnh."""
        pairs = (
            tuple(symbol_to_group.items())
            if isinstance(symbol_to_group, Mapping)
            else tuple(symbol_to_group)
        )
        explicit_specs = {spec.group_id: spec for spec in group_specs}
        base_specs: list[CorrelationGroupSpec] = []
        if include_defaults:
            for default_spec in DEFAULT_CORRELATION_GROUPS:
                if default_spec.group_id not in explicit_specs:
                    base_specs.append(default_spec)
        base_specs.extend(explicit_specs.values())
        return cls(
            groups=tuple(base_specs),
            custom_symbol_groups=pairs,
            default_max_risk_fraction=default_max_risk_fraction,
            default_max_notional_fraction=default_max_notional_fraction,
            default_max_positions=default_max_positions,
            fallback_group_id=fallback_group_id,
            enabled=enabled,
        )

    def group_spec(self, group_id: str) -> CorrelationGroupSpec | None:
        """Tra cứu đặc tả cấu hình của một nhóm theo `group_id`."""
        target = group_id.strip()
        for spec in self.groups:
            if spec.group_id == target:
                return spec
        return None


DEFAULT_CORRELATION_GROUP_POLICY = CorrelationGroupPolicy()


def resolve_correlation_group(
    symbol: str,
    policy: CorrelationGroupPolicy = DEFAULT_CORRELATION_GROUP_POLICY,
) -> str | None:
    """Xác định mã nhóm tương quan của `symbol` theo chính sách hiện hành."""
    normalized = _normalize_symbol(symbol)
    if not normalized:
        return None
    custom_map = dict(policy.custom_symbol_groups)
    if normalized in custom_map:
        return custom_map[normalized]
    for spec in policy.groups:
        if normalized in spec.symbols:
            return spec.group_id
    return policy.fallback_group_id


@dataclass(frozen=True, slots=True)
class CorrelationGroupExposure:
    """Tổng hợp rủi ro, notional và số lượng vị thế của một nhóm tương quan."""

    group_id: str
    symbols: tuple[str, ...]
    open_risk: Decimal
    requested_risk: Decimal
    total_risk: Decimal
    max_group_risk: Decimal
    open_notional: Decimal
    requested_notional: Decimal
    total_notional: Decimal
    max_group_notional: Decimal
    open_positions_in_group: int
    total_positions_in_group: int
    max_group_positions: int | None = None


@dataclass(frozen=True, slots=True)
class CorrelationGroupCheckResult:
    """Kết quả kiểm tra cổng giới hạn nhóm tương quan cho một kế hoạch giao dịch."""

    approved: bool
    reason_code: str
    detail_vi: str
    group_id: str | None = None
    exposure: CorrelationGroupExposure | None = None


def _compute_group_risk_cap(
    *,
    spec: CorrelationGroupSpec | None,
    policy: CorrelationGroupPolicy,
    limits: RiskLimits,
    equity: Decimal,
) -> Decimal:
    candidates: list[Decimal] = [limits.max_aggregate_open_risk]
    if spec is not None and spec.has_explicit_risk_cap:
        if spec.max_risk_amount is not None:
            candidates.append(spec.max_risk_amount)
        if spec.max_risk_fraction is not None:
            candidates.append(limits.max_aggregate_open_risk * spec.max_risk_fraction)
        if spec.max_equity_risk_fraction is not None:
            candidates.append(equity * spec.max_equity_risk_fraction)
        return min(candidates)

    per_trade_cap = equity * limits.per_trade_risk_fraction
    default_cap = max(
        limits.max_aggregate_open_risk * policy.default_max_risk_fraction,
        per_trade_cap,
    )
    candidates.append(default_cap)
    return min(candidates)


def _compute_group_notional_cap(
    *,
    spec: CorrelationGroupSpec | None,
    policy: CorrelationGroupPolicy,
    limits: RiskLimits,
    equity: Decimal,
) -> Decimal:
    candidates: list[Decimal] = [limits.max_notional]
    if spec is not None and spec.has_explicit_notional_cap:
        if spec.max_notional is not None:
            candidates.append(spec.max_notional)
        if spec.max_notional_fraction is not None:
            candidates.append(limits.max_notional * spec.max_notional_fraction)
        if spec.max_equity_notional_fraction is not None:
            candidates.append(equity * spec.max_equity_notional_fraction)
        return min(candidates)

    symbol_concentration_cap = min(
        limits.max_symbol_notional,
        equity * limits.max_concentration_fraction,
    )
    default_cap = max(
        limits.max_notional * policy.default_max_notional_fraction,
        symbol_concentration_cap,
    )
    candidates.append(default_cap)
    return min(candidates)


def calculate_group_exposure(
    *,
    plan: TradePlan,
    state: RiskState,
    limits: RiskLimits,
    policy: CorrelationGroupPolicy = DEFAULT_CORRELATION_GROUP_POLICY,
) -> CorrelationGroupExposure | None:
    """Tính tổng rủi ro và notional theo nhóm tương quan của `plan.symbol`."""
    group_id = resolve_correlation_group(plan.symbol, policy)
    if group_id is None:
        return None

    matching_exposures: list[OpenExposure] = [
        item
        for item in state.open_exposures
        if resolve_correlation_group(item.symbol, policy) == group_id
    ]
    pending_group_risk = getattr(state, "pending_group_reservation_risk", ZERO)
    pending_group_notional = max(
        getattr(state, "pending_group_reservation_notional", ZERO),
        state.pending_symbol_reservation_notional,
    )

    open_risk = sum((item.risk_amount for item in matching_exposures), ZERO) + pending_group_risk
    open_notional = (
        sum((item.notional for item in matching_exposures), ZERO) + pending_group_notional
    )
    requested_risk = plan.estimated_loss
    requested_notional = plan.notional

    spec = policy.group_spec(group_id)
    max_group_risk = _compute_group_risk_cap(
        spec=spec,
        policy=policy,
        limits=limits,
        equity=state.equity,
    )
    max_group_notional = _compute_group_notional_cap(
        spec=spec,
        policy=policy,
        limits=limits,
        equity=state.equity,
    )
    max_group_positions = (
        spec.max_positions
        if spec is not None and spec.max_positions is not None
        else policy.default_max_positions
    )

    symbols = tuple(
        sorted(
            {_normalize_symbol(item.symbol) for item in matching_exposures}
            | {_normalize_symbol(plan.symbol)}
        )
    )
    open_positions_in_group = len(matching_exposures)
    return CorrelationGroupExposure(
        group_id=group_id,
        symbols=symbols,
        open_risk=open_risk,
        requested_risk=requested_risk,
        total_risk=open_risk + requested_risk,
        max_group_risk=max_group_risk,
        open_notional=open_notional,
        requested_notional=requested_notional,
        total_notional=open_notional + requested_notional,
        max_group_notional=max_group_notional,
        open_positions_in_group=open_positions_in_group,
        total_positions_in_group=open_positions_in_group + 1,
        max_group_positions=max_group_positions,
    )


def evaluate_correlation_group_cap(
    *,
    plan: TradePlan,
    state: RiskState,
    limits: RiskLimits,
    policy: CorrelationGroupPolicy = DEFAULT_CORRELATION_GROUP_POLICY,
) -> CorrelationGroupCheckResult:
    """Đánh giá kế hoạch lệnh mới với các trần của nhóm tương quan."""
    if not policy.enabled:
        return CorrelationGroupCheckResult(
            approved=True,
            reason_code="APPROVED",
            detail_vi="Kiểm tra nhóm tương quan đang tắt.",
            group_id=None,
            exposure=None,
        )

    exposure = calculate_group_exposure(
        plan=plan,
        state=state,
        limits=limits,
        policy=policy,
    )
    if exposure is None:
        return CorrelationGroupCheckResult(
            approved=True,
            reason_code="APPROVED",
            detail_vi="Symbol không thuộc nhóm tương quan bị giới hạn.",
            group_id=None,
            exposure=None,
        )

    if (
        exposure.max_group_positions is not None
        and exposure.total_positions_in_group > exposure.max_group_positions
    ):
        return CorrelationGroupCheckResult(
            approved=False,
            reason_code=CORRELATION_GROUP_POSITIONS_EXCEEDED,
            detail_vi=(
                f"Số vị thế của nhóm tương quan {exposure.group_id} "
                f"({exposure.total_positions_in_group}) vượt trần "
                f"{exposure.max_group_positions}."
            ),
            group_id=exposure.group_id,
            exposure=exposure,
        )

    if exposure.total_risk > exposure.max_group_risk:
        return CorrelationGroupCheckResult(
            approved=False,
            reason_code=CORRELATION_GROUP_RISK_EXCEEDED,
            detail_vi=(
                f"Tổng rủi ro nhóm tương quan {exposure.group_id} "
                f"({exposure.total_risk}) vượt trần {exposure.max_group_risk}."
            ),
            group_id=exposure.group_id,
            exposure=exposure,
        )

    if exposure.total_notional > exposure.max_group_notional:
        return CorrelationGroupCheckResult(
            approved=False,
            reason_code=CORRELATION_GROUP_NOTIONAL_EXCEEDED,
            detail_vi=(
                f"Tổng notional nhóm tương quan {exposure.group_id} "
                f"({exposure.total_notional}) vượt trần {exposure.max_group_notional}."
            ),
            group_id=exposure.group_id,
            exposure=exposure,
        )

    return CorrelationGroupCheckResult(
        approved=True,
        reason_code="APPROVED",
        detail_vi=f"Kế hoạch nằm trong trần nhóm tương quan {exposure.group_id}.",
        group_id=exposure.group_id,
        exposure=exposure,
    )
