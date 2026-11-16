"""Họ scalping H18–H20 (SIG-4 lát 1) — giả thuyết ``EXPERIMENTAL_UNVALIDATED``, chỉ PAPER.

Đặt ở module riêng vì ``release_content_hash`` băm mã nguồn module của rule: thêm vào
``rules.py`` sẽ đổi danh tính mọi release H01/H03/H06 đang ghim. Định nghĩa theo
[nghiên cứu §6](../../../docs/research/signal-scalping-chart-research-2026-11-15.md) và quyết định
owner 16/11/2026 (BTCUSDT/ETHUSDT, M5 + M15; phí VIP0 taker 0,05%):

- **H18** quét + lấy lại mức phiên/PDH/PDL (M5): mức đang chờ bị râu vượt rồi nến đóng quay vào
  trong (``liquidity_levels`` trạng thái ``rejected`` ở nến hiện tại); delta nến (taker buy) phải
  ngược hướng quét; stop = cực trị râu + 0,1·ATR.
- **H19** hồi về VWAP phiên trong vùng đi ngang (M5): ADX14 < 18 và ER40 < 0,25 (cổng như H06),
  giá đóng lệch khỏi VWAP ngày UTC ≥ 2σ của độ lệch trong ngày; vào ngược hướng lệch; stop 1·ATR.
- **H20** BOS + retest (M5, xu hướng M15 cùng chiều): BOS bằng giá đóng trong 6 nến gần nhất có
  |delta| nến phá ≥ phân vị 80 của 50 nến trước; nến hiện tại chạm lại mức phá (≤ 0,25·ATR) và
  đóng ngoài mức, không xuyên sâu quá 0,5·ATR; stop = swing đối diện gần nhất.

Bộ lọc chung: chỉ trong phiên London hoặc New York; tránh ±10 phút quanh mốc funding 00/08/16 UTC
(lịch 8 giờ của BTCUSDT/ETHUSDT); stop ≥ 3 × chi phí khứ hồi taker (2 × 0,05%); vị thế phải phẳng.
Thang chốt lời (``SCALPING_LADDERS``): TP1 1R 50% → dời stop về hòa vốn → TP2 2R 50%.

Chênh lệch có chủ đích so với nghiên cứu (lát 1): khớp tại giá mở nến sau với phí taker (bảo thủ
hơn limit maker); TP theo R thay cho mức cấu trúc; chưa có cooldown sau chuỗi thua (cần lịch sử
account ngoài snapshot).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.domain.market.overview import active_sessions
from trading_bot.domain.market.sessions import liquidity_levels
from trading_bot.domain.market.structure import confirmed_swings, market_structure
from trading_bot.domain.risk.take_profit import LadderStep, TakeProfitLadder
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
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.rules import (
    _candidate,
    _no,
    _selected,
    _value,
    release_content_hash,
)

SCALPING_RULESET_VERSION = "sig4-scalping-v1"
ROUND_TRIP_COST_RATE = Decimal("0.001")  # taker 0,05% × 2 (quyết định owner #4)
MIN_STOP_COST_MULTIPLE = 3
FUNDING_BLACKOUT = timedelta(minutes=10)
FUNDING_HOURS_UTC = (0, 8, 16)
SCALPING_SESSIONS = frozenset({"london", "new_york"})
SCALPING_LADDER = TakeProfitLadder(
    steps=(LadderStep(Decimal("1"), Decimal("0.5")), LadderStep(Decimal("2"), Decimal("0.5"))),
    breakeven_after_first=True,
)
MAX_HOLD_BARS = 12
SCALPING_INTERVALS = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}


def _inputs(
    snapshot: DecisionSnapshot,
    timeframe: str,
    names: tuple[str, ...],
    strategy_id: str,
) -> tuple[MarketSeries, dict[str, FeatureObservation]] | NoTrade:
    """Cùng kiểm tra point-in-time như ``rules._inputs`` nhưng cho khung 5m/15m của scalping.

    ``rules._inputs`` chỉ biết 15m/1h/4h; sửa nó sẽ đổi ``release_content_hash`` của H01/H03/H06,
    nên bản này nhân đôi có chủ đích (mã giống, bảng interval khác).
    """
    series = snapshot.get_series(timeframe)
    if series is None or not series.bars:
        return _no(strategy_id, f"{strategy_id}_INSUFFICIENT_BARS", f"Thiếu chuỗi nến {timeframe}.")
    if any(not bar.is_closed for bar in series.bars):
        return _no(strategy_id, f"{strategy_id}_BAR_NOT_CLOSED", "Chuỗi có nến chưa đóng.")
    if any(
        max(bar.close_time, bar.event_at, bar.available_at) > snapshot.decision_time
        for bar in series.bars
    ):
        return _no(
            strategy_id,
            f"{strategy_id}_INPUT_UNAVAILABLE",
            "Mốc close/event/availability của nến vượt thời điểm quyết định.",
        )
    if series.quality is SeriesQuality.UNKNOWN:
        return _no(
            strategy_id, f"{strategy_id}_SERIES_QUALITY_UNKNOWN", "Chuỗi chưa xác nhận chất lượng."
        )
    if series.quality is SeriesQuality.STALE:
        return _no(strategy_id, f"{strategy_id}_SERIES_STALE", "Chuỗi được đánh dấu stale.")
    if series.quality is not SeriesQuality.READY:
        return _no(strategy_id, f"{strategy_id}_SERIES_INCOMPLETE", "Chuỗi không usable.")
    if series.expected_interval is None or series.max_event_age is None:
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_QUALITY_UNKNOWN",
            "Thiếu interval hoặc freshness limit đã cấu hình.",
        )
    if series.expected_interval != SCALPING_INTERVALS.get(timeframe):
        return _no(
            strategy_id, f"{strategy_id}_SERIES_INCOMPLETE", "Interval chuỗi không khớp timeframe."
        )
    if series.backfill_complete is not True:
        return _no(
            strategy_id, f"{strategy_id}_SERIES_INCOMPLETE", "Backfill chưa được xác nhận đầy đủ."
        )
    if any(
        current.open_time - previous.open_time != series.expected_interval
        for previous, current in zip(series.bars, series.bars[1:], strict=False)
    ):
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_INCOMPLETE",
            "Chuỗi có khoảng trống hoặc nhịp nến không đúng interval.",
        )
    age = snapshot.decision_time - series.bars[-1].event_at
    if age < timedelta(0):
        return _no(
            strategy_id, f"{strategy_id}_INPUT_UNAVAILABLE", "Event mới nhất nằm sau decision_time."
        )
    if age > series.max_event_age:
        return _no(strategy_id, f"{strategy_id}_SERIES_STALE", "Event mới nhất quá tuổi tối đa.")
    found: dict[str, FeatureObservation] = {}
    missing: list[str] = []
    for name in names:
        feature = snapshot.get_feature(name, timeframe)
        if feature is None or feature.quality is not FeatureQuality.READY or feature.value is None:
            missing.append(f"{timeframe}:{name}")
            continue
        if feature.available_at > snapshot.decision_time:
            return _no(
                strategy_id,
                f"{strategy_id}_INPUT_UNAVAILABLE",
                f"Đặc trưng {name} chỉ có sau thời điểm quyết định.",
            )
        if snapshot.decision_time - feature.available_at > series.max_event_age:
            return _no(
                strategy_id,
                f"{strategy_id}_FEATURE_STALE",
                f"Đặc trưng {name} quá tuổi tối đa cho timeframe.",
                missing=(f"{timeframe}:{name}",),
            )
        found[name] = feature
    if missing:
        return _no(
            strategy_id,
            f"{strategy_id}_FEATURE_UNAVAILABLE",
            "Thiếu hoặc không dùng được đặc trưng bắt buộc.",
            missing=tuple(missing),
        )
    return series, found


def _near_funding(moment: datetime) -> bool:
    utc = moment.astimezone(UTC)
    day = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    marks = [day + timedelta(hours=hour) for hour in (*FUNDING_HOURS_UTC, 24)]
    marks.insert(0, day - timedelta(hours=8))
    return any(abs(utc - mark) <= FUNDING_BLACKOUT for mark in marks)


def _common_gates(
    strategy_id: str, snapshot: DecisionSnapshot, signal_at: datetime
) -> NoTrade | None:
    if snapshot.position_is_flat is None:
        return _no(strategy_id, f"{strategy_id}_POSITION_UNKNOWN", "Chưa xác định vị thế phẳng.")
    if snapshot.position_is_flat is not True:
        return _no(strategy_id, f"{strategy_id}_POSITION_NOT_FLAT", "Scalping cấm vào chồng.")
    if snapshot.macro_blackout is True:
        return _no(
            strategy_id,
            f"{strategy_id}_MACRO_BLACKOUT",
            "Đang trong khoảng cấm quanh sự kiện vĩ mô.",
            evidence_refs=snapshot.macro_evidence_ids,
        )
    sessions = {item.key for item in active_sessions(signal_at)}
    if not sessions & SCALPING_SESSIONS:
        return _no(
            strategy_id,
            f"{strategy_id}_OUT_OF_SESSION",
            "Chỉ vào lệnh trong phiên London hoặc New York.",
        )
    if _near_funding(signal_at):
        return _no(
            strategy_id,
            f"{strategy_id}_FUNDING_WINDOW",
            "Trong ±10 phút quanh mốc funding 00/08/16 UTC.",
        )
    return None


def _cost_gate(strategy_id: str, price: Decimal, stop_distance: Decimal) -> NoTrade | None:
    minimum = price * ROUND_TRIP_COST_RATE * MIN_STOP_COST_MULTIPLE
    if stop_distance < minimum:
        return _no(
            strategy_id,
            f"{strategy_id}_STOP_TOO_TIGHT",
            "Khoảng stop nhỏ hơn 3 lần chi phí khứ hồi ước tính (phí taker hai chiều).",
        )
    return None


def _delta(bar: object) -> float | None:
    taker = getattr(bar, "taker_buy_base_volume", None)
    if taker is None:
        return None
    return 2 * float(taker) - float(getattr(bar, "volume"))  # noqa: B009


def _recipe(recipe_id: str, stop_multiple: float) -> ExitRecipe:
    return ExitRecipe(recipe_id, round(stop_multiple, 6), max_hold_bars=MAX_HOLD_BARS)


class H18SweepReclaimRule:
    """H18: quét + lấy lại mức phiên/PDH/PDL trên M5."""

    strategy_id = "H18"
    release_id = "h18-v1"
    recipe_id = "h18-exit-v1"
    __slots__ = ()
    recipe_manifest = (
        ("stop", "wick_extreme_plus_0.1_atr"),
        ("ladder", "1R:0.5,2R:0.5,breakeven"),
        ("max_hold_bars", MAX_HOLD_BARS),
        ("ruleset", SCALPING_RULESET_VERSION),
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        denied = _selected(snapshot, settings, self.strategy_id)
        if denied:
            return denied
        inputs = _inputs(snapshot, "5m", ("atr_14",), self.strategy_id)
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        bars = series.bars
        current = bars[-1]
        gate = _common_gates(self.strategy_id, snapshot, current.close_time)
        if gate:
            return gate
        interval = SCALPING_INTERVALS["5m"]
        levels = liquidity_levels(
            [bar.open_time for bar in bars],
            [float(bar.high) for bar in bars],
            [float(bar.low) for bar in bars],
            [float(bar.close) for bar in bars],
            interval,
        )
        last = len(bars) - 1
        swept = [
            level for level in levels if level.state == "rejected" and level.event_index == last
        ]
        if not swept:
            return _no(self.strategy_id, "H18_NO_SWEEP", "Nến hiện tại không quét-lấy lại mức nào.")
        level = max(swept, key=lambda item: (item.armed_at, item.key))
        direction = "short" if level.side == "high" else "long"
        delta = _delta(current)
        if delta is None:
            return _no(self.strategy_id, "H18_TAKER_UNAVAILABLE", "Nến thiếu taker buy (SIG-0).")
        if (direction == "short" and delta > 0) or (direction == "long" and delta < 0):
            return _no(
                self.strategy_id, "H18_DELTA_NOT_REVERSED", "Delta nến chưa đảo ngược hướng quét."
            )
        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(self.strategy_id, "H18_FEATURE_UNAVAILABLE", "ATR14 phải dương.")
        extreme = current.high if direction == "short" else current.low
        stop_distance = abs(extreme - current.close) + Decimal(str(atr * 0.1))
        cost = _cost_gate(self.strategy_id, current.close, stop_distance)
        if cost:
            return cost
        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=current,
            direction=direction,
            atr=atr,
            recipe=_recipe(self.recipe_id, float(stop_distance) / atr),
            evidence=(features["atr_14"],),
            evidence_bars=(current,),
            settings=settings,
            release_hash=release_content_hash(self),
        )


class H19VwapRangeReversionRule:
    """H19: hồi về VWAP phiên trong vùng đi ngang trên M5."""

    strategy_id = "H19"
    release_id = "h19-v1"
    recipe_id = "h19-exit-v1"
    __slots__ = ()
    recipe_manifest = (
        ("stop_atr_multiple", 1.0),
        ("ladder", "1R:0.5,2R:0.5,breakeven"),
        ("max_hold_bars", MAX_HOLD_BARS),
        ("ruleset", SCALPING_RULESET_VERSION),
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        denied = _selected(snapshot, settings, self.strategy_id)
        if denied:
            return denied
        names = ("atr_14", "adx_14", "efficiency_ratio_40")
        inputs = _inputs(snapshot, "5m", names, self.strategy_id)
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        current = series.bars[-1]
        gate = _common_gates(self.strategy_id, snapshot, current.close_time)
        if gate:
            return gate
        if _value(features, "adx_14") >= 18 or _value(features, "efficiency_ratio_40") >= 0.25:
            return _no(self.strategy_id, "H19_NOT_RANGE", "Cần ADX14 < 18 và ER40 < 0,25.")
        day = current.open_time.astimezone(UTC).date()
        today = [bar for bar in series.bars if bar.open_time.astimezone(UTC).date() == day]
        if len(today) < 20:
            return _no(self.strategy_id, "H19_INSUFFICIENT_BARS", "Cần ≥ 20 nến trong ngày UTC.")
        weights = [float(bar.volume) for bar in today]
        typical = [float(bar.high + bar.low + bar.close) / 3 for bar in today]
        total = sum(weights)
        if total <= 0:
            return _no(self.strategy_id, "H19_NO_VOLUME", "Khối lượng trong ngày bằng 0.")
        vwap = sum(t * w for t, w in zip(typical, weights, strict=True)) / total
        deviations = [t - vwap for t in typical]
        sigma = math.sqrt(sum(d * d for d in deviations) / len(deviations))
        deviation = float(current.close) - vwap
        if sigma <= 0 or abs(deviation) < 2 * sigma:
            return _no(self.strategy_id, "H19_NO_EXTREME", "Giá chưa lệch ≥ 2σ khỏi VWAP ngày.")
        direction = "short" if deviation > 0 else "long"
        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(self.strategy_id, "H19_FEATURE_UNAVAILABLE", "ATR14 phải dương.")
        cost = _cost_gate(self.strategy_id, current.close, Decimal(str(atr)))
        if cost:
            return cost
        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=current,
            direction=direction,
            atr=atr,
            recipe=_recipe(self.recipe_id, 1.0),
            evidence=tuple(features[name] for name in names),
            evidence_bars=(current,),
            settings=settings,
            release_hash=release_content_hash(self),
        )


class H20BreakRetestRule:
    """H20: BOS có mất cân bằng khối lượng + retest, xu hướng M15 cùng chiều."""

    strategy_id = "H20"
    release_id = "h20-v1"
    recipe_id = "h20-exit-v1"
    __slots__ = ()
    recipe_manifest = (
        ("stop", "opposite_confirmed_swing"),
        ("ladder", "1R:0.5,2R:0.5,breakeven"),
        ("max_hold_bars", MAX_HOLD_BARS),
        ("ruleset", SCALPING_RULESET_VERSION),
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        denied = _selected(snapshot, settings, self.strategy_id)
        if denied:
            return denied
        inputs = _inputs(snapshot, "5m", ("atr_14",), self.strategy_id)
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        context = _inputs(snapshot, "15m", ("atr_14",), self.strategy_id)
        if isinstance(context, NoTrade):
            return context
        bars = series.bars
        current = bars[-1]
        gate = _common_gates(self.strategy_id, snapshot, current.close_time)
        if gate:
            return gate
        if len(bars) < 60:
            return _no(self.strategy_id, "H20_INSUFFICIENT_BARS", "Cần ≥ 60 nến M5.")
        highs = [float(bar.high) for bar in bars]
        lows = [float(bar.low) for bar in bars]
        closes = [float(bar.close) for bar in bars]
        structure = market_structure(highs, lows, closes, left_bars=3, right_bars=3)
        last = len(bars) - 1
        recent = [
            item
            for item in structure.breaks
            if item.kind == "BOS" and last - 6 <= item.break_index < last
        ]
        if not recent:
            return _no(self.strategy_id, "H20_NO_RECENT_BOS", "Không có BOS trong 6 nến gần nhất.")
        brk = recent[-1]
        direction = "long" if brk.direction == "bullish" else "short"
        higher = context[0].bars
        trend = market_structure(
            [float(bar.high) for bar in higher],
            [float(bar.low) for bar in higher],
            [float(bar.close) for bar in higher],
            left_bars=3,
            right_bars=3,
        ).trend[-1]
        if trend != ("up" if direction == "long" else "down"):
            return _no(self.strategy_id, "H20_HTF_MISMATCH", "Xu hướng M15 không cùng chiều BOS.")
        deltas = [_delta(bar) for bar in bars[brk.break_index - 50 : brk.break_index + 1]]
        if any(value is None for value in deltas) or len(deltas) < 51:
            return _no(self.strategy_id, "H20_TAKER_UNAVAILABLE", "Thiếu taker buy quanh nến phá.")
        magnitudes = sorted(abs(value) for value in deltas[:-1] if value is not None)
        threshold = magnitudes[int(0.8 * (len(magnitudes) - 1))]
        if abs(deltas[-1] or 0.0) < threshold:
            return _no(
                self.strategy_id, "H20_WEAK_BREAK", "|Delta| nến phá dưới phân vị 80 của 50 nến."
            )
        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(self.strategy_id, "H20_FEATURE_UNAVAILABLE", "ATR14 phải dương.")
        level = brk.level
        touched = (
            float(current.low) <= level + 0.25 * atr
            if direction == "long"
            else float(current.high) >= level - 0.25 * atr
        )
        holds = (
            float(current.close) > level if direction == "long" else float(current.close) < level
        )
        deep = (
            float(current.low) < level - 0.5 * atr
            if direction == "long"
            else float(current.high) > level + 0.5 * atr
        )
        if not touched or not holds or deep:
            return _no(
                self.strategy_id, "H20_NO_RETEST", "Nến hiện tại chưa retest mức phá hợp lệ."
            )
        swings = confirmed_swings(highs, lows, left_bars=3, right_bars=3)
        opposite = [
            swing
            for swing in swings
            if swing.kind == ("low" if direction == "long" else "high")
            and swing.confirmed_index <= last
            and (
                swing.price < float(current.close)
                if direction == "long"
                else swing.price > float(current.close)
            )
        ]
        if not opposite:
            return _no(self.strategy_id, "H20_NO_SWING", "Chưa có swing đối diện để đặt stop.")
        stop_distance = abs(Decimal(str(opposite[-1].price)) - current.close)
        cost = _cost_gate(self.strategy_id, current.close, stop_distance)
        if cost:
            return cost
        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=current,
            direction=direction,
            atr=atr,
            recipe=_recipe(self.recipe_id, float(stop_distance) / atr),
            evidence=(features["atr_14"],),
            evidence_bars=(bars[brk.break_index], current),
            settings=settings,
            release_hash=release_content_hash(self),
        )


SCALPING_RULES = (H18SweepReclaimRule(), H19VwapRangeReversionRule(), H20BreakRetestRule())
SCALPING_LADDERS = {rule.release_id: SCALPING_LADDER for rule in SCALPING_RULES}


def ladder_for_release(release_id: str) -> TakeProfitLadder | None:
    """Thang chốt lời của release scalping; release khác giữ một mục tiêu theo recipe."""
    return SCALPING_LADDERS.get(release_id)
