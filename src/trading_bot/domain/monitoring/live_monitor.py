"""Mô hình hiển thị cho màn hình theo dõi bot trực tiếp (LIVE-MON, CRY-429).

Module thuần (không I/O): nhận các hàng đã đọc từ ledger PAPER/outbox dưới RLS và dựng
- dòng sự kiện (nến đóng → quyết định → reservation → lệnh → khớp → dời SL → đóng),
- vị thế mở với giá mark, khoảng cách tới SL/TP, R hiện tại và thời gian giữ,
- vốn/drawdown ước tính theo mark,
- sức khỏe nguồn dữ liệu (độ trễ feed, outbox, đồng hồ) và đánh giá ngắt mạch.

Mọi số tiền xuất ra dạng chuỗi Decimal để không mất độ chính xác khi qua JSON. Thiếu dữ kiện
thì trả ``None`` (giao diện ghi "chưa có dữ liệu"), không suy diễn số liệu.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from trading_bot.domain.risk.circuit_breaker import (
    CircuitBreakerEngine,
    CircuitBreakerPolicy,
    OperationalTelemetry,
)

# Hàng ghi trễ (transaction dài commit sau) có thể mang created_at sớm hơn con trỏ; đọc chồng
# lấn một cửa sổ rồi khử trùng theo id để không bỏ sót sự kiện.
CURSOR_OVERLAP = timedelta(seconds=30)
# Nến 1m của feed đóng mỗi 60 giây; breaker chỉ xét phần trễ vượt nhịp này.
FEED_BAR_INTERVAL_SECONDS = 60
FEED_OK_SECONDS = 90
FEED_WARN_SECONDS = 300
MARK_OK_SECONDS = 30
MARK_WARN_SECONDS = 120
CLOCK_WARN_MS = 500
CLOCK_STALE_MS = 1000
OUTBOX_WARN_SECONDS = 30
OUTBOX_STALE_SECONDS = 60
EQUITY_CURVE_POINTS = 200

EXIT_REASON_LABELS_VI = {
    "STOP_LOSS": "Dừng lỗ",
    "TAKE_PROFIT": "Chốt lời",
    "TIME_STOP": "Đóng theo thời gian",
    "OPPOSING_SIGNAL": "Tín hiệu ngược chiều",
    "CHANNEL_EXIT": "Thoát theo kênh giá",
    "FEATURE_EXIT": "Thoát theo chỉ báo",
    "FLATTEN_OPERATION": "Đóng theo thao tác quản trị",
    "DELIST_EXIT": "Đóng trước khi Binance gỡ hợp đồng",
}
# Chạm SL/TP là sự kiện cần chú ý ngay (highlight + âm thanh nếu người xem bật).
_ALERT_EXIT_REASONS = frozenset({"STOP_LOSS", "TAKE_PROFIT"})
_SIDE_LABELS_VI = {"LONG": "Mua (long)", "SHORT": "Bán (short)"}


class LiveEventKind(StrEnum):
    """Loại sự kiện trên dòng thời gian Live."""

    DECISION_CANDIDATE = "DECISION_CANDIDATE"
    DECISION_ABSTAIN = "DECISION_ABSTAIN"
    RESERVATION = "RESERVATION"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ENTRY_FILL = "ENTRY_FILL"
    POSITION_OPENED = "POSITION_OPENED"
    STOP_MOVED = "STOP_MOVED"
    EXIT_FILL = "EXIT_FILL"
    POSITION_CLOSED = "POSITION_CLOSED"
    FUNDING = "FUNDING"


class LiveSeverity(StrEnum):
    """Mức chú ý: ``muted`` (bỏ qua tín hiệu) < ``info`` < ``notice`` (lệnh/khớp) < ``alert``."""

    MUTED = "muted"
    INFO = "info"
    NOTICE = "notice"
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class LiveCursor:
    """Con trỏ dòng sự kiện theo thời điểm ghi DB (``created_at``), dùng làm SSE ``id``."""

    recorded_at: datetime

    def encode(self) -> str:
        return self.recorded_at.astimezone(UTC).isoformat()

    def query_floor(self) -> datetime:
        return self.recorded_at - CURSOR_OVERLAP

    @classmethod
    def parse(cls, raw: str | None) -> LiveCursor | None:
        """Đọc con trỏ từ ``Last-Event-ID``/query; giá trị lạ hoặc không có múi giờ bị bỏ."""
        if not raw or len(raw) > 64:
            return None
        try:
            # Query string chưa mã hóa biến "+00:00" thành " 00:00"; khôi phục dấu múi giờ.
            value = datetime.fromisoformat(raw.strip().replace(" ", "+"))
        except ValueError:
            return None
        if value.tzinfo is None:
            return None
        return cls(value.astimezone(UTC))


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    return result if result.is_finite() else None


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    # normalize() có thể ra số mũ (1E+1); format "f" giữ dạng thập phân thường.
    return format(normalized, "f")


def _fixed(value: Decimal | None, places: int = 2) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _iso(value: object) -> str | None:
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else None


def _side_sign(side: object) -> int:
    return -1 if str(side or "").upper() in {"SHORT", "SELL"} else 1


def exit_reason_label_vi(reason: object) -> str:
    if not isinstance(reason, str) or not reason:
        return "Chưa có lý do"
    return EXIT_REASON_LABELS_VI.get(reason, reason)


def r_multiple(net_pnl: Decimal | None, planned_risk: Decimal | None) -> Decimal | None:
    """Bội số R = PnL ròng / rủi ro dự trù của kế hoạch; thiếu rủi ro dương thì không tính."""
    if net_pnl is None or planned_risk is None or planned_risk <= 0:
        return None
    return net_pnl / planned_risk


def _event(
    row: Mapping[str, Any],
    *,
    kind: LiveEventKind,
    severity: LiveSeverity,
    title_vi: str,
    detail_vi: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recorded_at = row["recorded_at"]
    return {
        "id": f"{row['source']}:{row['id']}",
        "cursor": LiveCursor(recorded_at).encode(),
        "kind": kind.value,
        "severity": severity.value,
        "bot_id": str(row.get("bot_id")),
        "symbol": str(row.get("symbol") or ""),
        "side": str(row.get("side") or ""),
        "occurred_at": _iso(row.get("occurred_at")) or _iso(recorded_at),
        "recorded_at": _iso(recorded_at),
        "title_vi": title_vi,
        "detail_vi": detail_vi,
        "metrics": metrics or {},
    }


def _decision_event(row: Mapping[str, Any]) -> dict[str, Any]:
    reason = str(row.get("reason_code") or "")
    detail = str(row.get("detail_vi") or "").strip()
    bar_at = row.get("bar_close_at")
    # Nến đóng lúc hh:mm:59.999 → hiển thị mốc phút kế tiếp (giờ đóng thực của nến).
    bar = (
        (bar_at + timedelta(milliseconds=1)).astimezone(UTC).strftime("%H:%M UTC")
        if isinstance(bar_at, datetime)
        else None
    )
    prefix = f"Nến đóng {bar}. " if bar else ""
    if str(row.get("status")) == "CANDIDATE":
        side = _SIDE_LABELS_VI.get(str(row.get("side") or "").upper(), "")
        return _event(
            row,
            kind=LiveEventKind.DECISION_CANDIDATE,
            severity=LiveSeverity.INFO,
            title_vi=f"Tín hiệu vào lệnh {side}".strip(),
            detail_vi=f"{prefix}{detail or 'Chiến lược xác nhận ứng viên.'} ({reason})",
            metrics={"reason_code": reason},
        )
    return _event(
        row,
        kind=LiveEventKind.DECISION_ABSTAIN,
        severity=LiveSeverity.MUTED,
        title_vi="Bỏ qua tín hiệu",
        detail_vi=f"{prefix}{detail + ' ' if detail else ''}Lý do: {reason}",
        metrics={"reason_code": reason},
    )


def _position_event(row: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(row.get("event_type") or "")
    if event_type == "STOP_UPDATED":
        stop = _decimal(row.get("stop_price"))
        return _event(
            row,
            kind=LiveEventKind.STOP_MOVED,
            severity=LiveSeverity.INFO,
            title_vi="Dời dừng lỗ",
            detail_vi=f"SL mới {_text(stop) or 'chưa rõ'}",
            metrics={"stop_price": _text(stop)},
        )
    if event_type == "CLOSED":
        reason = row.get("exit_reason")
        net = _decimal(row.get("net_pnl"))
        funding = _decimal(row.get("funding_total"))
        r_value = r_multiple(net, _decimal(row.get("planned_risk")))
        asset = str(row.get("quote_asset") or "")
        severity = LiveSeverity.ALERT if reason in _ALERT_EXIT_REASONS else LiveSeverity.NOTICE
        pnl_text = f"{_text(net)} {asset}".strip() if net is not None else "chưa chốt"
        r_text = f", {_fixed(r_value)}R" if r_value is not None else ""
        return _event(
            row,
            kind=LiveEventKind.POSITION_CLOSED,
            severity=severity,
            title_vi=f"Đóng vị thế — {exit_reason_label_vi(reason)}",
            detail_vi=f"PnL ròng {pnl_text}{r_text}",
            metrics={
                "exit_reason": reason if isinstance(reason, str) else None,
                "net_pnl": _text(net),
                "funding": _text(funding),
                "r_multiple": _fixed(r_value),
                "quote_asset": asset or None,
            },
        )
    return _event(
        row,
        kind=LiveEventKind.POSITION_OPENED,
        severity=LiveSeverity.INFO,
        title_vi="Mở vị thế",
        detail_vi="Vị thế PAPER đã mở theo fill vào lệnh.",
    )


def _fill_event(row: Mapping[str, Any]) -> dict[str, Any]:
    price = _decimal(row.get("price"))
    quantity = _decimal(row.get("quantity"))
    entry = str(row.get("fill_role")) == "ENTRY"
    return _event(
        row,
        kind=LiveEventKind.ENTRY_FILL if entry else LiveEventKind.EXIT_FILL,
        severity=LiveSeverity.NOTICE,
        title_vi="Khớp lệnh vào" if entry else "Khớp lệnh thoát",
        detail_vi=f"{_text(quantity) or '?'} @ {_text(price) or '?'}",
        metrics={"price": _text(price), "quantity": _text(quantity)},
    )


def _simple_event(row: Mapping[str, Any]) -> dict[str, Any] | None:
    source = row.get("source")
    if source == "reservation":
        risk = _decimal(row.get("reserved_risk"))
        return _event(
            row,
            kind=LiveEventKind.RESERVATION,
            severity=LiveSeverity.INFO,
            title_vi="Giữ chỗ rủi ro (reservation)",
            detail_vi=f"Rủi ro giữ chỗ {_text(risk) or '?'}",
            metrics={"reserved_risk": _text(risk)},
        )
    if source == "order":
        quantity = _decimal(row.get("quantity"))
        side = _SIDE_LABELS_VI.get(str(row.get("side") or "").upper(), str(row.get("side") or ""))
        return _event(
            row,
            kind=LiveEventKind.ORDER_ACCEPTED,
            severity=LiveSeverity.NOTICE,
            title_vi="Lệnh PAPER mới",
            detail_vi=f"{side} khối lượng {_text(quantity) or '?'}",
            metrics={"quantity": _text(quantity)},
        )
    if source == "expired":
        return _event(
            row,
            kind=LiveEventKind.ORDER_EXPIRED,
            severity=LiveSeverity.INFO,
            title_vi="Lệnh hết hạn, không khớp",
            detail_vi="Reservation được nhả lại.",
        )
    if source == "funding":
        amount = _decimal(row.get("amount"))
        currency = str(row.get("currency") or "")
        return _event(
            row,
            kind=LiveEventKind.FUNDING,
            severity=LiveSeverity.INFO,
            title_vi="Thanh toán funding",
            detail_vi=f"{_text(amount) or '?'} {currency}".strip(),
            metrics={"funding": _text(amount)},
        )
    return None


def build_live_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Chuẩn hóa hàng từ nhiều bảng thành sự kiện, xếp theo thời điểm ghi rồi id."""
    events: list[tuple[datetime, str, dict[str, Any]]] = []
    for row in rows:
        source = row.get("source")
        if not isinstance(row.get("recorded_at"), datetime):
            continue
        event: dict[str, Any] | None
        if source == "decision":
            event = _decision_event(row)
        elif source == "position":
            event = _position_event(row)
        elif source == "fill":
            event = _fill_event(row)
        else:
            event = _simple_event(row)
        if event is not None:
            events.append((row["recorded_at"], event["id"], event))
    events.sort(key=lambda item: (item[0], item[1]))
    return [event for _at, _id, event in events]


