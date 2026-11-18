"""Tính ký quỹ duy trì (Maintenance Margin), giá thanh lý và biên an toàn stop (FX-7 / CRY-29).

Nguồn tài liệu chính thức của Binance USDⓈ-M Futures (đã kiểm tra ngày `2026-11-18`):
1. Công thức tính Giá Thanh lý (Liquidation Price) cho hợp đồng USDⓈ-M Futures
   (chế độ `Isolated Margin` / `One-Way Mode`, trong đó `TMM = 0`, `UPNL = 0`,
   `WB = isolatedWalletBalance` của riêng vị thế):
   https://www.binance.com/en/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93
   - Phương trình cân bằng tại điểm thanh lý (`Margin Balance == Maintenance Margin`):
     `WB + Side * Q * (LP - EP) = Q * LP * MMR - cum`
   - Vị thế LONG (`Side = +1`):
     `LP_long = (Q * EP - WB - cum) / (Q * (1 - MMR))`
   - Vị thế SHORT (`Side = -1`):
     `LP_short = (WB + cum + Q * EP) / (Q * (1 + MMR))`
2. Bảng bậc đòn bẩy và tỷ lệ ký quỹ duy trì (Leverage & Margin Tiers / MMR & Maintenance Amount):
   https://www.binance.com/en/support/faq/detail/360033162192
   - Công thức ký quỹ duy trì theo bậc notional:
     `Maintenance Margin = Position Notional * MMR - cum`
   - Tại mỗi mốc biên `notional_floor` giữa hai bậc liền kề, `cum` bảo đảm hàm ký quỹ duy trì
     liên tục: `floor_k * MMR_k - cum_k == floor_k * MMR_{k-1} - cum_{k-1}`.
3. Tài liệu REST API Binance Derivatives USDⓈ-M — Notional and Leverage Brackets
   (`GET /fapi/v1/leverageBracket`):
   https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account
   - Các trường chuẩn mỗi bậc: `bracket`, `initialLeverage`, `notionalFloor`, `notionalCap`,
     `maintMarginRatio` (MMR), `cum`.
4. Kiểu giá kích hoạt lệnh dừng (`workingType`: `MARK_PRICE` so với `CONTRACT_PRICE`)
   và Giao thức thanh lý:
   https://www.binance.com/en/support/faq/detail/ceccfcfb4e3a45e3b48b0b1bb1a8ae46
   https://www.binance.com/en/support/faq/detail/360033525271
   - Sàn Binance kích hoạt thanh lý bắt buộc theo `MARK_PRICE`. Lệnh bảo vệ stop-loss nên ưu tiên
     `workingType="MARK_PRICE"` và giữ khoảng đệm an toàn (`liquidation_safety_buffer`, mặc định
     20% khoảng cách từ giá vào lệnh tới giá thanh lý) để tránh bị thanh lý trước khi khớp stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_bot.domain.risk.sizing import TradingSide

ZERO = Decimal("0")
ONE = Decimal("1")

# Biên an toàn mặc định bảo thủ: khoảng cách từ stop tới giá thanh lý tối thiểu bằng 20%
# tổng khoảng cách từ entry tới giá thanh lý.
DEFAULT_LIQUIDATION_SAFETY_BUFFER = Decimal("0.20")


@dataclass(frozen=True, slots=True)
class MaintenanceMarginTier:
    """Một bậc ký quỹ duy trì theo quy tắc Notional & Leverage Brackets của Binance USDⓈ-M."""

    bracket: int
    notional_floor: Decimal
    notional_cap: Decimal | None
    max_leverage: Decimal
    maint_margin_ratio: Decimal
    cum: Decimal = ZERO

    def __post_init__(self) -> None:
        if type(self.bracket) is not int or self.bracket < 1:
            raise ValueError("Thứ tự bậc (bracket) phải là số nguyên dương.")
        decimals = (
            self.notional_floor,
            self.max_leverage,
            self.maint_margin_ratio,
            self.cum,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimals):
            raise ValueError("Tham số bậc MMR phải là Decimal hữu hạn.")
        if self.notional_floor < ZERO:
            raise ValueError("notional_floor phải không âm.")
        if self.notional_cap is not None and (
            not isinstance(self.notional_cap, Decimal)
            or not self.notional_cap.is_finite()
            or self.notional_cap <= self.notional_floor
        ):
            raise ValueError("notional_cap phải là Decimal hữu hạn lớn hơn notional_floor.")
        if self.max_leverage < ONE:
            raise ValueError("Đòn bẩy tối đa của bậc phải >= 1.")
        if not ZERO < self.maint_margin_ratio < ONE:
            raise ValueError("Tỷ lệ MMR phải nằm trong khoảng (0, 1).")
        if self.cum < ZERO:
            raise ValueError("Khoản khấu trừ tích lũy (cum) phải không âm.")


# Bảng bậc MMR chuẩn cho BTCUSDT trên Binance USDⓈ-M Futures (kiểm tra 2026-11-18).
BTCUSDT_MAINTENANCE_MARGIN_TIERS: tuple[MaintenanceMarginTier, ...] = (
    MaintenanceMarginTier(
        bracket=1,
        notional_floor=Decimal("0"),
        notional_cap=Decimal("50000"),
        max_leverage=Decimal("125"),
        maint_margin_ratio=Decimal("0.004"),
        cum=Decimal("0"),
    ),
    MaintenanceMarginTier(
        bracket=2,
        notional_floor=Decimal("50000"),
        notional_cap=Decimal("250000"),
        max_leverage=Decimal("100"),
        maint_margin_ratio=Decimal("0.005"),
        cum=Decimal("50"),
    ),
    MaintenanceMarginTier(
        bracket=3,
        notional_floor=Decimal("250000"),
        notional_cap=Decimal("1000000"),
        max_leverage=Decimal("50"),
        maint_margin_ratio=Decimal("0.010"),
        cum=Decimal("1300"),
    ),
    MaintenanceMarginTier(
        bracket=4,
        notional_floor=Decimal("1000000"),
        notional_cap=Decimal("5000000"),
        max_leverage=Decimal("25"),
        maint_margin_ratio=Decimal("0.025"),
        cum=Decimal("16300"),
    ),
    MaintenanceMarginTier(
        bracket=5,
        notional_floor=Decimal("5000000"),
        notional_cap=Decimal("20000000"),
        max_leverage=Decimal("10"),
        maint_margin_ratio=Decimal("0.050"),
        cum=Decimal("141300"),
    ),
    MaintenanceMarginTier(
        bracket=6,
        notional_floor=Decimal("20000000"),
        notional_cap=None,
        max_leverage=Decimal("5"),
        maint_margin_ratio=Decimal("0.100"),
        cum=Decimal("1141300"),
    ),
)

# Bảng bậc MMR chuẩn cho ETHUSDT trên Binance USDⓈ-M Futures (kiểm tra 2026-11-18).
ETHUSDT_MAINTENANCE_MARGIN_TIERS: tuple[MaintenanceMarginTier, ...] = (
    MaintenanceMarginTier(
        bracket=1,
        notional_floor=Decimal("0"),
        notional_cap=Decimal("50000"),
        max_leverage=Decimal("100"),
        maint_margin_ratio=Decimal("0.005"),
        cum=Decimal("0"),
    ),
    MaintenanceMarginTier(
        bracket=2,
        notional_floor=Decimal("50000"),
        notional_cap=Decimal("250000"),
        max_leverage=Decimal("75"),
        maint_margin_ratio=Decimal("0.010"),
        cum=Decimal("250"),
    ),
    MaintenanceMarginTier(
        bracket=3,
        notional_floor=Decimal("250000"),
        notional_cap=Decimal("1000000"),
        max_leverage=Decimal("50"),
        maint_margin_ratio=Decimal("0.025"),
        cum=Decimal("4000"),
    ),
    MaintenanceMarginTier(
        bracket=4,
        notional_floor=Decimal("1000000"),
        notional_cap=Decimal("5000000"),
        max_leverage=Decimal("25"),
        maint_margin_ratio=Decimal("0.050"),
        cum=Decimal("29000"),
    ),
    MaintenanceMarginTier(
        bracket=5,
        notional_floor=Decimal("5000000"),
        notional_cap=None,
        max_leverage=Decimal("10"),
        maint_margin_ratio=Decimal("0.100"),
        cum=Decimal("279000"),
    ),
)

# Bảng bậc MMR bảo thủ mặc định cho các hợp đồng altcoin USDⓈ-M khi chưa truyền bảng riêng.
DEFAULT_MAINTENANCE_MARGIN_TIERS: tuple[MaintenanceMarginTier, ...] = (
    MaintenanceMarginTier(
        bracket=1,
        notional_floor=Decimal("0"),
        notional_cap=Decimal("10000"),
        max_leverage=Decimal("50"),
        maint_margin_ratio=Decimal("0.010"),
        cum=Decimal("0"),
    ),
    MaintenanceMarginTier(
        bracket=2,
        notional_floor=Decimal("10000"),
        notional_cap=Decimal("50000"),
        max_leverage=Decimal("25"),
        maint_margin_ratio=Decimal("0.020"),
        cum=Decimal("100"),
    ),
    MaintenanceMarginTier(
        bracket=3,
        notional_floor=Decimal("50000"),
        notional_cap=Decimal("250000"),
        max_leverage=Decimal("10"),
        maint_margin_ratio=Decimal("0.025"),
        cum=Decimal("350"),
    ),
    MaintenanceMarginTier(
        bracket=4,
        notional_floor=Decimal("250000"),
        notional_cap=Decimal("1000000"),
        max_leverage=Decimal("5"),
        maint_margin_ratio=Decimal("0.050"),
        cum=Decimal("6600"),
    ),
    MaintenanceMarginTier(
        bracket=5,
        notional_floor=Decimal("1000000"),
        notional_cap=None,
        max_leverage=Decimal("2"),
        maint_margin_ratio=Decimal("0.100"),
        cum=Decimal("56600"),
    ),
)

SYMBOL_MAINTENANCE_MARGIN_TIERS: dict[str, tuple[MaintenanceMarginTier, ...]] = {
    "BTCUSDT": BTCUSDT_MAINTENANCE_MARGIN_TIERS,
    "ETHUSDT": ETHUSDT_MAINTENANCE_MARGIN_TIERS,
}


def default_tiers_for_symbol(symbol: str | None = None) -> tuple[MaintenanceMarginTier, ...]:
    """Trả bảng bậc MMR theo symbol; nếu chưa khai riêng thì dùng bảng bảo thủ mặc định."""
    if not symbol:
        return DEFAULT_MAINTENANCE_MARGIN_TIERS
    return SYMBOL_MAINTENANCE_MARGIN_TIERS.get(
        symbol.strip().upper(), DEFAULT_MAINTENANCE_MARGIN_TIERS
    )


def select_maintenance_tier(
    notional: Decimal,
    *,
    symbol: str | None = None,
    tiers: tuple[MaintenanceMarginTier, ...] | None = None,
) -> MaintenanceMarginTier:
    """Chọn bậc ký quỹ duy trì ứng với giá trị danh nghĩa (notional) của vị thế."""
    if not isinstance(notional, Decimal) or not notional.is_finite() or notional < ZERO:
        raise ValueError("Notional phải là Decimal hữu hạn không âm.")
    schedule = tiers if tiers is not None else default_tiers_for_symbol(symbol)
    if not schedule:
        raise ValueError("Bảng bậc MMR không được rỗng.")
    for tier in schedule:
        if tier.notional_cap is None or notional <= tier.notional_cap:
            return tier
    return schedule[-1]


def calculate_maintenance_margin(
    notional: Decimal,
    *,
    symbol: str | None = None,
    tiers: tuple[MaintenanceMarginTier, ...] | None = None,
) -> Decimal:
    """Tính ký quỹ duy trì theo công thức Binance: `MM = Notional * MMR - cum`."""
    tier = select_maintenance_tier(notional, symbol=symbol, tiers=tiers)
    mm = notional * tier.maint_margin_ratio - tier.cum
    return max(ZERO, mm)


def _normalize_side(side: TradingSide | str) -> TradingSide:
    normalized = side.strip().upper()
    if normalized not in ("LONG", "SHORT"):
        raise ValueError(f"Hướng vị thế không hợp lệ: {side!r}.")
    return "LONG" if normalized == "LONG" else "SHORT"


def _solve_tier_liquidation_price(
    *,
    normalized_side: TradingSide,
    position_units: Decimal,
    entry_price: Decimal,
    wallet_balance: Decimal,
    tier: MaintenanceMarginTier,
) -> Decimal:
    entry_notional = position_units * entry_price
    if normalized_side == "LONG":
        denominator = position_units * (ONE - tier.maint_margin_ratio)
        numerator = entry_notional - wallet_balance - tier.cum
    else:
        denominator = position_units * (ONE + tier.maint_margin_ratio)
        numerator = wallet_balance + tier.cum + entry_notional
    if denominator <= ZERO:
        return ZERO
    return max(ZERO, numerator / denominator)


def calculate_liquidation_price(
    *,
    side: TradingSide | str,
    entry_price: Decimal,
    quantity: Decimal,
    leverage: Decimal,
    contract_multiplier: Decimal = ONE,
    wallet_balance: Decimal | None = None,
    symbol: str | None = None,
    tiers: tuple[MaintenanceMarginTier, ...] | None = None,
) -> Decimal:
    """Tính giá thanh lý chế độ Isolated One-Way trên hợp đồng tuyến tính USDⓈ-M."""
    normalized_side = _normalize_side(side)
    positives = (entry_price, quantity, leverage, contract_multiplier)
    if any(not isinstance(v, Decimal) or not v.is_finite() or v <= ZERO for v in positives):
        raise ValueError("entry_price, quantity, leverage và contract_multiplier phải dương.")
    position_units = quantity * contract_multiplier
    entry_notional = position_units * entry_price
    if wallet_balance is None:
        wb = entry_notional / leverage
    else:
        if not isinstance(wallet_balance, Decimal) or not wallet_balance.is_finite():
            raise ValueError("wallet_balance phải là Decimal hữu hạn.")
        wb = wallet_balance
    schedule = tiers if tiers is not None else default_tiers_for_symbol(symbol)
    if not schedule:
        raise ValueError("Bảng bậc MMR không được rỗng.")

    current_tier = select_maintenance_tier(entry_notional, tiers=schedule)
    liq_price = _solve_tier_liquidation_price(
        normalized_side=normalized_side,
        position_units=position_units,
        entry_price=entry_price,
        wallet_balance=wb,
        tier=current_tier,
    )
    for _ in range(len(schedule)):
        liq_notional = position_units * liq_price
        next_tier = select_maintenance_tier(liq_notional, tiers=schedule)
        if next_tier.bracket == current_tier.bracket:
            break
        current_tier = next_tier
        liq_price = _solve_tier_liquidation_price(
            normalized_side=normalized_side,
            position_units=position_units,
            entry_price=entry_price,
            wallet_balance=wb,
            tier=current_tier,
        )
    return liq_price


@dataclass(frozen=True, slots=True)
class LiquidationSafetyCheck:
    """Kết quả kiểm định khoảng cách an toàn giữa stop-loss và giá thanh lý."""

    safe: bool
    reason_code: str | None
    detail_vi: str
    liquidation_price: Decimal
    safe_stop_limit: Decimal
    safety_buffer: Decimal
    maintenance_margin: Decimal
    maint_margin_ratio: Decimal
    bracket: int


def check_stop_before_liquidation(
    *,
    side: TradingSide | str,
    entry_price: Decimal,
    stop_price: Decimal,
    quantity: Decimal,
    leverage: Decimal,
    contract_multiplier: Decimal = ONE,
    wallet_balance: Decimal | None = None,
    safety_buffer: Decimal = DEFAULT_LIQUIDATION_SAFETY_BUFFER,
    symbol: str | None = None,
    tiers: tuple[MaintenanceMarginTier, ...] | None = None,
) -> LiquidationSafetyCheck:
    """Kiểm tra stop-loss nằm trước giá thanh lý với biên an toàn tối thiểu `safety_buffer`."""
    normalized_side = _normalize_side(side)
    if not isinstance(stop_price, Decimal) or not stop_price.is_finite() or stop_price <= ZERO:
        raise ValueError("stop_price phải là Decimal hữu hạn dương.")
    if (
        not isinstance(safety_buffer, Decimal)
        or not safety_buffer.is_finite()
        or not ZERO <= safety_buffer < ONE
    ):
        raise ValueError("Biên an toàn thanh lý phải nằm trong [0, 1).")

    schedule = tiers if tiers is not None else default_tiers_for_symbol(symbol)
    entry_notional = quantity * contract_multiplier * entry_price
    tier = select_maintenance_tier(entry_notional, tiers=schedule)
    mm = calculate_maintenance_margin(entry_notional, tiers=schedule)
    liq_price = calculate_liquidation_price(
        side=normalized_side,
        entry_price=entry_price,
        quantity=quantity,
        leverage=leverage,
        contract_multiplier=contract_multiplier,
        wallet_balance=wallet_balance,
        tiers=schedule,
    )

    if normalized_side == "LONG":
        if liq_price >= entry_price:
            return LiquidationSafetyCheck(
                safe=False,
                reason_code="STOP_LIQUIDATION_UNSAFE",
                detail_vi=(
                    f"Giá thanh lý LONG ({liq_price.quantize(Decimal('0.01'))}) "
                    f"không thấp hơn giá vào ({entry_price.quantize(Decimal('0.01'))})."
                ),
                liquidation_price=liq_price,
                safe_stop_limit=entry_price,
                safety_buffer=safety_buffer,
                maintenance_margin=mm,
                maint_margin_ratio=tier.maint_margin_ratio,
                bracket=tier.bracket,
            )
        safe_stop_limit = liq_price + safety_buffer * (entry_price - liq_price)
        if stop_price <= liq_price:
            return LiquidationSafetyCheck(
                safe=False,
                reason_code="STOP_LIQUIDATION_UNSAFE",
                detail_vi=(
                    f"Stop LONG ({stop_price.quantize(Decimal('0.01'))}) chạm hoặc vượt qua "
                    f"giá thanh lý ({liq_price.quantize(Decimal('0.01'))})."
                ),
                liquidation_price=liq_price,
                safe_stop_limit=safe_stop_limit,
                safety_buffer=safety_buffer,
                maintenance_margin=mm,
                maint_margin_ratio=tier.maint_margin_ratio,
                bracket=tier.bracket,
            )
        if stop_price < safe_stop_limit:
            return LiquidationSafetyCheck(
                safe=False,
                reason_code="STOP_LIQUIDATION_UNSAFE",
                detail_vi=(
                    f"Stop LONG ({stop_price.quantize(Decimal('0.01'))}) vi phạm biên an toàn "
                    f"thanh lý tối thiểu ({safe_stop_limit.quantize(Decimal('0.01'))}, "
                    f"giá thanh lý {liq_price.quantize(Decimal('0.01'))})."
                ),
                liquidation_price=liq_price,
                safe_stop_limit=safe_stop_limit,
                safety_buffer=safety_buffer,
                maintenance_margin=mm,
                maint_margin_ratio=tier.maint_margin_ratio,
                bracket=tier.bracket,
            )
        return LiquidationSafetyCheck(
            safe=True,
            reason_code=None,
            detail_vi="Stop LONG nằm trong vùng an toàn trước giá thanh lý.",
            liquidation_price=liq_price,
            safe_stop_limit=safe_stop_limit,
            safety_buffer=safety_buffer,
            maintenance_margin=mm,
            maint_margin_ratio=tier.maint_margin_ratio,
            bracket=tier.bracket,
        )

    if liq_price <= entry_price:
        return LiquidationSafetyCheck(
            safe=False,
            reason_code="STOP_LIQUIDATION_UNSAFE",
            detail_vi=(
                f"Giá thanh lý SHORT ({liq_price.quantize(Decimal('0.01'))}) "
                f"không cao hơn giá vào ({entry_price.quantize(Decimal('0.01'))})."
            ),
            liquidation_price=liq_price,
            safe_stop_limit=entry_price,
            safety_buffer=safety_buffer,
            maintenance_margin=mm,
            maint_margin_ratio=tier.maint_margin_ratio,
            bracket=tier.bracket,
        )
    safe_stop_limit = liq_price - safety_buffer * (liq_price - entry_price)
    if stop_price >= liq_price:
        return LiquidationSafetyCheck(
            safe=False,
            reason_code="STOP_LIQUIDATION_UNSAFE",
            detail_vi=(
                f"Stop SHORT ({stop_price.quantize(Decimal('0.01'))}) chạm hoặc vượt qua "
                f"giá thanh lý ({liq_price.quantize(Decimal('0.01'))})."
            ),
            liquidation_price=liq_price,
            safe_stop_limit=safe_stop_limit,
            safety_buffer=safety_buffer,
            maintenance_margin=mm,
            maint_margin_ratio=tier.maint_margin_ratio,
            bracket=tier.bracket,
        )
    if stop_price > safe_stop_limit:
        return LiquidationSafetyCheck(
            safe=False,
            reason_code="STOP_LIQUIDATION_UNSAFE",
            detail_vi=(
                f"Stop SHORT ({stop_price.quantize(Decimal('0.01'))}) vi phạm biên an toàn "
                f"thanh lý tối thiểu ({safe_stop_limit.quantize(Decimal('0.01'))}, "
                f"giá thanh lý {liq_price.quantize(Decimal('0.01'))})."
            ),
            liquidation_price=liq_price,
            safe_stop_limit=safe_stop_limit,
            safety_buffer=safety_buffer,
            maintenance_margin=mm,
            maint_margin_ratio=tier.maint_margin_ratio,
            bracket=tier.bracket,
        )
    return LiquidationSafetyCheck(
        safe=True,
        reason_code=None,
        detail_vi="Stop SHORT nằm trong vùng an toàn trước giá thanh lý.",
        liquidation_price=liq_price,
        safe_stop_limit=safe_stop_limit,
        safety_buffer=safety_buffer,
        maintenance_margin=mm,
        maint_margin_ratio=tier.maint_margin_ratio,
        bracket=tier.bracket,
    )
