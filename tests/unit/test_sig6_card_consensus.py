"""SIG-6 lát 1 (sửa G7): cảnh báo đồng thuận giả trên chuỗi đầu ra node của Strategy Card."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest
from tests.fixtures.sig6_consensus_bars import consensus_bars

from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.application.strategy_pipeline.consensus import (
    CONSENSUS_VERSION,
    MIN_CONSENSUS_SAMPLES,
    PriceBar,
    card_consensus,
)
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.application.strategy_pipeline.pipeline_engine import StrategyPipelineEngine
from trading_bot.auth.policy import Principal, Role

VOL_BREAKOUT, MEAN_REVERT = build_default_demo_cards()


_bars = consensus_bars


def test_node_series_has_no_lookahead_and_skips_dirty_bars() -> None:
    engine = StrategyPipelineEngine()
    bars = _bars(80, broken_at=40)
    series = engine.numeric_node_series(MEAN_REVERT, bars)
    assert set(series) == {"l1_body", "l1_upper_wick", "l2_ema", "l3_zscore"}
    assert {len(values) for values in series.values()} == {79}  # nến 40 vi phạm L0 bị bỏ
    shorter = engine.numeric_node_series(MEAN_REVERT, bars[:60])
    for node_id, values in shorter.items():
        # Thêm nến sau không đổi giá trị đã tính ở nến trước (không nhìn trước).
        assert series[node_id][: len(values)] == values
    atr = engine.numeric_node_series(VOL_BREAKOUT, bars)["l2_atr"]
    assert all(value > 0 for value in atr)


def test_consensus_flags_l3_composite_that_copies_price_and_counts_independent_groups() -> None:
    summary = card_consensus(MEAN_REVERT, _bars(120))
    assert summary.version == CONSENSUS_VERSION
    assert summary.status == "warning"
    assert summary.sample_size == 120
    assert summary.total_conditions == 4
    # L3 trung bình có giá đóng cửa áp đảo → gần như trùng L2 (giá đóng cửa): 2 node thành 1 nhóm.
    assert ("l2_ema", "l3_zscore") in {pair[:2] for pair in summary.high_correlation_pairs}
    assert summary.independent_conditions == 3
    assert ("l2_ema", "l3_zscore") in summary.groups
    assert summary.warnings
    assert "l2_ema" in summary.warnings[0]


def test_consensus_needs_minimum_sample_and_never_reports_ok_without_data() -> None:
    few = card_consensus(MEAN_REVERT, _bars(MIN_CONSENSUS_SAMPLES - 1))
    assert few.status == "insufficient_data"
    assert few.warnings == ()
    assert few.independent_conditions is None
    empty = card_consensus(MEAN_REVERT, [])
    assert empty.status == "insufficient_data"
    assert empty.sample_size == 0
    single = card_consensus(VOL_BREAKOUT, _bars(120))
    assert single.status == "ok"  # 2 node khác bản chất (râu dưới vs ATR) không tương quan cao
    assert single.independent_conditions == single.total_conditions == 2


PRINCIPAL = Principal(
    workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
    membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
    user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
    roles=frozenset({Role.OWNER}),
    mfa_verified_at=datetime.now(UTC),
)


@pytest.mark.asyncio
async def test_admin_service_uses_bars_provider_instead_of_hardcoded_no_warning() -> None:
    requested: list[tuple[str, str]] = []

    async def provider(_principal: Principal, symbol: str, timeframe: str) -> list[PriceBar]:
        requested.append((symbol, timeframe))
        return _bars(120)

    views = {
        view.card_id: view
        for view in await StrategyCardAdminService(bars_provider=provider).list_strategy_cards(
            PRINCIPAL
        )
    }
    revert = views[MEAN_REVERT.card_id]
    assert revert.has_consensus_warning is True
    assert revert.consensus_status == "warning"
    assert revert.is_verified is False
    assert revert.independent_conditions == 3
    assert revert.total_conditions == 4
    assert revert.to_dict()["consensus_status"] == "warning"
    assert views[VOL_BREAKOUT.card_id].consensus_status == "ok"
    # Đo trên mã/khung đầu tiên khai báo của thẻ.
    assert ("BTCUSDT", "5m") in requested
    assert ("BTCUSDT", "15m") in requested


@pytest.mark.asyncio
async def test_admin_service_without_bars_reports_insufficient_data_not_ok() -> None:
    views = await StrategyCardAdminService().list_strategy_cards(PRINCIPAL)
    for view in views:
        assert view.consensus_status == "insufficient_data"
        assert view.has_consensus_warning is False
        assert view.consensus_sample_size == 0
        assert view.independent_conditions is None
        # Thiếu dữ liệu không phải bằng chứng trực giao: không chặn xác minh cấu trúc nhưng có
        # trạng thái riêng để giao diện ghi rõ.
        assert view.is_verified is (view.dead_nodes_count == 0)


@pytest.mark.asyncio
async def test_postgres_bars_provider_reads_one_source_read_only_in_workspace() -> None:
    from trading_bot.application.strategy_card_bars import PostgresCardBarsProvider

    calls: dict[str, object] = {}

    class Unit:
        session = object()

        def __init__(self, _sessions: object, **kwargs: object) -> None:
            calls["uow"] = kwargs

        async def __aenter__(self) -> Unit:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def list_market_series(self, **_kwargs: object) -> list[str]:
            return [
                "BTCUSDT|5m|BINANCE_USDM_REST_BACKFILL",
                "BTCUSDT|5m|SEED",
                "ETHUSDT|5m|SEED",
            ]

        async def load_recent_bars(self, **kwargs: object) -> list[dict[str, float]]:
            calls["load"] = kwargs
            return [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]

    provider = PostgresCardBarsProvider(
        cast(Any, object()), unit_of_work=Unit, repository=Repository, limit=300
    )
    bars = await provider(PRINCIPAL, "BTCUSDT", "5m")
    assert bars == [PriceBar(Decimal("1.0"), Decimal("2.0"), Decimal("0.5"), Decimal("1.5"))]
    assert calls["uow"] == {
        "workspace_id": PRINCIPAL.workspace_id,
        "actor_membership_id": PRINCIPAL.membership_id,
        "snapshot_read_only": True,
    }
    load = cast(dict[str, object], calls["load"])
    assert (load["limit"], load["symbol"], load["timeframe"]) == (300, "BTCUSDT", "5m")
    assert load["source"] in {"BINANCE_USDM_REST_BACKFILL", "SEED"}
    assert await provider(PRINCIPAL, "SOLUSDT", "5m") == []  # không có nến → rỗng, không bịa
