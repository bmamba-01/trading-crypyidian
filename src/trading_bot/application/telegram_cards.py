"""View-model thẻ ảnh Telegram (TG-3): dữ liệu thuần để vẽ thẻ tín hiệu, giao dịch và tổng kết.

Module không phụ thuộc thư viện vẽ hay mạng. Renderer (``adapters/telegram/card_renderer.py``)
chỉ đọc các dataclass bất biến ở đây; caption văn bản thuần cũng dựng từ cùng view để ảnh và chữ
luôn khớp nhau. Mọi số hiển thị theo kiểu Việt Nam (dấu chấm nhóm nghìn, dấu phẩy thập phân) và
thời gian theo giờ ``Asia/Ho_Chi_Minh``.

Thẻ chỉ để tham khảo/PAPER: không chứa lệnh, không phải khuyến nghị đầu tư.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from trading_bot.application.telegram_signals import TelegramSignalCandidate

CARD_VIEW_VERSION = "tg3-card-v1"
MAX_CAPTION_CHARS = 1024
MAX_CARD_TARGETS = 6
MAX_CARD_CHECKLIST = 8
MAX_CARD_INVALIDATION = 5
MAX_SUMMARY_ROWS = 10

_QUOTE_ASSETS = ("USDT", "USDC", "FDUSD", "BUSD", "USD")
_OPERATORS = {">=": "≥", "<=": "≤", "==": "=", "!=": "≠", "gte": "≥", "lte": "≤"}

Direction = Literal["LONG", "SHORT"]
TradeEvent = Literal["OPENED", "CLOSED"]


# --- Định dạng số và thời gian ---------------------------------------------------------------


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    return number if number.is_finite() else None


def _group_vi(number: Decimal, decimals: int, *, min_decimals: int) -> str:
    """Làm tròn nửa lên rồi đổi sang kiểu Việt Nam, bỏ số 0 thừa nhưng giữ ``min_decimals``."""
    quantum = Decimal(1).scaleb(-decimals)
    rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
    text = f"{rounded:,.{decimals}f}"
    if decimals > min_decimals and "." in text:
        whole, fraction = text.split(".")
        fraction = fraction.rstrip("0")
        if len(fraction) < min_decimals:
            fraction = fraction.ljust(min_decimals, "0")
        text = f"{whole}.{fraction}" if fraction else whole
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def format_price_vi(value: object) -> str:
    """Giá kiểu Việt Nam; giá < 1 giữ 5 chữ số có nghĩa (``0,00001234``, ``0,012346``)."""
    number = _to_decimal(value)
    if number is None:
        return "—"
    magnitude = abs(number)
    if magnitude >= 1000:
        return _group_vi(number, 2, min_decimals=2)
    if magnitude >= 1:
        return _group_vi(number, 4, min_decimals=2)
    if magnitude == 0:
        return "0"
    # adjusted() = số mũ của chữ số có nghĩa đầu tiên (0,00001234 → -5).
    decimals = min(12, -magnitude.adjusted() + 4)
    return _group_vi(number, decimals, min_decimals=0)


def format_number_vi(value: object, *, decimals: int = 4) -> str:
    """Số chung (khối lượng, giá trị checklist): tối đa ``decimals`` chữ số thập phân."""
    number = _to_decimal(value)
    if number is None:
        return "—" if value is None else str(value)
    return _group_vi(number, decimals, min_decimals=0)


def format_usdt_vi(value: object, *, signed: bool = False) -> str:
    """Tiền USDT 2 chữ số thập phân; ``signed`` thêm dấu + cho số dương (lãi/lỗ)."""
    number = _to_decimal(value)
    if number is None:
        return "—"
    text = _group_vi(abs(number), 2, min_decimals=2)
    if number < 0 and text != "0,00":
        return f"−{text} USDT"
    if signed and number > 0 and text != "0,00":
        return f"+{text} USDT"
    return f"{text} USDT"


def format_r_vi(value: object, *, signed: bool = False) -> str:
    """Bội số R một chữ số thập phân."""
    number = _to_decimal(value)
    if number is None:
        return "—"
    text = _group_vi(abs(number), 1, min_decimals=1)
    sign = "−" if number < 0 and text != "0,0" else ("+" if signed and number > 0 else "")
    return f"{sign}{text}R"


def format_percent_vi(fraction: object, *, signed: bool = False, decimals: int = 1) -> str:
    """Tỉ lệ dạng phân số (0,523) → ``52,3%``."""
    number = _to_decimal(fraction)
    if number is None:
        return "—"
    text = _group_vi(abs(number) * 100, decimals, min_decimals=decimals)
    zero = _group_vi(Decimal(0), decimals, min_decimals=decimals)
    sign = "−" if number < 0 and text != zero else ("+" if signed and number > 0 else "")
    return f"{sign}{text}%"


def _local_zone() -> timezone | ZoneInfo:
    try:
        return ZoneInfo("Asia/Ho_Chi_Minh")
    except ZoneInfoNotFoundError:
        # Việt Nam không dùng giờ mùa hè; UTC+7 cố định là dự phòng an toàn khi thiếu tzdata.
        return timezone(timedelta(hours=7))


def format_local_time(value: datetime, *, with_date: bool = True) -> str:
    """Giờ Việt Nam rút gọn, ví dụ ``15/11 14:30``; datetime phải có múi giờ."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Thời điểm hiển thị phải có múi giờ.")
    local = value.astimezone(_local_zone())
    return local.strftime("%d/%m %H:%M" if with_date else "%H:%M")


