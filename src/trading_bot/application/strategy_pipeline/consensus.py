"""Cảnh báo đồng thuận giả cho Strategy Card trên chuỗi đầu ra node thật (SIG-6 lát 1, sửa G7).

Quy ước ``sig6-consensus-v1``:

- Chuỗi = đầu ra L1–L3 của card theo từng nến đã đóng (``numeric_node_series``, cùng công thức
  ``execute``, không nhìn trước). Không có nến hoặc ít hơn ``MIN_CONSENSUS_SAMPLES`` nến hợp lệ
  → ``insufficient_data``: **không** coi là trực giao (trước đây gán cứng "không cảnh báo").
- Hai node có |r Pearson| ≥ ``CONSENSUS_THRESHOLD`` bị xem là cùng đo một thứ; số **điều kiện
  độc lập** = số nhóm liên thông của đồ thị "tương quan cao" (đếm khi gộp các node trùng).
- Chuỗi hằng số có r = 0 với mọi chuỗi (theo ``calculate_correlation_matrix``) nên được tính là
  một nhóm riêng; đây là giới hạn đã biết (node hằng không mang thông tin nhưng không bị gộp).
- Chỉ để hiển thị/xác minh; không đổi quyết định vào lệnh.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_bot.application.strategy_pipeline.pipeline_engine import (
    PriceBarLike,
    StrategyPipelineEngine,
)
from trading_bot.domain.strategy_card.orthogonality import AntiConsensusAnalyzer

if TYPE_CHECKING:
    from trading_bot.domain.strategy_card.schema import StrategyCard

CONSENSUS_VERSION = "sig6-consensus-v1"
CONSENSUS_THRESHOLD = 0.85
MIN_CONSENSUS_SAMPLES = 50
# Số nến đã đóng gần nhất dùng để đo (đủ ổn định Pearson, nhẹ cho trang admin).
CONSENSUS_BAR_LIMIT = 300


@dataclass(frozen=True, slots=True)
class PriceBar:
    """Nến tối thiểu (OHLC) cho phân tích đồng thuận, dựng từ nến đã lưu."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


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
    series = (engine or StrategyPipelineEngine()).numeric_node_series(card, bars)
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
        )
    report = AntiConsensusAnalyzer().check_orthogonality(series, threshold=threshold)
    pairs = tuple((left, right, round(r, 4)) for left, right, r in report.high_correlation_pairs)
    groups = _groups(nodes, pairs)
    return ConsensusSummary(
        status="ok" if report.is_orthogonal else "warning",
        sample_size=sample,
        total_conditions=len(nodes),
        independent_conditions=len(groups),
        groups=tuple(tuple(group) for group in groups if len(group) > 1),
        high_correlation_pairs=pairs,
        warnings=tuple(report.warnings),
    )
