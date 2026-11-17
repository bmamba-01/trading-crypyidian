"""Cảnh báo đồng thuận giả cho Strategy Card trên chuỗi đầu ra node thật (SIG-6, sửa G7 & CRY-435).

Quy ước ``sig6-consensus-v1``:

- Chuỗi = đầu ra L1–L3 của card theo từng nến đã đóng (cùng công thức ``execute``, không nhìn
  trước, bỏ qua nến vi phạm L0). Không có nến hoặc ít hơn ``MIN_CONSENSUS_SAMPLES`` nến hợp lệ
  → ``insufficient_data``: **không** coi là trực giao (trước đây gán cứng "không cảnh báo").
- Hai node có |r Pearson| ≥ ``CONSENSUS_THRESHOLD`` bị xem là cùng đo một thứ; số **điều kiện
  độc lập** = số nhóm liên thông của đồ thị "tương quan cao" trên các node thực sự đo được và
  có biến thiên (đếm khi gộp các node trùng).
- Node luôn giữ hằng số (ví dụ hằng số ``0`` khi chưa đủ dữ liệu trường ``volume``/``open_time``/
  ``taker_buy_base_volume`` hoặc chưa qua warmup) được loại khỏi tập đếm độc lập thay vì cộng
  thành một nhóm riêng ảo, bảo đảm không làm sai lệch tỷ số ``x/y`` trên ``PriceBar`` hay
  ``OHLCVBar``. Nếu mọi node đều phẳng (không có biến thiên) → trả ``insufficient_data``.
- Chỉ để hiển thị/xác minh; không đổi quyết định vào lệnh.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from trading_bot.application.strategy_pipeline.pipeline_engine import (
    NUMERIC_LAYERS,
    WARMUP_SOURCE_INSUFFICIENT,
    PriceBarLike,
    StrategyPipelineEngine,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import DecisionSnapshot, MarketSeries, SeriesQuality
from trading_bot.domain.strategy_card.models import CompositionLayer
from trading_bot.domain.strategy_card.orthogonality import AntiConsensusAnalyzer

if TYPE_CHECKING:
    from trading_bot.domain.strategy_card.models import GraphNode
    from trading_bot.domain.strategy_card.schema import StrategyCard

CONSENSUS_VERSION = "sig6-consensus-v1"
CONSENSUS_THRESHOLD = 0.85
MIN_CONSENSUS_SAMPLES = 50
# Số nến đã đóng gần nhất dùng để đo (đủ ổn định Pearson, nhẹ cho trang admin).
CONSENSUS_BAR_LIMIT = 300

_VARIANCE_EPSILON = 1e-12
_UNMEASURED_SOURCES = frozenset({WARMUP_SOURCE_INSUFFICIENT, "taker_flow_unavailable"})
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}
_SYNTHETIC_ORIGIN = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PriceBar:
    """Nến cho phân tích đồng thuận: OHLC bắt buộc, hỗ trợ thêm volume/open_time/taker."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    open_time: datetime | None = None
    taker_buy_base_volume: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ConsensusSummary:
    """Kết quả đo đồng thuận giả của một card trên một chuỗi nến."""

    status: str  # "ok" | "warning" | "insufficient_data"
    sample_size: int
    total_conditions: int
    independent_conditions: int | None
    groups: tuple[tuple[str, ...], ...]
    high_correlation_pairs: tuple[tuple[str, str, float], ...]
    warnings: tuple[str, ...]
    version: str = CONSENSUS_VERSION
    inactive_conditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _BarEntry:
    raw: PriceBarLike
    ohlcv: OHLCVBar
    has_open_time: bool
    has_volume: bool
    has_taker_flow: bool


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        converted = Decimal(str(value))
        return converted if converted.is_finite() else None
    return None


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _resolve_series_meta(card: StrategyCard, bars: Sequence[PriceBarLike]) -> tuple[str, str]:
    symbols = card.compatibility.target_symbols
    symbol = symbols[0] if symbols else "BTCUSDT"
    timeframes = card.compatibility.timeframes
    timeframe = timeframes[0] if timeframes else "15m"
    for bar in bars:
        if isinstance(bar, OHLCVBar):
            return bar.symbol, bar.timeframe
    return symbol, timeframe


