"""Đặc trưng sổ lệnh L2 (Order Book Depth), Bid/Ask Walls & OBI cho cổng lọc bối cảnh L4 (DATA-2).

Module thuần (không I/O), sử dụng ``Decimal`` cho giá/notional và kiểm tra bất biến
point-in-time (``event_at <= as_of``, ``received_at <= as_of``).

BẢO ĐẢM KIẾN TRÚC: Các đặc trưng này CHỈ phục vụ bộ lọc/cảnh báo bối cảnh tại tầng L4,
tuyệt đối không dùng làm ngòi nổ vào lệnh tại tầng L5.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import TYPE_CHECKING, Any

from trading_bot.domain.strategy.contracts import FeatureObservation, FeatureQuality
from trading_bot.ports.market_data import OrderBookSnapshot as PortOrderBookSnapshot

if TYPE_CHECKING:
    from trading_bot.domain.strategy.contracts import DecisionSnapshot

ORDER_BOOK_CONTEXT_VERSION = "data2-orderbook-v1"
DEFAULT_ORDER_BOOK_MAX_AGE = timedelta(seconds=60)

# Tên feature chuẩn khi đóng gói vào DecisionSnapshot.features
FEATURE_OB_BEST_BID = "orderbook_best_bid"
FEATURE_OB_BEST_ASK = "orderbook_best_ask"
FEATURE_OB_MID_PRICE = "orderbook_mid_price"
FEATURE_OB_SPREAD_BPS = "orderbook_spread_bps"
FEATURE_OB_OBI_50BPS = "orderbook_obi_50bps"
FEATURE_OB_OBI_100BPS = "orderbook_obi_100bps"
FEATURE_OB_NEAREST_BID_WALL_PRICE = "orderbook_nearest_bid_wall_price"
FEATURE_OB_NEAREST_BID_WALL_NOTIONAL = "orderbook_nearest_bid_wall_notional"
FEATURE_OB_BID_WALL_DISTANCE_ATR = "orderbook_bid_wall_distance_atr"
FEATURE_OB_NEAREST_ASK_WALL_PRICE = "orderbook_nearest_ask_wall_price"
FEATURE_OB_NEAREST_ASK_WALL_NOTIONAL = "orderbook_nearest_ask_wall_notional"
FEATURE_OB_ASK_WALL_DISTANCE_ATR = "orderbook_ask_wall_distance_atr"
FEATURE_OB_IS_STALE = "orderbook_is_stale"

_BPS_DIVISOR = Decimal("10000")
_FIFTY_BPS = Decimal("0.005")
_HUNDRED_BPS = Decimal("0.010")


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """Một mức giá trong sổ lệnh L2 với giá và khối lượng dạng Decimal."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.price.is_finite() or self.price <= Decimal("0"):
            raise ValueError("price của OrderBookLevel phải dương và hữu hạn.")
        if not self.quantity.is_finite() or self.quantity < Decimal("0"):
            raise ValueError("quantity của OrderBookLevel phải không âm và hữu hạn.")

    @property
    def notional(self) -> Decimal:
        """Giá trị danh nghĩa (USDT) tại mức giá này."""
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Ảnh chụp sổ lệnh L2 bất biến kèm dấu thời gian point-in-time."""

    symbol: str
    as_of: datetime
    event_at: datetime
    received_at: datetime
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    source: str = "BINANCE_USDM_REST"

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol của OrderBookSnapshot không được rỗng.")
        for field_name, ts in (
            ("as_of", self.as_of),
            ("event_at", self.event_at),
            ("received_at", self.received_at),
        ):
            if ts.tzinfo is None or ts.utcoffset() is None:
                raise ValueError(f"{field_name} của OrderBookSnapshot phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class OrderBookWall:
    """Tường thanh khoản Bid/Ask gom cụm hoặc mức đơn nổi bật trên sổ lệnh L2."""

    side: str  # "BID" | "ASK"
    price: Decimal
    notional: Decimal
    distance_atr: Decimal | None
    level_count: int = 1


@dataclass(frozen=True, slots=True)
class OrderBookConfig:
    """Cấu hình ngưỡng stale, gom cụm bước giá và phát hiện tường thanh khoản."""

    max_age: timedelta = DEFAULT_ORDER_BOOK_MAX_AGE
    wall_median_multiplier: Decimal = Decimal("3.0")
    wall_min_notional: Decimal = Decimal("0")
    bucket_bps: Decimal | None = None
    tick_size: Decimal | None = None

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age phải lớn hơn 0.")
        if self.wall_median_multiplier <= Decimal("0"):
            raise ValueError("wall_median_multiplier phải dương.")
        if self.wall_min_notional < Decimal("0"):
            raise ValueError("wall_min_notional phải không âm.")
        if self.bucket_bps is not None and self.bucket_bps <= Decimal("0"):
            raise ValueError("bucket_bps phải dương khi được cấu hình.")
        if self.tick_size is not None and self.tick_size <= Decimal("0"):
            raise ValueError("tick_size phải dương khi được cấu hình.")


@dataclass(frozen=True, slots=True)
class OrderBookContext:
    """Bối cảnh sổ lệnh L2 point-in-time tại ``as_of`` phục vụ cổng L4."""

    symbol: str
    as_of: datetime
    event_at: datetime | None
    received_at: datetime | None
    is_stale: bool
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid_price: Decimal | None
    spread_bps: Decimal | None
    obi_50bps: Decimal | None
    obi_100bps: Decimal | None
    nearest_bid_wall_price: Decimal | None
    nearest_bid_wall_notional: Decimal | None
    bid_wall_distance_atr: Decimal | None
    nearest_ask_wall_price: Decimal | None
    nearest_ask_wall_notional: Decimal | None
    ask_wall_distance_atr: Decimal | None
    bid_walls: tuple[OrderBookWall, ...] = ()
    ask_walls: tuple[OrderBookWall, ...] = ()
    source: str = "BINANCE_USDM_REST"
    stale_reason: str | None = None
    version: str = ORDER_BOOK_CONTEXT_VERSION

    def to_metrics(self) -> dict[str, Any]:
        """Xuất từ điển chỉ số JSON-safe cho ContextGateResult.metrics và snapshot cache."""

        def _f(val: Decimal | None) -> float | None:
            return float(val) if val is not None else None

        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "event_at": self.event_at.isoformat() if self.event_at is not None else None,
            "received_at": self.received_at.isoformat() if self.received_at is not None else None,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "best_bid": _f(self.best_bid),
            "best_ask": _f(self.best_ask),
            "mid_price": _f(self.mid_price),
            "spread_bps": _f(self.spread_bps),
            "obi_50bps": _f(self.obi_50bps),
            "obi_100bps": _f(self.obi_100bps),
            "nearest_bid_wall_price": _f(self.nearest_bid_wall_price),
            "nearest_bid_wall_notional": _f(self.nearest_bid_wall_notional),
            "bid_wall_distance_atr": _f(self.bid_wall_distance_atr),
            "nearest_ask_wall_price": _f(self.nearest_ask_wall_price),
            "nearest_ask_wall_notional": _f(self.nearest_ask_wall_notional),
            "ask_wall_distance_atr": _f(self.ask_wall_distance_atr),
            "source": self.source,
            "version": self.version,
        }

    def to_feature_observations(
        self,
        *,
        timeframe: str = "1h",
        snapshot_prefix: str = "ob-ctx",
    ) -> tuple[FeatureObservation, ...]:
        """Đóng gói thành ``FeatureObservation`` chuẩn để gắn vào ``DecisionSnapshot.features``."""
        available_at = self.received_at or self.as_of
        if available_at > self.as_of:
            available_at = self.as_of
        stamp = int(self.as_of.timestamp())

        def _make(name: str, value: Decimal | float | None) -> FeatureObservation:
            num = float(value) if value is not None else None
            if self.is_stale:
                quality = FeatureQuality.STALE
            elif num is None:
                quality = FeatureQuality.MISSING
            else:
                quality = FeatureQuality.READY
            return FeatureObservation(
                feature_id=f"{snapshot_prefix}:{self.symbol}:{stamp}:{name}",
                name=name,
                timeframe=timeframe,
                value=num,
                previous_value=None,
                available_at=available_at,
                quality=quality,
            )

        return (
            _make(FEATURE_OB_BEST_BID, self.best_bid),
            _make(FEATURE_OB_BEST_ASK, self.best_ask),
            _make(FEATURE_OB_MID_PRICE, self.mid_price),
            _make(FEATURE_OB_SPREAD_BPS, self.spread_bps),
            _make(FEATURE_OB_OBI_50BPS, self.obi_50bps),
            _make(FEATURE_OB_OBI_100BPS, self.obi_100bps),
            _make(FEATURE_OB_NEAREST_BID_WALL_PRICE, self.nearest_bid_wall_price),
            _make(FEATURE_OB_NEAREST_BID_WALL_NOTIONAL, self.nearest_bid_wall_notional),
            _make(FEATURE_OB_BID_WALL_DISTANCE_ATR, self.bid_wall_distance_atr),
            _make(FEATURE_OB_NEAREST_ASK_WALL_PRICE, self.nearest_ask_wall_price),
            _make(FEATURE_OB_NEAREST_ASK_WALL_NOTIONAL, self.nearest_ask_wall_notional),
            _make(FEATURE_OB_ASK_WALL_DISTANCE_ATR, self.ask_wall_distance_atr),
            _make(FEATURE_OB_IS_STALE, 1.0 if self.is_stale else 0.0),
        )


def from_port_snapshot(
    port_snapshot: PortOrderBookSnapshot,
    *,
    as_of: datetime | None = None,
    source: str = "BINANCE_USDM_REST",
) -> OrderBookSnapshot:
    """Chuyển đổi ``ports.market_data.OrderBookSnapshot`` sang domain ``OrderBookSnapshot``."""
    effective_as_of = (as_of or port_snapshot.received_at).astimezone(UTC)
    return OrderBookSnapshot(
        symbol=port_snapshot.symbol,
        as_of=effective_as_of,
        event_at=port_snapshot.observed_at.astimezone(UTC),
        received_at=port_snapshot.received_at.astimezone(UTC),
        bids=tuple(OrderBookLevel(price=p, quantity=q) for p, q in port_snapshot.bids),
        asks=tuple(OrderBookLevel(price=p, quantity=q) for p, q in port_snapshot.asks),
        source=source,
    )


def _stale_context(
    *,
    symbol: str,
    as_of: datetime,
    event_at: datetime | None = None,
    received_at: datetime | None = None,
    source: str = "BINANCE_USDM_REST",
    reason: str,
) -> OrderBookContext:
    return OrderBookContext(
        symbol=symbol,
        as_of=as_of,
        event_at=event_at,
        received_at=received_at,
        is_stale=True,
        best_bid=None,
        best_ask=None,
        mid_price=None,
        spread_bps=None,
        obi_50bps=None,
        obi_100bps=None,
        nearest_bid_wall_price=None,
        nearest_bid_wall_notional=None,
        bid_wall_distance_atr=None,
        nearest_ask_wall_price=None,
        nearest_ask_wall_notional=None,
        ask_wall_distance_atr=None,
        bid_walls=(),
        ask_walls=(),
        source=source,
        stale_reason=reason,
    )


def _band_obi(
    bids: Sequence[OrderBookLevel],
    asks: Sequence[OrderBookLevel],
    *,
    mid_price: Decimal,
    band_ratio: Decimal,
) -> Decimal | None:
    low_bound = mid_price * (Decimal("1") - band_ratio)
    high_bound = mid_price * (Decimal("1") + band_ratio)
    bid_notional = sum(
        (lvl.notional for lvl in bids if low_bound <= lvl.price <= high_bound),
        Decimal("0"),
    )
    ask_notional = sum(
        (lvl.notional for lvl in asks if low_bound <= lvl.price <= high_bound),
        Decimal("0"),
    )
    total = bid_notional + ask_notional
    if total <= Decimal("0"):
        return None
    return (bid_notional - ask_notional) / total


def _detect_side_walls(
    levels: Sequence[OrderBookLevel],
    *,
    side: str,
    mid_price: Decimal,
    atr_14: Decimal | None,
    config: OrderBookConfig,
) -> tuple[OrderBookWall, ...]:
    positive = [lvl for lvl in levels if lvl.quantity > Decimal("0")]
    if not positive:
        return ()

    step: Decimal | None = config.tick_size
    if step is None and config.bucket_bps is not None:
        step = (mid_price * config.bucket_bps) / _BPS_DIVISOR

    # Gom cụm theo bước giá nếu có cấu hình; ngược lại giữ từng mức giá gốc
    clusters: list[tuple[Decimal, Decimal, int]] = []
    if step is not None and step > Decimal("0"):
        buckets: dict[int, list[OrderBookLevel]] = {}
        for lvl in positive:
            bucket_idx = int((lvl.price / step).to_integral_value())
            buckets.setdefault(bucket_idx, []).append(lvl)
        for group in buckets.values():
            total_notional = sum((item.notional for item in group), Decimal("0"))
            rep = max(group, key=lambda item: item.notional)
            clusters.append((rep.price, total_notional, len(group)))
    else:
        for lvl in positive:
            clusters.append((lvl.price, lvl.notional, 1))

    if not clusters:
        return ()

    notionals = [c[1] for c in clusters]
    med_notional = Decimal(str(median(notionals)))
    threshold = max(config.wall_min_notional, med_notional * config.wall_median_multiplier)

    walls: list[OrderBookWall] = []
    valid_atr = atr_14 is not None and atr_14 > Decimal("0")
    for price, notional, count in clusters:
        if notional >= threshold and (
            config.wall_median_multiplier <= Decimal("1") or notional > med_notional
        ):
            dist_atr = (
                (abs(price - mid_price) / atr_14) if valid_atr and atr_14 is not None else None
            )
            walls.append(
                OrderBookWall(
                    side=side,
                    price=price,
                    notional=notional,
                    distance_atr=dist_atr,
                    level_count=count,
                )
            )

    walls.sort(key=lambda w: abs(w.price - mid_price))
    return tuple(walls)


def compute_order_book_context(
    snapshot: OrderBookSnapshot | None,
    *,
    as_of: datetime,
    atr_14: Decimal | None = None,
    config: OrderBookConfig | None = None,
) -> OrderBookContext:
    """Tính OBI, Spread bps và Bid/Ask Walls point-in-time tại ``as_of`` (fail-closed)."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of phải có múi giờ.")
    instant = as_of.astimezone(UTC)
    cfg = config or OrderBookConfig()

    if snapshot is None:
        return _stale_context(symbol="", as_of=instant, reason="MISSING_SNAPSHOT")

    ev_at = snapshot.event_at.astimezone(UTC)
    rx_at = snapshot.received_at.astimezone(UTC)
    snap_as_of = snapshot.as_of.astimezone(UTC)

    # Bất biến point-in-time: tuyệt đối không dùng dữ liệu tương lai
    if ev_at > instant or rx_at > instant or snap_as_of > instant:
        return _stale_context(
            symbol=snapshot.symbol,
            as_of=instant,
            event_at=ev_at,
            received_at=rx_at,
            source=snapshot.source,
            reason="FUTURE_TIMESTAMP",
        )

    # Kiểm tra độ tươi dữ liệu
    if (instant - rx_at) > cfg.max_age or (instant - ev_at) > cfg.max_age:
        return _stale_context(
            symbol=snapshot.symbol,
            as_of=instant,
            event_at=ev_at,
            received_at=rx_at,
            source=snapshot.source,
            reason="STALE_AGE_EXCEEDED",
        )

    positive_bids = tuple(lvl for lvl in snapshot.bids if lvl.quantity > Decimal("0"))
    positive_asks = tuple(lvl for lvl in snapshot.asks if lvl.quantity > Decimal("0"))
    if not positive_bids or not positive_asks:
        return _stale_context(
            symbol=snapshot.symbol,
            as_of=instant,
            event_at=ev_at,
            received_at=rx_at,
            source=snapshot.source,
            reason="EMPTY_BOOK_SIDE",
        )

    best_bid = max(lvl.price for lvl in positive_bids)
    best_ask = min(lvl.price for lvl in positive_asks)
    if best_bid >= best_ask:
        return _stale_context(
            symbol=snapshot.symbol,
            as_of=instant,
            event_at=ev_at,
            received_at=rx_at,
            source=snapshot.source,
            reason="CROSSED_OR_LOCKED_BOOK",
        )

    mid_price = (best_bid + best_ask) / Decimal("2")
    spread_bps = ((best_ask - best_bid) / mid_price) * _BPS_DIVISOR
    obi_50bps = _band_obi(positive_bids, positive_asks, mid_price=mid_price, band_ratio=_FIFTY_BPS)
    obi_100bps = _band_obi(
        positive_bids, positive_asks, mid_price=mid_price, band_ratio=_HUNDRED_BPS
    )

    bid_walls = _detect_side_walls(
        positive_bids, side="BID", mid_price=mid_price, atr_14=atr_14, config=cfg
    )
    ask_walls = _detect_side_walls(
        positive_asks, side="ASK", mid_price=mid_price, atr_14=atr_14, config=cfg
    )

    nearest_bid = bid_walls[0] if bid_walls else None
    nearest_ask = ask_walls[0] if ask_walls else None

    return OrderBookContext(
        symbol=snapshot.symbol,
        as_of=instant,
        event_at=ev_at,
        received_at=rx_at,
        is_stale=False,
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        spread_bps=spread_bps,
        obi_50bps=obi_50bps,
        obi_100bps=obi_100bps,
        nearest_bid_wall_price=nearest_bid.price if nearest_bid is not None else None,
        nearest_bid_wall_notional=nearest_bid.notional if nearest_bid is not None else None,
        bid_wall_distance_atr=nearest_bid.distance_atr if nearest_bid is not None else None,
        nearest_ask_wall_price=nearest_ask.price if nearest_ask is not None else None,
        nearest_ask_wall_notional=nearest_ask.notional if nearest_ask is not None else None,
        ask_wall_distance_atr=nearest_ask.distance_atr if nearest_ask is not None else None,
        bid_walls=bid_walls,
        ask_walls=ask_walls,
        source=snapshot.source,
        stale_reason=None,
    )


