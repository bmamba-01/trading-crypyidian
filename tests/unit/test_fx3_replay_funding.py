"""FX-3 lát 3b (CRY-198): bundle replay mang funding đã chốt; replay trả/nhận như PAPER."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.sig4_runtime_replay import (
    DAY,
    build_h18_dataset_runtime_replay,
    build_sig4_bundle,
)
from tests.unit.test_backtest_dataset_replay_bundle import replay_input

from trading_bot.application.backtest_dataset_funding import (
    ReplayFundingReference,
    ReplayFundingSeries,
    funding_series_from_rows,
)
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetBundleMismatch,
    BacktestDatasetReplayBundleReference,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_dataset_replay_bundle_loader import (
    BacktestDatasetReplayBundleLoader,
)
from trading_bot.domain.execution.funding import FundingSettlement, settled_funding_amount
from trading_bot.domain.execution.paper import PaperExecutionGateway

WORKSPACE = UUID("51640000-0000-4000-8000-000000000001")


def _row(at: datetime, rate: str, mark: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        funding_time=at,
        funding_rate=Decimal(rate),
        mark_price=None if mark is None else Decimal(mark),
    )


# --- chuỗi funding --------------------------------------------------------------------------------
def test_series_keeps_window_rows_drops_missing_mark_and_hash_is_deterministic() -> None:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows = [
        _row(start + timedelta(hours=8), "0.0001", "100"),
        _row(start - timedelta(hours=8), "0.0001", "100"),  # ngoài cửa sổ
        _row(start, "-0.0002", "101"),
        _row(start + timedelta(hours=16), "0.0003", None),  # thiếu mark price: bỏ và đếm
    ]
    series = funding_series_from_rows(
        rows, symbol="BTCUSDT", window_start=start, window_end=end, as_of=end
    )
    assert [item.funding_time for item in series.settlements] == [
        start,
        start + timedelta(hours=8),
    ]
    assert series.missing_mark_count == 1
    again = funding_series_from_rows(
        list(reversed(rows)), symbol="BTCUSDT", window_start=start, window_end=end, as_of=end
    )
    assert again.content_hash == series.content_hash
    changed = funding_series_from_rows(
        [*rows[:-1], _row(start + timedelta(hours=16), "0.0003", "102")],
        symbol="BTCUSDT",
        window_start=start,
        window_end=end,
        as_of=end,
    )
    assert changed.content_hash != series.content_hash


def test_series_rejects_unordered_or_foreign_settlements() -> None:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    first = FundingSettlement("BTCUSDT", start + timedelta(hours=8), Decimal("0"), Decimal("1"))
    second = FundingSettlement("BTCUSDT", start, Decimal("0"), Decimal("1"))
    other = FundingSettlement("ETHUSDT", start, Decimal("0"), Decimal("1"))
    for settlements in ((first, second), (other,)):
        with pytest.raises(ValueError, match="funding"):
            ReplayFundingSeries(
                source="BINANCE_USDM_REST",
                symbol="BTCUSDT",
                window_start=start,
                window_end=start + timedelta(days=1),
                as_of=start + timedelta(days=1),
                settlements=settlements,
            )


# --- bundle schema 2 ----------------------------------------------------------------------------
def _funding_for(bundle: Any, *times: datetime) -> ReplayFundingSeries:
    return funding_series_from_rows(
        [_row(at, "0.0001", "100") for at in times],
        symbol=bundle.symbol,
        window_start=bundle.evaluation_start,
        window_end=bundle.range_end,
        as_of=bundle.range_end,
    )


def test_bundle_without_funding_keeps_schema_1_hash_and_funding_changes_hash() -> None:
    inputs = (replay_input("15m"), replay_input("1h"))
    legacy = build_backtest_dataset_replay_bundle(inputs)
    funded = build_backtest_dataset_replay_bundle(
        inputs, funding=_funding_for(legacy, legacy.evaluation_start + timedelta(minutes=30))
    )
    assert BacktestDatasetReplayBundleReference.from_bundle(legacy).schema_version == 1
    assert "funding" not in BacktestDatasetReplayBundleReference.from_bundle(legacy).canonical_json
    reference = BacktestDatasetReplayBundleReference.from_bundle(funded)
    assert reference.schema_version == 2
    assert funded.dataset_hash != legacy.dataset_hash
    # Hash tham chiếu bằng dataset hash của bundle và roundtrip canonical được.
    assert reference.bundle_hash == funded.dataset_hash
    parsed = BacktestDatasetReplayBundleReference.from_canonical_json(
        reference.canonical_json, expected_bundle_hash=funded.dataset_hash
    )
    assert parsed == reference
    assert isinstance(parsed.funding, ReplayFundingReference)


def test_bundle_rejects_funding_for_other_symbol_or_window() -> None:
    inputs = (replay_input("15m"), replay_input("1h"))
    legacy = build_backtest_dataset_replay_bundle(inputs)
    wrong_window = funding_series_from_rows(
        [],
        symbol=legacy.symbol,
        window_start=legacy.evaluation_start,
        window_end=legacy.range_end - timedelta(minutes=15),
        as_of=legacy.range_end,
    )
    with pytest.raises(BacktestDatasetBundleMismatch, match="Funding"):
        build_backtest_dataset_replay_bundle(inputs, funding=wrong_window)


class _Lookup:
    def __init__(self, reference: BacktestDatasetReplayBundleReference) -> None:
        self.reference = reference

    async def get_by_hash(self, *, workspace_id: UUID, bundle_hash: str) -> Any:
        del workspace_id, bundle_hash
        return self.reference


class _ChildLoader:
    def __init__(self, inputs: dict[str, Any]) -> None:
        self.inputs = inputs

    async def load_for_replay(self, *, workspace_id: UUID, dataset_hash: str, now: datetime) -> Any:
        del workspace_id, now
        return self.inputs[dataset_hash]


class _FundingSource:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def settled_in_window(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.append(kwargs)
        return self.rows


def test_loader_reloads_funding_rows_and_rejects_changed_history() -> None:
    inputs = (replay_input("15m"), replay_input("1h"))
    at = inputs[0].manifest.evaluation_start + timedelta(minutes=30)
    legacy = build_backtest_dataset_replay_bundle(inputs)
    funded = build_backtest_dataset_replay_bundle(inputs, funding=_funding_for(legacy, at))
    reference = BacktestDatasetReplayBundleReference.from_bundle(funded)
    children = {item.manifest.dataset_hash: item for item in inputs}
    source = _FundingSource([_row(at, "0.0001", "100")])
    loader = BacktestDatasetReplayBundleLoader(
        repository=_Lookup(reference),  # type: ignore[arg-type]
        replay_loader=_ChildLoader(children),  # type: ignore[arg-type]
        funding_source=source,
    )
    loaded = asyncio.run(
        loader.load_for_replay(
            workspace_id=funded.workspace_id, bundle_hash=funded.dataset_hash, now=funded.range_end
        )
    )
    assert loaded.funding == funded.funding
    assert source.calls[0]["as_of"] == funded.range_end
    assert source.calls[0]["window_start"] == funded.evaluation_start

    source.rows = [_row(at, "0.0009", "100")]  # lịch sử bị đổi → hash lệch
    with pytest.raises(BacktestDatasetBundleMismatch):
        asyncio.run(
            loader.load_for_replay(
                workspace_id=funded.workspace_id,
                bundle_hash=funded.dataset_hash,
                now=funded.range_end,
            )
        )
    no_source = BacktestDatasetReplayBundleLoader(
        repository=_Lookup(reference),  # type: ignore[arg-type]
        replay_loader=_ChildLoader(children),  # type: ignore[arg-type]
    )
    with pytest.raises(BacktestDatasetBundleMismatch, match="nguồn funding"):
        asyncio.run(
            no_source.load_for_replay(
                workspace_id=funded.workspace_id,
                bundle_hash=funded.dataset_hash,
                now=funded.range_end,
            )
        )


# --- replay áp funding như PAPER ----------------------------------------------------------------
def _quantity_at(gateway: PaperExecutionGateway, position: Any, at: datetime) -> Decimal:
    """Khối lượng mở tại mốc theo quy tắc ledger: trừ fill thoát có ``filled_at <= mốc``."""
    exits = sum(
        (
            receipt.fill.quantity
            for receipt in gateway._close_keys.values()  # noqa: SLF001 - soi state PAPER
            if receipt.fill is not None
            and receipt.position.intent_id == position.intent_id
            and receipt.fill.filled_at <= at
        ),
        Decimal(0),
    )
    return position.initial_quantity - exits


def test_replay_charges_funding_for_position_held_across_funding_time() -> None:
    base = build_sig4_bundle(WORKSPACE)
    # Lệnh H18 mở 10:05 và đóng nến 10:15 (fixture SIG-4). Mốc 10:05 trùng lúc vào (không thu),
    # 10:10 và 10:15 trong lúc giữ (thu), 10:30 sau khi đóng (không thu), 09:00 trước khi mở.
    times = [
        DAY + timedelta(hours=9),
        *(DAY + timedelta(hours=10, minutes=m) for m in (5, 10, 15, 30)),
    ]
    funding = funding_series_from_rows(
        [_row(at, "0.0001", "100") for at in times],
        symbol=base.symbol,
        window_start=base.evaluation_start,
        window_end=base.range_end,
        as_of=base.range_end,
    )
    bundle = build_backtest_dataset_replay_bundle(base.components, funding=funding)
    gateways: list[PaperExecutionGateway] = []
    _, comparison = build_h18_dataset_runtime_replay(WORKSPACE, bundle=bundle, gateways=gateways)
    assert comparison.is_complete
    (trade,) = comparison.runs[0].trade_outcomes
    gateway = gateways[0]
    (position,) = list(gateway._positions.values())  # noqa: SLF001
    charged = [DAY + timedelta(hours=10, minutes=m) for m in (10, 15)]
    settlements = {item.funding_time: item for item in funding.settlements}
    assert position.funding_event_ids == tuple(settlements[at].event_id for at in charged)
    expected = sum(
        (
            settled_funding_amount(
                direction=position.direction,
                quantity=_quantity_at(gateway, position, at),
                contract_multiplier=position.contract_multiplier,
                settlement=settlements[at],
            )
            for at in charged
        ),
        Decimal(0),
    )
    assert expected != 0
    # Short + rate dương → được nhận (chi phí âm), đi thẳng vào EvaluationTrade và báo cáo.
    assert expected < 0
    assert position.funding_cost == expected
    assert trade.funding == expected
    _, legacy = build_h18_dataset_runtime_replay(WORKSPACE)
    (legacy_trade,) = legacy.runs[0].trade_outcomes
    assert legacy_trade.funding == 0
    assert trade.gross_pnl == legacy_trade.gross_pnl
