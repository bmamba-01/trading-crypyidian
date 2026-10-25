"""Hợp đồng kiểm tra ngân sách rủi ro Multi-Bot, reservation và breach của MB04.2."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    PaperEquityBasis,
    RiskConversionObservation,
    mark_to_market_equity,
)
from trading_bot.domain.risk.workspace_budget import (
    AccountScopeInput,
    BudgetConflictError,
    BudgetDecisionResult,
    BudgetOperation,
    BudgetReasonCode,
    BudgetReservation,
    BudgetValidationError,
    PositionRisk,
    ReservationStatus,
    WorkspaceBudgetBook,
    WorkspaceBudgetEngine,
    WorkspacePolicy,
)

D = Decimal
NOW = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
MAX_AGE = timedelta(seconds=60)
CAPITAL_10K = Decimal("10000")
PNL_ZERO = Decimal("0")
DEFAULT_FX_RATE = Decimal("60000")
DEFAULT_ROOT_FRACTION = Decimal("0.20")
DEFAULT_ACCOUNT_FRACTION = Decimal("0.10")


def create_basis(
    workspace_id: str = "ws-1",
    bot_id: str = "bot-1",
    account_id: str = "acc-1",
    asset: str = "USDT",
    capital: Decimal = CAPITAL_10K,
    pnl: Decimal = PNL_ZERO,
    as_of: datetime = NOW,
    captured_at: datetime = NOW,
) -> PaperEquityBasis:
    """Tạo basis PAPER hợp lệ cho test."""
    return PaperEquityBasis(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        asset=asset,
        as_of=as_of,
        captured_at=captured_at,
        available_at=captured_at,
        source="paper-ledger",
        source_version="v1",
        max_mark_age=MAX_AGE,
        max_equity_age=MAX_AGE,
        initial_capital_before_realized_pnl=capital,
        cumulative_realized_pnl=pnl,
    )


def create_snapshot(
    workspace_id: str = "ws-1",
    bot_id: str = "bot-1",
    account_id: str = "acc-1",
    asset: str = "USDT",
    capital: Decimal = CAPITAL_10K,
    pnl: Decimal = PNL_ZERO,
    as_of: datetime = NOW,
    captured_at: datetime = NOW,
    max_equity_age: timedelta = MAX_AGE,
    known: bool = True,
    unknown_reason: str | None = None,
) -> AccountEquitySnapshot:
    """Tạo AccountEquitySnapshot hợp lệ qua mark_to_market_equity."""
    b = create_basis(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        asset=asset,
        capital=capital,
        pnl=pnl,
        as_of=as_of,
        captured_at=captured_at,
    )
    if max_equity_age != MAX_AGE:
        b = replace(b, max_equity_age=max_equity_age)
    if not known:
        b = replace(b, known=False, unknown_reason=unknown_reason or "SNAPSHOT_UNKNOWN")
        snap = mark_to_market_equity(replace(b, known=True, unknown_reason=None), (), {}, as_of)
        return replace(snap, known=False, unknown_reason=unknown_reason or "SNAPSHOT_UNKNOWN")
    return mark_to_market_equity(b, (), {}, as_of)


def create_conversion(
    workspace_id: str = "ws-1",
    base_asset: str = "BTC",
    rate: Decimal = DEFAULT_FX_RATE,
    as_of: datetime = NOW,
    approved: bool = True,
    quality: str = "ACCEPTED",
    known: bool = True,
) -> RiskConversionObservation:
    """Tạo RiskConversionObservation hợp lệ sang USDT."""
    return RiskConversionObservation(
        workspace_id=workspace_id,
        base_asset=base_asset,
        quote_asset="USDT",
        rate=rate,
        as_of=as_of,
        available_at=as_of,
        source_ref="binance-public-ticker",
        source_version="v1",
        source_hash="hash-fx",
        policy_version_id="fx-policy-v1",
        route="DIRECT",
        approved=approved,
        quality=quality,
        known=known,
    )


def create_policy(
    workspace_id: str = "ws-1",
    root_fraction: Decimal = DEFAULT_ROOT_FRACTION,
    max_age: timedelta = MAX_AGE,
) -> WorkspacePolicy:
    """Tạo WorkspacePolicy chuẩn."""
    return WorkspacePolicy(
        workspace_id=workspace_id,
        root_workspace_max_open_risk_fraction=root_fraction,
        root_settings_version_id="root-v1",
        root_settings_hash="a" * 64,
        max_equity_age=max_age,
        max_conversion_age=max_age,
    )


def create_account(
    workspace_id: str = "ws-1",
    bot_id: str = "bot-1",
    account_id: str = "acc-1",
    enabled: bool = True,
    account_fraction: Decimal = DEFAULT_ACCOUNT_FRACTION,
    bot_fraction: Decimal | None = None,
    capital: Decimal = CAPITAL_10K,
    snapshot: AccountEquitySnapshot | None = None,
    conversion: RiskConversionObservation | None = None,
) -> AccountScopeInput:
    """Tạo AccountScopeInput có snapshot vốn hợp lệ."""
    snap = snapshot or create_snapshot(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        capital=capital,
    )
    return AccountScopeInput(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        enabled=enabled,
        account_max_open_risk_fraction=account_fraction,
        bot_max_open_risk_fraction=bot_fraction,
        snapshot=snap,
        conversion=conversion,
        risk_profile_version_id="rp-v1",
        bot_config_version_id="bc-v1",
        bot_config_hash="b" * 64,
    )


# ==============================================================================
# 1. KIỂM TRA CAP TỪNG SCOPE VÀ BOUNDARY
# ==============================================================================


def test_scope_cap_calculation_and_effective_limit() -> None:
    """Kiểm tra cap của workspace, account, bot và áp dụng giới hạn nhỏ nhất."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.20"))  # Workspace cap: 20%
    # 2 accounts: acc-1 (10000 USDT, 10% account cap = 1000 USDT)
    #             acc-2 (10000 USDT, 15% account cap = 1500 USDT)
    # Total workspace equity = 20000 USDT -> workspace cap = 4000 USDT
    acc1 = create_account(
        bot_id="bot-1", account_id="acc-1", capital=D("10000"), account_fraction=D("0.10")
    )
    acc2 = create_account(
        bot_id="bot-2", account_id="acc-2", capital=D("10000"), account_fraction=D("0.15")
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc1, acc2),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("500"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.APPROVED
    assert decision.approved is True
    assert decision.reason_code == BudgetReasonCode.APPROVED
    assert decision.workspace_equity_usdt == D("20000")
    assert decision.workspace_budget.cap_usdt == D("4000")  # 20% of 20000
    assert decision.account_budget.cap_usdt == D("1000")  # 10% of 10000
    assert decision.bot_budget.cap_usdt == D("1000")  # Không có bot overlay -> bằng account cap


def test_bot_overlay_tightens_cap() -> None:
    """Bot overlay có thể siết nhưng không thể nới lỏng cap của account."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.20"))
    # Account cap 10% = 1000 USDT, bot overlay 5% = 500 USDT
    acc = create_account(
        bot_id="bot-1",
        account_id="acc-1",
        capital=D("10000"),
        account_fraction=D("0.10"),
        bot_fraction=D("0.05"),
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("600"),
        as_of=NOW,
    )

    # 600 USDT vượt bot cap 500 USDT dù còn nằm dưới account cap 1000 USDT
    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.approved is False
    assert decision.reason_code == BudgetReasonCode.BOT_CAP_EXCEEDED
    assert decision.bot_budget.cap_usdt == D("500")
    assert decision.account_budget.cap_usdt == D("1000")


def test_smallest_remaining_limit_applied_workspace() -> None:
    """Giới hạn nhỏ nhất được áp dụng: khi workspace headroom nhỏ hơn account."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.05"))  # Workspace cap: 5% of 10000 = 500 USDT
    acc = create_account(
        bot_id="bot-1",
        account_id="acc-1",
        capital=D("10000"),
        account_fraction=D("0.10"),  # Account cap: 10% = 1000 USDT
    )

    # Request 600 USDT: thỏa account cap (1000) nhưng vượt workspace cap (500)
    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("600"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.WORKSPACE_CAP_EXCEEDED


def test_exact_boundary_permitted_and_exceeded_rejected() -> None:
    """Boundary chính xác bằng cap được phép, chỉ cần vượt 1 quantum là bị từ chối."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.10"))  # Cap: 1000 USDT
    acc = create_account(capital=D("10000"), account_fraction=D("0.10"))

    # Đang có rủi ro mở 600 USDT -> còn lại đúng 400 USDT
    pos = PositionRisk(
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        position_id="pos-1",
        symbol="BTCUSDT",
        worst_case_loss_usdt=D("600"),
    )

    # 1. Yêu cầu đúng 400 USDT -> Chấp thuận chính xác tại boundary
    decision_exact = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(pos,),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("400"),
        as_of=NOW,
    )
    assert decision_exact.result == BudgetDecisionResult.APPROVED
    assert decision_exact.approved is True

    # 2. Yêu cầu 400.00000001 USDT -> Vượt 1 quantum -> Từ chối
    decision_over = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(pos,),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("400.00000001"),
        as_of=NOW,
    )
    assert decision_over.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision_over.approved is False
    assert decision_over.reason_code == BudgetReasonCode.ACCOUNT_CAP_EXCEEDED


# ==============================================================================
# 2. TỔNG HỢP RỦI RO MỞ VÀ TRẠNG THÁI RESERVATION
# ==============================================================================


def test_open_risk_includes_worst_case_and_active_reservations() -> None:
    """open_risk = Σ worst_case_loss + Σ RESERVED_or_UNKNOWN."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.50"))  # 5000 USDT cap
    acc = create_account(capital=D("10000"), account_fraction=D("0.50"))

    # Vị thế mở 300 USDT
    pos = PositionRisk("ws-1", "bot-1", "acc-1", "p1", "BTCUSDT", D("300"))

    # Reservation RESERVED còn hạn: 200 USDT
    r_reserved = BudgetReservation(
        reservation_id="res-1",
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        idempotency_key="key-1",
        reserved_risk_usdt=D("200"),
        status=ReservationStatus.RESERVED,
        expires_at=NOW + timedelta(minutes=5),
    )

    # Reservation UNKNOWN: 150 USDT (tiếp tục giữ ngân sách tới khi reconcile)
    r_unknown = BudgetReservation(
        reservation_id="res-2",
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        idempotency_key="key-2",
        reserved_risk_usdt=D("150"),
        status=ReservationStatus.UNKNOWN,
        expires_at=NOW - timedelta(minutes=1),  # Kể cả đã quá expires_at vẫn giữ
    )

    # Reservation RELEASED: 500 USDT (không được giữ ngân sách)
    r_released = BudgetReservation(
        reservation_id="res-3",
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        idempotency_key="key-3",
        reserved_risk_usdt=D("500"),
        status=ReservationStatus.RELEASED,
        expires_at=NOW + timedelta(minutes=5),
    )

    # Reservation TRANSFERRED: 400 USDT (đã chuyển thành vị thế mở, không double-count)
    r_transferred = BudgetReservation(
        reservation_id="res-4",
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        idempotency_key="key-4",
        reserved_risk_usdt=D("400"),
        status=ReservationStatus.TRANSFERRED,
        expires_at=NOW + timedelta(minutes=5),
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(pos,),
        reservations=(r_reserved, r_unknown, r_released, r_transferred),
        target_bot_id="bot-1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
    )

    # Total open risk = 300 (pos) + 200 (reserved) + 150 (unknown) = 650 USDT
    # RELEASED (500) và TRANSFERRED (400) bị bỏ qua
    assert decision.account_budget.open_risk_usdt == D("300")
    assert decision.account_budget.pending_risk_usdt == D("350")  # 200 + 150
    assert decision.account_budget.remaining_risk_usdt == D("4350")  # 5000 - 650


