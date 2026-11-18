"""Hợp đồng và nội dung thông báo tín hiệu-only qua Telegram."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SYMBOL = re.compile(r"^[A-Z0-9_]{2,40}$")
_DIRECTIONS = frozenset({"LONG", "SHORT"})
_TIMEFRAME = re.compile(r"^[1-9][0-9]{0,2}[smhdwM]$")
_FILTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_FILTER_VALUES = 100


@dataclass(frozen=True, slots=True)
class TelegramSignalFilters:
    """Allowlist tùy chọn theo dữ liệu của candidate; tuple rỗng nghĩa là mọi giá trị."""

    symbols: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    strategy_ids: tuple[str, ...] = ()
    setup_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _normalize_filter_values(self.symbols, "symbols"))
        object.__setattr__(
            self, "directions", _normalize_filter_values(self.directions, "directions")
        )
        object.__setattr__(
            self,
            "strategy_ids",
            _normalize_filter_values(self.strategy_ids, "strategy_ids"),
        )
        object.__setattr__(
            self,
            "setup_codes",
            _normalize_filter_values(self.setup_codes, "setup_codes"),
        )
        timeframes = _normalize_filter_values(self.timeframes, "timeframes", uppercase=False)
        if any(_TIMEFRAME.fullmatch(value) is None for value in timeframes):
            raise ValueError("timeframes chứa interval không hợp lệ.")
        if any(value not in _DIRECTIONS for value in self.directions):
            raise ValueError("directions chỉ nhận LONG hoặc SHORT.")
        if any(_SYMBOL.fullmatch(value) is None for value in self.symbols):
            raise ValueError("symbols chứa mã không hợp lệ.")
        if any(
            _FILTER_ID.fullmatch(value) is None for value in (*self.strategy_ids, *self.setup_codes)
        ):
            raise ValueError("strategy_ids/setup_codes chứa giá trị không hợp lệ.")
        object.__setattr__(self, "timeframes", timeframes)

    def matches(self, candidate: TelegramSignalCandidate) -> bool:
        """Trả đúng khi candidate nằm trong mọi allowlist đang được bật."""
        return (
            (not self.symbols or candidate.symbol in self.symbols)
            and (not self.timeframes or candidate.timeframe in self.timeframes)
            and (not self.directions or candidate.direction in self.directions)
            and (not self.strategy_ids or candidate.strategy_id in self.strategy_ids)
            and (not self.setup_codes or candidate.reason_code in self.setup_codes)
        )

    def as_dict(self) -> dict[str, list[str]]:
        """Trả payload JSON ổn định cho DB, API và audit."""
        return {
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "directions": list(self.directions),
            "strategy_ids": list(self.strategy_ids),
            "setup_codes": list(self.setup_codes),
        }


def _normalize_filter_values(
    values: tuple[str, ...], name: str, *, uppercase: bool = True
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_FILTER_VALUES:
        raise ValueError(f"{name} phải là tuple có tối đa {_MAX_FILTER_VALUES} giá trị.")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"{name} chứa giá trị rỗng hoặc quá dài.")
        clean = value.strip()
        normalized.add(clean.upper() if uppercase else clean)
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class TelegramSignalCandidate:
    """Candidate đã được decision pipeline xác nhận, chưa phải order hay vị thế."""

    decision_id: UUID
    symbol: str
    direction: str
    timeframe: str
    strategy_id: str
    strategy_release_id: str
    reference_price: Decimal
    signal_at: datetime
    expires_at: datetime
    reason_code: str
    detail_vi: str
    invalidation: str | None = None
    # TG-1 (SIG-3): thẻ tín hiệu dựng từ candidate + checklist đã xác minh hash; None khi không
    # dựng được (quyết định cũ, thang giá sai hướng) — tin vẫn gửi phần cơ bản.
    card: Mapping[str, object] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, UUID):
            raise ValueError("decision_id phải là UUID.")
        if not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("symbol không hợp lệ.")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction chỉ nhận LONG hoặc SHORT.")
        for name in (
            "timeframe",
            "strategy_id",
            "strategy_release_id",
            "reason_code",
            "detail_vi",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} không hợp lệ.")
        if not isinstance(self.reference_price, Decimal) or self.reference_price <= 0:
            raise ValueError("reference_price phải là Decimal dương.")
        for name in ("signal_at", "expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} phải có múi giờ.")
        if self.expires_at <= self.signal_at:
            raise ValueError("expires_at phải sau signal_at.")
        if self.invalidation is not None and len(self.invalidation) > 500:
            raise ValueError("invalidation vượt giới hạn.")


def is_candidate_deliverable(
    candidate: TelegramSignalCandidate,
    *,
    now: datetime,
    max_age: timedelta,
    allowed_clock_skew: timedelta = timedelta(seconds=2),
) -> bool:
    """Chặn candidate hết hạn, quá cũ hoặc có thời gian phát sinh bất hợp lý."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now phải có múi giờ.")
    if max_age <= timedelta(0) or allowed_clock_skew < timedelta(0):
        raise ValueError("Ngưỡng freshness không hợp lệ.")
    instant = now.astimezone(candidate.signal_at.tzinfo)
    return (
        candidate.expires_at > instant
        and candidate.signal_at <= instant + allowed_clock_skew
        and instant - candidate.signal_at <= max_age
    )


