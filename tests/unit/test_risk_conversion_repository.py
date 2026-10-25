"""Unit tests cho RiskConversionRepository (MB04.4).

Kiểm tra:
- Identity conversion cho USDT/USDT (rate = 1.0, quality=ACCEPTED, approved=True, route=DIRECT)
- Approved DIRECT pair non-USDT còn hạn trả về rate chính xác
- Rejected unapproved pair trả về known=False với CONVERSION_REJECTED
- Stale observation trả về known=False với CONVERSION_STALE
- Future observation trả về known=False với OBSERVATION_FUTURE
- Missing observation fail-closed trả về known=False với CONVERSION_MISSING, không fallback 1.0
- Non-direct route trả về known=False với ROUTE_UNSUPPORTED
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from trading_bot.adapters.database.repositories.risk_conversion import RiskConversionRepository
from trading_bot.domain.risk.equity import (
    EquityReasonCode,
    RiskConversionObservation,
)

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
MAX_AGE = timedelta(minutes=5)


def make_direct_obs(
    *,
    base_asset: str = "BTC",
    quote_asset: str = "USDT",
    rate: Decimal = Decimal("65000.0"),
    as_of: datetime = NOW,
    available_at: datetime = NOW,
    approved: bool = True,
    quality: str = "ACCEPTED",
    route: str = "DIRECT",
) -> RiskConversionObservation:
    return RiskConversionObservation(
        workspace_id=str(WORKSPACE_ID),
        base_asset=base_asset,
        quote_asset=quote_asset,
        rate=rate,
        as_of=as_of,
        available_at=available_at,
        source_ref="binance_spot_ticker",
        source_version="v1",
        source_hash="c" * 64,
        policy_version_id="policy_v1",
        route=route,
        approved=approved,
        quality=quality,
    )


@pytest.mark.asyncio
async def test_usdt_identity_conversion() -> None:
    session = SimpleNamespace()
    repo = RiskConversionRepository(session)  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="USDT",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is True
    assert obs.rate == Decimal("1.0")
    assert obs.approved is True
    assert obs.quality == "ACCEPTED"
    assert obs.route == "DIRECT"
    assert obs.base_asset == "USDT"
    assert obs.quote_asset == "USDT"
    assert obs.unknown_reason is None


@pytest.mark.asyncio
async def test_approved_direct_pair_non_usdt() -> None:
    valid_obs = make_direct_obs(base_asset="BTC", rate=Decimal("67500.12345678"))
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[valid_obs])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="BTC",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is True
    assert obs.rate == Decimal("67500.12345678")
    assert obs.approved is True
    assert obs.quality == "ACCEPTED"
    assert obs.route == "DIRECT"
    assert obs.unknown_reason is None


@pytest.mark.asyncio
async def test_rejected_unapproved_pair_fails_closed() -> None:
    unapproved_obs = make_direct_obs(base_asset="ETH", approved=False)
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[unapproved_obs])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="ETH",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is False
    assert obs.unknown_reason == EquityReasonCode.CONVERSION_REJECTED.value


@pytest.mark.asyncio
async def test_rejected_poor_quality_fails_closed() -> None:
    poor_quality_obs = make_direct_obs(base_asset="ETH", quality="STALE_VENUE")
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[poor_quality_obs])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="ETH",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is False
    assert obs.unknown_reason == EquityReasonCode.CONVERSION_REJECTED.value


@pytest.mark.asyncio
async def test_stale_observation_fails_closed() -> None:
    stale_time = NOW - MAX_AGE - timedelta(seconds=1)
    stale_obs = make_direct_obs(base_asset="SOL", as_of=stale_time, available_at=stale_time)
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[stale_obs])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="SOL",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is False
    assert obs.unknown_reason == EquityReasonCode.CONVERSION_STALE.value


@pytest.mark.asyncio
async def test_future_observation_fails_closed() -> None:
    future_time = NOW + timedelta(seconds=10)
    future_obs = make_direct_obs(base_asset="BNB", as_of=future_time, available_at=future_time)
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[future_obs])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="BNB",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is False
    assert obs.unknown_reason == EquityReasonCode.OBSERVATION_FUTURE.value


@pytest.mark.asyncio
async def test_missing_observation_never_fallbacks_to_unity() -> None:
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="DOGE",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is False
    assert obs.unknown_reason == EquityReasonCode.CONVERSION_MISSING.value
    assert obs.rate != Decimal("1.0")


@pytest.mark.asyncio
async def test_unsupported_multi_hop_route_fails_closed() -> None:
    multi_hop_obs = make_direct_obs(base_asset="XRP", route="TRIANGULAR")
    session = SimpleNamespace()
    repo = RiskConversionRepository(session, observations=[multi_hop_obs])  # type: ignore[arg-type]

    obs = await repo.get_conversion_observation(
        workspace_id=WORKSPACE_ID,
        base_asset="XRP",
        quote_asset="USDT",
        as_of=NOW,
        max_age=MAX_AGE,
    )

    assert obs.known is False
    assert obs.unknown_reason == EquityReasonCode.ROUTE_UNSUPPORTED.value