def test_expired_reserved_reservation_does_not_hold_budget() -> None:
    """Reservation trạng thái RESERVED nếu đã hết hạn tại as_of thì không giữ budget."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.20"))
    acc = create_account(capital=D("10000"), account_fraction=D("0.20"))

    r_expired = BudgetReservation(
        reservation_id="res-exp",
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        idempotency_key="key-exp",
        reserved_risk_usdt=D("500"),
        status=ReservationStatus.RESERVED,
        expires_at=NOW - timedelta(seconds=1),  # Đã hết hạn trước as_of
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(r_expired,),
        target_bot_id="bot-1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
    )

    assert decision.account_budget.pending_risk_usdt == D("0")
    assert decision.account_budget.remaining_risk_usdt == D("2000")


# ==============================================================================
# 3. MẪU SỐ VỐN & ACCOUNT/BOT DISABLED
# ==============================================================================


def test_disabled_account_excluded_from_equity_denominator() -> None:
    """Account/bot disabled không được tính vào mẫu số workspace equity."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.10"))
    acc_enabled = create_account(bot_id="b1", account_id="a1", enabled=True, capital=D("10000"))
    acc_disabled = create_account(bot_id="b2", account_id="a2", enabled=False, capital=D("5000"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc_enabled, acc_disabled),
        positions=(),
        reservations=(),
        target_bot_id="b1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
    )

    # Mẫu số chỉ tính acc_enabled (10000 USDT), loại trừ acc_disabled (5000 USDT)
    assert decision.workspace_equity_usdt == D("10000")
    assert decision.workspace_budget.cap_usdt == D("1000")  # 10% của 10000