_MAX_CHECKLIST_LINES = 8


def _price(value: object) -> str:
    """Giá kiểu Việt Nam: nhóm nghìn bằng dấu chấm, thập phân bằng dấu phẩy."""
    number = Decimal(str(value))
    digits = 2 if abs(number) >= 1 else 8
    text = f"{number:,.{digits}f}".rstrip("0").rstrip(".") if digits == 8 else f"{number:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _r_multiple(value: object) -> str:
    """Bội số R một chữ số thập phân, dấu phẩy thập phân."""
    return f"{Decimal(str(value)):.1f}".replace(".", ",")


def _card_lines(card: Mapping[str, object]) -> list[str]:
    """Các dòng thẻ tín hiệu (SIG-3) cho Telegram; chỉ đọc giá trị đã tính sẵn."""
    zone = card.get("entry_zone")
    lines = ["", "— THẺ TÍN HIỆU (PAPER) —"]
    if isinstance(zone, Mapping):
        low, high = zone.get("low"), zone.get("high")
        lines.append(
            f"Vùng vào: {_price(low)}"
            + (f" – {_price(high)}" if high is not None and high != low else "")
        )
    lines.append(f"Cắt lỗ (SL): {_price(card.get('stop'))}")
    targets = card.get("targets")
    if isinstance(targets, list | tuple) and targets:
        lines.append("Mục tiêu (R thô, chưa trừ phí):")
        for target in targets:
            if isinstance(target, Mapping):
                lines.append(
                    f"  • {target.get('label')}: {_price(target.get('price'))} "
                    f"({_r_multiple(target.get('r_multiple'))}R)"
                )
    checklist = card.get("checklist")
    if isinstance(checklist, list | tuple) and checklist:
        lines.append(
            f"Điều kiện ({card.get('checklist_passed')}/{card.get('checklist_total')} đạt):"
        )
        for item in list(checklist)[:_MAX_CHECKLIST_LINES]:
            if not isinstance(item, Mapping):
                continue
            passed = item.get("passed")
            mark = "✓" if passed is True else ("✗" if passed is False else "—")
            lines.append(
                f"  {mark} {item.get('label_vi')}: {item.get('actual')} · cần "
                f"{item.get('operator')} {item.get('threshold')}"
            )
    invalidation = card.get("invalidation")
    if isinstance(invalidation, list | tuple) and invalidation:
        lines.append("Mất hiệu lực khi:")
        lines.extend(f"  • {line}" for line in invalidation)
    confidence = card.get("confidence")
    if isinstance(confidence, Mapping):
        lines.append(f"Độ tin cậy: {confidence.get('label')} (chưa hiện %)")
    return lines


def build_signal_message(candidate: TelegramSignalCandidate) -> str:
    """Tạo nội dung thuần văn bản, không có lệnh giao dịch hoặc cú pháp parse-mode."""
    lines = [
        "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH",
        f"Mã tín hiệu: {candidate.decision_id}",
        f"Cặp: {candidate.symbol} · Hướng: {candidate.direction} · Khung: {candidate.timeframe}",
        f"Giá tham chiếu: {candidate.reference_price}",
        f"Chiến thuật: {candidate.strategy_id}",
        f"Bản chiến thuật: {candidate.strategy_release_id}",
        f"Mã bắt tín hiệu: {candidate.reason_code}",
        f"Vì sao bắt tín hiệu: {candidate.detail_vi}",
        f"Thời điểm: {candidate.signal_at.isoformat()}",
        f"Hết hiệu lực: {candidate.expires_at.isoformat()}",
    ]
    if candidate.invalidation:
        lines.append(f"Điều kiện mất hiệu lực: {candidate.invalidation}")
    if candidate.card is not None:
        lines.extend(_card_lines(candidate.card))
    lines.append("")
    lines.append("PAPER · mô phỏng nội bộ, không phải khuyến nghị đầu tư.")
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Nội dung tín hiệu vượt giới hạn Telegram.")
    return message


