from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.first_party_qa_dataset import (
    CRYPIDIAN_FIRST_PARTY_QA,
    CRYPIDIAN_FIRST_PARTY_QA_SOURCE,
    FirstPartyQADatasetRequest,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar


def _bars(source: str, start: datetime, *, workspace_id) -> tuple[OHLCVBar, ...]:
    bars = []
    for index in range(3):
        opened_at = start + timedelta(hours=index)
        closed_at = opened_at + timedelta(hours=1) - timedelta(milliseconds=1)
        cursor = f"crypidian-first-party-qa:v1:{opened_at.isoformat()}"
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=opened_at,
                close_time=closed_at,
                event_at=closed_at,
                received_at=closed_at,
                available_at=closed_at,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
                quote_volume=Decimal("100"),
                is_closed=True,
                source=source,
                source_cursor=cursor,
                checksum=hashlib.sha256(cursor.encode()).hexdigest(),
                source_identity=MarketBarSourceIdentity(
                    workspace_id=workspace_id,
                    record_id=uuid5(NAMESPACE_URL, cursor),
                    logical_key=cursor,
                    revision=1,
                    supersedes_record_id=None,
                    checksum=hashlib.sha256(cursor.encode()).hexdigest(),
                ),
            )
        )
    return tuple(bars)


def _rights(workspace_id, provider: str, start: datetime) -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=workspace_id,
        provider=provider,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url=(
            "https://github.com/bmamba-01/trading-crypyidian/blob/dev/"
            "docs/research/wp06-first-party-qa-fixture.md"
        ),
        policy_fingerprint=hashlib.sha256(provider.encode()).hexdigest(),
        verified_at=start - timedelta(days=1),
        checked_at=start - timedelta(hours=1),
        valid_until=start + timedelta(days=1),
    )


def test_first_party_qa_request_builds_manifest_with_explicit_source_identity() -> None:
    workspace_id = uuid4()
    start = datetime(2026, 10, 20, tzinfo=UTC)
    request = FirstPartyQADatasetRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=start + timedelta(hours=3),
    )

    manifest = build_backtest_dataset_manifest(
        request,
        evaluation_start=start + timedelta(hours=2),
        warmup_bars=2,
        bars=_bars(CRYPIDIAN_FIRST_PARTY_QA_SOURCE, start, workspace_id=workspace_id),
        rights_evidence=_rights(workspace_id, CRYPIDIAN_FIRST_PARTY_QA, start),
        prepared_at=start + timedelta(hours=3),
        schema_version=4,
    )

    assert manifest.rights_provider == CRYPIDIAN_FIRST_PARTY_QA
    assert manifest.request_fingerprint == request.fingerprint
    assert manifest.status == "READY"


def test_first_party_qa_cannot_claim_binance_market_bars() -> None:
    workspace_id = uuid4()
    start = datetime(2026, 10, 20, tzinfo=UTC)
    request = FirstPartyQADatasetRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=start + timedelta(hours=3),
    )

    with pytest.raises(ValueError, match="source"):
        build_backtest_dataset_manifest(
            request,
            evaluation_start=start + timedelta(hours=2),
            warmup_bars=2,
            bars=_bars("BINANCE_USDM_REST_BACKFILL", start, workspace_id=workspace_id),
            rights_evidence=_rights(workspace_id, CRYPIDIAN_FIRST_PARTY_QA, start),
            prepared_at=start + timedelta(hours=3),
            schema_version=4,
        )


def test_first_party_qa_rejects_bar_without_versioned_source_cursor() -> None:
    workspace_id = uuid4()
    start = datetime(2026, 10, 20, tzinfo=UTC)
    request = FirstPartyQADatasetRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=start + timedelta(hours=3),
    )
    bars = _bars(CRYPIDIAN_FIRST_PARTY_QA_SOURCE, start, workspace_id=workspace_id)

    with pytest.raises(ValueError, match="source cursor"):
        build_backtest_dataset_manifest(
            request,
            evaluation_start=start + timedelta(hours=2),
            warmup_bars=2,
            bars=(replace(bars[0], source_cursor=None), *bars[1:]),
            rights_evidence=_rights(workspace_id, CRYPIDIAN_FIRST_PARTY_QA, start),
            prepared_at=start + timedelta(hours=3),
            schema_version=4,
        )


def test_binance_rest_request_cannot_use_first_party_qa_rights() -> None:
    workspace_id = uuid4()
    start = datetime(2026, 10, 20, tzinfo=UTC)
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=start + timedelta(hours=3),
    )

    with pytest.raises(PermissionError, match="provider"):
        build_backtest_dataset_manifest(
            request,
            evaluation_start=start + timedelta(hours=2),
            warmup_bars=2,
            bars=_bars("BINANCE_USDM_REST_BACKFILL", start, workspace_id=workspace_id),
            rights_evidence=_rights(workspace_id, CRYPIDIAN_FIRST_PARTY_QA, start),
            prepared_at=start + timedelta(hours=3),
            schema_version=4,
        )


def test_first_party_qa_request_has_distinct_fingerprint_from_binance() -> None:
    start = datetime(2026, 10, 20, tzinfo=UTC)
    qa_request = FirstPartyQADatasetRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=start + timedelta(hours=3),
    )
    binance_request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=start + timedelta(hours=3),
    )

    assert qa_request.provider == CRYPIDIAN_FIRST_PARTY_QA
    assert qa_request.fingerprint != binance_request.fingerprint
