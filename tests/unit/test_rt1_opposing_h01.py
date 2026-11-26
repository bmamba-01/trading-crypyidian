"""Bộ kiểm thử unit cho RT-1/opposing-h01: tính tín hiệu ngược point-in-time
cho H01 trong monitor.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import trading_bot.application.paper_market_context as pmc
from trading_bot.application.paper_market_context import PostgresPaperDecisionContextProvider
from trading_bot.domain.execution.paper import PaperBar, PaperPosition
from trading_bot.domain.execution.position_monitor import monitor_position
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.recipes import ExitRecipe

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
SYMBOL = "BTCUSDT"
T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _make_bar(
    *,
    symbol: str = SYMBOL,
    timeframe: str,
    open_time: datetime,
    interval: timedelta,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    volume: float = 100.0,
) -> OHLCVBar:
    close_time = open_time + interval - timedelta(microseconds=1)
    cursor = f"bar:{symbol}:{timeframe}:{open_time.isoformat()}"
    return OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=close_time,
        available_at=close_time,
        open=Decimal(str(open_price)),
        high=Decimal(str(high_price)),
        low=Decimal(str(low_price)),
        close=Decimal(str(close_price)),
        volume=Decimal(str(volume)),
        quote_volume=Decimal(str(volume * close_price)),
        taker_buy_base_volume=Decimal(str(volume * 0.5)),
        is_closed=True,
        source="TEST_FIXTURE",
        source_cursor=cursor,
        checksum=hashlib.sha256(cursor.encode()).hexdigest(),
    )


def _make_position(
    *, direction: str = "long", recipe: ExitRecipe, is_closed: bool = False
) -> PaperPosition:
    return PaperPosition(
        position_id="pos-1",
        intent_id="intent-1",
        symbol=SYMBOL,
        direction=direction,
        quantity=Decimal("1") if not is_closed else Decimal("0"),
        entry_reference_price=Decimal("50000"),
        entry_execution_price=Decimal("50000"),
        stop_price=Decimal("45000"),
        target_price=Decimal("60000"),
        recipe=recipe,
        contract_multiplier=Decimal("1"),
        protective_stop_quantity=Decimal("1") if not is_closed else Decimal("0"),
        entry_bar_key="bar-0",
        fill_policy_version="paper-fill-v1",
        is_closed=is_closed,
    )


def _generate_h01_bars(mode: str, as_of: datetime) -> dict[str, tuple[OHLCVBar, ...]]:
    """Sinh chuỗi nến 1h và 4h đủ warmup window cho H01 (EMA80, EMA200).

    mode="bearish_cross": EMA20 vừa cắt xuống EMA80, 4h đóng dưới EMA200, ADX >= 20 -> SHORT.
    mode="bullish_cross": EMA20 vừa cắt lên EMA80, 4h đóng trên EMA200, ADX >= 20 -> LONG.
    """
    bars_4h: list[OHLCVBar] = []
    four_h_interval = timedelta(hours=4)
    start_4h = as_of - 620 * four_h_interval
    for i in range(620):
        t = start_4h + i * four_h_interval
        base = 50000.0
        if mode == "bearish_cross" and i >= 615:
            base = 40000.0
        elif mode == "bullish_cross" and i >= 615:
            base = 60000.0
        bars_4h.append(
            _make_bar(
                timeframe="4h",
                open_time=t,
                interval=four_h_interval,
                open_price=base,
                high_price=base + 100.0,
                low_price=base - 100.0,
                close_price=base,
            )
        )

    bars_1h: list[OHLCVBar] = []
    one_h_interval = timedelta(hours=1)
    start_1h = as_of - 620 * one_h_interval
    for i in range(620):
        t = start_1h + i * one_h_interval
        if mode == "bearish_cross":
            if i < 618:
                p = 50000.0 + max(0, i - 400) * 20.0
            elif i == 618:
                p = 55000.0
            else:
                p = 38000.0
        elif mode == "bullish_cross":
            if i < 618:
                p = 50000.0 - max(0, i - 400) * 20.0
            elif i == 618:
                p = 44000.0
            else:
                p = 62000.0
        else:
            p = 50000.0

        bars_1h.append(
            _make_bar(
                timeframe="1h",
                open_time=t,
                interval=one_h_interval,
                open_price=p,
                high_price=p + 200.0,
                low_price=p - 200.0,
                close_price=p,
            )
        )

    return {"1h": tuple(bars_1h), "4h": tuple(bars_4h)}


@pytest.mark.asyncio
async def test_opposing_signal_suppressed_when_recipe_has_no_opposite_signal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bars(*args: Any, **kwargs: Any) -> tuple[OHLCVBar, ...]:
        return ()

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    recipe_no_opp = ExitRecipe("h03-exit-v1", 2.0, opposite_signal_exit=False, max_hold_bars=120)
    position = _make_position(recipe=recipe_no_opp)
    command = SimpleNamespace(
        bar=PaperBar(
            "k-1",
            SYMBOL,
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=1),
            Decimal("50000"),
            Decimal("50100"),
            Decimal("49900"),
            Decimal("50000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h03-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is False


@pytest.mark.asyncio
async def test_opposing_signal_suppressed_when_strategy_bar_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bars(*args: Any, **kwargs: Any) -> tuple[OHLCVBar, ...]:
        return ()

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(recipe=h01_recipe)
    mid_bar_time = T0 + timedelta(minutes=15)
    command = SimpleNamespace(
        bar=PaperBar(
            f"source:{SYMBOL}:1m:{mid_bar_time.isoformat()}",
            SYMBOL,
            mid_bar_time,
            mid_bar_time + timedelta(minutes=1),
            mid_bar_time + timedelta(minutes=1),
            Decimal("50000"),
            Decimal("50100"),
            Decimal("49900"),
            Decimal("50000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is False
    assert evidence.strategy_bar_closed is False


@pytest.mark.asyncio
async def test_opposing_signal_suppressed_when_position_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bars(*args: Any, **kwargs: Any) -> tuple[OHLCVBar, ...]:
        return ()

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(recipe=h01_recipe, is_closed=True)
    command = SimpleNamespace(
        bar=PaperBar(
            "k-1",
            SYMBOL,
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=1),
            Decimal("50000"),
            Decimal("50100"),
            Decimal("49900"),
            Decimal("50000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is False


def test_opposing_signal_sets_pending_exit_and_closes_at_next_bar_open() -> None:
    """FX-4/RT-1: khi opposing_signal=True, monitor chuyển sang pending exit và đóng ở open
    nến sau.
    """
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(recipe=h01_recipe)
    bar1 = PaperBar(
        "bar-1",
        SYMBOL,
        T0,
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=1),
        Decimal("50000"),
        Decimal("51000"),
        Decimal("49000"),
        Decimal("49500"),
    )
    res1 = monitor_position(
        position,
        bar1,
        opposing_signal=True,
        strategy_bar_closed=True,
    )
    assert not res1.position.is_closed
    assert res1.position.pending_exit_reason == "OPPOSING_SIGNAL"

    bar2 = PaperBar(
        "bar-2",
        SYMBOL,
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=2),
        T0 + timedelta(hours=2),
        Decimal("49400"),
        Decimal("49800"),
        Decimal("49000"),
        Decimal("49200"),
    )
    res2 = monitor_position(
        res1.position,
        bar2,
        opposing_signal=False,
        strategy_bar_closed=True,
    )
    assert res2.position.is_closed is True
    assert res2.exit_reason == "OPPOSING_SIGNAL"
    assert res2.position.exit_reason == "OPPOSING_SIGNAL"
    assert res2.exit_reference_price == Decimal("49400")


@pytest.mark.asyncio
async def test_opposing_signal_detected_on_bearish_crossover_for_long_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Khi vị thế LONG gặp tín hiệu SHORT từ nến 1h vừa khép lại, opposing_signal phải là True."""
    bars_by_tf = _generate_h01_bars("bearish_cross", T0)

    async def fake_bars(
        session: Any,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        max_bars: int = 256,
        pinned_source: str | None = None,
    ) -> tuple[OHLCVBar, ...]:
        return bars_by_tf.get(timeframe, ())

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(direction="long", recipe=h01_recipe)

    command = SimpleNamespace(
        bar=PaperBar(
            f"source:{SYMBOL}:1h:{T0.isoformat()}",
            SYMBOL,
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=1),
            Decimal("38000"),
            Decimal("38500"),
            Decimal("37500"),
            Decimal("38000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is True


@pytest.mark.asyncio
async def test_opposing_signal_ignored_when_same_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Khi vị thế LONG gặp tín hiệu LONG (cùng chiều), opposing_signal phải là False."""
    bars_by_tf = _generate_h01_bars("bullish_cross", T0)

    async def fake_bars(
        session: Any,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        max_bars: int = 256,
        pinned_source: str | None = None,
    ) -> tuple[OHLCVBar, ...]:
        return bars_by_tf.get(timeframe, ())

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(direction="long", recipe=h01_recipe)

    command = SimpleNamespace(
        bar=PaperBar(
            f"source:{SYMBOL}:1h:{T0.isoformat()}",
            SYMBOL,
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=1),
            Decimal("62000"),
            Decimal("62500"),
            Decimal("61500"),
            Decimal("62000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is False


@pytest.mark.asyncio
async def test_opposing_signal_detected_on_bullish_crossover_for_short_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Khi vị thế SHORT gặp tín hiệu LONG từ nến 1h vừa khép lại, opposing_signal phải là True."""
    bars_by_tf = _generate_h01_bars("bullish_cross", T0)

    async def fake_bars(
        session: Any,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        max_bars: int = 256,
        pinned_source: str | None = None,
    ) -> tuple[OHLCVBar, ...]:
        return bars_by_tf.get(timeframe, ())

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(direction="short", recipe=h01_recipe)

    command = SimpleNamespace(
        bar=PaperBar(
            f"source:{SYMBOL}:1h:{T0.isoformat()}",
            SYMBOL,
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=1),
            Decimal("62000"),
            Decimal("62500"),
            Decimal("61500"),
            Decimal("62000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is True


@pytest.mark.asyncio
async def test_opposing_signal_ignored_when_short_position_meets_short_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Khi vị thế SHORT gặp tín hiệu SHORT (cùng chiều), opposing_signal phải là False."""
    bars_by_tf = _generate_h01_bars("bearish_cross", T0)

    async def fake_bars(
        session: Any,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        max_bars: int = 256,
        pinned_source: str | None = None,
    ) -> tuple[OHLCVBar, ...]:
        return bars_by_tf.get(timeframe, ())

    monkeypatch.setattr(pmc, "load_closed_market_bars", fake_bars)

    provider = PostgresPaperDecisionContextProvider(sessions=MagicMock(), workspace_id=WORKSPACE_ID)
    h01_recipe = ExitRecipe("h01-exit-v1", 2.0, opposite_signal_exit=True, max_hold_bars=120)
    position = _make_position(direction="short", recipe=h01_recipe)

    command = SimpleNamespace(
        bar=PaperBar(
            f"source:{SYMBOL}:1h:{T0.isoformat()}",
            SYMBOL,
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=1),
            Decimal("38000"),
            Decimal("38500"),
            Decimal("37500"),
            Decimal("38000"),
        ),
        feature_snapshot_id="f-snap",
    )
    published = SimpleNamespace(workspace_id=WORKSPACE_ID)
    account = SimpleNamespace(account_id=uuid4())

    uow = SimpleNamespace(session=AsyncMock())
    evidence = await provider.load_monitor_evidence(
        uow=uow,
        command=command,
        published_settings=published,
        execution_profile=SimpleNamespace(quote_asset="USDT"),
        account=account,
        position=position,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash="hash",
        pinned_strategy_settings_version_id="settings-v1",
        pinned_strategy_parameters=(),
    )
    assert evidence.opposing_signal is False
