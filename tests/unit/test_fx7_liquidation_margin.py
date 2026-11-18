"""Kiểm thử đơn vị cho FX-7 (CRY-29): MMR theo bậc, giá thanh lý và biên an toàn stop."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.execution.paper import (
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.risk.liquidation import (
    BTCUSDT_MAINTENANCE_MARGIN_TIERS,
    DEFAULT_LIQUIDATION_SAFETY_BUFFER,
    DEFAULT_MAINTENANCE_MARGIN_TIERS,
    ETHUSDT_MAINTENANCE_MARGIN_TIERS,
    MaintenanceMarginTier,
    calculate_liquidation_price,
    calculate_maintenance_margin,
    check_stop_before_liquidation,
    default_tiers_for_symbol,
    select_maintenance_tier,
)
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    PlanRejected,
    RiskLimits,
    TradePlan,
    TradePlanner,
    calculate_plan_hash,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _settings(
    *,
    leverage: Decimal = Decimal("2"),
    max_leverage: Decimal = Decimal("50"),
    liquidation_safety_buffer: Decimal = DEFAULT_LIQUIDATION_SAFETY_BUFFER,
) -> PlannerSettings:
    return PlannerSettings(
        workspace_id="workspace-fx7",
        settings_version_id="settings-fx7-v1",
        risk_profile_version_id="risk-fx7-v1",
        published=True,
        active_release_ids=("h01-v1",),
        leverage=leverage,
        exchange=ExchangeRules(
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("100000"),
            max_quantity=Decimal("1000"),
        ),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.02"),
            max_aggregate_open_risk=Decimal("1000"),
            max_notional=Decimal("100000"),
            max_leverage=max_leverage,
            max_positions=3,
            max_symbol_notional=Decimal("50000"),
            max_concentration_fraction=Decimal("0.80"),
            max_daily_loss=Decimal("1000"),
            max_rolling_loss=Decimal("2000"),
            max_account_age=timedelta(seconds=30),
            max_data_age=timedelta(minutes=5),
            liquidation_safety_buffer=liquidation_safety_buffer,
        ),
        release_content_hashes=(("h01-v1", "a" * 64),),
    )


def _candidate(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    reference_price: Decimal = Decimal("100"),
    atr_value: float = 2.0,
    stop_atr_multiple: float = 2.0,
    take_profit_atr_multiple: float | None = 3.0,
) -> StrategyCandidate:
    return StrategyCandidate(
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash="a" * 64,
        settings_version_id="settings-fx7-v1",
        snapshot_id="snapshot-fx7-1",
        symbol=symbol,
        direction=direction,
        reference_price=reference_price,
        atr_value=atr_value,
        signal_at=NOW - timedelta(minutes=1),
        eligible_after=NOW - timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=10),
        recipe=ExitRecipe(
            "exit-fx7-v1",
            stop_atr_multiple,
            take_profit_atr_multiple=take_profit_atr_multiple,
        ),
        evidence_feature_ids=("feature-1",),
        evidence_bar_keys=(f"{symbol}:1h:bar-1",),
        macro_evidence_ids=(),
        geometry_version=None,
        reason_code="H01_ENTRY",
        detail_vi="Tín hiệu kiểm thử FX-7.",
    )


def _account(
    *,
    equity: Decimal = Decimal("10000"),
    available_margin: Decimal = Decimal("5000"),
) -> AccountSnapshot:
    return AccountSnapshot(
        workspace_id="workspace-fx7",
        account_id="paper-fx7",
        settings_version_id="settings-fx7-v1",
        captured_at=NOW - timedelta(seconds=1),
        equity=equity,
        available_margin=available_margin,
        is_known=True,
    )


def _zero_costs() -> CostModel:
    return CostModel(
        version_id="cost-zero-fx7",
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=Decimal("0"),
        exit_slippage_rate=Decimal("0"),
    )


def _bar(
    key: str,
    minute: int,
    *,
    symbol: str = "BTCUSDT",
    open_price: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> PaperBar:
    opened = NOW + timedelta(minutes=minute)
    return PaperBar(
        bar_key=key,
        symbol=symbol,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        available_at=opened + timedelta(minutes=1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def _gateway() -> PaperExecutionGateway:
    return PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fx7-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            fee_rate=Decimal("0"),
        )
    )


def test_mmr_brackets_are_continuous_at_tier_boundaries() -> None:
    """Tại mỗi mốc biên notional_floor, MM = Notional * MMR - cum phải liên tục với bậc trước."""
    for tiers in (
        BTCUSDT_MAINTENANCE_MARGIN_TIERS,
        ETHUSDT_MAINTENANCE_MARGIN_TIERS,
        DEFAULT_MAINTENANCE_MARGIN_TIERS,
    ):
        for prev_tier, curr_tier in zip(tiers, tiers[1:], strict=False):
            boundary = curr_tier.notional_floor
            assert prev_tier.notional_cap == boundary
            mm_prev = boundary * prev_tier.maint_margin_ratio - prev_tier.cum
            mm_curr = boundary * curr_tier.maint_margin_ratio - curr_tier.cum
            assert mm_prev == mm_curr


def test_select_tier_and_calculate_maintenance_margin_across_notional_tiers() -> None:
    tier_1 = select_maintenance_tier(Decimal("10000"), symbol="BTCUSDT")
    assert tier_1.bracket == 1
    assert tier_1.maint_margin_ratio == Decimal("0.004")
    assert tier_1.cum == Decimal("0")
    assert calculate_maintenance_margin(Decimal("10000"), symbol="BTCUSDT") == Decimal("40")

    tier_2 = select_maintenance_tier(Decimal("100000"), symbol="BTCUSDT")
    assert tier_2.bracket == 2
    assert tier_2.maint_margin_ratio == Decimal("0.005")
    assert tier_2.cum == Decimal("50")
    # MM = 100_000 * 0.005 - 50 = 450
    assert calculate_maintenance_margin(Decimal("100000"), symbol="BTCUSDT") == Decimal("450")

    # Symbol chưa có bảng riêng dùng bảng bảo thủ mặc định.
    alt_tier = select_maintenance_tier(Decimal("5000"), symbol="SOLUSDT")
    assert alt_tier == DEFAULT_MAINTENANCE_MARGIN_TIERS[0]
    assert default_tiers_for_symbol("ETHUSDT") == ETHUSDT_MAINTENANCE_MARGIN_TIERS


def test_calculate_liquidation_price_isolated_one_way_long_and_short() -> None:
    # BTCUSDT bậc 1 (notional = 1_000 <= 50_000): MMR = 0.004, cum = 0.
    # LONG: EP = 100, Q = 10, leverage = 10 -> WB = 100.
    # LP_long = (1000 - 100 - 0) / (10 * (1 - 0.004)) = 900 / 9.96
    lp_long = calculate_liquidation_price(
        side="LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        symbol="BTCUSDT",
    )
    expected_long = Decimal("900") / Decimal("9.96")
    assert lp_long == expected_long

    # SHORT: EP = 100, Q = 10, leverage = 10 -> WB = 100.
    # LP_short = (100 + 0 + 1000) / (10 * (1 + 0.004)) = 1100 / 10.04
    lp_short = calculate_liquidation_price(
        side="SHORT",
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        symbol="BTCUSDT",
    )
    expected_short = Decimal("1100") / Decimal("10.04")
    assert lp_short == expected_short

    # Đòn bẩy 1x cho LONG với WB = notional: không bị thanh lý ở mức giá dương (kẹp về 0).
    lp_unlevered_long = calculate_liquidation_price(
        side="LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        leverage=Decimal("1"),
        symbol="BTCUSDT",
    )
    assert lp_unlevered_long == Decimal("0")


def test_check_stop_before_liquidation_enforces_configurable_safety_buffer() -> None:
    # LONG: EP = 100, leverage = 10, BTCUSDT -> LP ~ 90.3614, khoảng cách E - LP ~ 9.6386.
    # Với buffer 20%: safe_stop_limit = LP + 0.20 * (100 - LP) ~ 92.289.
    safe_check = check_stop_before_liquidation(
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        safety_buffer=Decimal("0.20"),
        symbol="BTCUSDT",
    )
    assert safe_check.safe

    too_close = check_stop_before_liquidation(
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("91.5"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        safety_buffer=Decimal("0.20"),
        symbol="BTCUSDT",
    )
    assert not too_close.safe
    assert too_close.reason_code == "STOP_LIQUIDATION_UNSAFE"
    detail_lower = too_close.detail_vi.lower()
    assert "biên an toàn" in detail_lower or "thanh lý" in detail_lower

    beyond_liq = check_stop_before_liquidation(
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("89.0"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        safety_buffer=Decimal("0.20"),
        symbol="BTCUSDT",
    )
    assert not beyond_liq.safe
    assert beyond_liq.reason_code == "STOP_LIQUIDATION_UNSAFE"

    # SHORT: EP = 100, leverage = 10 -> LP ~ 109.5617, safe_stop_limit ~ 107.649.
    short_safe = check_stop_before_liquidation(
        side="SHORT",
        entry_price=Decimal("100"),
        stop_price=Decimal("106.0"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        safety_buffer=Decimal("0.20"),
        symbol="BTCUSDT",
    )
    assert short_safe.safe

    short_unsafe = check_stop_before_liquidation(
        side="SHORT",
        entry_price=Decimal("100"),
        stop_price=Decimal("108.5"),
        quantity=Decimal("10"),
        leverage=Decimal("10"),
        safety_buffer=Decimal("0.20"),
        symbol="BTCUSDT",
    )
    assert not short_unsafe.safe
    assert short_unsafe.reason_code == "STOP_LIQUIDATION_UNSAFE"


def test_trade_planner_rejects_plan_when_stop_violates_liquidation_safety_zone() -> None:
    planner = TradePlanner()
    # Đòn bẩy 25x trên BTCUSDT: LP_long ~ 96.385, safe_stop_limit (20% buffer) ~ 97.108.
    # Candidate có reference_price=100, atr=2.0, stop_atr_multiple=2.0 -> stop=96.0 < 96.385!
    high_leverage_settings = _settings(leverage=Decimal("25"), max_leverage=Decimal("50"))
    rejected_long = planner.plan(
        _candidate(direction="long", atr_value=2.0, stop_atr_multiple=2.0),
        high_leverage_settings,
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(rejected_long, PlanRejected)
    assert rejected_long.reason_code == "STOP_LIQUIDATION_UNSAFE"
    assert "thanh lý" in rejected_long.detail_vi.lower()

    # Cùng candidate ở chiều SHORT: stop=104.0 > LP_short (~103.585) -> cũng bị từ chối!
    rejected_short = planner.plan(
        _candidate(direction="short", atr_value=2.0, stop_atr_multiple=2.0),
        high_leverage_settings,
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(rejected_short, PlanRejected)
    assert rejected_short.reason_code == "STOP_LIQUIDATION_UNSAFE"

    # Khi giảm đòn bẩy xuống 5x (LP_long ~ 80.32, safe_stop_limit ~ 84.26): stop=96.0 an toàn!
    safe_settings = _settings(leverage=Decimal("5"), max_leverage=Decimal("50"))
    approved = planner.plan(
        _candidate(direction="long", atr_value=2.0, stop_atr_multiple=2.0),
        safe_settings,
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(approved, TradePlan)


def test_paper_gateway_triggers_liquidation_for_long_and_short_positions() -> None:
    gateway = _gateway()
    # Tạo plan hợp lệ ở 5x, sau đó mô phỏng vị thế có đòn bẩy 25x hoặc bị funding bào mòn ký quỹ
    # khiến giá thanh lý nằm trước stop_price.
    base_plan = TradePlanner().plan(
        _candidate(direction="long", atr_value=5.0, stop_atr_multiple=2.0),  # stop = 90.0
        _settings(leverage=Decimal("5")),
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(base_plan, TradePlan)
    risky_long_plan = replace(
        base_plan,
        leverage=Decimal("25"),
        initial_margin=base_plan.notional / Decimal("25"),
        plan_hash="",
    )
    risky_long_plan = replace(risky_long_plan, plan_hash=calculate_plan_hash(risky_long_plan))

    order = gateway.submit(risky_long_plan, "fx7-long-liq", submitted_at=NOW, eligible_after=NOW)
    gateway.apply_market_event(_bar("bar-entry", 1, open_price="100", high="101", low="99"))
    position = gateway.position(order.intent_id)
    assert position is not None

    liq_price = gateway.position_liquidation_price(position)
    assert liq_price is not None
    # Ở 25x trên BTCUSDT: liq_price ~ 96.3855 > stop_price (90.0)
    assert Decimal("96") < liq_price < Decimal("97")

    # Nến rơi xuống 95.0 (chưa chạm stop 90.0 nhưng đã thủng liq_price ~ 96.3855) -> LIQUIDATION!
    monitored = gateway.monitor_position(
        position,
        _bar("bar-crash", 2, open_price="99", high="99.5", low="95.0", close="95.5"),
    )
    assert monitored.exit_reason == "LIQUIDATION"
    assert monitored.position.is_closed
    assert monitored.position.exit_reason == "LIQUIDATION"
    assert monitored.exit_reference_price == liq_price

    # Kiểm tra chiều SHORT: entry=100, stop=110, leverage=25 -> liq_price ~ 103.5856 < stop (110)
    short_base = TradePlanner().plan(
        _candidate(direction="short", atr_value=5.0, stop_atr_multiple=2.0),  # stop = 110.0
        _settings(leverage=Decimal("5")),
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(short_base, TradePlan)
    risky_short_plan = replace(
        short_base,
        leverage=Decimal("25"),
        initial_margin=short_base.notional / Decimal("25"),
        plan_hash="",
    )
    risky_short_plan = replace(risky_short_plan, plan_hash=calculate_plan_hash(risky_short_plan))
    short_order = gateway.submit(
        risky_short_plan, "fx7-short-liq", submitted_at=NOW, eligible_after=NOW
    )
    gateway.apply_market_event(_bar("bar-short-entry", 3, open_price="100", high="101", low="99"))
    short_pos = gateway.position(short_order.intent_id)
    assert short_pos is not None

    short_liq = gateway.position_liquidation_price(short_pos)
    assert short_liq is not None
    assert Decimal("103") < short_liq < Decimal("104")

    short_monitored = gateway.monitor_position(
        short_pos,
        _bar("bar-short-squeeze", 4, open_price="101", high="105.0", low="100.5", close="104.5"),
    )
    assert short_monitored.exit_reason == "LIQUIDATION"
    assert short_monitored.position.is_closed
    assert short_monitored.position.exit_reason == "LIQUIDATION"
    assert short_monitored.exit_reference_price == short_liq


def test_paper_funding_erosion_pushes_liquidation_ahead_of_stop() -> None:
    """Vị thế ban đầu an toàn nhưng bị funding bất lợi bào mòn isolated margin dẫn tới thanh lý."""
    gateway = _gateway()
    plan = TradePlanner().plan(
        _candidate(direction="long", atr_value=2.0, stop_atr_multiple=2.0),  # stop = 96.0
        _settings(leverage=Decimal("10")),
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(plan, TradePlan)
    order = gateway.submit(plan, "fx7-funding-erosion", submitted_at=NOW, eligible_after=NOW)
    gateway.apply_market_event(_bar("bar-1", 1, open_price="100", high="101", low="99"))
    pos = gateway.position(order.intent_id)
    assert pos is not None

    initial_liq = gateway.position_liquidation_price(pos)
    assert initial_liq is not None
    assert initial_liq < pos.stop_price  # Ban đầu LP (~90.36) nằm dưới stop (96.0)

    # Ghi nhận khoản phí funding lớn làm giảm isolated margin khả dụng của vị thế.
    eroded = gateway.apply_funding_event(
        pos.position_id,
        "funding-heavy-1",
        pos.quantity * Decimal("7.0"),
    )
    eroded_liq = gateway.position_liquidation_price(eroded)
    assert eroded_liq is not None
    assert eroded_liq > eroded.stop_price  # Nay LP (~97.39) đã vượt lên trên stop (96.0)

    result = gateway.monitor_position(
        eroded,
        _bar("bar-2", 2, open_price="98.5", high="99", low="96.5", close="97"),
    )
    assert result.exit_reason == "LIQUIDATION"
    assert result.position.is_closed


def test_cross_tier_liquidation_price_satisfies_exact_margin_balance_equation() -> None:
    """Khi notional tại giá thanh lý nhảy bậc MMR, nghiệm vẫn khớp MarginBalance == MM(LP)."""
    # LONG: vào ở bậc 2 BTCUSDT (notional = 52_000 > 50_000), khi thanh lý notional rơi về bậc 1.
    qty_long = Decimal("52")
    entry_long = Decimal("1000")
    leverage = Decimal("10")
    wb_long = (qty_long * entry_long) / leverage
    lp_long = calculate_liquidation_price(
        side="LONG",
        entry_price=entry_long,
        quantity=qty_long,
        leverage=leverage,
        symbol="BTCUSDT",
    )
    notional_at_lp_long = qty_long * lp_long
    assert notional_at_lp_long < Decimal("50000")
    margin_balance_long = wb_long + qty_long * (lp_long - entry_long)
    mm_long = calculate_maintenance_margin(notional_at_lp_long, symbol="BTCUSDT")
    assert abs(margin_balance_long - mm_long) < Decimal("1e-18")

    # SHORT: vào ở bậc 1 BTCUSDT (notional = 48_000 <= 50_000), khi thanh lý notional sang bậc 2.
    qty_short = Decimal("48")
    entry_short = Decimal("1000")
    wb_short = (qty_short * entry_short) / leverage
    lp_short = calculate_liquidation_price(
        side="SHORT",
        entry_price=entry_short,
        quantity=qty_short,
        leverage=leverage,
        symbol="BTCUSDT",
    )
    notional_at_lp_short = qty_short * lp_short
    assert notional_at_lp_short > Decimal("50000")
    margin_balance_short = wb_short + qty_short * (entry_short - lp_short)
    mm_short = calculate_maintenance_margin(notional_at_lp_short, symbol="BTCUSDT")
    assert abs(margin_balance_short - mm_short) < Decimal("1e-18")


def test_configurable_buffer_and_custom_tiers_in_planner() -> None:
    planner = TradePlanner()
    # Ở 10x BTCUSDT: LP_long ~ 90.3614. Stop = 94.0 (khoảng đệm ~ 37.7% > 20%, nhưng < 50%).
    strict_settings = _settings(
        leverage=Decimal("10"),
        max_leverage=Decimal("50"),
        liquidation_safety_buffer=Decimal("0.50"),
    )
    rejected = planner.plan(
        _candidate(direction="long", atr_value=3.0, stop_atr_multiple=2.0),  # stop = 94.0
        strict_settings,
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(rejected, PlanRejected)
    assert rejected.reason_code == "STOP_LIQUIDATION_UNSAFE"

    # Truyền bảng bậc MMR tùy chỉnh chặt hơn (MMR = 5%) làm LP tăng lên ~ 94.73 > stop 94.0.
    custom_tiers = (
        MaintenanceMarginTier(
            bracket=1,
            notional_floor=Decimal("0"),
            notional_cap=None,
            max_leverage=Decimal("20"),
            maint_margin_ratio=Decimal("0.05"),
            cum=Decimal("0"),
        ),
    )
    normal_settings = _settings(
        leverage=Decimal("10"),
        max_leverage=Decimal("50"),
        liquidation_safety_buffer=Decimal("0.20"),
    )
    rejected_custom = planner.plan(
        _candidate(direction="long", atr_value=3.0, stop_atr_multiple=2.0),
        normal_settings,
        _account(),
        (),
        _zero_costs(),
        now=NOW,
        liquidation_tiers=custom_tiers,
    )
    assert isinstance(rejected_custom, PlanRejected)
    assert rejected_custom.reason_code == "STOP_LIQUIDATION_UNSAFE"


def test_paper_gap_open_past_liquidation_preempts_stop_and_survives_recovery() -> None:
    gateway = _gateway()
    plan = TradePlanner().plan(
        _candidate(direction="long", atr_value=2.0, stop_atr_multiple=2.0),  # stop = 96.0
        _settings(leverage=Decimal("5")),  # LP ~ 80.321
        _account(),
        (),
        _zero_costs(),
        now=NOW,
    )
    assert isinstance(plan, TradePlan)
    order = gateway.submit(plan, "fx7-gap-liq", submitted_at=NOW, eligible_after=NOW)
    gateway.apply_market_event(_bar("bar-entry", 1, open_price="100", high="101", low="99"))

    # Khôi phục từ checkpoint: giá thanh lý vẫn được giữ nguyên.
    restored = PaperExecutionGateway.restore(gateway.policy, gateway.export_state())
    position = restored.position(order.intent_id)
    assert position is not None
    liq_price = restored.position_liquidation_price(position)
    assert liq_price is not None
    assert Decimal("80") < liq_price < Decimal("81")

    # Nhảy gap mở cửa thẳng xuống dưới giá thanh lý -> LIQUIDATION tại bar.open, thắng STOP_LOSS.
    gap_bar = _bar("bar-gap-liq", 2, open_price="75.0", high="98.0", low="74.0", close="97.0")
    monitored = restored.monitor_position(position, gap_bar)
    assert monitored.exit_reason == "LIQUIDATION"
    assert monitored.exit_reference_price == Decimal("75.0")
    assert monitored.position.is_closed
    assert restored.open_positions() == ()
    # Gọi lại cùng nến là idempotent.
    assert restored.monitor_position(monitored.position, gap_bar) == monitored

    # Ngược lại, nếu mở cửa ở trên stop và quét xuống dưới cả stop lẫn thanh lý trong cùng nến,
    # stop nằm trước nên STOP_LOSS khớp trước.
    gateway2 = _gateway()
    order2 = gateway2.submit(plan, "fx7-stop-first", submitted_at=NOW, eligible_after=NOW)
    gateway2.apply_market_event(_bar("bar-entry-2", 1, open_price="100", high="101", low="99"))
    pos2 = gateway2.position(order2.intent_id)
    assert pos2 is not None
    sweep_bar = _bar("bar-sweep", 2, open_price="99.0", high="99.5", low="74.0", close="75.0")
    stop_first = gateway2.monitor_position(pos2, sweep_bar)
    assert stop_first.exit_reason == "STOP_LOSS"
    assert stop_first.exit_reference_price == Decimal("96.0")


def test_custom_tier_validation_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="MMR"):
        MaintenanceMarginTier(
            bracket=1,
            notional_floor=Decimal("0"),
            notional_cap=Decimal("10000"),
            max_leverage=Decimal("20"),
            maint_margin_ratio=Decimal("1.0"),
        )
    with pytest.raises(ValueError, match="Biên an toàn"):
        _settings(liquidation_safety_buffer=Decimal("1.0"))