def test_disabled_account_open_risk_and_reservations_still_counted() -> None:
    """Vị thế mở và reservation của bot/account disabled VẪN tính vào tổng rủi ro workspace."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.10"))  # Cap: 1000 USDT (từ b1)
    acc1 = create_account(bot_id="b1", account_id="a1", enabled=True, capital=D("10000"))
    acc2 = create_account(bot_id="b2", account_id="a2", enabled=False, capital=D("5000"))

    # Bot disabled b2 đang có vị thế mở 700 USDT
    pos_disabled = PositionRisk("ws-1", "b2", "a2", "p-dis", "ETHUSDT", D("700"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc1, acc2),
        positions=(pos_disabled,),
        reservations=(),
        target_bot_id="b1",
        requested_risk_usdt=D("400"),
        as_of=NOW,
    )

    # Workspace exposure hiện tại = 700 USDT (từ b2). Cap workspace = 1000 USDT.
    # Còn lại chỉ 300 USDT -> yêu cầu 400 USDT cho b1 bị từ chối!
    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.WORKSPACE_CAP_EXCEEDED
    assert decision.workspace_budget.open_risk_usdt == D("700")
    assert decision.workspace_budget.remaining_risk_usdt == D("300")


def test_disabled_bot_cannot_open_new_entry() -> None:
    """Bot bị disabled từ chối mở entry mới với reason code BOT_DISABLED."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.20"))
    acc = create_account(bot_id="b-off", account_id="a-off", enabled=False, capital=D("10000"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="b-off",
        requested_risk_usdt=D("100"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.BOT_DISABLED


# ==============================================================================
# 4. FAIL-CLOSED KHI THIẾU/STALE/FUTURE DỮ LIỆU & BẢO VỆ EXIT/MONITORING
# ==============================================================================


@pytest.mark.parametrize(
    ("corrupt_action", "expected_code"),
    [
        ("MISSING_SNAPSHOT", BudgetReasonCode.DENOMINATOR_UNUSABLE),
        ("UNKNOWN_SNAPSHOT", BudgetReasonCode.EQUITY_UNKNOWN),
        ("STALE_SNAPSHOT", BudgetReasonCode.EQUITY_STALE),
        ("FUTURE_SNAPSHOT", BudgetReasonCode.OBSERVATION_FUTURE),
        ("AS_OF_MISMATCH", BudgetReasonCode.AS_OF_MISMATCH),
        ("MISSING_CONVERSION", BudgetReasonCode.CONVERSION_UNKNOWN),
        ("STALE_CONVERSION", BudgetReasonCode.CONVERSION_STALE),
    ],
)
def test_fail_closed_on_unreliable_equity_or_fx(
    corrupt_action: str, expected_code: BudgetReasonCode
) -> None:
    """Mẫu số vốn fail-closed khi bất kỳ enabled account nào bị thiếu/stale/future/unknown."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy()

    if corrupt_action == "MISSING_SNAPSHOT":
        acc = create_account(snapshot=None)
        # Bắt buộc None bằng cách gán thẳng
        acc = replace(acc, snapshot=None)
    elif corrupt_action == "UNKNOWN_SNAPSHOT":
        acc = create_account(snapshot=create_snapshot(known=False, unknown_reason="TEST_UNKNOWN"))
    elif corrupt_action == "STALE_SNAPSHOT":
        # Snapshot chụp quá max_equity_age của policy (60s)
        old_time = NOW - timedelta(seconds=120)
        stale_snap = create_snapshot(
            as_of=NOW, captured_at=old_time, max_equity_age=timedelta(hours=1)
        )
        acc = create_account(snapshot=stale_snap)
    elif corrupt_action == "FUTURE_SNAPSHOT":
        future_time = NOW + timedelta(seconds=30)
        future_snap = create_snapshot(as_of=future_time, captured_at=future_time)
        acc = create_account(snapshot=future_snap)
    elif corrupt_action == "AS_OF_MISMATCH":
        diff_time = NOW - timedelta(minutes=5)
        diff_snap = create_snapshot(as_of=diff_time, captured_at=diff_time)
        acc = create_account(snapshot=diff_snap)
    elif corrupt_action == "MISSING_CONVERSION":
        # Asset không phải USDT nhưng thiếu observation FX
        btc_snap = create_snapshot(asset="BTC", capital=D("2"))
        acc = create_account(snapshot=btc_snap, conversion=None)
    elif corrupt_action == "STALE_CONVERSION":
        btc_snap = create_snapshot(asset="BTC", capital=D("2"))
        old_fx = create_conversion(as_of=NOW - timedelta(seconds=120))
        acc = create_account(snapshot=btc_snap, conversion=old_fx)

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.approved is False
    assert decision.reason_code == expected_code


def test_protective_management_not_denied_on_data_failure_or_cap_exceeded() -> None:
    """Quyết định quản trị bảo vệ (PROTECTIVE_EXIT/MONITORING) KHÔNG bị từ chối dù dữ liệu lỗi."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.01"))  # Cap rất nhỏ

    # Dữ liệu snapshot hoàn toàn unknown
    acc = create_account(
        snapshot=create_snapshot(known=False, unknown_reason="TEST_DATA_BROKEN"),
    )

    # 1. Protective Exit phải được duyệt
    exit_decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("0"),
        as_of=NOW,
        operation=BudgetOperation.PROTECTIVE_EXIT,
    )
    assert exit_decision.result == BudgetDecisionResult.APPROVED
    assert exit_decision.approved is True
    assert exit_decision.reason_code == BudgetReasonCode.APPROVED

    # 2. Protective Monitoring phải được duyệt
    monitor_decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("0"),
        as_of=NOW,
        operation=BudgetOperation.PROTECTIVE_MONITORING,
    )
    assert monitor_decision.result == BudgetDecisionResult.APPROVED
    assert monitor_decision.approved is True


