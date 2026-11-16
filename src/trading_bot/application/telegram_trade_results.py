"""TG-3: kết quả giao dịch PAPER gửi Telegram — view model thuần + bộ định dạng văn bản.

Tách hai tầng để sau này thay bằng ảnh thẻ (PNG) mà không đổi đường giao nhận:

- ``build_trade_result_view`` / ``build_paper_report_view`` dựng *view model* bất biến chứa mọi
  trường (tên bot, symbol, hướng, giá, PnL ròng, bội số R, lý do thoát, mốc thời gian, hàng
  tổng hợp theo bot). Không định dạng chuỗi ở tầng này.
- ``format_trade_result_text`` / ``format_paper_report_text`` biến view model thành văn bản thuần
  (không parse mode, ≤ 4096 ký tự). Bản văn bản là caption/fallback khi gửi ảnh.

Mọi số liệu là dữ kiện ledger PAPER đã ghi; module này không đọc DB và không phát lệnh.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

TELEGRAM_TEXT_LIMIT = 4096
PAPER_RESULT_HEADER = "KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT"
PAPER_REPORT_HEADER = "TỔNG HỢP KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT"
PAPER_FOOTER = "Mô phỏng PAPER nội bộ; không có lệnh nào được gửi lên sàn."
# Việt Nam dùng UTC+7 cố định (không có giờ mùa hè) nên không cần dữ liệu tzdata.
VIETNAM_TZ = timezone(timedelta(hours=7), "ICT")
# Cửa sổ "tổng" của bảng tổng hợp: trùng giới hạn cửa sổ báo cáo MB-06 (366 ngày) để lệnh
# `/report` (qua BotReportingService) và tin định kỳ của signal-worker cho cùng số liệu.
SUMMARY_TOTAL_WINDOW = timedelta(days=366)
_ZERO = Decimal(0)

# Mã lý do thoát do position monitor/gateway PAPER ghi trong ``position_event.payload``.
EXIT_REASON_VI: dict[str, str] = {
    "STOP_LOSS": "Chạm dừng lỗ (SL)",
    "TAKE_PROFIT": "Chạm mục tiêu chốt lời",
    "TRAILING_STOP": "Chạm dừng lỗ trượt (trailing stop)",
    "CHANNEL_EXIT": "Thoát theo kênh giá",
    "TIME_STOP": "Hết thời gian giữ lệnh",
    "FEATURE_EXIT": "Thoát theo điều kiện đặc trưng của chiến lược",
    "OPPOSING_SIGNAL": "Có tín hiệu ngược chiều",
    "PROTECTIVE_EXIT": "Thoát bảo vệ",
    "REDUCE_ONLY": "Giảm vị thế (reduce-only)",
    "FLATTEN_OPERATION": "Đóng toàn bộ theo thao tác vận hành",
    "DELIST_EXIT": "Đóng trước khi Binance gỡ hợp đồng",
}


class TradeNotificationKind(StrEnum):
    """Loại thông báo kết quả: mở vị thế (fill vào) hoặc đóng trade."""

    OPENED = "OPENED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class TradeFillFact:
    """Một fill PAPER đã ghi: vai trò ENTRY/EXIT, khối lượng, giá và thời điểm."""

    role: str
    quantity: Decimal
    price: Decimal
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TradeResultSource:
    """Dữ kiện thô đọc từ ledger cho một thông báo; chưa tính toán gì."""

    kind: TradeNotificationKind
    bot_id: UUID
    bot_name: str
    trade_id: UUID
    symbol: str
    side: str
    quote_asset: str
    status: str
    opened_at: datetime
    closed_at: datetime | None
    net_pnl: Decimal
    fills: tuple[TradeFillFact, ...]
    reserved_risk: Decimal | None = None
    exit_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TradeResultView:
    """View model một thông báo mở/đóng trade PAPER (dùng cho văn bản hoặc ảnh thẻ)."""

    kind: TradeNotificationKind
    bot_id: UUID
    bot_name: str
    trade_id: UUID
    symbol: str
    direction: str
    quote_asset: str
    entry_price: Decimal
    entry_quantity: Decimal
    opened_at: datetime
    exit_price: Decimal | None = None
    exit_quantity: Decimal | None = None
    closed_at: datetime | None = None
    exit_reason_code: str | None = None
    exit_reason_vi: str | None = None
    net_pnl: Decimal | None = None
    risk_amount: Decimal | None = None
    r_multiple: Decimal | None = None
    hold_duration: timedelta | None = None

    def as_dict(self) -> dict[str, Any]:
        """Dạng JSON ổn định cho renderer ảnh; Decimal/thời gian thành chuỗi."""

        def text(value: object) -> str | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, timedelta):
                return str(int(value.total_seconds()))
            return str(value)

        return {
            "kind": self.kind.value,
            "bot_id": str(self.bot_id),
            "bot_name": self.bot_name,
            "trade_id": str(self.trade_id),
            "symbol": self.symbol,
            "direction": self.direction,
            "quote_asset": self.quote_asset,
            "entry_price": text(self.entry_price),
            "entry_quantity": text(self.entry_quantity),
            "opened_at": text(self.opened_at),
            "exit_price": text(self.exit_price),
            "exit_quantity": text(self.exit_quantity),
            "closed_at": text(self.closed_at),
            "exit_reason_code": self.exit_reason_code,
            "exit_reason_vi": self.exit_reason_vi,
            "net_pnl": text(self.net_pnl),
            "risk_amount": text(self.risk_amount),
            "r_multiple": text(self.r_multiple),
            "hold_seconds": text(self.hold_duration),
        }


def _weighted_price(fills: Iterable[TradeFillFact]) -> tuple[Decimal, Decimal] | None:
    rows = list(fills)
    quantity = sum((fill.quantity for fill in rows), _ZERO)
    if quantity <= 0:
        return None
    notional = sum((fill.quantity * fill.price for fill in rows), _ZERO)
    return notional / quantity, quantity


def r_multiple(net_pnl: Decimal, risk_amount: Decimal | None) -> Decimal | None:
    """Bội số R = PnL ròng ÷ 1R (``reserved_risk`` = ``estimated_loss`` đã duyệt); 2 chữ số."""
    if risk_amount is None or not risk_amount.is_finite() or risk_amount <= 0:
        return None
    return (net_pnl / risk_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_trade_result_view(source: TradeResultSource) -> TradeResultView:
    """Dựng view model từ dữ kiện ledger; thiếu fill vào hoặc trade chưa đóng thì từ chối."""
    direction = source.side.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("Hướng trade PAPER phải là LONG hoặc SHORT.")
    entry = _weighted_price(fill for fill in source.fills if fill.role == "ENTRY")
    if entry is None:
        raise ValueError("Trade PAPER chưa có fill vào lệnh.")
    entry_price, entry_quantity = entry
    if source.kind is TradeNotificationKind.OPENED:
        return TradeResultView(
            kind=source.kind,
            bot_id=source.bot_id,
            bot_name=source.bot_name,
            trade_id=source.trade_id,
            symbol=source.symbol,
            direction=direction,
            quote_asset=source.quote_asset,
            entry_price=entry_price,
            entry_quantity=entry_quantity,
            opened_at=source.opened_at,
            risk_amount=source.reserved_risk,
        )
    if source.status != "CLOSED" or source.closed_at is None:
        raise ValueError("Thông báo đóng lệnh cần trade PAPER đã CLOSED.")
    exit_fill = _weighted_price(fill for fill in source.fills if fill.role == "EXIT")
    reason = source.exit_reason
    return TradeResultView(
        kind=source.kind,
        bot_id=source.bot_id,
        bot_name=source.bot_name,
        trade_id=source.trade_id,
        symbol=source.symbol,
        direction=direction,
        quote_asset=source.quote_asset,
        entry_price=entry_price,
        entry_quantity=entry_quantity,
        opened_at=source.opened_at,
        exit_price=None if exit_fill is None else exit_fill[0],
        exit_quantity=None if exit_fill is None else exit_fill[1],
        closed_at=source.closed_at,
        exit_reason_code=reason,
        exit_reason_vi=None if reason is None else EXIT_REASON_VI.get(reason, reason),
        net_pnl=source.net_pnl,
        risk_amount=source.reserved_risk,
        r_multiple=r_multiple(source.net_pnl, source.reserved_risk),
        hold_duration=max(source.closed_at - source.opened_at, timedelta(0)),
    )


# --- định dạng văn bản ----------------------------------------------------------------------


def format_number(value: Decimal, *, places: int = 8) -> str:
    """Số thập phân gọn: làm tròn tối đa ``places`` chữ số, bỏ số 0 thừa, không dùng mũ."""
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def format_signed(value: Decimal, *, places: int = 2) -> str:
    """Số có dấu (+/−) để PnL luôn thấy rõ lãi hay lỗ."""
    text = format_number(value, places=places)
    return text if text.startswith("-") or text == "0" else f"+{text}"


def format_vn_time(value: datetime) -> str:
    """Thời điểm theo giờ Việt Nam, ví dụ ``15/11/2026 13:05 (giờ VN)``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(VIETNAM_TZ).strftime("%d/%m/%Y %H:%M") + " (giờ VN)"