# --- TG-2 & WP-07B/delivery-policy: chống spam, quiet hours, trần ngày và loại tin ------------

SUPPRESSED_COOLDOWN = "SIGNAL_COOLDOWN_DUPLICATE"
SUPPRESSED_HOURLY_CAP = "SIGNAL_HOURLY_CAP"
SUPPRESSED_DAILY_CAP = "SIGNAL_DAILY_CAP"
SUPPRESSED_QUIET_HOURS = "SIGNAL_QUIET_HOURS"
SUPPRESSED_NOTIFICATION_TYPE = "SIGNAL_NOTIFICATION_TYPE_DISABLED"
_HOUR = timedelta(hours=1)
_DAY = timedelta(hours=24)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ALL_NOTIFICATION_TYPES: tuple[str, ...] = (
    "CANDIDATE",
    "INVALIDATION",
    "EXPIRY",
    "PAPER_TRADE_OPENED",
    "PAPER_TRADE_CLOSED",
)


def _parse_hhmm(value: str, field_name: str) -> time:
    match = _HHMM.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"{field_name} phải có định dạng HH:MM (00:00–23:59).")
    return time(int(match.group(1)), int(match.group(2)))


@dataclass(frozen=True, slots=True)
class SignalThrottlePolicy:
    """Giới hạn tin cho mỗi subscription (một bot → một chat).

    - ``cooldown``: không gửi lại cùng mã + hướng + chiến lược + release trong khoảng này
      (gộp tín hiệu trùng khi rule lặp lại trên các nến liên tiếp).
    - ``max_per_hour``: tối đa số tin trong 60 phút trượt; tin vượt bị ghi ``SUPPRESSED``.
    """

    cooldown: timedelta = timedelta(minutes=90)
    max_per_hour: int = 6

    def __post_init__(self) -> None:
        if not timedelta(0) <= self.cooldown <= timedelta(hours=24):
            raise ValueError("Cooldown tín hiệu phải trong 0–24 giờ.")
        if not 1 <= self.max_per_hour <= 60:
            raise ValueError("Giới hạn tin/giờ phải trong 1–60.")

    @property
    def lookback(self) -> timedelta:
        """Khoảng lịch sử cần đọc để quyết định."""
        return max(self.cooldown, _HOUR)


