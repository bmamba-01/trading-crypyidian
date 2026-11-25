"""Unit tests cho semantics equity, risk window và hard limits Live (OPS07F / Task 6 / CRY-464)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trading_bot.domain.risk.live_risk_profile import (
    LiveAccountMode,
    LiveOrderRiskRequest,
    LiveRiskReasonCode,
    SignedLiveRiskProfile,
    VenueEquitySnapshot,
    evaluate_live_risk_limits,
    sign_live_risk_profile_digest,
    validate_live_risk_cap_change,
)
from trading_bot.domain.risk.risk_windows import RealizedTradePnlEvent

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
SIGNING_KEY = "test-risk-owner-signing-key-non-prod"
ACCOUNT_ID = "acc-live-01"
BUILD_REV = "rev-20261125"


def _signed_profile(**overrides) -> SignedLiveRiskProfile:
    base = SignedLiveRiskProfile(
        profile_version="live-risk-v1",
        account_id=ACCOUNT_ID,
        environment="LIVE",
        build_revision=BUILD_REV,
        account_mode=LiveAccountMode.SINGLE_ASSET_USDT,
        timezone_name="UTC",
        daily_reset_hour=0,
        rolling_window_hours=24,
        include_unrealized_loss=True,
        max_account_notional=Decimal("1000"),
        max_symbol_notional=Decimal("600"),
        max_order_notional=Decimal("400"),
        max_leverage=Decimal("3"),
        max_aggregate_exposure_notional=Decimal("1000"),
        max_daily_loss=Decimal("100"),
        max_rolling_loss=Decimal("150"),
        owner_id="owner-alice",
        approver_id="risk-bob",
        signed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=23),
        signature_digest="placeholder",
    )
    updated = replace(base, **overrides)
    if "signature_digest" not in overrides:
        sig = sign_live_risk_profile_digest(updated.compute_hash(), signing_key=SIGNING_KEY)
        updated = replace(updated, signature_digest=sig)
    return updated


def _equity(**overrides) -> VenueEquitySnapshot:
    base = VenueEquitySnapshot(
        account_id=ACCOUNT_ID,
        environment="LIVE",
        account_mode=LiveAccountMode.SINGLE_ASSET_USDT,
        wallet_balance=Decimal("5000"),
        unrealized_pnl=Decimal("0"),
        available_balance=Decimal("4500"),
        initial_margin_used=Decimal("500"),
        fee_and_funding_today=Decimal("0"),
        fee_and_funding_rolling=Decimal("0"),
        open_exposure_notional=Decimal("0"),
        symbol_open_exposure_notional=Decimal("0"),
        active_reservations_notional=Decimal("0"),
        symbol_reservations_notional=Decimal("0"),
        unknown_order_exposure_notional=Decimal("0"),
        symbol_unknown_exposure_notional=Decimal("0"),
        mark_price=Decimal("90000"),
        mark_observed_at=NOW - timedelta(seconds=2),
    )
    return replace(base, **overrides)


def _request(profile: SignedLiveRiskProfile, **overrides) -> LiveOrderRiskRequest:
    base = LiveOrderRiskRequest(
        symbol="BTCUSDT",
        order_notional=Decimal("200"),
        leverage=Decimal("2"),
        build_revision=BUILD_REV,
        expected_profile_hash=profile.compute_hash(),
        paused_or_killed=False,
    )
    return replace(base, **overrides)


def test_boundary_below_equal_and_above_caps() -> None:
    """Kiểm tra ranh giới dưới, đúng bằng và vượt trên ngưỡng cho order/leverage/symbol/account."""
    profile = _signed_profile()

    # Dưới ngưỡng -> duyệt
    dec_below = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(),
        request=_request(profile, order_notional=Decimal("399.99"), leverage=Decimal("2.5")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_below.approved is True
    assert dec_below.reason_code == LiveRiskReasonCode.APPROVED
    assert dec_below.profile_hash == profile.compute_hash()

    # Đúng bằng ngưỡng order & leverage -> duyệt
    dec_exact = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(
            open_exposure_notional=Decimal("600"), symbol_open_exposure_notional=Decimal("200")
        ),
        request=_request(profile, order_notional=Decimal("400"), leverage=Decimal("3")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_exact.approved is True

    # Vượt order cap
    dec_over_order = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(),
        request=_request(profile, order_notional=Decimal("400.01")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_over_order.approved is False
    assert dec_over_order.reason_code == LiveRiskReasonCode.ORDER_CAP_EXCEEDED

    # Vượt leverage cap
    dec_over_lev = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(),
        request=_request(profile, leverage=Decimal("3.01")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_over_lev.approved is False
    assert dec_over_lev.reason_code == LiveRiskReasonCode.LEVERAGE_CAP_EXCEEDED

    # Vượt symbol cap
    dec_over_sym = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(symbol_open_exposure_notional=Decimal("450")),
        request=_request(profile, order_notional=Decimal("200")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_over_sym.approved is False
    assert dec_over_sym.reason_code == LiveRiskReasonCode.SYMBOL_CAP_EXCEEDED

    # Vượt account cap
    dec_over_acc = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(open_exposure_notional=Decimal("850")),
        request=_request(profile, order_notional=Decimal("200")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_over_acc.approved is False
    assert dec_over_acc.reason_code == LiveRiskReasonCode.ACCOUNT_CAP_EXCEEDED


def test_timezone_rollover_and_fee_funding_loss_accounting() -> None:
    """Kiểm tra rollover múi giờ Asia/Ho_Chi_Minh và cộng dồn phí/funding vào cửa sổ lỗ."""
    vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
    profile = _signed_profile(
        timezone_name="Asia/Ho_Chi_Minh",
        daily_reset_hour=7,
        max_daily_loss=Decimal("100"),
        max_rolling_loss=Decimal("200"),
        signed_at=datetime(2026, 11, 24, 0, 0, tzinfo=UTC),
        expires_at=datetime(2026, 11, 27, 0, 0, tzinfo=UTC),
    )
    # Giao dịch lỗ -90 lúc 06:30 VN ngày 25/11 (trước mốc reset 07:00 VN)
    old_event = RealizedTradePnlEvent(
        trade_id="t-before-reset",
        closed_at=datetime(2026, 11, 25, 6, 30, tzinfo=vn_tz),
        net_pnl=Decimal("-90"),
    )
    # Lúc 06:45 VN: vẫn trong ngày cũ -> lỗ ngày = 90 + fee/funding 15 = 105 >= 100 -> bị chặn
    before_reset = datetime(2026, 11, 25, 6, 45, tzinfo=vn_tz)
    dec_before = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(
            fee_and_funding_today=Decimal("-15"),
            fee_and_funding_rolling=Decimal("-15"),
            mark_observed_at=before_reset - timedelta(seconds=1),
        ),
        request=_request(profile),
        realized_events=[old_event],
        as_of=before_reset,
        signing_key=SIGNING_KEY,
    )
    assert dec_before.approved is False
    assert dec_before.reason_code == LiveRiskReasonCode.DAILY_LOSS_EXCEEDED
    assert dec_before.effective_daily_loss == Decimal("105")

    # Lúc 07:15 VN: đã qua rollover 07:00 VN -> lỗ ngày chỉ còn fee mới (-5), rolling vẫn gồm -90
    after_reset = datetime(2026, 11, 25, 7, 15, tzinfo=vn_tz)
    dec_after = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(
            fee_and_funding_today=Decimal("-5"),
            fee_and_funding_rolling=Decimal("-15"),
            mark_observed_at=after_reset - timedelta(seconds=1),
        ),
        request=_request(profile),
        realized_events=[old_event],
        as_of=after_reset,
        signing_key=SIGNING_KEY,
    )
    assert dec_after.approved is True
    assert dec_after.effective_daily_loss == Decimal("5")
    assert dec_after.effective_rolling_loss == Decimal("105")


def test_missing_or_stale_mark_and_unknown_order_capacity() -> None:
    """Giá mark thiếu/cũ phải fail-closed; lệnh UNKNOWN không giải phóng capacity."""
    profile = _signed_profile()

    # Thiếu giá mark
    dec_no_mark = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(mark_price=None, mark_observed_at=None),
        request=_request(profile),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_no_mark.approved is False
    assert dec_no_mark.reason_code == LiveRiskReasonCode.MARK_MISSING

    # Giá mark cũ quá 10s
    dec_stale_mark = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(mark_observed_at=NOW - timedelta(seconds=15)),
        request=_request(profile),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_stale_mark.approved is False
    assert dec_stale_mark.reason_code == LiveRiskReasonCode.MARK_STALE

    # UNKNOWN order giữ 900 notional -> lệnh mới 200 vượt cap 1000
    dec_unknown = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(unknown_order_exposure_notional=Decimal("900")),
        request=_request(profile, order_notional=Decimal("200")),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_unknown.approved is False
    assert dec_unknown.reason_code == LiveRiskReasonCode.ACCOUNT_CAP_EXCEEDED
    assert dec_unknown.projected_account_exposure == Decimal("1100")


def test_profile_signature_hash_expiry_scope_and_maker_checker() -> None:
    """Chặn profile sai hash, hết hạn, sai chữ ký, sai scope hoặc tự duyệt."""
    profile = _signed_profile()

    # Sai hash pin
    dec_hash = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(),
        request=_request(profile, expected_profile_hash="0" * 64),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_hash.approved is False
    assert dec_hash.reason_code == LiveRiskReasonCode.PROFILE_HASH_MISMATCH

    # Tự duyệt (owner == approver)
    self_approved = _signed_profile(owner_id="alice", approver_id="alice")
    dec_mc = evaluate_live_risk_limits(
        profile=self_approved,
        equity=_equity(),
        request=_request(self_approved),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_mc.approved is False
    assert dec_mc.reason_code == LiveRiskReasonCode.MAKER_CHECKER_VIOLATION

    # Chữ ký sai
    bad_sig = _signed_profile(signature_digest="forged-signature")
    dec_sig = evaluate_live_risk_limits(
        profile=bad_sig,
        equity=_equity(),
        request=_request(bad_sig),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_sig.approved is False
    assert dec_sig.reason_code == LiveRiskReasonCode.PROFILE_UNSIGNED

    # Hết hạn
    expired = _signed_profile(expires_at=NOW - timedelta(seconds=1))
    dec_exp = evaluate_live_risk_limits(
        profile=expired,
        equity=_equity(),
        request=_request(expired),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_exp.approved is False
    assert dec_exp.reason_code == LiveRiskReasonCode.PROFILE_EXPIRED

    # Pause tức thời
    dec_pause = evaluate_live_risk_limits(
        profile=profile,
        equity=_equity(),
        request=_request(profile, paused_or_killed=True),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_pause.approved is False
    assert dec_pause.reason_code == LiveRiskReasonCode.PAUSED_OR_KILLED


def test_cap_change_governance_tighten_immediate_loosen_requires_maker_checker() -> None:
    """Giảm cap hiệu lực ngay không cần approver; tăng cap cần maker-checker."""
    current = _signed_profile(max_account_notional=Decimal("1000"))
    tightened = _signed_profile(max_account_notional=Decimal("500"))
    loosened = _signed_profile(max_account_notional=Decimal("2000"))

    ok_tighten, _ = validate_live_risk_cap_change(
        current=current, proposed=tightened, requester_id="op-1", approver_id=None
    )
    assert ok_tighten is True

    ok_loosen_no_approver, _ = validate_live_risk_cap_change(
        current=current, proposed=loosened, requester_id="op-1", approver_id=None
    )
    assert ok_loosen_no_approver is False

    ok_loosen_same_actor, _ = validate_live_risk_cap_change(
        current=current, proposed=loosened, requester_id="op-1", approver_id="op-1"
    )
    assert ok_loosen_same_actor is False

    ok_loosen_mc, _ = validate_live_risk_cap_change(
        current=current, proposed=loosened, requester_id="op-1", approver_id="risk-2"
    )
    assert ok_loosen_mc is True


def test_profile_rejects_non_positive_limits() -> None:
    """Không cho phép khởi tạo profile với ngưỡng <= 0."""
    with pytest.raises(ValueError, match="max_order_notional"):
        _signed_profile(max_order_notional=Decimal("0"))