def format_duration(value: timedelta) -> str:
    """Thời lượng tiếng Việt: ``2 ngày 3 giờ``, ``1 giờ 5 phút`` hoặc ``45 giây``."""
    seconds = max(int(value.total_seconds()), 0)
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} ngày {hours} giờ"
    if hours:
        return f"{hours} giờ {minutes} phút"
    if minutes:
        return f"{minutes} phút"
    return f"{secs} giây"


def _limit(message: str) -> str:
    if len(message) > TELEGRAM_TEXT_LIMIT:
        raise ValueError("Nội dung kết quả PAPER vượt giới hạn Telegram.")
    return message


def format_trade_result_text(view: TradeResultView) -> str:
    """Văn bản thuần cho thông báo mở/đóng trade PAPER (caption hoặc fallback)."""
    quote = view.quote_asset
    lines = [PAPER_RESULT_HEADER, f"Bot: {view.bot_name}"]
    if view.kind is TradeNotificationKind.OPENED:
        lines += [
            f"Mở vị thế PAPER: {view.symbol} · {view.direction}",
            f"Giá vào: {format_number(view.entry_price)} · "
            f"Khối lượng: {format_number(view.entry_quantity)}",
            f"Thời điểm vào: {format_vn_time(view.opened_at)}",
        ]
        if view.risk_amount is not None:
            lines.append(
                f"Rủi ro dự kiến (1R): {format_number(view.risk_amount, places=2)} {quote}"
            )
    else:
        lines += [
            f"Đóng lệnh PAPER: {view.symbol} · {view.direction}",
            f"Giá vào: {format_number(view.entry_price)} · "
            f"Khối lượng: {format_number(view.entry_quantity)}",
        ]
        if view.exit_price is not None:
            lines.append(f"Giá ra: {format_number(view.exit_price)}")
        reason = view.exit_reason_vi or "Không rõ"
        if view.exit_reason_code and view.exit_reason_code != view.exit_reason_vi:
            reason = f"{reason} ({view.exit_reason_code})"
        lines.append(f"Lý do thoát: {reason}")
        if view.net_pnl is not None:
            lines.append(f"PnL ròng (sau phí): {format_signed(view.net_pnl)} {quote}")
        lines.append(
            "Bội số R: "
            + ("không tính được" if view.r_multiple is None else f"{view.r_multiple:+}R")
        )
        if view.hold_duration is not None:
            lines.append(f"Thời gian giữ lệnh: {format_duration(view.hold_duration)}")
        if view.closed_at is not None:
            lines.append(f"Thời điểm đóng: {format_vn_time(view.closed_at)}")
    lines += ["", PAPER_FOOTER]
    return _limit("\n".join(lines))