def _prepare_clean_entries(
    card: StrategyCard,
    bars: Sequence[PriceBarLike],
    engine: StrategyPipelineEngine,
) -> tuple[list[_BarEntry], str, str]:
    symbol, timeframe = _resolve_series_meta(card, bars)
    step = timedelta(minutes=_TIMEFRAME_MINUTES.get(timeframe, 15))
    entries: list[_BarEntry] = []

    for bar in bars:
        if engine._check_data_hygiene(bar) is not None:
            continue

        raw_open_time = getattr(bar, "open_time", None)
        raw_volume = _optional_decimal(getattr(bar, "volume", None))
        raw_taker = _optional_decimal(getattr(bar, "taker_buy_base_volume", None))

        has_open_time = isinstance(raw_open_time, datetime)
        has_volume = raw_volume is not None and raw_volume >= Decimal("0")
        has_taker_flow = (
            has_volume
            and raw_volume is not None
            and raw_volume > Decimal("0")
            and raw_taker is not None
            and Decimal("0") <= raw_taker <= raw_volume
        )

        if (
            isinstance(bar, OHLCVBar)
            and bar.timeframe == timeframe
            and bar.symbol == symbol
            and (not entries or bar.open_time > entries[-1].ohlcv.open_time)
        ):
            ohlcv = bar
        else:
            if isinstance(raw_open_time, datetime):
                candidate_t = _normalize_utc(raw_open_time)
            else:
                candidate_t = _SYNTHETIC_ORIGIN + len(entries) * step
            if entries and candidate_t <= entries[-1].ohlcv.open_time:
                candidate_t = entries[-1].ohlcv.open_time + step
            close_t = candidate_t + step - timedelta(microseconds=1)
            vol_dec = (
                raw_volume
                if raw_volume is not None and raw_volume >= Decimal("0")
                else Decimal("0")
            )
            tbuy_dec = (
                raw_taker
                if raw_taker is not None and Decimal("0") <= raw_taker <= vol_dec
                else None
            )
            ohlcv = OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=candidate_t,
                close_time=close_t,
                event_at=close_t,
                received_at=close_t,
                available_at=close_t,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=vol_dec,
                taker_buy_base_volume=tbuy_dec,
                is_closed=True,
            )

        entries.append(
            _BarEntry(
                raw=bar,
                ohlcv=ohlcv,
                has_open_time=has_open_time,
                has_volume=has_volume,
                has_taker_flow=has_taker_flow,
            )
        )
    return entries, symbol, timeframe


def _compute_l3_point_in_time(
    engine: StrategyPipelineEngine,
    node: GraphNode,
    node_values: dict[str, Any],
    entries: Sequence[_BarEntry],
    index: int,
    snapshot: DecisionSnapshot,
    *,
    l2_vwap_ready: bool,
) -> tuple[Decimal, str]:
    """Đánh giá nút L3 point-in-time tương đương ``execute``, an toàn khi thiếu trường mở rộng."""
    op = node.operation.lower()
    params = node.parameters

    if "squeeze" in op:
        lookback = int(params.get("lookback", 20))
        if index + 1 < 60 or any(
            not entries[i].has_volume for i in range(max(0, index - lookback), index + 1)
        ):
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT

    if "vwap" in op:
        if not entries[index].has_open_time or not entries[index].has_volume or not l2_vwap_ready:
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT
        # Nếu chuỗi có trộn nến thiếu open_time, chỉ truyền các nến có open_time thật cho L3 VWAP.
        if not all(entries[i].has_open_time and entries[i].has_volume for i in range(index + 1)):
            timed_bars = tuple(
                entries[i].ohlcv
                for i in range(index + 1)
                if entries[i].has_open_time and entries[i].has_volume
            )
            vwap_snapshot = DecisionSnapshot(
                symbol=snapshot.symbol,
                decision_time=snapshot.decision_time,
                snapshot_id=f"{snapshot.snapshot_id}-vwap",
                series=(
                    MarketSeries(
                        timeframe=snapshot.series[0].timeframe,
                        bars=timed_bars,
                        quality=SeriesQuality.READY,
                    ),
                ),
                features=(),
            )
            val, src, _ = engine._compute_l3_node_with_source(
                node, node_values, bar=entries[index].ohlcv, snapshot=vwap_snapshot
            )
            return val, src

    if "taker_flow" in op:
        lookback = int(params.get("lookback", 20))
        if index + 1 < lookback + 1:
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT
        if any(not entries[i].has_volume for i in range(index - lookback, index + 1)) or any(
            not entries[i].has_taker_flow for i in range(index - 2, index + 1)
        ):
            return Decimal("0"), "taker_flow_unavailable"

    val, src, _ = engine._compute_l3_node_with_source(
        node, node_values, bar=entries[index].ohlcv, snapshot=snapshot
    )
    return val, src