# ==============================================================================
# 5. CAP TIGHTENING BREACH VÀ PHỤC HỒI
# ==============================================================================


def test_cap_tightening_creates_breach_and_blocks_entry_without_flattening() -> None:
    """Siết cap dưới exposure tạo breach, chặn entry mới, không tự flatten vị thế."""
    engine = WorkspaceBudgetEngine()
    acc = create_account(capital=D("10000"), account_fraction=D("0.20"))

    # Đang có vị thế mở 1500 USDT
    pos = PositionRisk("ws-1", "bot-1", "acc-1", "pos-1", "BTCUSDT", D("1500"))

    # Admin siết root workspace fraction xuống 0.10 (cap mới: 1000 USDT < 1500 USDT)
    policy_tightened = create_policy(root_fraction=D("0.10"))

    decision = engine.evaluate(
        policy=policy_tightened,
        accounts=(acc,),
        positions=(pos,),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("50"),
        as_of=NOW,
    )

    # Chặn entry mới do breach siết cap
    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING
    assert len(decision.breaches) > 0
    breach = decision.breaches[0]
    assert breach.scope_name == "WORKSPACE"
    assert breach.cap_usdt == D("1000")
    assert breach.current_exposure_usdt == D("1500")
    assert breach.excess_usdt == D("500")

    # Protective exit trên vị thế đang breach vẫn chạy bình thường (không bị flatten cưỡng bức)
    prot_decision = engine.evaluate(
        policy=policy_tightened,
        accounts=(acc,),
        positions=(pos,),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("0"),
        as_of=NOW,
        operation=BudgetOperation.PROTECTIVE_EXIT,
    )
    assert prot_decision.result == BudgetDecisionResult.APPROVED