@dataclass(frozen=True, slots=True)
class TelegramDeliveryPolicy:
    """Chính sách giao nhận đầy đủ theo từng subscription (WP-07B / CRY-346)."""

    cooldown: timedelta = timedelta(minutes=90)
    max_per_hour: int = 6
    max_per_day: int = 24
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    timezone_name: str = "Asia/Ho_Chi_Minh"
    notification_types: tuple[str, ...] = ALL_NOTIFICATION_TYPES

    def __post_init__(self) -> None:
        if not timedelta(0) <= self.cooldown <= timedelta(hours=24):
            raise ValueError("Cooldown tín hiệu phải trong 0–24 giờ.")
        if not 1 <= self.max_per_hour <= 60:
            raise ValueError("Giới hạn tin/giờ phải trong 1–60.")
        if not 1 <= self.max_per_day <= 200:
            raise ValueError("Giới hạn tin/ngày phải trong 1–200.")
        if self.max_per_day < self.max_per_hour:
            raise ValueError("Giới hạn tin/ngày không được nhỏ hơn giới hạn tin/giờ.")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"Múi giờ không hợp lệ: {self.timezone_name}") from error
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("Phải cấu hình cả giờ bắt đầu và kết thúc quiet hours.")
        if self.quiet_hours_start is not None and self.quiet_hours_end is not None:
            start = _parse_hhmm(self.quiet_hours_start, "quiet_hours_start")
            end = _parse_hhmm(self.quiet_hours_end, "quiet_hours_end")
            if start == end:
                raise ValueError("quiet_hours_start và quiet_hours_end không được trùng nhau.")
            object.__setattr__(self, "quiet_hours_start", f"{start.hour:02d}:{start.minute:02d}")
            object.__setattr__(self, "quiet_hours_end", f"{end.hour:02d}:{end.minute:02d}")
        normalized_types = tuple(
            dict.fromkeys(str(item).strip().upper() for item in self.notification_types if item)
        )
        invalid = [item for item in normalized_types if item not in ALL_NOTIFICATION_TYPES]
        if invalid:
            raise ValueError(f"Loại thông báo không hợp lệ: {', '.join(invalid)}")
        object.__setattr__(self, "notification_types", normalized_types)

    @property
    def lookback(self) -> timedelta:
        """Khoảng lịch sử cần đọc để kiểm tra cooldown, giờ và ngày."""
        return max(self.cooldown, _DAY)

    def is_quiet_at(self, now: datetime) -> bool:
        """Kiểm tra `now` có nằm trong quiet hours theo timezone của subscription không."""
        if self.quiet_hours_start is None or self.quiet_hours_end is None:
            return False
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        local_time = now.astimezone(ZoneInfo(self.timezone_name)).time()
        start = _parse_hhmm(self.quiet_hours_start, "quiet_hours_start")
        end = _parse_hhmm(self.quiet_hours_end, "quiet_hours_end")
        if start < end:
            return start <= local_time < end
        # Khoảng qua đêm (vd 23:00 → 07:00)
        return local_time >= start or local_time < end

    def allows_notification_type(self, notification_type: str) -> bool:
        """Kiểm tra loại thông báo có được phép gửi cho subscription này không."""
        return notification_type.strip().upper() in self.notification_types

    def as_dict(self) -> dict[str, Any]:
        """Serialize JSON ổn định cho DB và API."""
        return {
            "cooldown_minutes": int(self.cooldown.total_seconds() // 60),
            "max_per_hour": self.max_per_hour,
            "max_per_day": self.max_per_day,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "timezone_name": self.timezone_name,
            "notification_types": list(self.notification_types),
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        fallback: SignalThrottlePolicy | None = None,
    ) -> TelegramDeliveryPolicy:
        """Khôi phục policy từ JSONB; fallback về mặc định của worker khi thiếu trường."""
        data = dict(raw or {})
        base_cooldown = fallback.cooldown if fallback is not None else timedelta(minutes=90)
        base_hourly = fallback.max_per_hour if fallback is not None else 6
        cooldown_minutes = data.get("cooldown_minutes")
        cooldown = (
            timedelta(minutes=int(cooldown_minutes))
            if cooldown_minutes is not None
            else base_cooldown
        )
        max_per_hour = int(data.get("max_per_hour", base_hourly))
        max_per_day = int(data.get("max_per_day", max(24, max_per_hour)))
        raw_types = data.get("notification_types")
        notification_types = (
            tuple(str(item) for item in raw_types)
            if isinstance(raw_types, list | tuple)
            else ALL_NOTIFICATION_TYPES
        )
        return cls(
            cooldown=cooldown,
            max_per_hour=max_per_hour,
            max_per_day=max_per_day,
            quiet_hours_start=data.get("quiet_hours_start"),
            quiet_hours_end=data.get("quiet_hours_end"),
            timezone_name=str(data.get("timezone_name") or "Asia/Ho_Chi_Minh"),
            notification_types=notification_types,
        )


@dataclass(frozen=True, slots=True)
class RecentSignalDelivery:
    """Một tin đã gửi hoặc đang gửi của cùng subscription: khóa gộp và thời điểm."""

    dedup_key: str
    at: datetime


def signal_dedup_key(
    *, symbol: str, direction: str, strategy_id: str, strategy_release_id: str
) -> str:
    """Khóa gộp tín hiệu trùng; timeframe đã nằm trong release nên không lặp lại."""
    return f"{symbol.upper()}|{direction.upper()}|{strategy_id}|{strategy_release_id}"


def candidate_dedup_key(candidate: TelegramSignalCandidate) -> str:
    return signal_dedup_key(
        symbol=candidate.symbol,
        direction=candidate.direction,
        strategy_id=candidate.strategy_id,
        strategy_release_id=candidate.strategy_release_id,
    )


def throttle_reason(
    candidate: TelegramSignalCandidate,
    recent: Iterable[RecentSignalDelivery],
    *,
    now: datetime,
    policy: SignalThrottlePolicy | TelegramDeliveryPolicy,
    notification_type: str = "CANDIDATE",
) -> str | None:
    """Mã lý do chặn tin (None = được gửi)."""
    if isinstance(policy, TelegramDeliveryPolicy):
        if not policy.allows_notification_type(notification_type):
            return SUPPRESSED_NOTIFICATION_TYPE
        if policy.is_quiet_at(now):
            return SUPPRESSED_QUIET_HOURS

    key = candidate_dedup_key(candidate)
    history = list(recent)
    if any(item.dedup_key == key and now - item.at < policy.cooldown for item in history):
        return SUPPRESSED_COOLDOWN
    if sum(1 for item in history if now - item.at < _HOUR) >= policy.max_per_hour:
        return SUPPRESSED_HOURLY_CAP
    if isinstance(policy, TelegramDeliveryPolicy):
        tz = ZoneInfo(policy.timezone_name)
        local_date = now.astimezone(tz).date()
        sent_today = sum(1 for item in history if item.at.astimezone(tz).date() == local_date)
        if sent_today >= policy.max_per_day:
            return SUPPRESSED_DAILY_CAP
    return None