def format_duration_vi(delta: timedelta) -> str:
    """Thời gian giữ lệnh: ``2 ngày 3 giờ``, ``3 giờ 05 phút``, ``12 phút``."""
    seconds = max(0, int(delta.total_seconds()))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes = rest // 60
    if days:
        return f"{days} ngày {hours} giờ"
    if hours:
        return f"{hours} giờ {minutes:02d} phút"
    if minutes:
        return f"{minutes} phút"
    return "dưới 1 phút"


def base_asset(symbol: str) -> str:
    """Tên coin cơ sở từ mã hợp đồng (``BTCUSDT`` → ``BTC``, ``1000PEPEUSDT`` → ``1000PEPE``)."""
    upper = symbol.upper()
    for quote in _QUOTE_ASSETS:
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)]
    return upper


def clean_text(value: object, *, limit: int = 300) -> str:
    """Chuẩn hóa NFC (dấu tiếng Việt dựng sẵn), bỏ ký tự điều khiển và giới hạn độ dài."""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_id(value: object) -> str:
    """Mã rút gọn 8 ký tự cho người đọc đối chiếu (giá trị đầy đủ nằm ở caption/log)."""
    return str(value).replace("-", "")[:8]


# --- View thẻ tín hiệu -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardTarget:
    """Một mốc giá mục tiêu; ``source`` là ``recipe`` hoặc ``r_ladder`` (mốc R tham chiếu)."""

    label: str
    price: Decimal
    r_multiple: Decimal | None
    source: str = "r_ladder"


@dataclass(frozen=True, slots=True)
class CardChecklistItem:
    """Một điều kiện của rule: giá trị thực, phép so, ngưỡng; ``passed`` None = chưa xác định."""

    label: str
    actual: str
    operator: str
    threshold: str
    passed: bool | None