def test_breach_recovery_when_exposure_drops_below_cap() -> None:
    """Entry được phục hồi khi vị thế giảm làm exposure xuống dưới cap đã siết."""
    engine = WorkspaceBudgetEngine()
    acc = create_account(capital=D("10000"), account_fraction=D("0.20"))
    policy_tightened = create_policy(root_fraction=D("0.10"))  # Cap: 1000 USDT

    # Vị thế giảm từ 1500 USDT xuống còn 600 USDT (dưới cap 1000 USDT)
    pos_reduced = PositionRisk("ws-1", "bot-1", "acc-1", "pos-1", "BTCUSDT", D("600"))

    decision = engine.evaluate(
        policy=policy_tightened,
        accounts=(acc,),
        positions=(pos_reduced,),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("200"),
        as_of=NOW,
    )

    # Phục hồi mở entry bình thường
    assert decision.result == BudgetDecisionResult.APPROVED
    assert decision.approved is True
    assert decision.reason_code == BudgetReasonCode.APPROVED
    assert len(decision.breaches) == 0


# ==============================================================================
# 6. IDEMPOTENCY VÀ CONFLICT
# ==============================================================================


def test_idempotency_same_key_and_payload_returns_cached_result() -> None:
    """Cùng idempotency key và payload trả về kết quả cũ đã ghi."""
    book = WorkspaceBudgetBook()
    engine = WorkspaceBudgetEngine()
    policy = create_policy()
    acc = create_account()

    decision_first = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
        idempotency_key="req-123",
        plan_hash="plan-hash-abc",
    )
    book.record(decision_first)

    # Gọi lại với cùng key và cùng plan_hash
    cached = book.get_or_evaluate(
        key=("ws-1", "bot-1", "acc-1", BudgetOperation.NEW_ENTRY, "req-123"),
        payload_hash="plan-hash-abc",
    )

    assert cached is not None
    assert cached.decision_id == decision_first.decision_id
    assert cached.approved == decision_first.approved


