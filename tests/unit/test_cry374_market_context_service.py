"""Unit test cho dịch vụ/read-model bối cảnh thị trường đã kiểm quyền (CRY-374)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import trading_bot.adapters.database.repositories.market_context as repository_module
from trading_bot.adapters.database.repositories.market_context import (
    SqlAlchemyMarketContextRepository,
)
from trading_bot.application.market_context import MarketContextReadModelService
from trading_bot.domain.portfolio.market_context import (
    MarketContextAlertCode,
    MarketContextKind,
    MarketContextObservation,
)
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    ObservedSourcePolicy,
    SourceBudget,
    SourceRegistryEntry,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
SOURCE_ID = "BINANCE_USDM"
SYMBOL = "BTCUSDT"


def _observation() -> MarketContextObservation:
    return MarketContextObservation(
        kind=MarketContextKind.FUNDING,
        symbol=SYMBOL,
        source=SOURCE_ID,
        checksum="b" * 64,
        event_at=NOW - timedelta(minutes=5),
        available_at=NOW - timedelta(minutes=4),
        value=Decimal("0.0001"),
        unit="ratio",
        revision=2,
        raw_reference="BINANCE_USDM:BTCUSDT:funding:1",
    )


class FakeRepository:
    def __init__(
        self,
        *,
        permitted: bool = True,
        observed: tuple[MarketContextObservation, ...] = (_observation(),),
    ) -> None:
        self.permitted = permitted
        self.observed = observed
        self.calls: list[str] = []
        self.window: tuple[datetime, datetime] | None = None

    async def database_clock(self, *, workspace_id):
        self.calls.append("clock")
        assert workspace_id == WORKSPACE_ID
        return NOW + timedelta(seconds=3)

    async def display_permitted(self, *, workspace_id, source_id, at):
        self.calls.append("rights")
        assert workspace_id == WORKSPACE_ID
        assert source_id == SOURCE_ID
        assert at == NOW + timedelta(seconds=3)
        return self.permitted

    async def list_in_window(self, *, workspace_id, symbol, start, end):
        self.calls.append("read")
        self.window = (start, end)
        return self.observed


@pytest.mark.asyncio
async def test_service_cho_phep_hien_thi_va_dung_db_clock() -> None:
    repo = FakeRepository()
    service = MarketContextReadModelService(repo)
    report = await service.get_report(workspace_id=WORKSPACE_ID, symbol=SYMBOL)
    assert repo.calls == ["clock", "rights", "read"]
    assert report.display_allowed is True
    assert report.as_of == NOW + timedelta(seconds=3)
    assert repo.window == (
        NOW + timedelta(seconds=3) - timedelta(hours=24),
        NOW + timedelta(seconds=3),
    )
    assert report.observations[0].checksum == "b" * 64
    assert MarketContextAlertCode.NO_DATA in report.alerts


@pytest.mark.asyncio
async def test_tu_choi_quyen_thi_khong_doc_du_lieu() -> None:
    repo = FakeRepository(permitted=False)
    service = MarketContextReadModelService(repo)
    report = await service.get_report(workspace_id=WORKSPACE_ID, symbol=SYMBOL)
    assert repo.calls == ["clock", "rights"]
    assert repo.window is None
    assert report.display_allowed is False
    assert report.alerts == (MarketContextAlertCode.RIGHTS_NOT_ALLOWED,)
    assert report.to_dict()["observations"] == []


@pytest.mark.asyncio
async def test_service_tu_choi_input_khong_hop_le() -> None:
    service = MarketContextReadModelService(FakeRepository())
    with pytest.raises(ValueError, match="UUID"):
        await service.get_report(workspace_id="not-uuid", symbol=SYMBOL)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="symbol"):
        await service.get_report(workspace_id=WORKSPACE_ID, symbol="  ")


def _entry(
    *,
    purposes: frozenset[SourceUsePurpose],
    valid_until: datetime | None = NOW + timedelta(days=1),
) -> SourceRegistryEntry:
    discovered = SourceRegistryEntry.discover(
        source_id=SOURCE_ID,
        publisher="Binance",
        endpoint_url="https://fapi.binance.com/fapi/v1/klines",
        access_method="GET",
        auth_scope="public",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=100,
            max_bytes=1024 * 1024,
            max_cost=Decimal("0"),
            cost_unit="USD",
        ),
        actor_id="operator",
        at=NOW - timedelta(days=3),
        reason="Nguồn được phát hiện để xét quyền.",
    )
    pending = discovered.transition(
        policy_id=f"{SOURCE_ID}:pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="operator",
        at=NOW - timedelta(days=2),
        reason="Chờ duyệt quyền.",
    )
    return pending.transition(
        policy_id=f"{SOURCE_ID}:verified",
        state=SourcePolicyState.VERIFIED,
        actor_id="operator",
        at=NOW - timedelta(days=1),
        reason="Đã duyệt purpose hiển thị.",
        allowed_purposes=purposes,
        evidence_ref="https://www.binance.com/en/terms",
        reviewed_at=NOW - timedelta(days=1),
        valid_until=valid_until,
        approved_by="reviewer",
    )


def _observed(entry: SourceRegistryEntry) -> ObservedSourcePolicy:
    return ObservedSourcePolicy(
        entry,
        {
            revision.policy.policy_id: max(
                NOW - timedelta(minutes=2),
                revision.created_at,
                revision.policy.reviewed_at or revision.created_at,
            )
            for revision in entry.revisions
        },
    )


class FakeRegistry:
    def __init__(self, observed: ObservedSourcePolicy | None) -> None:
        self.observed = observed

    async def get_observed_entry(self, *, workspace_id, source_id):
        return self.observed

    async def database_clock(self, *, workspace_id):
        return NOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (_observed(_entry(purposes=frozenset({SourceUsePurpose.DISPLAY}))), True),
        (_observed(_entry(purposes=frozenset({SourceUsePurpose.BACKTEST_RESEARCH}))), False),
        (None, False),
        (
            _observed(
                _entry(
                    purposes=frozenset({SourceUsePurpose.DISPLAY}),
                    valid_until=NOW - timedelta(hours=1),
                )
            ),
            False,
        ),
    ],
)
async def test_repo_display_permitted_theo_policy_va_receipt(
    monkeypatch: pytest.MonkeyPatch, observed: ObservedSourcePolicy | None, expected: bool
) -> None:
    monkeypatch.setattr(
        repository_module, "SourceRegistryRepository", lambda session: FakeRegistry(observed)
    )
    repo = SqlAlchemyMarketContextRepository(session=object())  # type: ignore[arg-type]
    result = await repo.display_permitted(workspace_id=WORKSPACE_ID, source_id=SOURCE_ID, at=NOW)
    assert result is expected
