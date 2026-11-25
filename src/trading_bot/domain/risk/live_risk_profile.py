"""Hợp đồng semantics equity, cửa sổ lỗ và hard limits cho tài khoản Live (OPS07F / Task 6).

Nguyên tắc cốt lõi:
- Không có giá trị mặc định cho vốn, cap, leverage hay cửa sổ rủi ro: mọi ngưỡng bắt buộc
  do Owner/Risk Approver cung cấp tường minh và ký (maker-checker).
- Mọi trạng thái không chắc chắn (thiếu chữ ký, hết hạn, sai hash/account/build, thiếu hoặc
  cũ giá mark, trạng thái pause/kill) đều fail-closed.
- Lệnh/reservation ở trạng thái UNKNOWN luôn được cộng vào exposure đang sử dụng và tuyệt đối
  không được giải phóng sức chứa.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from trading_bot.domain.risk.risk_windows import (
    LossWindowPolicy,
    RealizedTradePnlEvent,
    calculate_loss_windows,
)

ZERO = Decimal("0")


class LiveAccountMode(StrEnum):
    """Chế độ tài khoản Binance USDⓈ-M được chọn tường minh."""

    SINGLE_ASSET_USDT = "SINGLE_ASSET_USDT"
    MULTI_ASSET_MARGIN = "MULTI_ASSET_MARGIN"


class LiveRiskReasonCode(StrEnum):
    """Mã lý do chuẩn hóa cho đánh giá hard limits Live."""

    APPROVED = "LIVE_RISK_APPROVED"
    PAUSED_OR_KILLED = "LIVE_RISK_PAUSED_OR_KILLED"
    PROFILE_UNSIGNED = "LIVE_RISK_PROFILE_UNSIGNED"
    MAKER_CHECKER_VIOLATION = "LIVE_RISK_MAKER_CHECKER_VIOLATION"
    PROFILE_EXPIRED = "LIVE_RISK_PROFILE_EXPIRED"
    PROFILE_SCOPE_MISMATCH = "LIVE_RISK_PROFILE_SCOPE_MISMATCH"
    PROFILE_HASH_MISMATCH = "LIVE_RISK_PROFILE_HASH_MISMATCH"
    MARK_MISSING = "LIVE_RISK_MARK_MISSING"
    MARK_STALE = "LIVE_RISK_MARK_STALE"
    EQUITY_NON_POSITIVE = "LIVE_RISK_EQUITY_NON_POSITIVE"
    ORDER_CAP_EXCEEDED = "LIVE_RISK_ORDER_CAP_EXCEEDED"
    LEVERAGE_CAP_EXCEEDED = "LIVE_RISK_LEVERAGE_CAP_EXCEEDED"
    SYMBOL_CAP_EXCEEDED = "LIVE_RISK_SYMBOL_CAP_EXCEEDED"
    ACCOUNT_CAP_EXCEEDED = "LIVE_RISK_ACCOUNT_CAP_EXCEEDED"
    AGGREGATE_EXPOSURE_EXCEEDED = "LIVE_RISK_AGGREGATE_EXPOSURE_EXCEEDED"
    DAILY_LOSS_EXCEEDED = "LIVE_RISK_DAILY_LOSS_EXCEEDED"
    ROLLING_LOSS_EXCEEDED = "LIVE_RISK_ROLLING_LOSS_EXCEEDED"


def _require_positive(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} phải là Decimal dương hữu hạn.")


def _require_non_negative(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} phải là Decimal không âm hữu hạn.")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có timezone.")


@dataclass(frozen=True, slots=True)
class SignedLiveRiskProfile:
    """Profile rủi ro Live có phiên bản, chữ ký maker-checker và hash cố định.

    Không đặt giá trị mặc định cho các giới hạn vốn/rủi ro để tránh dùng nhầm ngưỡng PAPER.
    """

    profile_version: str
    account_id: str
    environment: str
    build_revision: str
    account_mode: LiveAccountMode
    timezone_name: str
    daily_reset_hour: int
    rolling_window_hours: int
    include_unrealized_loss: bool
    max_account_notional: Decimal
    max_symbol_notional: Decimal
    max_order_notional: Decimal
    max_leverage: Decimal
    max_aggregate_exposure_notional: Decimal
    max_daily_loss: Decimal
    max_rolling_loss: Decimal
    owner_id: str
    approver_id: str
    signed_at: datetime
    expires_at: datetime
    signature_digest: str

    def __post_init__(self) -> None:
        if not self.profile_version.strip():
            raise ValueError("profile_version không được rỗng.")
        if not self.account_id.strip():
            raise ValueError("account_id không được rỗng.")
        if not self.environment.strip():
            raise ValueError("environment không được rỗng.")
        if not self.build_revision.strip():
            raise ValueError("build_revision không được rỗng.")
        _require_aware(self.signed_at, "signed_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.signed_at:
            raise ValueError("expires_at phải sau signed_at.")
        if self.rolling_window_hours <= 0:
            raise ValueError("rolling_window_hours phải lớn hơn 0.")
        LossWindowPolicy(
            timezone_name=self.timezone_name,
            daily_reset_hour=self.daily_reset_hour,
            rolling_window=timedelta(hours=self.rolling_window_hours),
            include_unrealized_loss=self.include_unrealized_loss,
        )
        _require_positive(self.max_account_notional, "max_account_notional")
        _require_positive(self.max_symbol_notional, "max_symbol_notional")
        _require_positive(self.max_order_notional, "max_order_notional")
        _require_positive(self.max_leverage, "max_leverage")
        _require_positive(self.max_aggregate_exposure_notional, "max_aggregate_exposure_notional")
        _require_positive(self.max_daily_loss, "max_daily_loss")
        _require_positive(self.max_rolling_loss, "max_rolling_loss")

    def compute_hash(self) -> str:
        """Tính SHA-256 chuẩn hóa để pin vào mỗi quyết định rủi ro."""
        payload = {
            "profile_version": self.profile_version,
            "account_id": self.account_id,
            "environment": self.environment.upper(),
            "build_revision": self.build_revision,
            "account_mode": self.account_mode.value,
            "timezone_name": self.timezone_name,
            "daily_reset_hour": self.daily_reset_hour,
            "rolling_window_hours": self.rolling_window_hours,
            "include_unrealized_loss": self.include_unrealized_loss,
            "max_account_notional": str(self.max_account_notional),
            "max_symbol_notional": str(self.max_symbol_notional),
            "max_order_notional": str(self.max_order_notional),
            "max_leverage": str(self.max_leverage),
            "max_aggregate_exposure_notional": str(self.max_aggregate_exposure_notional),
            "max_daily_loss": str(self.max_daily_loss),
            "max_rolling_loss": str(self.max_rolling_loss),
            "owner_id": self.owner_id,
            "approver_id": self.approver_id,
            "signed_at": self.signed_at.astimezone(UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def loss_window_policy(self) -> LossWindowPolicy:
        return LossWindowPolicy(
            timezone_name=self.timezone_name,
            daily_reset_hour=self.daily_reset_hour,
            rolling_window=timedelta(hours=self.rolling_window_hours),
            include_unrealized_loss=self.include_unrealized_loss,
        )


def sign_live_risk_profile_digest(profile_hash: str, *, signing_key: str) -> str:
    """Tạo chữ ký HMAC-SHA256 cho profile_hash phục vụ kiểm chứng chữ ký duyệt."""
    return hmac.new(
        signing_key.encode("utf-8"),
        profile_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class VenueEquitySnapshot:
    """Ảnh chụp vốn, margin, exposure và giá mark theo semantics Binance USDⓈ-M.

    `wallet_balance` đã bao gồm realized PnL, phí và funding đã thanh toán nên không cộng
    trùng realized PnL vào `venue_equity`. `venue_equity = wallet_balance + unrealized_pnl`.
    """

    account_id: str
    environment: str
    account_mode: LiveAccountMode
    wallet_balance: Decimal
    unrealized_pnl: Decimal
    available_balance: Decimal
    initial_margin_used: Decimal
    fee_and_funding_today: Decimal  # số âm là chi phí/lỗ ròng phát sinh hôm nay
    fee_and_funding_rolling: Decimal  # số âm là chi phí/lỗ ròng trong cửa sổ trượt
    open_exposure_notional: Decimal
    symbol_open_exposure_notional: Decimal
    active_reservations_notional: Decimal
    symbol_reservations_notional: Decimal
    unknown_order_exposure_notional: Decimal
    symbol_unknown_exposure_notional: Decimal
    mark_price: Decimal | None
    mark_observed_at: datetime | None

    @property
    def venue_equity(self) -> Decimal:
        """Equity sàn (`totalMarginBalance`): wallet_balance + unrealized_pnl."""
        return self.wallet_balance + self.unrealized_pnl

    @property
    def total_effective_exposure(self) -> Decimal:
        """Tổng exposure đang chiếm dụng gồm vị thế mở, reservation và UNKNOWN."""
        return (
            self.open_exposure_notional
            + self.active_reservations_notional
            + self.unknown_order_exposure_notional
        )

    @property
    def symbol_effective_exposure(self) -> Decimal:
        """Exposure theo symbol gồm vị thế mở, reservation và UNKNOWN."""
        return (
            self.symbol_open_exposure_notional
            + self.symbol_reservations_notional
            + self.symbol_unknown_exposure_notional
        )


@dataclass(frozen=True, slots=True)
class LiveOrderRiskRequest:
    """Yêu cầu kiểm tra rủi ro cho một lệnh mới trên tài khoản Live."""

    symbol: str
    order_notional: Decimal
    leverage: Decimal
    build_revision: str
    expected_profile_hash: str
    paused_or_killed: bool = False

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol không được rỗng.")
        _require_positive(self.order_notional, "order_notional")
        _require_positive(self.leverage, "leverage")


@dataclass(frozen=True, slots=True)
class LiveRiskDecision:
    """Kết quả đánh giá hard limits gắn chặt `profile_hash` và `profile_version`."""

    approved: bool
    reason_code: LiveRiskReasonCode
    detail_vi: str
    profile_version: str
    profile_hash: str
    evaluated_at: datetime
    venue_equity: Decimal
    projected_account_exposure: Decimal
    projected_symbol_exposure: Decimal
    effective_daily_loss: Decimal
    effective_rolling_loss: Decimal


def evaluate_live_risk_limits(
    *,
    profile: SignedLiveRiskProfile,
    equity: VenueEquitySnapshot,
    request: LiveOrderRiskRequest,
    realized_events: Iterable[RealizedTradePnlEvent],
    as_of: datetime,
    signing_key: str,
    max_mark_age: timedelta = timedelta(seconds=10),
) -> LiveRiskDecision:
    """Đánh giá toàn bộ hard limits Live theo nguyên tắc fail-closed."""
    _require_aware(as_of, "as_of")
    actual_hash = profile.compute_hash()
    projected_account = equity.total_effective_exposure + request.order_notional
    projected_symbol = equity.symbol_effective_exposure + request.order_notional

    def _deny(
        code: LiveRiskReasonCode,
        detail_vi: str,
        *,
        daily_loss: Decimal = ZERO,
        rolling_loss: Decimal = ZERO,
    ) -> LiveRiskDecision:
        return LiveRiskDecision(
            approved=False,
            reason_code=code,
            detail_vi=detail_vi,
            profile_version=profile.profile_version,
            profile_hash=actual_hash,
            evaluated_at=as_of,
            venue_equity=equity.venue_equity,
            projected_account_exposure=projected_account,
            projected_symbol_exposure=projected_symbol,
            effective_daily_loss=daily_loss,
            effective_rolling_loss=rolling_loss,
        )

    # 1. Pause / kill có hiệu lực ngay lập tức
    if request.paused_or_killed:
        return _deny(
            LiveRiskReasonCode.PAUSED_OR_KILLED,
            "Tài khoản hoặc workspace đang ở trạng thái pause/kill; từ chối mở lệnh ngay.",
        )

    # 2. Xác thực chữ ký maker-checker và hash pin của profile
    if not profile.owner_id.strip() or not profile.approver_id.strip():
        return _deny(
            LiveRiskReasonCode.PROFILE_UNSIGNED,
            "Profile rủi ro Live thiếu thông tin Owner hoặc Risk Approver.",
        )
    if profile.owner_id.strip() == profile.approver_id.strip():
        return _deny(
            LiveRiskReasonCode.MAKER_CHECKER_VIOLATION,
            "Vi phạm maker-checker: người duyệt phải khác người lập profile.",
        )
    expected_sig = sign_live_risk_profile_digest(actual_hash, signing_key=signing_key)
    if not profile.signature_digest or not hmac.compare_digest(
        profile.signature_digest, expected_sig
    ):
        return _deny(
            LiveRiskReasonCode.PROFILE_UNSIGNED,
            "Chữ ký profile rủi ro Live không hợp lệ hoặc bị thiếu.",
        )
    if request.expected_profile_hash != actual_hash:
        return _deny(
            LiveRiskReasonCode.PROFILE_HASH_MISMATCH,
            "Mã băm profile rủi ro không khớp với phiên bản đã ghim vào yêu cầu.",
        )
    if as_of.astimezone(UTC) >= profile.expires_at.astimezone(UTC):
        return _deny(
            LiveRiskReasonCode.PROFILE_EXPIRED,
            "Profile rủi ro Live đã hết hạn hiệu lực.",
        )
    if (
        profile.account_id != equity.account_id
        or profile.environment.upper() != equity.environment.upper()
        or profile.account_mode is not equity.account_mode
        or profile.build_revision != request.build_revision
    ):
        return _deny(
            LiveRiskReasonCode.PROFILE_SCOPE_MISMATCH,
            "Profile rủi ro không khớp account_id, environment, account_mode hoặc build_revision.",
        )

    # 3. Kiểm tra độ tin cậy của giá mark và equity
    if equity.mark_price is None or equity.mark_price <= ZERO or equity.mark_observed_at is None:
        return _deny(
            LiveRiskReasonCode.MARK_MISSING,
            "Thiếu giá mark hoặc thời điểm quan sát; không cho phép ước lượng thay thế.",
        )
    _require_aware(equity.mark_observed_at, "mark_observed_at")
    mark_age = as_of.astimezone(UTC) - equity.mark_observed_at.astimezone(UTC)
    if mark_age < timedelta(0) or mark_age > max_mark_age:
        return _deny(
            LiveRiskReasonCode.MARK_STALE,
            "Giá mark đã cũ hoặc ở tương lai so với mốc đánh giá.",
        )
    if equity.venue_equity <= ZERO:
        return _deny(
            LiveRiskReasonCode.EQUITY_NON_POSITIVE,
            "Venue equity không dương; chặn mở vị thế mới.",
        )

    # 4. Tính cửa sổ lỗ theo múi giờ cấu hình + phí/funding + unrealized loss (nếu bật)
    window_snap = calculate_loss_windows(
        realized_events,
        as_of=as_of,
        policy=profile.loss_window_policy(),
        unrealized_pnl=equity.unrealized_pnl,
    )
    fee_loss_today = max(ZERO, -equity.fee_and_funding_today)
    fee_loss_rolling = max(ZERO, -equity.fee_and_funding_rolling)
    effective_daily_loss = window_snap.realized_loss_today + fee_loss_today
    effective_rolling_loss = window_snap.realized_loss_rolling + fee_loss_rolling

    if effective_daily_loss >= profile.max_daily_loss:
        return _deny(
            LiveRiskReasonCode.DAILY_LOSS_EXCEEDED,
            "Lỗ ròng ngày (gồm phí/funding) đã chạm hoặc vượt giới hạn ngày.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )
    if effective_rolling_loss >= profile.max_rolling_loss:
        return _deny(
            LiveRiskReasonCode.ROLLING_LOSS_EXCEEDED,
            "Lỗ lũy kế cửa sổ trượt đã chạm hoặc vượt giới hạn.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )

    # 5. Kiểm tra các giới hạn cứng (boundary <= đạt, > từ chối)
    if request.order_notional > profile.max_order_notional:
        return _deny(
            LiveRiskReasonCode.ORDER_CAP_EXCEEDED,
            "Giá trị lệnh vượt quá giới hạn tối đa cho mỗi lệnh.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )
    if request.leverage > profile.max_leverage:
        return _deny(
            LiveRiskReasonCode.LEVERAGE_CAP_EXCEEDED,
            "Đòn bẩy yêu cầu vượt quá giới hạn tối đa được duyệt.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )
    if projected_symbol > profile.max_symbol_notional:
        return _deny(
            LiveRiskReasonCode.SYMBOL_CAP_EXCEEDED,
            "Tổng exposure của symbol (gồm reservation và UNKNOWN) vượt giới hạn.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )
    if projected_account > profile.max_account_notional:
        return _deny(
            LiveRiskReasonCode.ACCOUNT_CAP_EXCEEDED,
            "Tổng exposure tài khoản (gồm reservation và UNKNOWN) vượt giới hạn.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )
    if projected_account > profile.max_aggregate_exposure_notional:
        return _deny(
            LiveRiskReasonCode.AGGREGATE_EXPOSURE_EXCEEDED,
            "Tổng exposure hợp nhất vượt trần rủi ro cho phép.",
            daily_loss=effective_daily_loss,
            rolling_loss=effective_rolling_loss,
        )

    return LiveRiskDecision(
        approved=True,
        reason_code=LiveRiskReasonCode.APPROVED,
        detail_vi="Đạt toàn bộ giới hạn cứng và chữ ký rủi ro Live.",
        profile_version=profile.profile_version,
        profile_hash=actual_hash,
        evaluated_at=as_of,
        venue_equity=equity.venue_equity,
        projected_account_exposure=projected_account,
        projected_symbol_exposure=projected_symbol,
        effective_daily_loss=effective_daily_loss,
        effective_rolling_loss=effective_rolling_loss,
    )


def validate_live_risk_cap_change(
    *,
    current: SignedLiveRiskProfile,
    proposed: SignedLiveRiskProfile,
    requester_id: str,
    approver_id: str | None,
) -> tuple[bool, str]:
    """Kiểm tra quy tắc thay đổi cap: giảm cap hiệu lực ngay, tăng cap cần maker-checker."""
    if not requester_id.strip():
        return False, "Thiếu requester_id."

    loosened = any(
        (
            proposed.max_account_notional > current.max_account_notional,
            proposed.max_symbol_notional > current.max_symbol_notional,
            proposed.max_order_notional > current.max_order_notional,
            proposed.max_leverage > current.max_leverage,
            proposed.max_aggregate_exposure_notional > current.max_aggregate_exposure_notional,
            proposed.max_daily_loss > current.max_daily_loss,
            proposed.max_rolling_loss > current.max_rolling_loss,
        )
    )
    if not loosened:
        return True, "Giảm cap / siết rủi ro có hiệu lực ngay lập tức."

    if not approver_id or not approver_id.strip():
        return False, "Tăng giới hạn rủi ro bắt buộc phải có Risk Approver phê duyệt."
    if approver_id.strip() == requester_id.strip():
        return False, "Vi phạm maker-checker: người duyệt tăng cap phải khác người đề xuất."
    return True, "Tăng giới hạn rủi ro hợp lệ theo maker-checker."
