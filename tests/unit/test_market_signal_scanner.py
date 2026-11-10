from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.market_signal_scanner import (
    MarketSignalObservation,
    MarketSignalScanError,
    MarketSignalScanner,
)
from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureSnapshot,
    MarketRegime,
    RegimeAssessment,
)
from trading_bot.domain.market.models import OHLCVBar

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OBSERVED_AT = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
BAR_AVAILABLE_AT = OBSERVED_AT + timedelta(seconds=2)
NOW = BAR_AVAILABLE_AT + timedelta(seconds=2)


def _bar(
    index: int = 0,
    *,
    closed: bool = True,
    available_at: datetime | None = None,
    anchor_close_at: datetime = OBSERVED_AT,
    checksum: str | None = None,
) -> OHLCVBar:
    close_time = anchor_close_at + timedelta(hours=index)
    open_time = close_time - timedelta(hours=1)
    received_at = available_at or close_time + timedelta(seconds=2)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=received_at,
        available_at=received_at,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("12"),
        is_closed=closed,
        source="BINANCE_USDM",
        source_cursor=f"cursor-{index}",
        checksum=checksum or f"checksum-{index}",
    )


def _feature(
    *,
    is_usable: bool = True,
    data_freshness: DataFreshness = DataFreshness.FRESH,
    event_ids: tuple[str, ...] | None = None,
    as_of: datetime = BAR_AVAILABLE_AT,
    computed_at: datetime = NOW,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    bars: tuple[OHLCVBar, ...] | None = None,
) -> FeatureSnapshot:
    source_bars = bars or (_bar(),)
    return FeatureSnapshot(
        snapshot_id="f" * 64,
        content_hash="f" * 64,
        workspace_id=WORKSPACE_ID,
        symbol=symbol,
        timeframe=timeframe,
        as_of=as_of,
        computed_at=computed_at,
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=tuple(_bar_id(bar) for bar in source_bars),
        input_event_ids=event_ids or tuple(f"event-{index}" for index in range(len(source_bars))),
        features={"atr_14": 1.0},
        warmup={"atr_14": True},
        quality_flags=(),
        data_freshness=data_freshness,
        data_coverage=1.0,
        regime=RegimeAssessment(MarketRegime.TREND, 0.8, 1.0, "FIXTURE", "Dữ liệu kiểm thử."),
        is_usable=is_usable,
    )


def _observation(
    *,
    bars: tuple[OHLCVBar, ...] | None = None,
    feature: FeatureSnapshot | None = None,
    setup_type: str = "market.bar.closed",
) -> MarketSignalObservation:
    source_bars = bars or (_bar(),)
    latest_available = max(bar.available_at for bar in source_bars)
    return MarketSignalObservation(
        workspace_id=WORKSPACE_ID,
        bars=source_bars,
        feature_snapshot=feature
        or _feature(
            as_of=latest_available,
            computed_at=latest_available + timedelta(seconds=2),
            bars=source_bars,
        ),
        setup_type=setup_type,
    )


def test_observation_requires_feature_snapshot_from_exact_bar_sequence() -> None:
    with pytest.raises(MarketSignalScanError, match="đúng nến đầu vào"):
        _observation(
            bars=(_bar(),),
            feature=_feature(bars=(_bar(index=1),)),
        )


def test_observation_rejects_duplicate_or_empty_source_event_ids() -> None:
    bars = (_bar(index=0), _bar(index=1))
    with pytest.raises(MarketSignalScanError, match="event ID"):
        _observation(feature=_feature(bars=bars, event_ids=("same", "same")), bars=bars)


def _bar_id(bar: OHLCVBar) -> str:
    payload = {
        "open_time": bar.open_time.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _BotContext:
    workspace_id: UUID = WORKSPACE_ID
    strategy_release_id: str = "h01-v1"
    symbols: tuple[str, ...] = ("BTCUSDT",)


@dataclass
class _Contexts:
    values: tuple[object, ...] = (_BotContext(),)
    calls: list[tuple[UUID, datetime]] | None = None

    async def contexts_for(self, *, workspace_id: UUID, as_of: datetime) -> tuple[object, ...]:
        if self.calls is None:
            self.calls = []
        self.calls.append((workspace_id, as_of))
        return self.values


@dataclass
class _Dispatcher:
    calls: list[tuple[object, tuple[object, ...], datetime]] | None = None

    async def dispatch_evidence(self, evidence, bots, as_of):  # type: ignore[no-untyped-def]
        if self.calls is None:
            self.calls = []
        self.calls.append((evidence, tuple(bots), as_of))
        return ()


@dataclass
class _Source:
    observations: tuple[MarketSignalObservation, ...]
    calls: list[tuple[UUID, datetime, int, tuple[str, ...], tuple[str, ...]]] | None = None

    async def qualified_closed_observations(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
        limit: int,
        symbols: tuple[str, ...],
        timeframes: tuple[str, ...],
    ) -> tuple[MarketSignalObservation, ...]:
        if self.calls is None:
            self.calls = []
        self.calls.append((workspace_id, as_of, limit, symbols, timeframes))
        return self.observations[:limit]


@pytest.mark.asyncio
async def test_scan_builds_deterministic_evidence_and_dispatches_without_order_ports() -> None:
    observation = _observation()
    source = _Source((observation,))
    contexts = _Contexts()
    dispatcher = _Dispatcher()
    scanner = MarketSignalScanner(
        source,
        contexts,
        dispatcher,
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(minutes=2),
    )

    result = await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)

    assert result.dispatched_count == 1
    assert result.decision_count == 0
    assert source.calls == [(WORKSPACE_ID, NOW, 100, ("BTCUSDT",), ("1h",))]
    assert contexts.calls == [(WORKSPACE_ID, NOW)]
    assert dispatcher.calls is not None
    assert len(dispatcher.calls) == 1
    evidence, bots, evaluated_at = dispatcher.calls[0]
    assert evidence.workspace_id == WORKSPACE_ID
    assert evidence.symbol == "BTCUSDT"
    assert evidence.setup_type == "market.bar.closed"
    assert evidence.feature_snapshot_id == "f" * 64
    assert evidence.observed_at == BAR_AVAILABLE_AT
    assert evidence.available_at == NOW
    assert evidence.validity_end == NOW + timedelta(minutes=2)
    assert evidence.source_refs == ("market-bar-event:event-0",)
    assert bots == contexts.values
    assert evaluated_at == NOW