# --- tổng hợp định kỳ ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClosedTradeFact:
    """Một trade PAPER đã đóng dùng cho thống kê: PnL ròng và thời điểm đóng."""

    trade_id: UUID
    net_pnl: Decimal
    closed_at: datetime


@dataclass(frozen=True, slots=True)
class BotSummaryRow:
    """Một hàng của bảng tổng hợp: số liệu một bot trong cửa sổ và trong ``SUMMARY_TOTAL``."""

    bot_id: UUID
    bot_name: str
    quote_asset: str
    window_closed_count: int
    window_net_pnl: Decimal
    total_closed_count: int
    total_winning_count: int
    total_net_pnl: Decimal
    open_positions: int
    max_drawdown: Decimal

    @property
    def win_rate_percent(self) -> Decimal | None:
        """Tỷ lệ thắng (%) trên trade đã đóng của cửa sổ tổng; chưa có trade thì ``None``."""
        if self.total_closed_count == 0:
            return None
        return (Decimal(self.total_winning_count * 100) / self.total_closed_count).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )

    def as_dict(self) -> dict[str, Any]:
        rate = self.win_rate_percent
        return {
            "bot_id": str(self.bot_id),
            "bot_name": self.bot_name,
            "quote_asset": self.quote_asset,
            "window_closed_count": self.window_closed_count,
            "window_net_pnl": str(self.window_net_pnl),
            "total_closed_count": self.total_closed_count,
            "total_winning_count": self.total_winning_count,
            "win_rate_percent": None if rate is None else str(rate),
            "total_net_pnl": str(self.total_net_pnl),
            "open_positions": self.open_positions,
            "max_drawdown": str(self.max_drawdown),
        }


@dataclass(frozen=True, slots=True)
class PaperReportView:
    """View model tin tổng hợp: cửa sổ ``[window_start, window_end)`` và các hàng theo bot.

    ``rows`` đã sắp theo PnL ròng tổng giảm dần (hòa thì theo tên bot).
    """

    window_start: datetime
    window_end: datetime
    total_start: datetime
    rows: tuple[BotSummaryRow, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "total_start": self.total_start.isoformat(),
            "rows": [row.as_dict() for row in self.rows],
        }