@dataclass(frozen=True, slots=True)
class SignalCardView:
    """Dữ liệu thẻ tín hiệu tham khảo. ``stop`` None khi candidate không dựng được thang giá."""

    decision_id: str
    symbol: str
    direction: Direction
    timeframe: str
    strategy_id: str
    strategy_release_id: str
    reason_code: str
    detail_vi: str
    reference_price: Decimal
    entry_low: Decimal
    entry_high: Decimal
    stop: Decimal | None
    signal_at: datetime
    expires_at: datetime
    targets: tuple[CardTarget, ...] = ()
    checklist: tuple[CardChecklistItem, ...] = ()
    checklist_passed: int | None = None
    checklist_total: int | None = None
    invalidation: tuple[str, ...] = ()
    confidence_label: str = "Chưa hiệu chuẩn"
    sample: bool = False

    def __post_init__(self) -> None:
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction chỉ nhận LONG hoặc SHORT.")
        for name in ("signal_at", "expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} phải có múi giờ.")
        if self.entry_low > self.entry_high:
            raise ValueError("Vùng vào có cận dưới lớn hơn cận trên.")

    @property
    def coin(self) -> str:
        return base_asset(self.symbol)


def _checklist_value(value: object) -> str:
    if isinstance(value, bool):
        return "đúng" if value else "sai"
    if isinstance(value, int | float | Decimal):
        return format_number_vi(value)
    return clean_text(value, limit=40) if value is not None else "—"


def _operator(value: object) -> str:
    text = clean_text(value, limit=12) if value is not None else ""
    return _OPERATORS.get(text, text)


def signal_card_view_from(
    candidate: TelegramSignalCandidate, *, sample: bool = False
) -> SignalCardView:
    """Chuyển candidate (kèm ``card`` SIG-3 nếu có) thành view; thiếu thẻ thì chỉ có giá tham chiếu.

    Chỉ đọc giá trị đã tính sẵn trong ``card`` (``build_signal_card``), không tính lại SL/TP.
    """
    card: Mapping[str, object] = candidate.card or {}
    reference = candidate.reference_price
    entry_low = entry_high = reference
    zone = card.get("entry_zone")
    if isinstance(zone, Mapping):
        low, high = _to_decimal(zone.get("low")), _to_decimal(zone.get("high"))
        if low is not None and high is not None and low <= high:
            entry_low, entry_high = low, high
    stop = _to_decimal(card.get("stop"))
    targets: list[CardTarget] = []
    raw_targets = card.get("targets")
    if isinstance(raw_targets, Sequence) and not isinstance(raw_targets, str):
        for item in raw_targets:
            if not isinstance(item, Mapping):
                continue
            price = _to_decimal(item.get("price"))
            if price is None:
                continue
            targets.append(
                CardTarget(
                    label=clean_text(item.get("label") or "Mục tiêu", limit=60),
                    price=price,
                    r_multiple=_to_decimal(item.get("r_multiple")),
                    source=str(item.get("source") or "r_ladder"),
                )
            )
    checklist: list[CardChecklistItem] = []
    raw_checklist = card.get("checklist")
    if isinstance(raw_checklist, Sequence) and not isinstance(raw_checklist, str):
        for item in raw_checklist:
            if not isinstance(item, Mapping):
                continue
            passed = item.get("passed")
            checklist.append(
                CardChecklistItem(
                    label=clean_text(item.get("label_vi") or item.get("label") or "Điều kiện"),
                    actual=_checklist_value(item.get("actual")),
                    operator=_operator(item.get("operator")),
                    threshold=_checklist_value(item.get("threshold")),
                    passed=passed if isinstance(passed, bool) else None,
                )
            )
    invalidation: list[str] = []
    raw_invalidation = card.get("invalidation")
    if isinstance(raw_invalidation, Sequence) and not isinstance(raw_invalidation, str):
        invalidation = [clean_text(line) for line in raw_invalidation if line]
    if not invalidation and candidate.invalidation:
        invalidation = [clean_text(candidate.invalidation)]
    confidence = card.get("confidence")
    confidence_label = "Chưa hiệu chuẩn"
    if isinstance(confidence, Mapping) and confidence.get("label"):
        confidence_label = clean_text(confidence["label"], limit=40)
    passed_count = card.get("checklist_passed")
    total_count = card.get("checklist_total")
    return SignalCardView(
        decision_id=str(candidate.decision_id),
        symbol=candidate.symbol,
        direction="LONG" if candidate.direction == "LONG" else "SHORT",
        timeframe=candidate.timeframe,
        strategy_id=clean_text(candidate.strategy_id, limit=80),
        strategy_release_id=clean_text(candidate.strategy_release_id, limit=80),
        reason_code=clean_text(candidate.reason_code, limit=80),
        detail_vi=clean_text(candidate.detail_vi),
        reference_price=reference,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        signal_at=candidate.signal_at,
        expires_at=candidate.expires_at,
        targets=tuple(targets),
        checklist=tuple(checklist),
        checklist_passed=passed_count if isinstance(passed_count, int) else None,
        checklist_total=total_count if isinstance(total_count, int) else None,
        invalidation=tuple(invalidation),
        confidence_label=confidence_label,
        sample=sample,
    )