@pytest.mark.asyncio
async def test_scan_suppresses_stale_unusable_and_expired_observations() -> None:
    old_bar = _bar(anchor_close_at=NOW - timedelta(hours=1))
    stale = _observation(
        bars=(old_bar,),
        feature=_feature(
            as_of=old_bar.available_at,
            computed_at=old_bar.available_at,
            bars=(old_bar,),
        ),
    )
    unusable = _observation(feature=_feature(is_usable=False))
    stale_quality = _observation(feature=_feature(data_freshness=DataFreshness.STALE))
    expired = _observation(
        feature=_feature(as_of=BAR_AVAILABLE_AT, computed_at=BAR_AVAILABLE_AT, bars=(_bar(),))
    )
    source = _Source((stale, unusable, stale_quality, expired))
    contexts = _Contexts()
    dispatcher = _Dispatcher()
    scanner = MarketSignalScanner(
        source,
        contexts,
        dispatcher,
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(seconds=1),
    )

    result = await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)

    assert result.dispatched_count == 0
    assert result.stale_count == 1
    assert result.unusable_count == 2
    assert result.expired_count == 1
    assert contexts.calls == [(WORKSPACE_ID, NOW)]
    assert dispatcher.calls is None


@pytest.mark.asyncio
async def test_scan_deduplicates_repeated_observation_within_one_batch() -> None:
    observation = _observation()
    scanner = MarketSignalScanner(
        _Source((observation, observation)),
        _Contexts(),
        _Dispatcher(),
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(minutes=2),
    )

    result = await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)

    assert result.dispatched_count == 1
    assert result.duplicate_count == 1


@pytest.mark.asyncio
async def test_scan_reuses_same_evidence_id_for_retry_across_ticks() -> None:
    observation = _observation()
    dispatcher = _Dispatcher()
    scanner = MarketSignalScanner(
        _Source((observation,)),
        _Contexts(),
        dispatcher,
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(minutes=2),
    )

    first = await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)
    retry = await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW + timedelta(seconds=1))

    assert first.dispatched_evidence_ids == retry.dispatched_evidence_ids
    assert dispatcher.calls is not None
    assert dispatcher.calls[0][0].evidence_hash == dispatcher.calls[1][0].evidence_hash


@pytest.mark.asyncio
async def test_scan_rejects_future_or_unclosed_market_inputs() -> None:
    future_bar = _bar(available_at=NOW + timedelta(seconds=1))
    source = _Source(
        (
            _observation(
                bars=(future_bar,),
                feature=_feature(
                    as_of=future_bar.available_at,
                    computed_at=future_bar.available_at,
                    bars=(future_bar,),
                ),
            ),
        )
    )
    scanner = MarketSignalScanner(
        source,
        _Contexts(),
        _Dispatcher(),
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="khả dụng trong tương lai"):
        await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)

    with pytest.raises(ValueError, match="đã đóng"):
        _observation(bars=(_bar(closed=False),))


@pytest.mark.asyncio
async def test_scan_skips_when_no_bot_context_and_validates_options() -> None:
    observation = _observation()
    source = _Source((observation,))
    contexts = _Contexts(values=())
    dispatcher = _Dispatcher()
    scanner = MarketSignalScanner(
        source,
        contexts,
        dispatcher,
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(minutes=2),
    )

    result = await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)

    assert result.dispatched_count == 0
    assert result.no_context_count == 1
    assert source.calls is None
    assert contexts.calls == [(WORKSPACE_ID, NOW)]
    assert dispatcher.calls is None
    with pytest.raises(ValueError, match="limit"):
        await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW, limit=0)


@pytest.mark.asyncio
async def test_scan_filters_to_matching_strategy_entry_timeframe_and_symbol() -> None:
    observation = _observation()
    bot_one_hour = _BotContext(strategy_release_id="h01-v1", symbols=("BTCUSDT",))
    bot_fifteen_minute = _BotContext(strategy_release_id="h06-v1", symbols=("BTCUSDT",))
    bot_other_symbol = _BotContext(strategy_release_id="h03-v1", symbols=("ETHUSDT",))
    contexts = _Contexts(values=(bot_one_hour, bot_fifteen_minute, bot_other_symbol))
    source = _Source((observation,))
    dispatcher = _Dispatcher()
    scanner = MarketSignalScanner(
        source,
        contexts,
        dispatcher,
        max_observation_age=timedelta(minutes=5),
        signal_validity=timedelta(minutes=2),
    )

    await scanner.scan_once(workspace_id=WORKSPACE_ID, now=NOW)

    assert source.calls == [(WORKSPACE_ID, NOW, 100, ("BTCUSDT", "ETHUSDT"), ("15m", "1h"))]
    assert dispatcher.calls is not None
    assert dispatcher.calls[0][1] == (bot_one_hour,)
