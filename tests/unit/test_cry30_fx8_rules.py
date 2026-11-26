"""Unit tests cho CRY-30 (FX-8): cải tiến rule H01 v2, H03 v4 (15m/1h/4h) và H06 v3 (1h)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from trading_bot.application.decision_snapshot_builder import build_decision_snapshot_from_bars
from trading_bot.application.learning_feature_vector import build_learning_feature_vector
from trading_bot.application.macro_calendar_state import release_uses_macro
from trading_bot.application.strategy_catalog import fx8_rules, strategy_registry
from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.domain.market.features import ema
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy import rules as rules_module
from trading_bot.domain.strategy import rules_fx8, rules_v2, rules_v3
from trading_bot.domain.strategy.checklists import strategy_checklist
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    NoTrade,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.rules import (
    H01EmaTrendRule,
    H03DonchianBreakoutRule,
    H06RangeMeanReversionRule,
    release_content_hash,
)
from trading_bot.domain.strategy.rules_fx8 import (
    H01EmaTrendRuleV2,
    H03DonchianBreakoutRuleV41h,
    H03DonchianBreakoutRuleV44h,
    H03DonchianBreakoutRuleV415m,
    H06MeanReversionRuleV31h,
    exit_evidence_spec,
)

BASE_TIME = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
_INTERVALS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


def _bar(
    index: int,
    timeframe: str,
    *,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    quote_volume: float = 1000.0,
    symbol: str = "BTCUSDT",
    workspace_id: UUID | None = None,
) -> OHLCVBar:
    interval = _INTERVALS[timeframe]
    opened = BASE_TIME + interval * index
    closed = opened + interval - timedelta(milliseconds=1)
    checksum = hashlib.sha256(f"fx8:{symbol}:{timeframe}:{index}".encode()).hexdigest()
    ws = workspace_id if workspace_id is not None else uuid4()
    return OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal(str(open_price)),
        high=Decimal(str(high_price)),
        low=Decimal(str(low_price)),
        close=Decimal(str(close_price)),
        volume=Decimal("10"),
        quote_volume=Decimal(str(quote_volume)),
        taker_buy_base_volume=Decimal("5"),
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=f"{timeframe}:{index}",
        checksum=checksum,
        raw_reference=f"raw:{timeframe}:{index}",
        source_identity=MarketBarSourceIdentity(
            workspace_id=ws,
            record_id=uuid4(),
            logical_key=f"fx8:{symbol}:{timeframe}:{index}",
            revision=1,
            supersedes_record_id=None,
            checksum=checksum,
        ),
    )


def _series(
    timeframe: str,
    bars: tuple[OHLCVBar, ...],
    quote_volumes: tuple[float, ...] | None = None,
) -> MarketSeries:
    interval = _INTERVALS[timeframe]
    return MarketSeries(
        timeframe=timeframe,
        bars=bars,
        quote_volumes=quote_volumes,
        quality=SeriesQuality.READY,
        expected_interval=interval,
        max_event_age=interval * 2,
        backfill_complete=True,
    )


def _feature(
    name: str,
    timeframe: str,
    value: float,
    available_at: datetime,
    previous_value: float | None = None,
) -> FeatureObservation:
    return FeatureObservation(
        feature_id=f"{timeframe}:{name}",
        name=name,
        timeframe=timeframe,
        value=value,
        previous_value=previous_value,
        available_at=available_at,
        quality=FeatureQuality.READY,
    )


def _selection(
    release_id: str,
    parameters: tuple[tuple[str, int | float], ...] = (),
    *,
    published: bool = True,
) -> StrategySelection:
    return StrategySelection(
        settings_version_id="settings-fx8-1",
        active_release_ids=(release_id,),
        published=published,
        parameters=parameters,
    )


def _build_h01_cross_bars(bars_after_cross: int) -> tuple[OHLCVBar, ...]:
    """Tạo chuỗi nến 1h có giao cắt tăng EMA20/EMA80 cách nến cuối ``bars_after_cross`` nến."""
    # 95 nến giảm nhẹ để EMA20 < EMA80, sau đó tăng mạnh để EMA20 cắt lên trên EMA80.
    closes: list[float] = [120.0 - 0.1 * i for i in range(95)]
    while True:
        closes.append(closes[-1] + 2.5)
        ema20_s = ema(closes, 20)
        ema80_s = ema(closes, 80)
        if (
            ema20_s[-2] is not None
            and ema80_s[-2] is not None
            and ema20_s[-1] is not None
            and ema80_s[-1] is not None
            and ema20_s[-2] <= ema80_s[-2]
            and ema20_s[-1] > ema80_s[-1]
        ):
            break
    for _ in range(bars_after_cross):
        closes.append(closes[-1] + 1.5)
    return tuple(
        _bar(
            idx,
            "1h",
            open_price=c - 0.3,
            high_price=c + 0.5,
            low_price=c - 0.5,
            close_price=c,
        )
        for idx, c in enumerate(closes)
    )


def _h01_snapshot(
    *,
    bars_after_cross: int = 0,
    adx: float = 25.0,
    atr: float = 2.0,
    h4_close: float = 150.0,
    h4_ema200: float = 120.0,
    ema_gap_override: tuple[float, float] | None = None,
    last_stop_exit_at: datetime | None = None,
) -> DecisionSnapshot:
    bars_1h = _build_h01_cross_bars(bars_after_cross)
    closes = [float(b.close) for b in bars_1h]
    ema20_s = ema(closes, 20)
    ema80_s = ema(closes, 80)
    fast_val = ema_gap_override[0] if ema_gap_override else float(ema20_s[-1] or 0.0)
    slow_val = ema_gap_override[1] if ema_gap_override else float(ema80_s[-1] or 0.0)
    fast_prev = float(ema20_s[-2] or 0.0)
    slow_prev = float(ema80_s[-2] or 0.0)
    decision_time = bars_1h[-1].close_time + timedelta(seconds=1)
    h4_bar = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="4h",
        open_time=bars_1h[-1].open_time - timedelta(hours=3),
        close_time=bars_1h[-1].close_time,
        event_at=bars_1h[-1].close_time,
        received_at=bars_1h[-1].close_time,
        available_at=bars_1h[-1].close_time,
        open=Decimal(str(h4_close - 1.0)),
        high=Decimal(str(h4_close + 1.0)),
        low=Decimal(str(h4_close - 2.0)),
        close=Decimal(str(h4_close)),
        volume=Decimal("100"),
        quote_volume=Decimal("10000"),
        taker_buy_base_volume=Decimal("50"),
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor="4h:latest",
        checksum="c" * 64,
        raw_reference="raw:4h:latest",
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision_time,
        snapshot_id="a" * 64,
        series=(_series("1h", bars_1h), _series("4h", (h4_bar,))),
        features=(
            _feature("ema_20", "1h", fast_val, bars_1h[-1].close_time, previous_value=fast_prev),
            _feature("ema_80", "1h", slow_val, bars_1h[-1].close_time, previous_value=slow_prev),
            _feature("adx_14", "1h", adx, bars_1h[-1].close_time),
            _feature("atr_14", "1h", atr, bars_1h[-1].close_time),
            _feature("ema_200", "4h", h4_ema200, h4_bar.close_time),
        ),
        position_is_flat=True,
        last_stop_exit_at=last_stop_exit_at,
    )


def _h03_snapshot(
    timeframe: str = "1h",
    *,
    close: float = 102.0,
    atr: float = 2.0,
    adx: float = 24.0,
    er: float = 0.35,
    current_volume: float = 1600.0,
    prior_volume: float = 1000.0,
) -> DecisionSnapshot:
    prior_bars = [
        _bar(
            idx,
            timeframe,
            open_price=99.0,
            high_price=100.0,
            low_price=95.0,
            close_price=99.5,
            quote_volume=prior_volume,
        )
        for idx in range(55)
    ]
    current_bar = _bar(
        55,
        timeframe,
        open_price=99.8,
        high_price=max(close + 0.5, 100.5),
        low_price=min(close - 0.5, 99.0),
        close_price=close,
        quote_volume=current_volume,
    )
    bars = (*prior_bars, current_bar)
    volumes = tuple([prior_volume] * 55 + [current_volume])
    decision_time = current_bar.close_time + timedelta(seconds=1)
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision_time,
        snapshot_id="b" * 64,
        series=(_series(timeframe, bars, quote_volumes=volumes),),
        features=(
            _feature("atr_14", timeframe, atr, current_bar.close_time),
            _feature("adx_14", timeframe, adx, current_bar.close_time),
            _feature("efficiency_ratio_40", timeframe, er, current_bar.close_time),
        ),
        position_is_flat=True,
    )


def _h06_snapshot(
    *,
    direction: str = "long",
    zscore: float = -2.4,
    rsi_val: float = 25.0,
    adx: float = 14.0,
    er: float = 0.18,
    atr: float = 2.0,
    current_ohlc: tuple[float, float, float, float] | None = None,
    macro_blackout: bool | None = False,
    macro_is_fresh: bool | None = True,
) -> DecisionSnapshot:
    # 120 nến đi ngang quanh 100 (low 92, high 108) để mean_20 ~ 100.
    prior_bars = [
        _bar(
            idx,
            "1h",
            open_price=100.0,
            high_price=108.0,
            low_price=92.0,
            close_price=100.0,
        )
        for idx in range(120)
    ]
    if current_ohlc is None:
        if direction == "long":
            # Nến đóng tăng tại 96.0 (mean_20 ~ 99.8 -> distance 3.8 / atr 2.0 = 1.9 >= 1.2).
            current_ohlc = (95.0, 96.5, 94.5, 96.0)
        else:
            current_ohlc = (105.0, 105.5, 103.5, 104.0)
    open_p, high_p, low_p, close_p = current_ohlc
    current_bar = _bar(
        120,
        "1h",
        open_price=open_p,
        high_price=high_p,
        low_price=low_p,
        close_price=close_p,
    )
    bars = (*prior_bars, current_bar)
    decision_time = current_bar.close_time + timedelta(seconds=1)
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision_time,
        snapshot_id="c" * 64,
        series=(_series("1h", bars),),
        features=(
            _feature("adx_14", "1h", adx, current_bar.close_time),
            _feature("efficiency_ratio_40", "1h", er, current_bar.close_time),
            _feature("zscore_20", "1h", zscore, current_bar.close_time),
            _feature("rsi_14", "1h", rsi_val, current_bar.close_time),
            _feature("atr_14", "1h", atr, current_bar.close_time),
        ),
        position_is_flat=True,
        macro_blackout=macro_blackout,
        macro_evidence_ids=("macro:fed-1",),
        macro_evidence_available_at=current_bar.close_time,
        macro_is_fresh=macro_is_fresh,
    )


def test_legacy_rules_hashes_remain_immutable_with_fx8() -> None:
    """rules.py, rules_v2.py, rules_v3.py không đổi byte nào và hash release cũ giữ nguyên."""
    source = Path(rules_module.__file__).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    assert digest == rules_v2.BASE_RULES_SOURCE_SHA256
    assert digest == rules_v3.BASE_RULES_SOURCE_SHA256
    assert digest == rules_fx8.BASE_RULES_SOURCE_SHA256

    registry = strategy_registry()
    assert registry.get_release("h01-v1").content_hash == release_content_hash(H01EmaTrendRule())
    assert registry.get_release("h03-v1").content_hash == release_content_hash(
        H03DonchianBreakoutRule()
    )
    assert registry.get_release("h06-v1").content_hash == release_content_hash(
        H06RangeMeanReversionRule()
    )
    for rule, _, _, _ in fx8_rules():
        release = registry.get_release(rule.release_id)
        assert release.status == "EXPERIMENTAL_UNVALIDATED"
        assert release.content_hash == release_content_hash(rule)


def test_h01_v2_cross_window_ema_gap_and_cooldown() -> None:
    """H01 v2 nhận tín hiệu trong cửa sổ giao cắt, lọc khoảng cách EMA và cooldown sau stop."""
    rule = H01EmaTrendRuleV2()
    settings = _selection("h01-v2")

    # Giao cắt cách 2 nến (trong cửa sổ mặc định 3 nến) -> CANDIDATE.
    snap_in_window = _h01_snapshot(bars_after_cross=2, atr=1.0)
    res = rule.evaluate(snap_in_window, settings)
    assert isinstance(res, StrategyCandidate)
    assert res.strategy_release_id == "h01-v2"
    assert res.direction == "long"

    # Giao cắt cách 4 nến (vượt cửa sổ mặc định 3 nến) -> NoTrade(H01_NO_CROSS).
    snap_outside = _h01_snapshot(bars_after_cross=4, atr=1.0)
    res_out = rule.evaluate(snap_outside, settings)
    assert isinstance(res_out, NoTrade)
    assert res_out.code == "H01_NO_CROSS"

    # Mở rộng cross_window_bars=5 qua settings.parameters -> vào được.
    res_wide = rule.evaluate(snap_outside, _selection("h01-v2", (("cross_window_bars", 5),)))
    assert isinstance(res_wide, StrategyCandidate)

    # Khoảng cách |EMA20 - EMA80| < 0.15 * ATR14 -> H01_EMA_GAP_TOO_NARROW.
    snap_narrow = _h01_snapshot(bars_after_cross=1, atr=10.0, ema_gap_override=(100.5, 100.0))
    res_narrow = rule.evaluate(snap_narrow, settings)
    assert isinstance(res_narrow, NoTrade)
    assert res_narrow.code == "H01_EMA_GAP_TOO_NARROW"

    # Cooldown sau STOP_LOSS: mới dừng cách 3h < 6h -> H01_POST_STOP_COOLDOWN.
    snap_cooldown = replace(
        snap_in_window,
        last_stop_exit_at=snap_in_window.decision_time - timedelta(hours=3),
    )
    res_cool = rule.evaluate(snap_cooldown, settings)
    assert isinstance(res_cool, NoTrade)
    assert res_cool.code == "H01_POST_STOP_COOLDOWN"

    # Đã qua 6h sau stop -> cho phép vào lệnh trở lại.
    snap_after_cool = replace(
        snap_in_window,
        last_stop_exit_at=snap_in_window.decision_time - timedelta(hours=6),
    )
    res_ok = rule.evaluate(snap_after_cool, settings)
    assert isinstance(res_ok, StrategyCandidate)


def test_h03_v4_atr_buffer_volume_multiplier_regime_and_exit_spec() -> None:
    """H03 v4 kiểm biên đệm ATR, bội số median volume, ADX/ER và đăng ký exit evidence."""
    for rule, tf in (
        (H03DonchianBreakoutRuleV415m(), "15m"),
        (H03DonchianBreakoutRuleV41h(), "1h"),
        (H03DonchianBreakoutRuleV44h(), "4h"),
    ):
        settings = _selection(rule.release_id)
        # Đủ buffer (100 + 0.25*2 = 100.5 < 102.0), vol 1600 >= 1.5*1000, ADX 24, ER 0.35.
        ok = rule.evaluate(_h03_snapshot(tf, close=102.0), settings)
        assert isinstance(ok, StrategyCandidate)
        assert ok.strategy_release_id == rule.release_id
        assert ok.direction == "long"

        # Phá đỉnh 100.0 nhưng chưa vượt đệm 0.25 * 2.0 = 100.5 -> H03_NO_BREAKOUT.
        weak_break = rule.evaluate(_h03_snapshot(tf, close=100.3), settings)
        assert isinstance(weak_break, NoTrade)
        assert weak_break.code == "H03_NO_BREAKOUT"

        # Volume chỉ 1.2x median (< 1.5x) -> H03_VOLUME_NOT_ABOVE_MEDIAN.
        low_vol = rule.evaluate(_h03_snapshot(tf, close=102.0, current_volume=1200.0), settings)
        assert isinstance(low_vol, NoTrade)
        assert low_vol.code == "H03_VOLUME_NOT_ABOVE_MEDIAN"

        # Regime chop: ADX < 20 hoặc ER < 0.25 -> H03_REGIME_CHOP.
        chop_adx = rule.evaluate(_h03_snapshot(tf, close=102.0, adx=15.0), settings)
        assert isinstance(chop_adx, NoTrade)
        assert chop_adx.code == "H03_REGIME_CHOP"

        chop_er = rule.evaluate(_h03_snapshot(tf, close=102.0, er=0.20), settings)
        assert isinstance(chop_er, NoTrade)
        assert chop_er.code == "H03_REGIME_CHOP"

        spec = exit_evidence_spec(rule.recipe.recipe_id)
        assert spec is not None
        assert (spec.timeframe, spec.channel_period) == (tf, 20)


def test_h06_v3_1h_reversal_candle_mean_tp_and_macro_guard() -> None:
    """H06 v3 1h kiểm nến đảo chiều, TP theo mean_20 >= 1.2 ATR và giữ guard macro AI-4."""
    rule = H06MeanReversionRuleV31h()
    settings = _selection("h06-v3-1h")
    assert release_uses_macro("h06-v3-1h") is True

    # Nến đóng tăng + distance tới mean_20 ~ 3.8 / atr 2.0 = 1.9 >= 1.2 -> CANDIDATE.
    cand = rule.evaluate(_h06_snapshot(direction="long"), settings)
    assert isinstance(cand, StrategyCandidate)
    assert cand.strategy_release_id == "h06-v3-1h"
    assert cand.direction == "long"
    assert cand.recipe.recipe_id == "h06-exit-v3-1h"
    assert cand.recipe.exit_feature_name == "zscore_20"
    assert cand.recipe.exit_feature_threshold == 0.0
    assert cand.recipe.take_profit_atr_multiple is not None
    assert cand.recipe.take_profit_atr_multiple >= 1.2

    # Nến đóng giảm nhưng râu dưới >= 40% range (lower_wick=3.0 / range=4.0 = 75%).
    wick_cand = rule.evaluate(
        _h06_snapshot(direction="long", current_ohlc=(96.5, 97.0, 93.0, 96.0)), settings
    )
    assert isinstance(wick_cand, StrategyCandidate)

    # Nến đóng giảm mạnh, không có râu dưới rõ -> H06_NO_REVERSAL_CANDLE.
    no_rev = rule.evaluate(
        _h06_snapshot(direction="long", current_ohlc=(98.0, 98.2, 95.8, 96.0)), settings
    )
    assert isinstance(no_rev, NoTrade)
    assert no_rev.code == "H06_NO_REVERSAL_CANDLE"

    # TP distance tới mean_20 quá hẹp (< 1.2 ATR) -> H06_TP_BELOW_MIN_ATR.
    narrow_tp = rule.evaluate(
        _h06_snapshot(direction="long", atr=5.0, current_ohlc=(95.0, 96.5, 94.5, 96.0)), settings
    )
    assert isinstance(narrow_tp, NoTrade)
    assert narrow_tp.code == "H06_TP_BELOW_MIN_ATR"

    # Macro blackout fail-closed.
    blackout = rule.evaluate(_h06_snapshot(macro_blackout=True), settings)
    assert isinstance(blackout, NoTrade)
    assert blackout.code == "H06_MACRO_BLACKOUT"

    unknown_macro = rule.evaluate(_h06_snapshot(macro_blackout=None), settings)
    assert isinstance(unknown_macro, NoTrade)
    assert unknown_macro.code == "H06_MACRO_UNKNOWN"

    spec = exit_evidence_spec("h06-exit-v3-1h")
    assert spec is not None
    assert (spec.timeframe, spec.feature_name) == ("1h", "zscore_20")


def test_fx8_market_requirements_checklist_and_learning_feature_vector() -> None:
    """Các release FX-8 đồng bộ market requirements, checklist và LearningFeatureVector."""
    expected_timeframes = {
        "h01-v2": "1h",
        "h03-v4-15m": "15m",
        "h03-v4-1h": "1h",
        "h03-v4-4h": "4h",
        "h06-v3-1h": "1h",
    }
    registry = strategy_registry()
    for release_id, tf in expected_timeframes.items():
        assert entry_timeframe_for_release(release_id) == tf
        reqs = feature_requirements_for_release(release_id)
        assert tf in reqs
        assert registry.get_release(release_id).release_id == release_id

    # Kiểm tra checklist sinh đầy đủ cho cả 3 họ FX-8.
    assert len(strategy_checklist("h01-v2", _h01_snapshot(), _selection("h01-v2"))) >= 4
    assert len(strategy_checklist("h03-v4-1h", _h03_snapshot("1h"), _selection("h03-v4-1h"))) >= 4
    assert len(strategy_checklist("h06-v3-1h", _h06_snapshot(), _selection("h06-v3-1h"))) >= 6

    # Kiểm tra DecisionSnapshot + LearningFeatureVector build thành công cho h03-v4-1h.
    ws_id = uuid4()
    bars_1h = tuple(
        _bar(
            idx,
            "1h",
            open_price=100.0 + 0.1 * idx,
            high_price=101.0 + 0.1 * idx,
            low_price=99.0 + 0.1 * idx,
            close_price=100.5 + 0.1 * idx,
            workspace_id=ws_id,
        )
        for idx in range(620)
    )
    decision_time = bars_1h[-1].close_time + timedelta(seconds=1)
    snapshot = build_decision_snapshot_from_bars(
        workspace_id=ws_id,
        symbol="BTCUSDT",
        strategy_release_id="h03-v4-1h",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe={"1h": bars_1h},
        position_is_flat=True,
        source_identity_required=True,
    )
    # Bổ sung last_stop_exit_at=None mặc định không đổi snapshot_id.
    assert snapshot.last_stop_exit_at is None
    vector = build_learning_feature_vector(
        snapshot,
        strategy_release_id="h03-v4-1h",
        strategy_release_content_hash=registry.get_release("h03-v4-1h").content_hash,
    )
    assert vector.strategy_release_id == "h03-v4-1h"
    assert vector.feature_keys == ("1h:atr_14", "1h:adx_14", "1h:efficiency_ratio_40")