def observation_from_snapshot_features(snapshot: DecisionSnapshot) -> OrderBookContext | None:
    """Khôi phục ``OrderBookContext`` từ các feature ``orderbook_*`` trên ``DecisionSnapshot``."""
    by_name: dict[str, FeatureObservation] = {}
    for feat in snapshot.features:
        if feat.name.startswith("orderbook_") and feat.available_at <= snapshot.decision_time:
            existing = by_name.get(feat.name)
            if existing is None or feat.available_at >= existing.available_at:
                by_name[feat.name] = feat

    if not by_name:
        return None

    stale_feat = by_name.get(FEATURE_OB_IS_STALE)
    is_stale = any(f.quality is FeatureQuality.STALE for f in by_name.values()) or (
        stale_feat is not None and stale_feat.value is not None and stale_feat.value >= 0.5
    )
    latest_at = max(f.available_at for f in by_name.values()).astimezone(UTC)
    as_of = snapshot.decision_time.astimezone(UTC)

    if is_stale:
        return _stale_context(
            symbol=snapshot.symbol,
            as_of=as_of,
            received_at=latest_at,
            reason="SNAPSHOT_FEATURE_STALE",
        )

    def _ready_dec(name: str) -> Decimal | None:
        feat = by_name.get(name)
        if feat is None or feat.quality is not FeatureQuality.READY or feat.value is None:
            return None
        return Decimal(str(feat.value))

    return OrderBookContext(
        symbol=snapshot.symbol,
        as_of=as_of,
        event_at=latest_at,
        received_at=latest_at,
        is_stale=False,
        best_bid=_ready_dec(FEATURE_OB_BEST_BID),
        best_ask=_ready_dec(FEATURE_OB_BEST_ASK),
        mid_price=_ready_dec(FEATURE_OB_MID_PRICE),
        spread_bps=_ready_dec(FEATURE_OB_SPREAD_BPS),
        obi_50bps=_ready_dec(FEATURE_OB_OBI_50BPS),
        obi_100bps=_ready_dec(FEATURE_OB_OBI_100BPS),
        nearest_bid_wall_price=_ready_dec(FEATURE_OB_NEAREST_BID_WALL_PRICE),
        nearest_bid_wall_notional=_ready_dec(FEATURE_OB_NEAREST_BID_WALL_NOTIONAL),
        bid_wall_distance_atr=_ready_dec(FEATURE_OB_BID_WALL_DISTANCE_ATR),
        nearest_ask_wall_price=_ready_dec(FEATURE_OB_NEAREST_ASK_WALL_PRICE),
        nearest_ask_wall_notional=_ready_dec(FEATURE_OB_NEAREST_ASK_WALL_NOTIONAL),
        ask_wall_distance_atr=_ready_dec(FEATURE_OB_ASK_WALL_DISTANCE_ATR),
    )


__all__ = [
    "DEFAULT_ORDER_BOOK_MAX_AGE",
    "FEATURE_OB_ASK_WALL_DISTANCE_ATR",
    "FEATURE_OB_BEST_ASK",
    "FEATURE_OB_BEST_BID",
    "FEATURE_OB_BID_WALL_DISTANCE_ATR",
    "FEATURE_OB_IS_STALE",
    "FEATURE_OB_MID_PRICE",
    "FEATURE_OB_NEAREST_ASK_WALL_NOTIONAL",
    "FEATURE_OB_NEAREST_ASK_WALL_PRICE",
    "FEATURE_OB_NEAREST_BID_WALL_NOTIONAL",
    "FEATURE_OB_NEAREST_BID_WALL_PRICE",
    "FEATURE_OB_OBI_100BPS",
    "FEATURE_OB_OBI_50BPS",
    "FEATURE_OB_SPREAD_BPS",
    "ORDER_BOOK_CONTEXT_VERSION",
    "OrderBookConfig",
    "OrderBookContext",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "OrderBookWall",
    "compute_order_book_context",
    "from_port_snapshot",
    "observation_from_snapshot_features",
]
