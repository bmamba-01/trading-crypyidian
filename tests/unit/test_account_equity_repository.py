"""Unit tests cho AccountEquityRepository (MB04.4, sửa review I2/I3).

Vốn account mục tiêu không còn tính bằng đường Python riêng (giá entry đầu tiên, bỏ qua exit một
phần/chi phí, freshness khác SQL). Repository chỉ đọc một hàng của hàm SQL dùng chung
`workspace_account_risk_state` và dựng snapshot domain có thể kiểm lại; các phép tính PnL/mark
được kiểm bằng E2E PostgreSQL thật (`test_workspace_risk_concurrency_e2e.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from trading_bot.adapters.database.repositories.account_equity import (
    LEDGER_EQUITY_SOURCE,
    AccountEquityRepository,
    mark_age_seconds,
)
from trading_bot.domain.risk.equity import (
    PaperLedgerEquityBasis,
    normalize_equity_to_usdt,
)
from trading_bot.domain.risk.workspace_budget import MarkPolicy

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
BOT = UUID("22222222-2222-4222-8222-222222222222")
ACCOUNT = UUID("33333333-3333-4333-8333-333333333333")
OTHER_BOT = UUID("44444444-4444-4444-8444-444444444444")
OTHER_ACCOUNT = UUID("55555555-5555-4555-8555-555555555555")
POLICY = MarkPolicy("BINANCE_USDM", ("1m", "1h"))


def _row(
    *,
    bot: UUID = BOT,
    account: UUID = ACCOUNT,
    enabled: bool = True,
    seed: str | None = "10000",
    realized: str = "0",
    open_pnl: str = "0",
    open_count: int = 0,
    equity: str | None = "10000",
    unknown_equity: bool = False,
    open_risk: str = "0",
    pending: str = "0",
    unknown_exposure: bool = False,
    source_refs: tuple[str, ...] = (),
) -> tuple[Any, ...]:
    return (
        bot,
        account,
        enabled,
        None if seed is None else Decimal(seed),
        Decimal(realized),
        Decimal(open_pnl),
        open_count,
        None if equity is None else Decimal(equity),
        unknown_equity,
        Decimal(open_risk),
        Decimal(pending),
        unknown_exposure,
        source_refs,
    )


class FakeStateSession:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> Any:
        self.calls.append((str(statement), params))
        rows = self.rows
        return type("Result", (), {"all": lambda *_: rows})()


async def _snapshot(rows: list[tuple[Any, ...]], **kwargs: Any) -> Any:
    session = FakeStateSession(rows)
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]
    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        as_of=NOW,
        max_equity_age=timedelta(seconds=300),
        max_mark_age=kwargs.get("max_mark_age", timedelta(seconds=60)),
        mark_policy=POLICY,
    )
    return snapshot, session


@pytest.mark.asyncio
async def test_snapshot_uses_shared_sql_state_and_round_trips_domain_lineage() -> None:
    """Seed + realized + PnL vị thế mở (gồm exit một phần, chi phí) từ SQL dùng chung."""
    snapshot, session = await _snapshot(
        [
            _row(
                seed="10000",
                realized="250",
                open_pnl="867.125",
                open_count=1,
                equity="11117.125",
            ),
            _row(bot=OTHER_BOT, account=OTHER_ACCOUNT, seed=None, equity=None),
        ]
    )

    assert snapshot.known is True
    assert snapshot.equity_amount == Decimal("11117.125")
    assert snapshot.seed_capital == Decimal("10000")
    assert snapshot.cumulative_realized_pnl == Decimal("250")
    assert snapshot.unrealized_pnl == Decimal("867.125")
    assert snapshot.source == LEDGER_EQUITY_SOURCE
    assert snapshot.basis.source_refs == ()
    assert isinstance(snapshot.basis, PaperLedgerEquityBasis)
    assert snapshot.positions == ()
    assert snapshot.mark_refs == ()
    # Domain kiểm lại lineage (tính lại từ basis) và cho đúng số vốn.
    assert normalize_equity_to_usdt(snapshot, None, NOW, timedelta(seconds=300)) == Decimal(
        "11117.125"
    )
    statement, params = session.calls[0]
    assert "workspace_account_risk_state" in statement
    # Dấu vết ledger và mark được đọc trong cùng câu lệnh/MVCC snapshot với phép tính vốn.
    assert "source_rows" in statement
    assert "paper_account_seed" in statement
    assert "public.fill" in statement
    assert "public.trade_cost" in statement
    assert "public.market_bar" in statement
    assert params == {
        "workspace_id": WORKSPACE,
        "as_of": NOW,
        "max_mark_age_seconds": 60,
        "mark_source": "BINANCE_USDM",
        "mark_timeframes": ["1m", "1h"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "BASIS_UNKNOWN"),
        ([_row(seed=None, equity=None)], "BASIS_UNKNOWN"),
        ([_row(unknown_equity=True)], "SNAPSHOT_UNKNOWN"),
        # Hàng của bot khác cùng account ID không được dùng (scope bot/account phải khớp).
        ([_row(bot=OTHER_BOT)], "BASIS_UNKNOWN"),
        # Vốn âm khớp cờ unknown của SQL: domain không nhận vốn âm.
        (
            [_row(seed="10", open_pnl="-20", open_count=1, equity="-10")],
            "AMOUNT_INVALID",
        ),
        # SQL và domain cộng cùng thành phần phải khớp tuyệt đối.
        ([_row(realized="5", equity="10000")], "LINEAGE_INVALID"),
    ],
)
async def test_snapshot_fails_closed(rows: list[tuple[Any, ...]], reason: str) -> None:
    snapshot, _ = await _snapshot(rows)

    assert snapshot.known is False
    assert snapshot.unknown_reason == reason
    assert snapshot.equity_amount == Decimal("0")


@pytest.mark.asyncio
async def test_snapshot_hash_tracks_source_revision_when_equity_is_unchanged() -> None:
    """Cùng tổng tiền nhưng thay nguồn/revision phải tạo snapshot lineage khác."""
    first, _ = await _snapshot([_row(source_refs=("seed:seed-a", "bar:bar-a:revision-1"))])
    corrected, _ = await _snapshot([_row(source_refs=("seed:seed-a", "bar:bar-a:revision-2"))])

    assert first.equity_amount == corrected.equity_amount == Decimal("10000")
    assert first.source_hash != corrected.source_hash
    assert first.basis.source_refs != corrected.basis.source_refs


@pytest.mark.asyncio
async def test_account_states_map_to_domain_scalars() -> None:
    session = FakeStateSession(
        [
            _row(
                open_risk="100",
                pending="50",
                unknown_exposure=True,
                enabled=False,
                source_refs=("seed:source-a", "bar:source-b:revision-2"),
            )
        ]
    )
    states = await AccountEquityRepository(session).load_account_states(  # type: ignore[arg-type]
        workspace_id=WORKSPACE, as_of=NOW, max_mark_age=timedelta(seconds=120), mark_policy=POLICY
    )

    domain = states[0].to_domain()
    assert domain.enabled is False
    assert domain.open_risk_usdt == Decimal("100")
    assert domain.pending_risk_usdt == Decimal("50")
    assert domain.exposure_known is False
    assert domain.equity_known is True
    assert states[0].source_refs == ("seed:source-a", "bar:source-b:revision-2")


@pytest.mark.parametrize("value", [timedelta(0), timedelta(seconds=-1), timedelta(seconds=1.5)])
def test_mark_age_must_be_positive_whole_seconds(value: timedelta) -> None:
    with pytest.raises(ValueError, match="max_mark_age"):
        mark_age_seconds(value)


@pytest.mark.asyncio
async def test_naive_as_of_is_rejected() -> None:
    repo = AccountEquityRepository(FakeStateSession([]))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="as_of"):
        await repo.load_account_states(
            workspace_id=WORKSPACE,
            as_of=NOW.replace(tzinfo=None),
            max_mark_age=timedelta(seconds=60),
            mark_policy=POLICY,
        )
