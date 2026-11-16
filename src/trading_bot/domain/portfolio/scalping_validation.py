"""Báo cáo kiểm định scalping theo setup và kết luận đề xuất PAPER (SIG-5) — hàm thuần.

Quy ước (``sig5-validation-v1``):

- Một lệnh = một trade PAPER đã đóng của replay. ``R`` = PnL ròng (sau phí, trượt giá, funding)
  chia cho rủi ro ban đầu ``risk_amount`` = |giá vào thực tế − stop ban đầu| × khối lượng × hệ số
  hợp đồng. Thắng = PnL ròng > 0.
- Tỷ lệ hòa vốn = lỗ TB / (lãi TB + lỗ TB) theo PnL ròng (cùng công thức CK-10): tỷ lệ thắng tối
  thiểu để kỳ vọng ≥ 0 với cặp lãi/lỗ trung bình quan sát được.
- Walk-forward: cửa sổ đánh giá chia thành ``folds`` khối liền nhau theo thời gian, mỗi khối bắt
  đầu sau một khoảng ``embargo`` (lệnh mở trong embargo không tính ngoài mẫu). Rule H18–H20 có tham
  số **khóa trước khi chạy** (không tối ưu trên dữ liệu), nên mọi khối đều là ngoài mẫu; báo cáo
  theo khối để thấy độ ổn định chứ không để chọn tham số. Đổi tham số = một lượt thử mới.
- Phiên theo giờ mở lệnh (``active_sessions``, tự theo giờ mùa hè); một lệnh có thể thuộc 2 phiên
  khi chồng lấn London/New York; "off" khi không phiên nào mở. Giờ theo UTC.
- Cờ "chưa đủ mẫu" khi số lệnh ngoài mẫu < ``MIN_OUT_OF_SAMPLE_TRADES``.
- Hash báo cáo = SHA-256 của JSON chuẩn (khóa sắp xếp), không phụ thuộc thứ tự lệnh đầu vào.

Kết luận ``sig5-paper-criterion-v1`` (quyết định owner #5, khóa 16/11/2026 trước khi chạy): chỉ
**đề xuất** bật PAPER khi số lệnh ngoài mẫu ≥ 200 **và** kỳ vọng ròng ngoài mẫu > 0 ở kịch bản xấu
(trượt giá ×2, chỉ khớp taker). Đây không phải bằng chứng lợi nhuận tương lai; owner vẫn duyệt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.domain.market.overview import active_sessions

VALIDATION_VERSION = "sig5-validation-v1"
CRITERION_VERSION = "sig5-paper-criterion-v1"
MIN_OUT_OF_SAMPLE_TRADES = 200
R_BUCKETS = ("≤ −1R", "−1R…0", "0…1R", "1R…2R", "≥ 2R")
SESSIONS = ("asia", "london", "new_york", "off")
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ScalpTrade:
    """Một trade đã đóng của replay, đủ để tính R và phân nhóm."""

    trade_id: str
    setup_id: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    net_pnl: Decimal
    fees: Decimal
    risk_amount: Decimal

    def __post_init__(self) -> None:
        if self.opened_at.tzinfo is None or self.closed_at.tzinfo is None:
            raise ValueError("Thời điểm lệnh phải có timezone.")
        if self.closed_at < self.opened_at:
            raise ValueError("Lệnh đóng trước khi mở.")
        if self.risk_amount <= ZERO:
            raise ValueError("Khoản rủi ro ban đầu phải dương để tính R.")

    @property
    def r_multiple(self) -> Decimal:
        return self.net_pnl / self.risk_amount


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def walk_forward_folds(
    start: datetime, end: datetime, *, folds: int, embargo: timedelta
) -> tuple[Fold, ...]:
    """Chia ``[start, end)`` thành các khối liền nhau, mỗi khối mở sau ``embargo``."""
    if folds < 1:
        raise ValueError("Số fold phải ≥ 1.")
    if end <= start:
        raise ValueError("Cửa sổ walk-forward rỗng.")
    if embargo < timedelta(0):
        raise ValueError("Embargo không được âm.")
    span = (end - start) / folds
    if embargo >= span:
        raise ValueError("Embargo phải ngắn hơn độ dài mỗi fold.")
    result: list[Fold] = []
    for index in range(folds):
        block_start = start + span * index
        block_end = end if index == folds - 1 else start + span * (index + 1)
        result.append(Fold(index + 1, block_start + embargo, block_end))
    return tuple(result)


def _bucket(r: Decimal) -> str:
    if r <= -1:
        return R_BUCKETS[0]
    if r < 0:
        return R_BUCKETS[1]
    if r < 1:
        return R_BUCKETS[2]
    if r < 2:
        return R_BUCKETS[3]
    return R_BUCKETS[4]


def _stats(trades: Sequence[ScalpTrade]) -> dict[str, Any]:
    n = len(trades)
    pnls = [trade.net_pnl for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [-value for value in pnls if value < 0]
    average_win = sum(wins, ZERO) / len(wins) if wins else None
    average_loss = sum(losses, ZERO) / len(losses) if losses else None
    breakeven = (
        float(average_loss / (average_win + average_loss))
        if average_win is not None and average_loss is not None
        else None
    )
    total = sum(pnls, ZERO)
    r_values = [trade.r_multiple for trade in trades]
    return {
        "n": n,
        "wins": len(wins),
        "win_rate": len(wins) / n if n else None,
        "breakeven_win_rate": breakeven,
        "net_pnl": str(total),
        "fees": str(sum((trade.fees for trade in trades), ZERO)),
        "expectancy_net": str(total / n) if n else None,
        "expectancy_r": float(sum(r_values, ZERO) / n) if n else None,
    }


def _sessions(moment: datetime) -> tuple[str, ...]:
    keys = tuple(item.key for item in active_sessions(moment) if item.key in SESSIONS)
    return keys or ("off",)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def setup_report(
    setup_id: str, trades: Sequence[ScalpTrade], *, folds: Sequence[Fold]
) -> dict[str, Any]:
    """Báo cáo một setup trên các lệnh của một kịch bản chi phí."""
    if any(trade.setup_id != setup_id for trade in trades):
        raise ValueError("Mọi lệnh trong báo cáo phải cùng một setup.")
    if not folds:
        raise ValueError("Cần ít nhất một fold walk-forward.")
    ordered = sorted(trades, key=lambda trade: (trade.opened_at, trade.trade_id))
    in_folds = [[trade for trade in ordered if fold.contains(trade.opened_at)] for fold in folds]
    out_of_sample = [trade for block in in_folds for trade in block]
    distribution = dict.fromkeys(R_BUCKETS, 0)
    for trade in out_of_sample:
        distribution[_bucket(trade.r_multiple)] += 1
    by_session: dict[str, list[ScalpTrade]] = {key: [] for key in SESSIONS}
    by_hour: dict[str, list[ScalpTrade]] = {}
    for trade in out_of_sample:
        for key in _sessions(trade.opened_at):
            by_session[key].append(trade)
        by_hour.setdefault(str(trade.opened_at.hour), []).append(trade)
    body: dict[str, Any] = {
        "formula_version": VALIDATION_VERSION,
        "setup_id": setup_id,
        **_stats(out_of_sample),
        "n": len(ordered),
        "out_of_sample_n": len(out_of_sample),
        "min_sample": MIN_OUT_OF_SAMPLE_TRADES,
        "insufficient_sample": len(out_of_sample) < MIN_OUT_OF_SAMPLE_TRADES,
        "r_distribution": distribution,
        "by_session": {key: _stats(items) for key, items in by_session.items()},
        "by_hour_utc": {
            key: _stats(by_hour[key]) for key in sorted(by_hour, key=lambda item: int(item))
        },
        "folds": [
            {
                "index": fold.index,
                "start": fold.start.isoformat(),
                "end": fold.end.isoformat(),
                **_stats(block),
            }
            for fold, block in zip(folds, in_folds, strict=True)
        ],
        "trade_ids": [trade.trade_id for trade in ordered],
    }
    body["report_hash"] = hashlib.sha256(_canonical(body).encode()).hexdigest()
    return body


def paper_enable_verdict(*, base: dict[str, Any], stress: dict[str, Any]) -> dict[str, Any]:
    """Kết luận tiêu chí #5 từ báo cáo kịch bản gốc và kịch bản xấu của cùng setup."""
    if base["setup_id"] != stress["setup_id"]:
        raise ValueError("Hai báo cáo phải cùng một setup.")
    reasons: list[str] = []
    if stress["out_of_sample_n"] < MIN_OUT_OF_SAMPLE_TRADES:
        reasons.append("INSUFFICIENT_OUT_OF_SAMPLE_TRADES")
    expectancy = stress["expectancy_net"]
    if expectancy is None or Decimal(expectancy) <= 0:
        reasons.append("STRESS_EXPECTANCY_NOT_POSITIVE")
    return {
        "criterion_version": CRITERION_VERSION,
        "setup_id": base["setup_id"],
        "recommend_paper": not reasons,
        "reasons": reasons,
        "base_report_hash": base["report_hash"],
        "stress_report_hash": stress["report_hash"],
        "stress_out_of_sample_n": stress["out_of_sample_n"],
        "stress_expectancy_net": expectancy,
        "base_expectancy_net": base["expectancy_net"],
    }