def test_idempotency_same_key_different_payload_raises_conflict() -> None:
    """Cùng idempotency key nhưng payload khác phải báo CONFLICT."""
    book = WorkspaceBudgetBook()
    engine = WorkspaceBudgetEngine()
    policy = create_policy()
    acc = create_account()

    decision_first = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
        idempotency_key="req-123",
        plan_hash="plan-hash-ORIGINAL",
    )
    book.record(decision_first)

    # Thử với cùng key nhưng payload khác
    with pytest.raises(BudgetConflictError) as err:
        book.get_or_evaluate(
            key=("ws-1", "bot-1", "acc-1", BudgetOperation.NEW_ENTRY, "req-123"),
            payload_hash="plan-hash-TAMPERED",
        )
    assert "CONFLICT" in str(err.value).upper() or "TRÙNG KHÓA" in str(err.value).upper()


def test_idempotency_different_bot_scope_does_not_conflict() -> None:
    """Khóa idempotency có bot scope khác nhau không gây xung đột."""
    book = WorkspaceBudgetBook()
    engine = WorkspaceBudgetEngine()
    policy = create_policy()
    acc1 = create_account(bot_id="bot-A", account_id="acc-A")
    acc2 = create_account(bot_id="bot-B", account_id="acc-B")

    d1 = engine.evaluate(
        policy=policy,
        accounts=(acc1, acc2),
        positions=(),
        reservations=(),
        target_bot_id="bot-A",
        requested_risk_usdt=D("100"),
        as_of=NOW,
        idempotency_key="shared-key",
        plan_hash="hash-1",
    )
    book.record(d1)

    d2 = engine.evaluate(
        policy=policy,
        accounts=(acc1, acc2),
        positions=(),
        reservations=(),
        target_bot_id="bot-B",
        requested_risk_usdt=D("100"),
        as_of=NOW,
        idempotency_key="shared-key",
        plan_hash="hash-2",
    )
    book.record(d2)

    assert d1.bot_id == "bot-A"
    assert d2.bot_id == "bot-B"
    assert d1.decision_id != d2.decision_id


# ==============================================================================
# 7. KIỂM TRA DECIMAL VÀ VALIDATION
# ==============================================================================


def test_validation_rejects_float_and_invalid_types() -> None:
    """Domain ngân sách thuần bắt buộc Decimal và dữ liệu hợp lệ."""
    with pytest.raises(BudgetValidationError) as err:
        WorkspacePolicy(
            workspace_id="ws-1",
            root_workspace_max_open_risk_fraction=0.20,  # type: ignore[arg-type]
            root_settings_version_id="v1",
            root_settings_hash="a" * 64,
            max_equity_age=MAX_AGE,
            max_conversion_age=MAX_AGE,
        )
    assert err.value.reason_code == BudgetReasonCode.DECIMAL_REQUIRED


def test_validation_rejects_fractions_outside_unit_interval() -> None:
    """Tỷ lệ rủi ro phải nằm trong khoảng (0, 1]."""
    with pytest.raises(BudgetValidationError) as err:
        WorkspacePolicy(
            workspace_id="ws-1",
            root_workspace_max_open_risk_fraction=D("1.5"),
            root_settings_version_id="v1",
            root_settings_hash="a" * 64,
            max_equity_age=MAX_AGE,
            max_conversion_age=MAX_AGE,
        )
    assert err.value.reason_code == BudgetReasonCode.FRACTION_INVALID


def test_bot_overlay_greater_than_account_fraction_rejected() -> None:
    """Bot overlay không được lớn hơn account cap fraction."""
    with pytest.raises(BudgetValidationError) as err:
        create_account(
            account_fraction=D("0.10"),
            bot_fraction=D("0.15"),  # 15% > 10% -> Reject
        )
    assert err.value.reason_code == BudgetReasonCode.FRACTION_INVALID


def test_disabled_account_with_broken_snapshot_does_not_poison_workspace_equity() -> None:
    """Account disabled có snapshot lỗi không làm hỏng mẫu số vốn của các bot enabled."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.10"))
    acc_enabled = create_account(bot_id="b1", account_id="a1", enabled=True, capital=D("10000"))
    # acc_disabled có snapshot không known
    acc_disabled = create_account(
        bot_id="b2",
        account_id="a2",
        enabled=False,
        snapshot=create_snapshot(known=False, unknown_reason="BROKEN_HISTORY"),
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc_enabled, acc_disabled),
        positions=(),
        reservations=(),
        target_bot_id="b1",
        requested_risk_usdt=D("100"),
        as_of=NOW,
    )

    # Vẫn tính được workspace equity từ b1 và phê duyệt bình thường
    assert decision.result == BudgetDecisionResult.APPROVED
    assert decision.workspace_equity_usdt == D("10000")


def test_non_usdt_account_converted_to_usdt_in_workspace_equity() -> None:
    """Tài khoản có tài sản gốc BTC được quy đổi chuẩn xác sang USDT theo tỷ giá quan sát."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.10"))

    # acc-btc có 2 BTC, tỷ giá 60000 USDT/BTC -> 120,000 USDT
    btc_snap = create_snapshot(asset="BTC", capital=D("2"))
    fx = create_conversion(base_asset="BTC", rate=D("60000"))
    acc_btc = create_account(bot_id="b-btc", account_id="a-btc", snapshot=btc_snap, conversion=fx)

    # acc-usdt có 30,000 USDT
    acc_usdt = create_account(bot_id="b-usdt", account_id="a-usdt", capital=D("30000"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc_btc, acc_usdt),
        positions=(),
        reservations=(),
        target_bot_id="b-btc",
        requested_risk_usdt=D("500"),
        as_of=NOW,
    )

    # Tổng vốn workspace = 120,000 (BTC) + 30,000 (USDT) = 150,000 USDT
    assert decision.result == BudgetDecisionResult.APPROVED
    assert decision.workspace_equity_usdt == D("150000")
    assert decision.account_equity_usdt == D("120000")
    assert decision.workspace_budget.cap_usdt == D("15000")  # 10% của 150,000


