from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetNotReady,
    backtest_dataset_bars_hash,
    build_backtest_dataset_manifest,
)
from trading_bot.application.backtest_dataset_replay import (
    BacktestDatasetDataChanged,
    BacktestDatasetReplayInput,
    BacktestDatasetReplayLoader,
    BacktestDatasetRightsChanged,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.models import OHLCVBar

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
START = NOW - timedelta(minutes=2)
WORKSPACE_ID = uuid4()


def market_bars(*, include_second_bar: bool = True) -> tuple[OHLCVBar, ...]:
    return tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time=open_time,
            close_time=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
            event_at=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
            received_at=NOW,
            available_at=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1"),
            is_closed=True,
            source="BINANCE_USDM_REST_BACKFILL",
            source_cursor=open_time.isoformat(),
            checksum=hashlib.sha256(open_time.isoformat().encode()).hexdigest(),
        )
        for index, open_time in enumerate((START, START + timedelta(minutes=1)))
        if include_second_bar or index == 0
    )


def manifest(*, include_second_bar: bool = True):
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1m",
        start=START,
        end=NOW,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    return build_backtest_dataset_manifest(
        request,
        evaluation_start=START,
        warmup_bars=0,
        bars=market_bars(include_second_bar=include_second_bar),
        rights_evidence=BackfillRightsEvidence(
            workspace_id=WORKSPACE_ID,
            provider="BINANCE_USDM_REST",
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            terms_url="https://example.invalid/terms",
            policy_fingerprint=hashlib.sha256(b"source-policy-v1").hexdigest(),
            verified_at=NOW - timedelta(days=1),
            checked_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(days=1),
        ),
        prepared_at=NOW,
    )


class FakeManifestRepository:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def get_by_hash(self, *, workspace_id, dataset_hash):
        self.calls.append((workspace_id, dataset_hash))
        return self.value


class FakeRightsGate:
    def __init__(self, evidence=None, error: Exception | None = None):
        self.evidence = evidence
        self.error = error
        self.request = None

    async def authorize(self, *, request, now):
        self.request = (request, now)
        if self.error is not None:
            raise self.error
        return self.evidence


class FakeMarketData:
    def __init__(self, bars: tuple[OHLCVBar, ...]) -> None:
        self.bars = bars
        self.query = None

    async def list_bars(self, **query):
        self.query = query
        return list(self.bars)


def evidence(*, fingerprint: str | None = None) -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/terms",
        policy_fingerprint=fingerprint or hashlib.sha256(b"source-policy-v1").hexdigest(),
        verified_at=NOW - timedelta(days=1),
        checked_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )


def test_bar_content_hash_is_independent_of_repository_result_order() -> None:
    bars = market_bars()

    assert backtest_dataset_bars_hash(bars) == backtest_dataset_bars_hash(tuple(reversed(bars)))


@pytest.mark.asyncio
async def test_replay_loader_returns_pinned_bars_after_rights_and_hash_recheck() -> None:
    value = manifest()
    repository = FakeManifestRepository(value)
    rights = FakeRightsGate(evidence())
    bars = market_bars()
    market_data = FakeMarketData(bars)
    loader = BacktestDatasetReplayLoader(
        repository=repository, rights_gate=rights, market_data=market_data
    )

    loaded = await loader.load_for_replay(
        workspace_id=WORKSPACE_ID,
        dataset_hash=value.dataset_hash,
        now=NOW,
    )

    assert isinstance(loaded, BacktestDatasetReplayInput)
    assert loaded.manifest is value
    assert loaded.bars == bars
    assert loaded.current_rights == evidence()
    assert repository.calls == [(WORKSPACE_ID, value.dataset_hash)]
    request, checked_at = rights.request
    assert request.symbol == value.symbol
    assert request.interval == value.interval
    assert request.start == value.warmup_start
    assert request.end == value.range_end
    assert request.purpose is BackfillRightsPurpose.BACKTEST_RESEARCH
    assert checked_at == NOW
    assert market_data.query == {
        "workspace_id": WORKSPACE_ID,
        "symbol": value.symbol,
        "timeframe": value.interval,
        "start": value.warmup_start,
        "end": value.range_end,
        "as_of": value.prepared_at,
        "closed_only": True,
        "source": "BINANCE_USDM_REST_BACKFILL",
    }