# --- View thẻ giao dịch ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeCardView:
    """Thông báo vị thế PAPER: ``OPENED`` (mở) hoặc ``CLOSED`` (đóng, có PnL ròng và R)."""

    event: TradeEvent
    trade_id: str
    bot_name: str
    symbol: str
    direction: Direction
    strategy_id: str
    timeframe: str
    entry_price: Decimal
    quantity: Decimal
    opened_at: datetime
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    exit_price: Decimal | None = None
    exit_reason_vi: str | None = None
    net_pnl: Decimal | None = None
    r_multiple: Decimal | None = None
    closed_at: datetime | None = None
    fees: Decimal | None = None
    sample: bool = False

    def __post_init__(self) -> None:
        if self.event not in ("OPENED", "CLOSED"):
            raise ValueError("event chỉ nhận OPENED hoặc CLOSED.")
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction chỉ nhận LONG hoặc SHORT.")
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("opened_at phải có múi giờ.")
        if self.event == "CLOSED":
            if self.exit_price is None or self.net_pnl is None or self.closed_at is None:
                raise ValueError("Thẻ đóng vị thế cần exit_price, net_pnl và closed_at.")
            if self.closed_at.tzinfo is None or self.closed_at.utcoffset() is None:
                raise ValueError("closed_at phải có múi giờ.")

    @property
    def coin(self) -> str:
        return base_asset(self.symbol)

    @property
    def notional(self) -> Decimal:
        """Giá trị danh nghĩa lúc vào = giá vào × khối lượng."""
        return self.entry_price * self.quantity

    @property
    def risk_to_stop(self) -> Decimal | None:
        """Lỗ thô nếu chạm SL (chưa gồm phí/trượt giá)."""
        if self.stop_price is None:
            return None
        return abs(self.entry_price - self.stop_price) * self.quantity

    @property
    def hold_time(self) -> timedelta | None:
        return None if self.closed_at is None else self.closed_at - self.opened_at


# --- View thẻ tổng kết -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SummaryBotRow:
    """Một bot trong cửa sổ tổng kết. ``max_drawdown`` là phân số (0,084 = 8,4%)."""

    bot_name: str
    strategy_id: str
    timeframe: str
    trades_closed: int
    wins: int
    net_pnl: Decimal
    open_positions: int
    max_drawdown: Decimal | None = None

    def __post_init__(self) -> None:
        if self.trades_closed < 0 or not 0 <= self.wins <= self.trades_closed:
            raise ValueError("Số lệnh/lệnh thắng không hợp lệ.")
        if self.open_positions < 0:
            raise ValueError("open_positions không được âm.")

    @property
    def win_rate(self) -> Decimal | None:
        if self.trades_closed == 0:
            return None
        return Decimal(self.wins) / Decimal(self.trades_closed)


