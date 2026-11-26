"""Chính sách ưu tiên nguồn nến thống nhất cho chart và runtime PAPER (RT-1/CRY-205).

Khi cả luồng WebSocket ``/market`` (``BINANCE_USDM``) lẫn collector/backfill REST
(``BINANCE_USDM_REST_BACKFILL``) cùng hoạt động, mọi thành phần (chart, scanner, producer
tín hiệu và vòng sự kiện PAPER) dùng chung một bảng thứ tự ưu tiên duy nhất để không trộn
lẫn nến ở cùng mốc thời gian và luôn tôn trọng ``pinned_source`` của vị thế đang mở.
"""

from __future__ import annotations

from collections.abc import Sequence

LIVE_BAR_SOURCE = "BINANCE_USDM"
REST_BACKFILL_BAR_SOURCE = "BINANCE_USDM_REST_BACKFILL"

# Thứ tự ưu tiên thống nhất: live WS > REST realtime/backfill > nguồn khác/fixture.
BAR_SOURCE_PRIORITY: tuple[str, ...] = (LIVE_BAR_SOURCE, REST_BACKFILL_BAR_SOURCE)
REAL_BAR_SOURCES: frozenset[str] = frozenset(BAR_SOURCE_PRIORITY)

_KNOWN_TIMEFRAMES = frozenset({"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"})


def bar_source_rank(source: str) -> int:
    """Thứ hạng ưu tiên (nhỏ hơn = ưu tiên cao hơn)."""
    try:
        return BAR_SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(BAR_SOURCE_PRIORITY)


def select_primary_bar_source(
    available_sources: Sequence[str],
    *,
    pinned_source: str | None = None,
) -> str | None:
    """Chọn một nguồn nến chính theo chính sách thống nhất.

    Khi có ``pinned_source`` (vị thế đã ghim nguồn), bắt buộc trả đúng nguồn đó nếu hiện hữu
    và trả ``None`` nếu vắng mặt — tuyệt đối không fallback sang nguồn khác.
    """
    normalized = tuple(source for source in available_sources if isinstance(source, str) and source)
    if pinned_source is not None and pinned_source.strip():
        target = pinned_source.strip()
        return target if target in normalized else None
    if not normalized:
        return None
    for source in BAR_SOURCE_PRIORITY:
        if source in normalized:
            return source
    return sorted(set(normalized))[0]


def allowed_bar_sources(*, pinned_source: str | None = None) -> tuple[str, ...]:
    """Danh sách nguồn nến được phép truy vấn; ghim đúng một nguồn khi có ``pinned_source``."""
    if pinned_source is not None and pinned_source.strip():
        return (pinned_source.strip(),)
    return BAR_SOURCE_PRIORITY


def extract_bar_source_from_key(bar_key: str) -> str | None:
    """Suy nguồn nến từ khóa ``{source}:{symbol}:{timeframe}:{open_time_iso}``."""
    if not isinstance(bar_key, str) or not bar_key.strip():
        return None
    parts = bar_key.split(":", 3)
    if len(parts) < 4:
        return None
    source, symbol, timeframe, timestamp = parts
    if not source.strip() or not symbol.strip() or timeframe not in _KNOWN_TIMEFRAMES:
        return None
    if "T" not in timestamp:
        return None
    return source.strip()


def resolve_bar_source(bar_source: str | None, bar_key: str) -> str | None:
    """Ưu tiên ``bar_source`` tường minh, fallback tách từ ``bar_key`` chuẩn của event loop."""
    if isinstance(bar_source, str) and bar_source.strip():
        return bar_source.strip()
    return extract_bar_source_from_key(bar_key)