def realized_max_drawdown(trades: Sequence[ClosedTradeFact]) -> Decimal:
    """Sụt giảm lớn nhất của PnL ròng tích lũy (đỉnh xuất phát từ 0), theo thứ tự đóng."""
    cumulative = peak = worst = _ZERO
    for trade in sorted(trades, key=lambda item: (item.closed_at, str(item.trade_id))):
        cumulative += trade.net_pnl
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def build_bot_summary_row(
    *,
    bot_id: UUID,
    bot_name: str,
    closed_trades: Sequence[ClosedTradeFact],
    open_positions: int,
    window_start: datetime,
    window_end: datetime,
    quote_asset: str = "USDT",
) -> BotSummaryRow:
    """Tính một hàng tổng hợp từ trade đã đóng trong cửa sổ tổng (đã lọc sẵn theo thời gian)."""
    in_window = [t for t in closed_trades if window_start <= t.closed_at < window_end]
    return BotSummaryRow(
        bot_id=bot_id,
        bot_name=bot_name,
        quote_asset=quote_asset,
        window_closed_count=len(in_window),
        window_net_pnl=sum((t.net_pnl for t in in_window), _ZERO),
        total_closed_count=len(closed_trades),
        total_winning_count=sum(1 for t in closed_trades if t.net_pnl > 0),
        total_net_pnl=sum((t.net_pnl for t in closed_trades), _ZERO),
        open_positions=open_positions,
        max_drawdown=realized_max_drawdown(closed_trades),
    )


def build_paper_report_view(
    *,
    window_start: datetime,
    window_end: datetime,
    rows: Iterable[BotSummaryRow],
    total_start: datetime | None = None,
) -> PaperReportView:
    """Sắp các bot theo PnL ròng tổng giảm dần để so sánh hiệu suất."""
    if not window_start < window_end:
        raise ValueError("Cửa sổ tổng hợp cần start < end.")
    ordered = sorted(rows, key=lambda row: (-row.total_net_pnl, row.bot_name, str(row.bot_id)))
    return PaperReportView(
        window_start=window_start,
        window_end=window_end,
        total_start=total_start or window_end - SUMMARY_TOTAL_WINDOW,
        rows=tuple(ordered),
    )


def report_window(now: datetime, interval: timedelta) -> tuple[datetime, datetime]:
    """Cửa sổ đã khép gần nhất, căn theo mốc epoch UTC: ``[end - interval, end)``.

    Cùng một ``interval`` thì mọi replica/lần khởi động lại trong cùng cửa sổ cho cùng cặp mốc,
    nên khóa idempotent ``(chat, window_start)`` không sinh tin trùng.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Thời gian tổng hợp phải có múi giờ.")
    step = int(interval.total_seconds())
    if step < 60:
        raise ValueError("Chu kỳ tổng hợp tối thiểu 1 phút.")
    epoch = int(now.astimezone(UTC).timestamp())
    end = datetime.fromtimestamp(epoch - epoch % step, tz=UTC)
    return end - interval, end


def _summary_line(index: int, row: BotSummaryRow) -> list[str]:
    rate = row.win_rate_percent
    quote = row.quote_asset
    return [
        f"{index}. {row.bot_name}",
        f"   Trong kỳ: {row.window_closed_count} lệnh đóng · "
        f"PnL ròng {format_signed(row.window_net_pnl)} {quote}",
        f"   Tổng: {row.total_closed_count} lệnh đóng · thắng "
        + ("-" if rate is None else f"{format_number(rate, places=1)}%")
        + f" · PnL ròng {format_signed(row.total_net_pnl)} {quote}",
        f"   Đang mở: {row.open_positions} · Sụt giảm tối đa: "
        f"{format_number(row.max_drawdown, places=2)} {quote}",
    ]


def format_paper_report_text(view: PaperReportView) -> str:
    """Văn bản thuần tin tổng hợp; quá dài thì cắt bớt bot cuối và ghi số bot bị lược."""
    head = [
        PAPER_REPORT_HEADER,
        f"Kỳ: {format_vn_time(view.window_start)} → {format_vn_time(view.window_end)}",
        "Xếp theo PnL ròng tổng (sau phí), tổng tính trong 366 ngày gần nhất.",
        "",
    ]
    tail = ["", PAPER_FOOTER]
    if not view.rows:
        return _limit("\n".join([*head, "Chưa có bot nào để tổng hợp.", *tail]))
    body: list[str] = []
    for index, row in enumerate(view.rows, start=1):
        block = _summary_line(index, row)
        omitted = len(view.rows) - index + 1
        candidate = "\n".join([*head, *body, *block, f"(Lược bớt {omitted} bot)", *tail])
        if len(candidate) > TELEGRAM_TEXT_LIMIT:
            body.append(f"(Lược bớt {omitted} bot)")
            break
        body.extend(block)
    return _limit("\n".join([*head, *body, *tail]))