@dataclass(frozen=True, slots=True)
class SummaryCardView:
    """Bảng so sánh tối đa ``MAX_SUMMARY_ROWS`` bot, xếp theo PnL ròng giảm dần khi vẽ."""

    window_label: str
    generated_at: datetime
    rows: tuple[SummaryBotRow, ...] = field(default=())
    sample: bool = False

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at phải có múi giờ.")

    def ranked(self) -> tuple[SummaryBotRow, ...]:
        """Xếp PnL giảm dần, hòa thì theo tên để kết quả xác định."""
        return tuple(sorted(self.rows, key=lambda row: (-row.net_pnl, row.bot_name)))

    @property
    def total_pnl(self) -> Decimal:
        return sum((row.net_pnl for row in self.rows), Decimal(0))

    @property
    def total_trades(self) -> int:
        return sum(row.trades_closed for row in self.rows)

    @property
    def total_open(self) -> int:
        return sum(row.open_positions for row in self.rows)

    @property
    def overall_win_rate(self) -> Decimal | None:
        trades = self.total_trades
        if trades == 0:
            return None
        return Decimal(sum(row.wins for row in self.rows)) / Decimal(trades)


# --- Caption văn bản thuần (≤ 1024 ký tự, không parse mode) ------------------------------------


def _fit_caption(lines: Sequence[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= MAX_CAPTION_CHARS:
        return text
    return text[: MAX_CAPTION_CHARS - 1] + "…"


def signal_card_caption(view: SignalCardView) -> str:
    """Tóm tắt ngắn đi kèm ảnh thẻ tín hiệu; chi tiết đầy đủ nằm trong ảnh."""
    zone = format_price_vi(view.entry_low)
    if view.entry_high != view.entry_low:
        zone += f" – {format_price_vi(view.entry_high)}"
    lines = [
        "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH (PAPER)",
        f"{view.symbol} · {view.direction} · khung {view.timeframe}",
        f"Vùng vào: {zone}",
    ]
    if view.stop is not None:
        lines.append(f"SL: {format_price_vi(view.stop)}")
    if view.targets:
        lines.append(
            "Mục tiêu: "
            + " · ".join(
                f"{format_price_vi(t.price)} ({format_r_vi(t.r_multiple)})"
                for t in view.targets[:MAX_CARD_TARGETS]
            )
        )
    lines.append(
        f"Hết hạn: {format_local_time(view.expires_at)} (giờ VN) · Độ tin cậy: "
        f"{view.confidence_label}"
    )
    lines.append(f"Mã tín hiệu: {view.decision_id}")
    if view.sample:
        lines.append("DỮ LIỆU MẪU")
    lines.append("Mô phỏng nội bộ, không phải khuyến nghị đầu tư.")
    return _fit_caption(lines)


def trade_card_caption(view: TradeCardView) -> str:
    """Tóm tắt thông báo vị thế PAPER đi kèm ảnh."""
    lines = ["KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT"]
    head = f"{view.bot_name} · {view.symbol} {view.direction}"
    if view.event == "OPENED":
        lines.append(f"Đã mở vị thế: {head}")
        lines.append(
            f"Giá vào {format_price_vi(view.entry_price)} · KL {format_number_vi(view.quantity)}"
            f" · danh nghĩa {format_usdt_vi(view.notional)}"
        )
        if view.stop_price is not None or view.target_price is not None:
            lines.append(
                f"SL {format_price_vi(view.stop_price)} · TP {format_price_vi(view.target_price)}"
            )
    else:
        lines.append(f"Đã đóng vị thế: {head}")
        lines.append(
            f"PnL ròng {format_usdt_vi(view.net_pnl, signed=True)}"
            + (
                f" ({format_r_vi(view.r_multiple, signed=True)})"
                if view.r_multiple is not None
                else ""
            )
        )
        lines.append(
            f"Vào {format_price_vi(view.entry_price)} → ra {format_price_vi(view.exit_price)}"
            + (f" · {view.exit_reason_vi}" if view.exit_reason_vi else "")
        )
    lines.append(f"Mã giao dịch: {view.trade_id}")
    if view.sample:
        lines.append("DỮ LIỆU MẪU")
    return _fit_caption(lines)


def summary_card_caption(view: SummaryCardView) -> str:
    """Tóm tắt bảng tổng kết: tổng PnL, số lệnh và bot dẫn đầu."""
    ranked = view.ranked()
    lines = [
        f"TỔNG KẾT PAPER · {view.window_label}",
        f"{len(view.rows)} bot · {view.total_trades} lệnh đóng · PnL ròng "
        f"{format_usdt_vi(view.total_pnl, signed=True)}",
    ]
    if ranked:
        best = ranked[0]
        lines.append(f"Dẫn đầu: {best.bot_name} ({format_usdt_vi(best.net_pnl, signed=True)})")
    lines.append(f"Tạo lúc {format_local_time(view.generated_at)} (giờ VN)")
    if view.sample:
        lines.append("DỮ LIỆU MẪU")
    lines.append("Mô phỏng nội bộ, không phải kết quả giao dịch thật.")
    return _fit_caption(lines)


# --- View thẻ phân tích AI (AI-2) -------------------------------------------------------------

MAX_ANALYSIS_ITEMS = 6
_CONFIDENCE_VI = {"low": "thấp", "medium": "trung bình", "high": "cao"}


@dataclass(frozen=True, slots=True)
class AnalysisCardView:
    """Kết quả AI Analyst **đã qua validator số liệu**; chỉ là gợi ý, phải backtest trước."""

    topic_key: str
    confidence: str
    window_start: datetime
    window_end: datetime
    summary: str
    findings: tuple[str, ...] = ()
    hypotheses: tuple[tuple[str, str], ...] = ()
    proposals: tuple[tuple[str, str, str], ...] = ()
    report_hash: str = ""
    engine: str = ""
    sample: bool = False
    # Lịch vĩ mô (AI-4): dữ liệu tất định từ lịch chính thức, không phải lời AI.
    macro_events: tuple[str, ...] = ()
    macro_horizon_days: int = 0
    macro_blackout: bool = False
    # Niêm yết (AI-4 lát 3): coin trong danh mục bot bị Binance lên lịch gỡ/không còn TRADING.
    listing_alerts: tuple[str, ...] = ()

    @property
    def confidence_vi(self) -> str:
        return _CONFIDENCE_VI.get(self.confidence, self.confidence)

    @property
    def topic_label(self) -> str:
        """``family:H03`` → ``Họ H03``; giữ nguyên chủ đề lạ."""
        kind, _, name = self.topic_key.partition(":")
        return f"Họ {name}" if kind == "family" and name else self.topic_key


def analysis_card_view_from(
    topic_key: str, report: Mapping[str, object], output: Mapping[str, object]
) -> AnalysisCardView:
    """Dựng view từ Run Report theo chủ đề và đầu ra AI đã hợp lệ."""
    window = report.get("window") or {}
    assert isinstance(window, Mapping)
    findings = output.get("findings") or []
    hypotheses = output.get("hypotheses") or []
    proposals = output.get("proposals") or []
    assert isinstance(findings, list | tuple)
    assert isinstance(hypotheses, list | tuple)
    assert isinstance(proposals, list | tuple)
    return AnalysisCardView(
        topic_key=topic_key,
        confidence=str(output.get("confidence", "")),
        window_start=datetime.fromisoformat(str(window.get("start"))),
        window_end=datetime.fromisoformat(str(window.get("end"))),
        summary=clean_text(output.get("summary", ""), limit=600),
        findings=tuple(clean_text(item.get("statement", ""), limit=400) for item in findings),
        hypotheses=tuple(
            (
                clean_text(item.get("statement", ""), limit=400),
                clean_text(item.get("how_to_test", ""), limit=300),
            )
            for item in hypotheses
        ),
        proposals=tuple(
            (
                clean_text(item.get("bot_name", ""), limit=80),
                clean_text(
                    ", ".join(
                        f"{change.get('field')} → {change.get('to')}"
                        for change in item.get("changes") or ()
                    ),
                    limit=200,
                ),
                clean_text(item.get("risk", ""), limit=300),
            )
            for item in proposals
        ),
        report_hash=str(report.get("report_hash", "")),
        **_macro_fields(report.get("macro_calendar")),
        listing_alerts=_listing_alerts(report.get("listings")),
    )


MAX_MACRO_EVENTS = 5
_LISTING_STATUS_VI = {
    "MISSING": "không còn trong danh sách hợp đồng",
    "SETTLING": "đã ngừng giao dịch (đang tất toán)",
    "PENDING_TRADING": "chưa mở giao dịch",
    "TRADING_HALT": "tạm dừng giao dịch",
    "TRADING_CANCEL_ONLY": "chỉ cho hủy lệnh",
}


def _listing_alerts(listings: object) -> tuple[str, ...]:
    if not isinstance(listings, Mapping):
        return ()
    alerts = []
    for item in listings.get("watched_at_risk") or ():
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status"))
        when = item.get("delist_at_vn")
        text = (
            f"{item.get('symbol')}: Binance lên lịch gỡ lúc {when} (giờ VN)"
            if status == "TRADING" and when
            else f"{item.get('symbol')}: {_LISTING_STATUS_VI.get(status, status)}"
        )
        alerts.append(clean_text(text, limit=120))
    return tuple(alerts[:MAX_MACRO_EVENTS])


def _macro_fields(macro: object) -> dict[str, Any]:
    if not isinstance(macro, Mapping):
        return {}
    upcoming = macro.get("upcoming") or []
    state = macro.get("state") or {}
    horizon = macro.get("horizon_days")
    return {
        "macro_events": tuple(
            clean_text(
                f"{item.get('vn_date')} {item.get('vn_time')} · {item.get('label_vi')}", limit=80
            )
            for item in list(upcoming)[:MAX_MACRO_EVENTS]
            if isinstance(item, Mapping)
        ),
        "macro_horizon_days": horizon if isinstance(horizon, int) else 0,
        "macro_blackout": isinstance(state, Mapping) and state.get("blackout") is True,
    }


def analysis_card_caption(view: AnalysisCardView) -> str:
    """Caption ngắn kèm ảnh thẻ phân tích AI."""
    lines = [
        "PHÂN TÍCH AI (PAPER) — CHỈ LÀ GỢI Ý, PHẢI BACKTEST TRƯỚC",
        f"{view.topic_label} · Độ tin cậy: {view.confidence_vi}",
        view.summary,
    ]
    if view.proposals:
        lines.append(f"{len(view.proposals)} đề xuất để backtest (không tự áp).")
    if view.listing_alerts:
        lines.append(f"CẢNH BÁO niêm yết: {view.listing_alerts[0]}")
    if view.macro_blackout:
        lines.append("Đang trong khoảng cấm vào lệnh quanh sự kiện vĩ mô.")
    if view.macro_events:
        lines.append(f"Vĩ mô kế tiếp (giờ VN): {view.macro_events[0]}")
    lines.append(f"Report {view.report_hash[:12]} · số liệu đã đối chiếu với Run Report.")
    return _fit_caption(lines)


# --- Thẻ backtest đề xuất AI (AI-3) -------------------------------------------------------------

_VERDICT_VI = {
    "BETTER": "Bản đề xuất tốt hơn",
    "WORSE": "Bản đề xuất kém hơn",
    "INCONCLUSIVE": "Chưa kết luận",
    "UNSUPPORTED": "Không backtest được",
}


@dataclass(frozen=True, slots=True)
class BacktestCardView:
    """So sánh bản gốc/bản đề xuất của một đề xuất AI trên cùng dữ liệu nến đã lưu."""

    bot_name: str
    verdict: str
    changes: str
    baseline: Mapping[str, object]
    variant: Mapping[str, object]
    window_start: datetime | None
    window_end: datetime | None
    notes: tuple[str, ...] = ()
    skipped: int = 0
    sample: bool = False

    @property
    def verdict_vi(self) -> str:
        return _VERDICT_VI.get(self.verdict, self.verdict)

    def rows(self) -> list[tuple[str, str, str]]:
        """(chỉ số, bản gốc, bản đề xuất) đã định dạng."""

        def ratio(value: object) -> str:
            return "—" if value is None else format_percent_vi(value)

        def pf(value: object) -> str:
            return "—" if value is None else format_number_vi(value, decimals=2)

        base, var = self.baseline, self.variant
        return [
            ("Coin đã chạy", str(base.get("symbols", 0)), str(var.get("symbols", 0))),
            ("Số lệnh", str(base.get("trades", 0)), str(var.get("trades", 0))),
            ("Tỷ lệ thắng", ratio(base.get("win_rate")), ratio(var.get("win_rate"))),
            ("Profit factor", pf(base.get("profit_factor")), pf(var.get("profit_factor"))),
            (
                "PnL ròng",
                format_usdt_vi(base.get("net_pnl", "0"), signed=True),
                format_usdt_vi(var.get("net_pnl", "0"), signed=True),
            ),
            (
                "Drawdown lớn nhất/coin",
                format_usdt_vi(base.get("max_drawdown", "0")),
                format_usdt_vi(var.get("max_drawdown", "0")),
            ),
        ]


def backtest_card_view_from(
    proposal: Mapping[str, object], result: Mapping[str, object]
) -> BacktestCardView:
    """Payload ``proposal_backtest`` → view thẻ; không thêm số nào ngoài kết quả replay."""

    def seq(value: object) -> Sequence[object]:
        return value if isinstance(value, Sequence) and not isinstance(value, str) else ()

    def mapping(value: object) -> Mapping[str, object]:
        return value if isinstance(value, Mapping) else {}

    window = seq(result.get("window"))
    changes = seq(proposal.get("changes"))
    notes = seq(result.get("notes"))
    return BacktestCardView(
        bot_name=clean_text(str(result.get("bot_name", "")), limit=80),
        verdict=str(result.get("verdict", "INCONCLUSIVE")),
        changes=clean_text(
            ", ".join(
                f"{change.get('field')} → {change.get('to')}"
                for change in changes
                if isinstance(change, Mapping)
            ),
            limit=200,
        ),
        baseline=mapping(result.get("baseline")),
        variant=mapping(result.get("variant")),
        window_start=datetime.fromisoformat(str(window[0])) if len(window) == 2 else None,
        window_end=datetime.fromisoformat(str(window[1])) if len(window) == 2 else None,
        notes=tuple(clean_text(str(note), limit=200) for note in notes[:MAX_ANALYSIS_ITEMS]),
        skipped=len(seq(result.get("skipped_symbols"))),
    )


def backtest_card_caption(view: BacktestCardView) -> str:
    """Caption ngắn kèm ảnh thẻ backtest đề xuất."""
    rows = {label: (base, var) for label, base, var in view.rows()}
    lines = [
        "BACKTEST ĐỀ XUẤT AI (PAPER) — KHÔNG TỰ ÁP VÀO BOT",
        f"{view.bot_name}: {view.changes}",
        f"Kết luận: {view.verdict_vi}",
        f"Số lệnh {rows['Số lệnh'][0]} → {rows['Số lệnh'][1]}; "
        f"PnL ròng {rows['PnL ròng'][0]} → {rows['PnL ròng'][1]}",
        "Muốn áp dụng: tạo bản nháp config rồi publish trên Admin (MFA).",
    ]
    return _fit_caption(lines)
