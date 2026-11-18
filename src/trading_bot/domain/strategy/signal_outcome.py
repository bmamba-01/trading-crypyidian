"""Chấm kết quả tín hiệu đã phát (CRY-430, phương pháp ``sig-outcome-v2``).

Mỗi tín hiệu (``bot_decision`` CANDIDATE, kể cả tin bị lọc/SUPPRESSED hoặc không thành lệnh) được
chấm tại các horizon cố định 1×, 4×, 24× khung vào lệnh, chỉ bằng **nến đã đóng sau nến tín hiệu**
và đã khả dụng tại lúc chấm (point-in-time, không nhìn trước).

Quy ước ``sig-outcome-v2`` (v1 + kiểm giá tham chiếu, 17/11/2026):

- **Mức giá** lấy đúng như thẻ tín hiệu Telegram (``build_signal_card`` không kế hoạch): giá vào =
  giá tham chiếu (giá đóng nến tín hiệu), SL theo recipe (tham chiếu ∓ ATR × hệ số stop), TP1/TP2 =
  hai mục tiêu đầu của thẻ (mốc 1R/2R hoặc mục tiêu recipe nếu gần hơn). 1R = |vào − SL|.
- **Chạm** dùng đỉnh/đáy nến. Nến mở vượt SL/TP (gap) thì giá thoát là giá mở (xấu hơn với SL,
  tốt hơn với TP). Một nến chạm **cả SL lẫn TP1** mà không biết thứ tự trong nến → giả định SL
  trước (thận trọng) và đặt cờ ``same_bar_conflict``. TP2 chỉ tính là chạm khi không có SL ở nến
  trước hoặc cùng nến đó.
- **outcome_r** (R theo bracket SL/TP1): SL trước → R tại giá thoát; TP1 trước → R tại giá thoát;
  chưa chạm gì → R theo giá đóng nến cuối horizon (``close_r``).
- **MFE/MAE** (biên thuận/bất lợi lớn nhất) tính trên toàn horizon theo R, kể cả sau khi chạm.
- **Nhãn** dải R ``signal-gross-r-band-v1``: |outcome_r| ≤ 0,1R → 0 (hòa), lớn hơn → +1 (đúng),
  nhỏ hơn → −1 (sai). Cùng dải ±0,1R với nhãn lệnh ``net-r-band-v1`` của AI-5 nhưng là R **thô**
  (tín hiệu không có phí/trượt giá), nên không trộn lẫn với nhãn lệnh thật.
- **Mất hiệu lực** (invalidated) = giá chạm SL bất kỳ lúc nào trong horizon.
- **Kiểm giá tham chiếu** (từ v2): theo định nghĩa thẻ, giá tham chiếu là giá đóng nến tín hiệu. Nếu
  nến tín hiệu đã lưu có giá đóng lệch quá ``REFERENCE_TOLERANCE`` (0,5%) thì tín hiệu không khớp
  dữ liệu thị trường (ví dụ dữ liệu giả lập lẫn vào workspace) → ``INVALID_SIGNAL``, không chấm. v1
  thiếu bước này nên tín hiệu giả giá 100 trên nến BTC thật cho R hàng vạn (phát hiện trên stack
  local 17/11). Không tìm thấy nến tín hiệu thì vẫn chấm (nguồn nến có thể thiếu nến cũ).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trading_bot.domain.strategy.signal_card import SignalCardError, build_signal_card

METHOD_VERSION = "sig-outcome-v2"
LABEL_VERSION = "signal-gross-r-band-v1"
HORIZON_MULTIPLES: tuple[int, ...] = (1, 4, 24)
R_BAND_HALF_WIDTH = Decimal("0.1")
REFERENCE_TOLERANCE = Decimal("0.005")
_R_QUANTUM = Decimal("0.0001")


class SignalOutcomeError(ValueError):
    """Dữ liệu tín hiệu hoặc nến không dùng được để chấm."""


class FirstTouch(StrEnum):
    TP1 = "TP1"
    SL = "SL"
    NONE = "NONE"


class OutcomeStatus(StrEnum):
    """``SCORED`` có số liệu; ``DATA_GAP``/``INVALID_SIGNAL`` là hàng đánh dấu không chấm được."""

    SCORED = "SCORED"
    DATA_GAP = "DATA_GAP"
    INVALID_SIGNAL = "INVALID_SIGNAL"


@dataclass(frozen=True, slots=True)
class SignalLevels:
    direction: str
    entry: Decimal
    stop: Decimal
    tp1: Decimal
    tp2: Decimal | None

    def __post_init__(self) -> None:
        if self.direction not in ("long", "short"):
            raise SignalOutcomeError("Hướng tín hiệu không hợp lệ.")
        if self.risk <= 0:
            raise SignalOutcomeError("Khoảng rủi ro phải dương.")
        sign = self.sign
        if (self.stop - self.entry) * sign >= 0 or (self.tp1 - self.entry) * sign <= 0:
            raise SignalOutcomeError("SL/TP1 nằm sai phía so với giá vào.")
        if self.tp2 is not None and (self.tp2 - self.tp1) * sign <= 0:
            raise SignalOutcomeError("TP2 phải xa hơn TP1.")

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self.direction == "long" else Decimal(-1)

    @property
    def risk(self) -> Decimal:
        return abs(self.entry - self.stop)

    def r_at(self, price: Decimal) -> Decimal:
        return ((price - self.entry) * self.sign / self.risk).quantize(_R_QUANTUM)


@dataclass(frozen=True, slots=True)
class ForwardBar:
    """Nến đã đóng sau nến tín hiệu (chỉ trường cần để chấm)."""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    ref: str = ""


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    horizon_bars: int
    horizon_end_at: datetime
    first_touch: FirstTouch
    same_bar_conflict: bool
    tp1_hit: bool
    tp2_hit: bool
    invalidated: bool
    bars_to_first_touch: int | None
    seconds_to_first_touch: int | None
    mfe_r: Decimal
    mae_r: Decimal
    close_r: Decimal
    outcome_r: Decimal
    label: int
    input_hash: str


def levels_from_candidate(candidate: Mapping[str, Any]) -> SignalLevels:
    """Mức giá của thẻ tín hiệu (không kế hoạch) — cùng quy ước với tin Telegram đã gửi."""
    try:
        card = build_signal_card(candidate)
    except (SignalCardError, KeyError, TypeError) as error:
        raise SignalOutcomeError(f"Không dựng được thẻ tín hiệu: {error}") from error
    targets = [Decimal(str(item["price"])) for item in card["targets"]]
    if not targets:
        raise SignalOutcomeError("Thẻ tín hiệu không có mục tiêu.")
    return SignalLevels(
        direction=str(card["direction"]),
        entry=Decimal(str(candidate["reference_price"])),
        stop=Decimal(str(card["stop"])),
        tp1=targets[0],
        tp2=targets[1] if len(targets) > 1 else None,
    )


def r_band_label(r_multiple: Decimal) -> int:
    """Nhãn 3 lớp theo dải ±0,1R (cùng ngưỡng với ``net-r-band-v1`` của AI-5)."""
    if abs(r_multiple) <= R_BAND_HALF_WIDTH:
        return 0
    return 1 if r_multiple > 0 else -1


def reference_matches(reference_price: Decimal, signal_bar_close: Decimal) -> bool:
    """Giá tham chiếu khớp giá đóng nến tín hiệu trong sai số tương đối ``REFERENCE_TOLERANCE``."""
    if signal_bar_close <= 0:
        return False
    return abs(reference_price - signal_bar_close) / signal_bar_close <= REFERENCE_TOLERANCE


def signal_bar(
    bars: Sequence[ForwardBar], *, signal_at: datetime, interval: timedelta
) -> ForwardBar | None:
    """Nến chứa mốc ``signal_at`` (nến tín hiệu đóng tại đó), nếu có trong ``bars``."""
    for bar in bars:
        if bar.open_time <= signal_at < bar.open_time + interval:
            return bar
    return None


def contiguous_window(
    bars: Sequence[ForwardBar], *, signal_at: datetime, interval: timedelta, horizon_bars: int
) -> tuple[ForwardBar, ...] | None:
    """``horizon_bars`` nến liền mạch ngay sau nến tín hiệu; thiếu hoặc hở thì trả ``None``.

    Nến đầu phải mở đúng sau ``signal_at`` (đóng nến tín hiệu) trong một khoảng < 1 khung.
    """
    if horizon_bars < 1:
        raise SignalOutcomeError("horizon_bars phải ≥ 1.")
    ordered = sorted((bar for bar in bars if bar.open_time > signal_at), key=lambda b: b.open_time)
    if len(ordered) < horizon_bars:
        return None
    window = tuple(ordered[:horizon_bars])
    if window[0].open_time - signal_at > interval:
        return None
    for previous, current in zip(window, window[1:], strict=False):
        if current.open_time - previous.open_time != interval:
            return None
    return window


def _input_hash(levels: SignalLevels, window: Sequence[ForwardBar]) -> str:
    canonical = json.dumps(
        {
            "method": METHOD_VERSION,
            "levels": [str(levels.entry), str(levels.stop), str(levels.tp1), str(levels.tp2)],
            "direction": levels.direction,
            "bars": [
                [bar.open_time.isoformat(), str(bar.open), str(bar.high), str(bar.low),
                 str(bar.close), bar.ref]
                for bar in window
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )  # fmt: skip
    return hashlib.sha256(canonical.encode()).hexdigest()


def score_signal(
    levels: SignalLevels,
    *,
    signal_at: datetime,
    window: Sequence[ForwardBar],
) -> SignalOutcome:
    """Chấm một horizon trên cửa sổ nến liền mạch (do ``contiguous_window`` trả về)."""
    if not window:
        raise SignalOutcomeError("Cửa sổ nến rỗng.")
    long = levels.direction == "long"

    def favorable(bar: ForwardBar) -> Decimal:
        return bar.high if long else bar.low

    def adverse(bar: ForwardBar) -> Decimal:
        return bar.low if long else bar.high

    def reached(price: Decimal, level: Decimal, *, toward_profit: bool) -> bool:
        if toward_profit:
            return price >= level if long else price <= level
        return price <= level if long else price >= level

    first_touch = FirstTouch.NONE
    exit_price: Decimal | None = None
    touch_index: int | None = None
    same_bar_conflict = False
    tp2_hit = False
    sl_seen = False
    for index, bar in enumerate(window):
        sl_now = reached(adverse(bar), levels.stop, toward_profit=False)
        tp1_now = reached(favorable(bar), levels.tp1, toward_profit=True)
        tp2_now = levels.tp2 is not None and reached(favorable(bar), levels.tp2, toward_profit=True)
        if first_touch is FirstTouch.NONE and (sl_now or tp1_now):
            touch_index = index
            if reached(bar.open, levels.stop, toward_profit=False):
                # Gap qua SL: thoát ở giá mở (xấu hơn SL).
                first_touch, exit_price = FirstTouch.SL, bar.open
            elif reached(bar.open, levels.tp1, toward_profit=True):
                # Gap qua TP1: thoát ở giá mở (tốt hơn TP1).
                first_touch, exit_price = FirstTouch.TP1, bar.open
            elif sl_now:
                # Cả SL và TP1 cùng nến mà không biết thứ tự: giả định SL trước (thận trọng).
                same_bar_conflict = tp1_now
                first_touch, exit_price = FirstTouch.SL, levels.stop
            else:
                first_touch, exit_price = FirstTouch.TP1, levels.tp1
        if first_touch is FirstTouch.TP1 and not sl_seen and not tp2_hit and tp2_now:
            gap_through_tp2 = levels.tp2 is not None and reached(
                bar.open, levels.tp2, toward_profit=True
            )
            tp2_hit = gap_through_tp2 or not sl_now
        sl_seen = sl_seen or sl_now
    mfe = max(levels.r_at(favorable(bar)) for bar in window)
    mae = min(levels.r_at(adverse(bar)) for bar in window)
    close_r = levels.r_at(window[-1].close)
    outcome_r = close_r if exit_price is None else levels.r_at(exit_price)
    seconds = None
    if touch_index is not None:
        seconds = int((window[touch_index].close_time - signal_at).total_seconds())
    return SignalOutcome(
        horizon_bars=len(window),
        horizon_end_at=window[-1].close_time,
        first_touch=first_touch,
        same_bar_conflict=same_bar_conflict,
        tp1_hit=first_touch is FirstTouch.TP1,
        tp2_hit=tp2_hit,
        invalidated=sl_seen,
        bars_to_first_touch=None if touch_index is None else touch_index + 1,
        seconds_to_first_touch=seconds,
        mfe_r=max(mfe, Decimal(0)),
        mae_r=min(mae, Decimal(0)),
        close_r=close_r,
        outcome_r=outcome_r,
        label=r_band_label(outcome_r),
        input_hash=_input_hash(levels, window),
    )


__all__ = [
    "HORIZON_MULTIPLES",
    "LABEL_VERSION",
    "METHOD_VERSION",
    "R_BAND_HALF_WIDTH",
    "FirstTouch",
    "ForwardBar",
    "OutcomeStatus",
    "SignalLevels",
    "SignalOutcome",
    "SignalOutcomeError",
    "REFERENCE_TOLERANCE",
    "contiguous_window",
    "levels_from_candidate",
    "reference_matches",
    "signal_bar",
    "r_band_label",
    "score_signal",
]
