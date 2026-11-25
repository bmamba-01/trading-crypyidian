"""Hợp đồng projection rủi ro admin không trả hàng trade/fill thô."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from tests.unit.test_admin_settings_service import sample_settings

from trading_bot.application.workspace_risk import (
    load_admin_paper_policy_counts,
    load_admin_risk_capacity_states,
)

WORKSPACE = uuid4()
MEMBERSHIP = uuid4()
BOT = uuid4()
ACCOUNT = uuid4()
NOW = datetime(2026, 11, 25, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows

    def mappings(self) -> _Result:
        return self

    def one(self) -> dict[str, int]:
        return {
            "active_intents": 2,
            "open_positions": 1,
            "active_reservations": 3,
        }


class _Session:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def scalar(self, _statement: object) -> datetime:
        return NOW

    async def execute(self, statement: object, parameters: dict[str, object]):
        self.calls.append((statement, parameters))
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_admin_capacity_loader_returns_per_account_aggregate_without_ledger_ids() -> None:
    session = _Session(
        [
            (
                BOT,
                ACCOUNT,
                True,
                Decimal("9000"),
                False,
                Decimal("125.25"),
                Decimal("10"),
                False,
            )
        ]
    )

    states = await load_admin_risk_capacity_states(
        session,  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        settings=sample_settings(),
    )

    assert len(states) == 1
    assert states[0].bot_id == str(BOT)
    assert states[0].account_id == str(ACCOUNT)
    assert states[0].equity_usdt == Decimal("9000")
    assert states[0].equity_known is True
    assert states[0].open_risk_usdt == Decimal("125.25")
    assert states[0].pending_risk_usdt == Decimal("10")
    assert states[0].exposure_known is True
    statement, parameters = session.calls[0]
    assert "wp06_admin_risk_capacity_snapshot" in str(statement)
    assert "FROM trade" not in str(statement)
    assert "trade_id" not in str(statement)
    assert parameters["workspace_id"] == WORKSPACE
    assert parameters["membership_id"] == MEMBERSHIP
    assert parameters["as_of"] == NOW


@pytest.mark.asyncio
async def test_admin_capacity_loader_preserves_unknown_equity_and_exposure() -> None:
    session = _Session(
        [
            (
                BOT,
                ACCOUNT,
                True,
                None,
                True,
                Decimal("0"),
                Decimal("0"),
                True,
            )
        ]
    )

    (state,) = await load_admin_risk_capacity_states(
        session,  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        settings=sample_settings(),
    )

    assert state.equity_usdt is None
    assert state.equity_known is False
    assert state.exposure_known is False


@pytest.mark.asyncio
async def test_paper_policy_guard_loader_returns_only_counts() -> None:
    session = _Session([])

    snapshot = await load_admin_paper_policy_counts(
        session,  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
    )

    assert (
        snapshot.active_intents,
        snapshot.open_positions,
        snapshot.active_reservations,
    ) == (2, 1, 3)
    statement, parameters = session.calls[0]
    assert "wp06_admin_paper_policy_snapshot" in str(statement)
    assert "trade_id" not in str(statement)
    assert parameters == {"workspace_id": WORKSPACE, "membership_id": MEMBERSHIP}