def test_account_level_breach_when_account_cap_tightened() -> None:
    """Breach chỉ xảy ra ở cấp account khi chỉ có account cap bị siết dưới exposure."""
    engine = WorkspaceBudgetEngine()
    # Workspace cap lớn (50% của 20000 = 10000 USDT)
    policy = create_policy(root_fraction=D("0.50"))
    acc1 = create_account(
        bot_id="b1", account_id="a1", capital=D("10000"), account_fraction=D("0.05")
    )  # 5% = 500 USDT
    acc2 = create_account(
        bot_id="b2", account_id="a2", capital=D("10000"), account_fraction=D("0.50")
    )

    # b1 có vị thế mở 700 USDT (> account cap 500 USDT, nhưng < workspace cap 10000 USDT)
    pos1 = PositionRisk("ws-1", "b1", "a1", "pos-1", "BTCUSDT", D("700"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc1, acc2),
        positions=(pos1,),
        reservations=(),
        target_bot_id="b1",
        requested_risk_usdt=D("50"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING
    assert len(decision.breaches) == 1
    assert decision.breaches[0].scope_name == "ACCOUNT"
    assert decision.breaches[0].account_id == "a1"
    assert decision.breaches[0].excess_usdt == D("200")  # 700 - 500


def test_bot_level_breach_when_bot_overlay_tightened() -> None:
    """Breach chỉ xảy ra ở cấp bot khi chỉ có bot overlay bị siết dưới exposure."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.50"))
    # Account cap 20% = 2000 USDT, bot overlay 5% = 500 USDT
    acc = create_account(
        bot_id="b1",
        account_id="a1",
        capital=D("10000"),
        account_fraction=D("0.20"),
        bot_fraction=D("0.05"),
    )

    # b1 có vị thế mở 600 USDT (> bot cap 500 USDT, nhưng < account cap 2000 USDT)
    pos = PositionRisk("ws-1", "b1", "a1", "pos-1", "BTCUSDT", D("600"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(pos,),
        reservations=(),
        target_bot_id="b1",
        requested_risk_usdt=D("50"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING
    assert len(decision.breaches) == 1
    assert decision.breaches[0].scope_name == "BOT"
    assert decision.breaches[0].bot_id == "b1"
    assert decision.breaches[0].excess_usdt == D("100")  # 600 - 500


def test_simultaneous_workspace_and_account_breaches() -> None:
    """Phát hiện đồng thời nhiều cấp breach (Workspace và Account)."""
    engine = WorkspaceBudgetEngine()
    # Workspace cap 5% = 500 USDT, Account cap 5% = 500 USDT
    policy = create_policy(root_fraction=D("0.05"))
    acc = create_account(
        bot_id="b1", account_id="a1", capital=D("10000"), account_fraction=D("0.05")
    )

    # Vị thế 800 USDT (vượt cả account cap 500 và workspace cap 500)
    pos = PositionRisk("ws-1", "b1", "a1", "pos-1", "BTCUSDT", D("800"))

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(pos,),
        reservations=(),
        target_bot_id="b1",
        requested_risk_usdt=D("50"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.DENY_NEW_ENTRY
    assert decision.reason_code == BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING
    assert len(decision.breaches) == 2
    scope_names = {b.scope_name for b in decision.breaches}
    assert scope_names == {"WORKSPACE", "ACCOUNT"}


def test_unknown_target_bot_raises_scope_mismatch() -> None:
    """Yêu cầu đánh giá cho bot không tồn tại trong danh sách accounts báo lỗi SCOPE_MISMATCH."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy()
    acc = create_account(bot_id="b1", account_id="a1")

    with pytest.raises(BudgetValidationError) as err:
        engine.evaluate(
            policy=policy,
            accounts=(acc,),
            positions=(),
            reservations=(),
            target_bot_id="ghost-bot",
            requested_risk_usdt=D("100"),
            as_of=NOW,
        )
    assert err.value.reason_code == BudgetReasonCode.SCOPE_MISMATCH


def test_zero_requested_risk_evaluation() -> None:
    """Yêu cầu rủi ro bằng 0 được duyệt hợp lệ nếu không có breach."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy()
    acc = create_account()

    decision = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(),
        target_bot_id="bot-1",
        requested_risk_usdt=D("0"),
        as_of=NOW,
    )

    assert decision.result == BudgetDecisionResult.APPROVED
    assert decision.approved is True


def test_validation_rejects_negative_amounts() -> None:
    """Từ chối số tiền hoặc rủi ro âm."""
    with pytest.raises(BudgetValidationError) as err:
        PositionRisk("ws-1", "b1", "a1", "p1", "BTCUSDT", D("-10"))
    assert err.value.reason_code == BudgetReasonCode.AMOUNT_INVALID

    with pytest.raises(BudgetValidationError) as err:
        BudgetReservation(
            reservation_id="res-1",
            workspace_id="ws-1",
            bot_id="b1",
            account_id="a1",
            idempotency_key="k1",
            reserved_risk_usdt=D("-5"),
            status=ReservationStatus.RESERVED,
            expires_at=NOW,
        )
    assert err.value.reason_code == BudgetReasonCode.AMOUNT_INVALID


def test_validation_rejects_naive_datetime() -> None:
    """Bắt buộc datetime có timezone UTC, từ chối datetime naive."""
    naive_dt = datetime(2026, 10, 25, 10, 0, 0, tzinfo=None)  # noqa: DTZ001

    with pytest.raises(BudgetValidationError) as err:
        BudgetReservation(
            reservation_id="res-1",
            workspace_id="ws-1",
            bot_id="b1",
            account_id="a1",
            idempotency_key="k1",
            reserved_risk_usdt=D("100"),
            status=ReservationStatus.RESERVED,
            expires_at=naive_dt,
        )
    assert err.value.reason_code == BudgetReasonCode.TIMESTAMP_INVALID

    engine = WorkspaceBudgetEngine()
    policy = create_policy()
    acc = create_account()
    with pytest.raises(BudgetValidationError) as err:
        engine.evaluate(
            policy=policy,
            accounts=(acc,),
            positions=(),
            reservations=(),
            target_bot_id="bot-1",
            requested_risk_usdt=D("100"),
            as_of=naive_dt,
        )
    assert err.value.reason_code == BudgetReasonCode.TIMESTAMP_INVALID


def test_reservation_transition_no_double_counting() -> None:
    """Quy trình chuyển đổi reservation sang vị thế mở không làm double-count rủi ro."""
    engine = WorkspaceBudgetEngine()
    policy = create_policy(root_fraction=D("0.20"))  # Cap 2000 USDT
    acc = create_account(capital=D("10000"), account_fraction=D("0.20"))

    # Bước 1: Reservation đang active (RESERVED) 500 USDT
    res_active = BudgetReservation(
        reservation_id="res-1",
        workspace_id="ws-1",
        bot_id="bot-1",
        account_id="acc-1",
        idempotency_key="key-1",
        reserved_risk_usdt=D("500"),
        status=ReservationStatus.RESERVED,
        expires_at=NOW + timedelta(minutes=5),
    )
    d1 = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(),
        reservations=(res_active,),
        target_bot_id="bot-1",
        requested_risk_usdt=D("0"),
        as_of=NOW,
    )
    assert d1.account_budget.open_risk_usdt == D("0")
    assert d1.account_budget.pending_risk_usdt == D("500")
    assert d1.account_budget.remaining_risk_usdt == D("1500")

    # Bước 2: Lệnh khớp -> Vị thế mở được ghi nhận (500 USDT)
    # và reservation chuyển thành TRANSFERRED
    pos_opened = PositionRisk("ws-1", "bot-1", "acc-1", "pos-1", "BTCUSDT", D("500"))
    res_transferred = replace(res_active, status=ReservationStatus.TRANSFERRED)

    d2 = engine.evaluate(
        policy=policy,
        accounts=(acc,),
        positions=(pos_opened,),
        reservations=(res_transferred,),
        target_bot_id="bot-1",
        requested_risk_usdt=D("0"),
        as_of=NOW,
    )
    # Tổng rủi ro vẫn là 500 USDT, không bị double-count thành 1000 USDT!
    assert d2.account_budget.open_risk_usdt == D("500")
    assert d2.account_budget.pending_risk_usdt == D("0")
    assert d2.account_budget.remaining_risk_usdt == D("1500")