def dedupe_events(
    events: Sequence[dict[str, Any]], seen: dict[str, None], *, limit: int = 5000
) -> list[dict[str, Any]]:
    """Loại sự kiện đã gửi (do đọc chồng lấn); ``seen`` là tập có thứ tự, giữ tối đa ``limit``."""
    fresh: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event["id"])
        if event_id in seen:
            continue
        seen[event_id] = None
        fresh.append(event)
    while len(seen) > limit:
        seen.pop(next(iter(seen)))
    return fresh


def build_position_view(position: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Vị thế mở: giá vào bình quân, PnL chưa chốt theo mark, khoảng cách SL/TP, R, giờ giữ."""
    sign = _side_sign(position.get("side"))
    entry_qty = Decimal(0)
    notional = Decimal(0)
    for fill in position.get("entry_fills") or ():
        price = _decimal(fill.get("price"))
        quantity = _decimal(fill.get("quantity"))
        if price is None or quantity is None or quantity <= 0:
            continue
        entry_qty += quantity
        notional += price * quantity
    exit_qty = _decimal(position.get("exit_quantity")) or Decimal(0)
    remaining = entry_qty - exit_qty
    entry_price = notional / entry_qty if entry_qty > 0 else None
    mark = position.get("mark") or None
    mark_price = _decimal(mark.get("price")) if mark else None
    mark_at = mark.get("at") if mark else None
    initial_stop = _decimal(position.get("initial_stop"))
    current_stop = _decimal(position.get("current_stop"))
    stop = current_stop if current_stop is not None else initial_stop
    target = _decimal(position.get("target"))

    unrealized: Decimal | None = None
    r_now: Decimal | None = None
    stop_pct: Decimal | None = None
    target_pct: Decimal | None = None
    if mark_price is not None and mark_price > 0 and entry_price is not None and remaining > 0:
        unrealized = sign * (mark_price - entry_price) * remaining
        if initial_stop is not None and initial_stop != entry_price:
            r_now = sign * (mark_price - entry_price) / abs(entry_price - initial_stop)
        if stop is not None:
            stop_pct = sign * (mark_price - stop) / mark_price * 100
        if target is not None:
            target_pct = sign * (target - mark_price) / mark_price * 100
    opened_at = position.get("opened_at")
    hold_seconds = (
        int((now - opened_at).total_seconds()) if isinstance(opened_at, datetime) else None
    )
    mark_age = int((now - mark_at).total_seconds()) if isinstance(mark_at, datetime) else None
    return {
        "trade_id": str(position.get("trade_id")),
        "bot_id": str(position.get("bot_id")),
        "symbol": str(position.get("symbol") or ""),
        "side": str(position.get("side") or ""),
        "quote_asset": str(position.get("quote_asset") or ""),
        "opened_at": _iso(opened_at),
        "hold_seconds": hold_seconds,
        "quantity": _text(remaining) if entry_qty > 0 else None,
        "entry_price": _text(entry_price),
        "mark_price": _text(mark_price),
        "mark_source": (mark or {}).get("source"),
        "mark_age_seconds": mark_age,
        "stop_price": _text(stop),
        "initial_stop": _text(initial_stop),
        "stop_moved": current_stop is not None and current_stop != initial_stop,
        "target_price": _text(target),
        "stop_distance_pct": _fixed(stop_pct),
        "target_distance_pct": _fixed(target_pct),
        "unrealized_pnl": _text(unrealized),
        "r_multiple": _fixed(r_now),
        "funding_total": _text(_decimal(position.get("funding_total"))),
        "fee_total": _text(_decimal(position.get("fee_total"))),
        "protected": stop is not None,
    }


def build_equity_view(
    *,
    seed_equity: Decimal | None,
    closed: Sequence[tuple[datetime, Decimal]],
    unrealized: Decimal | None,
    now: datetime,
    realized_before: Decimal = Decimal(0),
    peak_realized_before: Decimal = Decimal(0),
) -> dict[str, Any]:
    """Vốn = vốn ban đầu + PnL ròng đã chốt + PnL chưa chốt theo mark; drawdown từ đỉnh.

    ``closed`` chỉ là đoạn cuối của đường vốn; phần trước đó gộp thành ``realized_before``
    (PnL lũy kế) và ``peak_realized_before`` (đỉnh PnL lũy kế, ≥ 0) để đỉnh không bị đánh giá thấp.
    """
    if seed_equity is None:
        return {
            "equity": None,
            "seed_equity": None,
            "realized_pnl": None,
            "unrealized_pnl": _text(unrealized),
            "peak": None,
            "drawdown": None,
            "drawdown_pct": None,
            "curve": [],
        }
    running = seed_equity + realized_before
    peak = seed_equity + max(Decimal(0), peak_realized_before, realized_before)
    curve: list[tuple[datetime | None, Decimal]] = [(None, running)]
    realized = realized_before
    for closed_at, net in sorted(closed, key=lambda item: item[0]):
        running += net
        realized += net
        peak = max(peak, running)
        curve.append((closed_at, running))
    current = running + (unrealized or Decimal(0))
    peak = max(peak, current)
    curve.append((now, current))
    drawdown = peak - current
    drawdown_pct = drawdown / peak * 100 if peak > 0 else None
    points = curve[-EQUITY_CURVE_POINTS:]
    return {
        "equity": _text(current),
        "seed_equity": _text(seed_equity),
        "realized_pnl": _text(realized),
        "unrealized_pnl": _text(unrealized),
        "peak": _text(peak),
        "drawdown": _text(drawdown),
        "drawdown_pct": _fixed(drawdown_pct),
        "curve": [[_iso(at), _text(value)] for at, value in points],
    }


def measure_clock_skew_ms(sent: datetime, database_now: datetime, received: datetime) -> int:
    """Lệch đồng hồ app↔DB theo điểm giữa khứ hồi (kiểu NTP đơn giản), có dấu: DB nhanh → dương."""
    midpoint = sent + (received - sent) / 2
    return int((database_now - midpoint).total_seconds() * 1000)


def _age_seconds(now: datetime, at: datetime | None) -> int | None:
    return max(0, int((now - at).total_seconds())) if isinstance(at, datetime) else None


def _status(value: int | None, ok: int, warn: int) -> str:
    if value is None:
        return "unknown"
    if value <= ok:
        return "ok"
    return "warn" if value <= warn else "stale"


def build_feed_health(
    *,
    now: datetime,
    clock_skew_ms: int | None,
    latest_mark_at: datetime | None,
    latest_bar_close_at: datetime | None,
    outbox_oldest_pending_at: datetime | None,
    outbox_pending: int,
    outbox_unconsumed_pending: int,
    daily_loss: Decimal,
    drawdown_pct: Decimal | None,
    api_error_count: int,
    unprotected_positions: int,
    policy: CircuitBreakerPolicy | None = None,
) -> dict[str, Any]:
    """Độ trễ feed/mark/outbox, lệch đồng hồ app↔DB và đánh giá ngắt mạch theo telemetry thật.

    ``clock_skew_ms`` phải đo bằng một truy vấn ``clock_timestamp()`` riêng so với điểm giữa
    thời gian gửi/nhận (xem ``measure_clock_skew_ms``) để không lẫn thời gian chạy truy vấn.

    Đây là đánh giá **hiển thị** không lưu trạng thái; quyết định chặn vào lệnh vẫn do runtime
    (entry_state/kill switch) nắm. Outbox chỉ tính các loại sự kiện có consumer; ``paper.*``
    hiện chưa có consumer (ledger là nguồn thật) nên báo riêng là ``unconsumed_pending``.
    """
    policy = policy or CircuitBreakerPolicy()
    feed_age = _age_seconds(now, latest_bar_close_at)
    mark_age = _age_seconds(now, latest_mark_at)
    skew_ms = abs(clock_skew_ms) if clock_skew_ms is not None else None
    outbox_lag = _age_seconds(now, outbox_oldest_pending_at) or 0
    breaker_feed_age = max(0, (feed_age or 0) - FEED_BAR_INTERVAL_SECONDS)
    snapshot = CircuitBreakerEngine(policy).evaluate(
        OperationalTelemetry(
            evaluated_at=now,
            feed_age_seconds=breaker_feed_age,
            daily_loss=max(Decimal(0), daily_loss),
            drawdown_pct=drawdown_pct or Decimal(0),
            consecutive_api_errors=api_error_count,
            outbox_lag_seconds=outbox_lag,
            clock_skew_ms=skew_ms or 0,
            unprotected_exposure_count=unprotected_positions,
            reconciliation_discrepancy_count=0,
            unknown_order_count=0,
        )
    )
    return {
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "feed": {
            "age_seconds": feed_age,
            "latest_at": _iso(latest_bar_close_at),
            "status": _status(feed_age, FEED_OK_SECONDS, FEED_WARN_SECONDS),
        },
        "mark": {
            "age_seconds": mark_age,
            "latest_at": _iso(latest_mark_at),
            "status": _status(mark_age, MARK_OK_SECONDS, MARK_WARN_SECONDS),
        },
        "clock": {
            "skew_ms": skew_ms,
            "status": _status(skew_ms, CLOCK_WARN_MS, CLOCK_STALE_MS - 1),
        },
        "outbox": {
            "lag_seconds": outbox_lag,
            "pending": outbox_pending,
            "unconsumed_pending": outbox_unconsumed_pending,
            "status": _status(outbox_lag, OUTBOX_WARN_SECONDS, OUTBOX_STALE_SECONDS),
        },
        "breaker": {
            "state": snapshot.state.value,
            "entry_allowed": snapshot.entry_allowed,
            "reasons": [reason.value for reason in snapshot.tripped_reasons],
            "detail_vi": snapshot.detail_vi,
        },
    }