def _evaluate_series_and_sources(
    card: StrategyCard,
    bars: Sequence[PriceBarLike],
    engine: StrategyPipelineEngine,
) -> tuple[dict[str, list[float]], dict[str, list[str]] | None]:
    if type(engine).numeric_node_series is not StrategyPipelineEngine.numeric_node_series:
        return engine.numeric_node_series(card, bars), None

    nodes = [node for node in card.nodes if node.layer in NUMERIC_LAYERS]
    series: dict[str, list[float]] = {node.node_id: [] for node in nodes}
    sources_by_node: dict[str, list[str]] = {node.node_id: [] for node in nodes}
    if not nodes:
        return series, sources_by_node

    entries, symbol, timeframe = _prepare_clean_entries(card, bars, engine)
    if not entries:
        return series, sources_by_node

    l1_nodes = [n for n in nodes if n.layer == CompositionLayer.L1_CANDLE_ANATOMY]
    l2_nodes = [n for n in nodes if n.layer == CompositionLayer.L2_INDICATORS]
    l3_nodes = [n for n in nodes if n.layer == CompositionLayer.L3_ALGORITHMS]

    # Chỉ truyền bản chuẩn hóa UTC cho _l2_from_bars khi nến thật sự có cả open_time và volume.
    l2_bars_all = [
        entry.ohlcv if (entry.has_open_time and entry.has_volume) else entry.raw
        for entry in entries
    ]
    ohlcv_all = tuple(entry.ohlcv for entry in entries)

    for index, entry in enumerate(entries):
        values: dict[str, Any] = {}
        node_sources: dict[str, str] = {}
        l2_prefix = l2_bars_all[: index + 1]
        l2_bar = l2_bars_all[index]

        for node in l1_nodes:
            values[node.node_id] = engine._compute_l1_node(node.operation, entry.raw)
            node_sources[node.node_id] = "l1"

        l2_vwap_ready = False
        for node in l2_nodes:
            val, src, _ = engine._l2_from_bars(node, l2_bar, l2_prefix)
            values[node.node_id] = val
            node_sources[node.node_id] = src
            if "vwap" in node.operation.lower() and src not in _UNMEASURED_SOURCES:
                l2_vwap_ready = True

        if l3_nodes:
            ohlcv_prefix = ohlcv_all[: index + 1]
            snapshot = DecisionSnapshot(
                symbol=symbol,
                decision_time=entry.ohlcv.close_time,
                snapshot_id=f"sig6-{index}",
                series=(
                    MarketSeries(
                        timeframe=timeframe,
                        bars=ohlcv_prefix,
                        quality=SeriesQuality.READY,
                    ),
                ),
                features=(),
            )
            for node in l3_nodes:
                val, src = _compute_l3_point_in_time(
                    engine,
                    node,
                    values,
                    entries,
                    index,
                    snapshot,
                    l2_vwap_ready=l2_vwap_ready,
                )
                values[node.node_id] = val
                node_sources[node.node_id] = src

        for node in nodes:
            series[node.node_id].append(float(values[node.node_id]))
            sources_by_node[node.node_id].append(node_sources[node.node_id])

    return series, sources_by_node


