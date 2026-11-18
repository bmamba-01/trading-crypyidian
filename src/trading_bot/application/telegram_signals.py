"""Hợp đồng và nội dung thông báo tín hiệu-only qua Telegram."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

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
    risk_pct = card.get("account_risk_pct")
    if risk_pct is not None:
        lines.append(f"Rủi ro % vốn: {Decimal(str(risk_pct)):.2f}%".replace(".", ","))
    targets = card.get("targets")
    if isinstance(targets, list | tuple) and targets:
        lines.append("Mục tiêu (R thô / R:R ròng sau phí):")
        for target in targets:
            if isinstance(target, Mapping):
                net_r = target.get("net_r_multiple")
                net_part = f" · ròng {_r_multiple(net_r)}R" if net_r is not None else ""
                lines.append(
                    f"  • {target.get('label')}: {_price(target.get('price'))} "
                    f"({_r_multiple(target.get('r_multiple'))}R{net_part})"
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
        tier = confidence.get("tier")
        tier_note = f" [{tier}]" if tier and tier != "uncalibrated" else " (chưa hiện %)"
        lines.append(f"Độ tin cậy: {confidence.get('label')}{tier_note}")
    snapshot_id = card.get("feature_snapshot_id")
    if snapshot_id:
        lines.append(f"Feature snapshot: {snapshot_id}")
    return lines


def build_signal_message(candidate: TelegramSignalCandidate) -> str:
    """Tạo nội dung thuần văn bản, không có lệnh giao dịch hoặc cú pháp parse-mode."""
    from trading_bot.application.market_narrative import (
        decision_narrative,
        format_decision_narrative_text,
    )

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
    narrative_text = format_decision_narrative_text(
        decision_narrative(
            {
                "decision_id": str(candidate.decision_id),
                "status": "CANDIDATE",
                "reason_code": candidate.reason_code,
                "detail_vi": candidate.detail_vi,
                "evaluated_at": candidate.signal_at.isoformat(),
                "strategy_release_id": candidate.strategy_release_id,
                "direction": candidate.direction,
            },
            card=candidate.card,
        ),
        max_sentences=2,
    )
    if narrative_text:
        lines.append(f"Tường thuật: {narrative_text}")
    lines.append("")
    lines.append("PAPER · mô phỏng nội bộ, không phải khuyến nghị đầu tư.")
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Nội dung tín hiệu vượt giới hạn Telegram.")
    return message


# --- TG-2: chống spam -----------------------------------------------------------------------

SUPPRESSED_COOLDOWN = "SIGNAL_COOLDOWN_DUPLICATE"
SUPPRESSED_HOURLY_CAP = "SIGNAL_HOURLY_CAP"
_HOUR = timedelta(hours=1)
SCALPING_STRATEGY_IDS = frozenset({"H18", "H19", "H20"})
SCALPING_TIMEFRAMES = frozenset({"1m", "3m", "5m"})


@dataclass(frozen=True, slots=True)
class SignalThrottlePolicy:
    """Giới hạn tin cho mỗi subscription (một bot → một chat).

    - ``cooldown``: không gửi lại cùng mã + hướng + chiến lược + release trong khoảng này
      (gộp tín hiệu trùng khi rule lặp lại trên các nến liên tiếp).
    - ``scalping_cooldown``: cooldown riêng cho scalping (H18–H20 hoặc khung 1m/3m/5m), tách
      theo symbol + setup (``reason_code``).
    - ``max_per_hour``: tối đa số tin trong 60 phút trượt; tin vượt bị ghi ``SUPPRESSED``.
    """

    cooldown: timedelta = timedelta(minutes=90)
    scalping_cooldown: timedelta = timedelta(minutes=15)
    max_per_hour: int = 6

    def __post_init__(self) -> None:
        if not timedelta(0) <= self.cooldown <= timedelta(hours=24):
            raise ValueError("Cooldown tín hiệu phải trong 0–24 giờ.")
        if not timedelta(0) <= self.scalping_cooldown <= timedelta(hours=24):
            raise ValueError("Cooldown scalping phải trong 0–24 giờ.")
        if not 1 <= self.max_per_hour <= 60:
            raise ValueError("Giới hạn tin/giờ phải trong 1–60.")

    @property
    def lookback(self) -> timedelta:
        """Khoảng lịch sử cần đọc để quyết định."""
        return max(self.cooldown, self.scalping_cooldown, _HOUR)

    def cooldown_for(self, candidate: TelegramSignalCandidate) -> timedelta:
        """Trả cooldown theo loại setup: scalping (H18–H20 / 1m–5m) dùng ngưỡng ngắn hơn."""
        if is_scalping_candidate(candidate):
            return self.scalping_cooldown
        return self.cooldown


def is_scalping_candidate(candidate: TelegramSignalCandidate) -> bool:
    """Nhận diện tín hiệu scalping theo họ chiến lược (H18–H20)."""
    return candidate.strategy_id.upper() in SCALPING_STRATEGY_IDS


@dataclass(frozen=True, slots=True)
class RecentSignalDelivery:
    """Một tin đã gửi hoặc đang gửi của cùng subscription: khóa gộp và thời điểm."""

    dedup_key: str
    at: datetime


def signal_dedup_key(
    *,
    symbol: str,
    direction: str,
    strategy_id: str,
    strategy_release_id: str,
    setup_code: str | None = None,
) -> str:
    """Khóa gộp tín hiệu trùng; scalping (H18–H20) tách theo ``setup_code``."""
    base = f"{symbol.upper()}|{direction.upper()}|{strategy_id}|{strategy_release_id}"
    if setup_code:
        return f"{base}|{setup_code.upper()}"
    return base


def candidate_dedup_key(candidate: TelegramSignalCandidate) -> str:
    return signal_dedup_key(
        symbol=candidate.symbol,
        direction=candidate.direction,
        strategy_id=candidate.strategy_id,
        strategy_release_id=candidate.strategy_release_id,
        setup_code=candidate.reason_code if is_scalping_candidate(candidate) else None,
    )


def throttle_reason(
    candidate: TelegramSignalCandidate,
    recent: Iterable[RecentSignalDelivery],
    *,
    now: datetime,
    policy: SignalThrottlePolicy,
) -> str | None:
    """Mã lý do chặn tin (None = được gửi). Trùng được kiểm trước giới hạn giờ."""
    key = candidate_dedup_key(candidate)
    cooldown = policy.cooldown_for(candidate)
    history = list(recent)
    if any(item.dedup_key == key and now - item.at < cooldown for item in history):
        return SUPPRESSED_COOLDOWN
    if sum(1 for item in history if now - item.at < _HOUR) >= policy.max_per_hour:
        return SUPPRESSED_HOURLY_CAP
    return None
