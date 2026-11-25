"""Read-model bối cảnh thị trường có provenance cho báo cáo (WP-07D / CRY-374).

Dữ liệu funding/OI/thanh lý/mark price chỉ để **hiển thị có nguồn gốc**: không được
dùng làm giá khớp lệnh, tín hiệu vào lệnh hoặc bằng chứng quyền dữ liệu. Mọi quan
sát mang checksum, nguồn và mốc thời gian để truy vết tới bản gốc.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from trading_bot.domain.research.source_policy import SourceUsePurpose


class MarketContextKind(StrEnum):
    """Nhóm dữ liệu bối cảnh được phép hiển thị trên báo cáo."""

    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    LIQUIDATION = "liquidation"
    MARK_PRICE = "mark_price"
    TRADE_FLOW = "trade_flow"


class MarketContextAlertCode(StrEnum):
    """Mã cảnh báo reason-coded, không chứa secret hay dữ liệu cá nhân."""

    NO_DATA = "NO_DATA"
    STALE = "STALE"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    RIGHTS_NOT_ALLOWED = "RIGHTS_NOT_ALLOWED"


_ALERT_MESSAGE_VI: dict[MarketContextAlertCode, str] = {
    MarketContextAlertCode.NO_DATA: "Không có dữ liệu bối cảnh trong cửa sổ quan sát.",
    MarketContextAlertCode.STALE: "Dữ liệu bối cảnh đã cũ, không dùng để nhận định hiện tại.",
    MarketContextAlertCode.PARTIAL_COVERAGE: "Dữ liệu bối cảnh chưa đầy đủ theo kỳ vọng.",
    MarketContextAlertCode.RIGHTS_NOT_ALLOWED: (
        "Nguồn chưa được cấp quyền hiển thị bối cảnh thị trường."
    ),
}


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class MarketContextObservation:
    """Một quan sát bối cảnh kèm provenance tối thiểu để truy vết."""

    kind: MarketContextKind
    symbol: str
    source: str
    checksum: str
    event_at: datetime
    available_at: datetime
    value: Decimal | None = None
    unit: str = ""
    revision: int | None = None
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol không được rỗng.")
        if not self.source.strip() or not self.checksum.strip():
            raise ValueError("source và checksum là bắt buộc để truy vết nguồn.")
        _require_aware(self.event_at, "event_at")
        _require_aware(self.available_at, "available_at")
        if self.available_at < self.event_at:
            raise ValueError("available_at không được trước event_at.")

    def to_dict(self) -> dict[str, object]:
        """Biểu diễn an toàn cho API/admin; giá trị chỉ để đọc, không phải giá fill."""
        return {
            "kind": str(self.kind),
            "symbol": self.symbol,
            "source": self.source,
            "checksum": self.checksum,
            "event_at": self.event_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "value": str(self.value) if self.value is not None else None,
            "unit": self.unit,
            "revision": self.revision,
            "raw_reference": self.raw_reference,
            "used_for_fill_or_signal": False,
        }


@dataclass(frozen=True, slots=True)
class MarketContextCoverage:
    """Độ phủ của một nhóm bối cảnh trong cửa sổ quan sát."""

    kind: MarketContextKind
    count: int
    window_start: datetime
    window_end: datetime
    latest_event_at: datetime | None
    expected_count: int
    stale: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "count": self.count,
            "expected_count": self.expected_count,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "latest_event_at": self.latest_event_at.isoformat() if self.latest_event_at else None,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class MarketContextReport:
    """Báo cáo bối cảnh đã kiểm quyền hiển thị và độ tươi dữ liệu."""

    workspace_id: str
    symbol: str
    as_of: datetime
    purpose: SourceUsePurpose
    display_allowed: bool
    observations: tuple[MarketContextObservation, ...]
    coverage: tuple[MarketContextCoverage, ...]
    alerts: tuple[MarketContextAlertCode, ...]

    def to_dict(self) -> dict[str, object]:
        # Khi không có quyền hiển thị, không trả giá trị/checksum/nguồn dù đã tính ở DB.
        observations: tuple[MarketContextObservation, ...] = (
            self.observations if self.display_allowed else ()
        )
        coverage: tuple[MarketContextCoverage, ...] = self.coverage if self.display_allowed else ()
        return {
            "workspace_id": self.workspace_id,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "purpose": str(self.purpose),
            "display_allowed": self.display_allowed,
            "used_for_fill_or_signal": False,
            "observations": [item.to_dict() for item in observations],
            "coverage": [item.to_dict() for item in coverage],
            "alerts": [
                {"code": str(code), "message_vi": _ALERT_MESSAGE_VI[code]} for code in self.alerts
            ],
        }


def build_market_context_report(
    *,
    workspace_id: str,
    symbol: str,
    observations: tuple[MarketContextObservation, ...],
    purpose: SourceUsePurpose,
    now: datetime | None = None,
    window: timedelta = timedelta(hours=24),
    staleness_threshold: timedelta = timedelta(hours=2),
    expected_per_kind: Mapping[MarketContextKind, int] | None = None,
    allowed_purposes: tuple[SourceUsePurpose, ...] = (SourceUsePurpose.DISPLAY,),
) -> MarketContextReport:
    """Dựng read-model bối cảnh: lọc theo symbol/cửa sổ, tính độ phủ và kiểm quyền.

    `expected_per_kind` là kỳ vọng vận hành (số kỳ funding/OI... trong 24h), không phải
    dữ liệu thị trường; khi thiếu, mỗi nhóm có kỳ vọng bằng số quan sát thực tế.
    """
    if not workspace_id.strip():
        raise ValueError("workspace_id không được rỗng.")
    current = now if now is not None else datetime.now(UTC)
    _require_aware(current, "now")
    if window <= timedelta(0) or staleness_threshold <= timedelta(0):
        raise ValueError("window và staleness_threshold phải dương.")

    window_start = current - window
    selected = tuple(
        item
        for item in observations
        if item.symbol == symbol and window_start <= item.event_at <= current
    )
    coverage: list[MarketContextCoverage] = []
    alerts: list[MarketContextAlertCode] = []
    for kind in MarketContextKind:
        items = tuple(item for item in selected if item.kind == kind)
        latest = max((item.event_at for item in items), default=None)
        stale = latest is not None and current - latest > staleness_threshold
        expected = int((expected_per_kind or {}).get(kind, len(items)))
        coverage.append(
            MarketContextCoverage(
                kind=kind,
                count=len(items),
                window_start=window_start,
                window_end=current,
                latest_event_at=latest,
                expected_count=expected,
                stale=stale,
            )
        )
        if not items:
            alerts.append(MarketContextAlertCode.NO_DATA)
        elif stale:
            alerts.append(MarketContextAlertCode.STALE)
        if expected > len(items):
            alerts.append(MarketContextAlertCode.PARTIAL_COVERAGE)

    display_allowed = purpose in allowed_purposes
    if not display_allowed:
        # Chỉ giữ mã cảnh báo quyền; không giữ lại quan sát/độ phủ trong payload.
        return MarketContextReport(
            workspace_id=workspace_id,
            symbol=symbol,
            as_of=current,
            purpose=purpose,
            display_allowed=False,
            observations=(),
            coverage=(),
            alerts=(MarketContextAlertCode.RIGHTS_NOT_ALLOWED,),
        )
    return MarketContextReport(
        workspace_id=workspace_id,
        symbol=symbol,
        as_of=current,
        purpose=purpose,
        display_allowed=True,
        observations=selected,
        coverage=tuple(coverage),
        alerts=tuple(dict.fromkeys(alerts)),
    )