def card_node_series(
    card: StrategyCard,
    bars: Sequence[PriceBarLike],
    *,
    engine: StrategyPipelineEngine | None = None,
) -> dict[str, list[float]]:
    """Trả chuỗi đầu ra L1–L3 point-in-time đầy đủ (gồm cả L3) cho phân tích đồng thuận."""
    resolved_engine = engine or StrategyPipelineEngine()
    series, _ = _evaluate_series_and_sources(card, bars, resolved_engine)
    return series


def _has_variance(values: Sequence[float]) -> bool:
    if len(values) < 2:
        return False
    mean_val = sum(values) / len(values)
    return sum((val - mean_val) ** 2 for val in values) >= _VARIANCE_EPSILON


def _is_active_node(
    node_id: str,
    values: Sequence[float],
    sources_by_node: dict[str, list[str]] | None,
) -> bool:
    if sources_by_node is not None:
        node_sources = sources_by_node.get(node_id, ())
        if node_sources and all(src in _UNMEASURED_SOURCES for src in node_sources):
            return False
    return _has_variance(values)


def _groups(nodes: Sequence[str], pairs: Sequence[tuple[str, str, float]]) -> list[list[str]]:
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right, _ in pairs:
        parent[find(left)] = find(right)
    grouped: dict[str, list[str]] = {}
    for node in sorted(nodes):
        grouped.setdefault(find(node), []).append(node)
    return sorted(grouped.values())


def card_consensus(
    card: StrategyCard,
    bars: Sequence[PriceBarLike],
    *,
    threshold: float = CONSENSUS_THRESHOLD,
    min_samples: int = MIN_CONSENSUS_SAMPLES,
    engine: StrategyPipelineEngine | None = None,
) -> ConsensusSummary:
    """Đo tương quan giữa các node số của card trên ``bars`` (tăng dần theo thời gian)."""
    resolved_engine = engine or StrategyPipelineEngine()
    series, sources_by_node = _evaluate_series_and_sources(card, bars, resolved_engine)
    nodes = sorted(series)
    sample = len(next(iter(series.values()))) if series else len(bars)
    if not series or sample < min_samples:
        return ConsensusSummary(
            status="insufficient_data",
            sample_size=sample,
            total_conditions=len(nodes),
            independent_conditions=None,
            groups=(),
            high_correlation_pairs=(),
            warnings=(),
            inactive_conditions=(),
        )

    active_nodes = [
        node_id for node_id in nodes if _is_active_node(node_id, series[node_id], sources_by_node)
    ]
    inactive_nodes = tuple(node_id for node_id in nodes if node_id not in active_nodes)

    if not active_nodes:
        # Mọi node đều phẳng/thiếu trường đo lường → không thể kiểm chứng trực giao.
        return ConsensusSummary(
            status="insufficient_data",
            sample_size=sample,
            total_conditions=len(nodes),
            independent_conditions=None,
            groups=(),
            high_correlation_pairs=(),
            warnings=(),
            inactive_conditions=inactive_nodes,
        )

    active_series = {node_id: series[node_id] for node_id in active_nodes}
    report = AntiConsensusAnalyzer().check_orthogonality(active_series, threshold=threshold)
    pairs = tuple((left, right, round(r, 4)) for left, right, r in report.high_correlation_pairs)
    groups = _groups(active_nodes, pairs)
    return ConsensusSummary(
        status="ok" if report.is_orthogonal else "warning",
        sample_size=sample,
        total_conditions=len(nodes),
        independent_conditions=len(groups),
        groups=tuple(tuple(group) for group in groups if len(group) > 1),
        high_correlation_pairs=pairs,
        warnings=tuple(report.warnings),
        inactive_conditions=inactive_nodes,
    )