@pytest.mark.asyncio
async def test_replay_loader_rejects_missing_or_changed_pinned_bars() -> None:
    value = manifest()
    for observed in (market_bars(include_second_bar=False),):
        loader = BacktestDatasetReplayLoader(
            repository=FakeManifestRepository(value),
            rights_gate=FakeRightsGate(evidence()),
            market_data=FakeMarketData(observed),
        )
        with pytest.raises(BacktestDatasetDataChanged, match="(?i)số lượng"):
            await loader.load_for_replay(
                workspace_id=WORKSPACE_ID,
                dataset_hash=value.dataset_hash,
                now=NOW,
            )

    altered = (replace(market_bars()[0], close=Decimal("99")), market_bars()[1])
    loader = BacktestDatasetReplayLoader(
        repository=FakeManifestRepository(value),
        rights_gate=FakeRightsGate(evidence()),
        market_data=FakeMarketData(altered),
    )
    with pytest.raises(BacktestDatasetDataChanged, match="(?i)hash"):
        await loader.load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=value.dataset_hash,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_replay_loader_rejects_wrong_source_or_timeframe_bars() -> None:
    value = manifest()
    source_changed = (replace(market_bars()[0], source="OTHER_SOURCE"), market_bars()[1])
    timeframe_changed = (replace(market_bars()[0], timeframe="5m"), market_bars()[1])
    for observed in (source_changed, timeframe_changed):
        loader = BacktestDatasetReplayLoader(
            repository=FakeManifestRepository(value),
            rights_gate=FakeRightsGate(evidence()),
            market_data=FakeMarketData(observed),
        )
        with pytest.raises(BacktestDatasetDataChanged, match="(?i)hash"):
            await loader.load_for_replay(
                workspace_id=WORKSPACE_ID,
                dataset_hash=value.dataset_hash,
                now=NOW,
            )


@pytest.mark.asyncio
async def test_legacy_v2_manifest_rejects_unpinned_quote_volume() -> None:
    current = manifest()
    payload = json.loads(current.canonical_json)
    payload["schema_version"] = 2
    payload["bars_content_hash"] = backtest_dataset_bars_hash(market_bars(), schema_version=2)
    payload.pop("dataset_hash")
    legacy_canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    legacy_hash = hashlib.sha256(legacy_canonical.encode("utf-8")).hexdigest()
    payload["dataset_hash"] = legacy_hash
    legacy_canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    legacy = type(current).from_canonical_json(
        legacy_canonical,
        expected_dataset_hash=legacy_hash,
    )
    observed = tuple(replace(bar, quote_volume=Decimal("101.25")) for bar in market_bars())
    loader = BacktestDatasetReplayLoader(
        repository=FakeManifestRepository(legacy),
        rights_gate=FakeRightsGate(evidence()),
        market_data=FakeMarketData(observed),
    )

    with pytest.raises(BacktestDatasetDataChanged, match="schema v2 không ghim quote volume"):
        await loader.load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=legacy.dataset_hash,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_replay_loader_denies_changed_policy_fingerprint_and_current_revocation() -> None:
    value = manifest()
    changed = FakeRightsGate(evidence(fingerprint=hashlib.sha256(b"source-policy-v2").hexdigest()))
    loader = BacktestDatasetReplayLoader(
        repository=FakeManifestRepository(value),
        rights_gate=changed,
        market_data=FakeMarketData(market_bars()),
    )

    with pytest.raises(BacktestDatasetRightsChanged, match="Policy"):
        await loader.load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=value.dataset_hash,
            now=NOW,
        )

    revoked = FakeRightsGate(error=PermissionError("Policy hiện hành đã bị thu hồi."))
    loader = BacktestDatasetReplayLoader(
        repository=FakeManifestRepository(value),
        rights_gate=revoked,
        market_data=FakeMarketData(market_bars()),
    )
    with pytest.raises(PermissionError, match="thu hồi"):
        await loader.load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=value.dataset_hash,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_replay_loader_checks_coverage_before_current_rights() -> None:
    value = manifest(include_second_bar=False)
    rights = FakeRightsGate(evidence())
    market_data = FakeMarketData(market_bars(include_second_bar=False))
    loader = BacktestDatasetReplayLoader(
        repository=FakeManifestRepository(value),
        rights_gate=rights,
        market_data=market_data,
    )

    with pytest.raises(BacktestDatasetNotReady):
        await loader.load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=value.dataset_hash,
            now=NOW,
        )
    assert rights.request is None
    assert market_data.query is None
