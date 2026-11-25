"""MB05.6 ruling 29: không disable bot/account khi còn vị thế, lệnh chưa khớp hoặc reservation."""

from __future__ import annotations

import asyncio
import inspect
from uuid import uuid4

import pytest
from tests.unit.runtime_fakes import RecordingSession

from trading_bot.application.bot_lifecycle_guard import BotLifecycleGuard, BotNotFlatError

WORKSPACE, BOT = uuid4(), uuid4()


class _ProjectionResult:
    def __init__(self, row: dict[str, int | bool]) -> None:
        self._row = row

    def mappings(self):
        return self

    def one(self):
        return self._row


class _LifecycleSession(RecordingSession):
    def __init__(self, row: dict[str, int | bool]) -> None:
        super().__init__()
        self.row = row

    async def execute(self, statement, params=None):
        query = self.statements.append
        compiled_query = str(statement)
        query(compiled_query)
        self.events.append("execute")
        if "wp06_admin_bot_lifecycle_snapshot" in compiled_query:
            self.parameters = params
            return _ProjectionResult(self.row)
        return None


def _guard(bot_exists: bool, positions: int, orders: int, reservations: int):
    session = _LifecycleSession(
        {
            "bot_exists": bot_exists,
            "open_positions": positions,
            "open_orders": orders,
            "held_reservations": reservations,
        }
    )
    return BotLifecycleGuard(session), session  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("positions", "orders", "reservations"),
    [(1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 1, 1)],
    ids=["open-position", "open-order", "held-reservation", "all"],
)
def test_not_flat_bot_raises_with_counts(positions: int, orders: int, reservations: int) -> None:
    guard, session = _guard(True, positions, orders, reservations)

    with pytest.raises(BotNotFlatError) as error:
        asyncio.run(guard.ensure_flat_for_disable(WORKSPACE, BOT))

    assert error.value.reason_code == "BOT_NOT_FLAT"
    assert (error.value.open_positions, error.value.open_orders) == (positions, orders)
    assert error.value.held_reservations == reservations
    assert str(BOT) not in str(error.value)


def test_flat_bot_passes_and_queries_only_that_bot_scope() -> None:
    guard, session = _guard(True, 0, 0, 0)

    assert asyncio.run(guard.ensure_flat_for_disable(WORKSPACE, BOT)) is None

    # L1 advisory lock phải đứng trước một truy vấn projection hẹp trong cùng transaction.
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "wp06_admin_bot_lifecycle_snapshot" in session.statements[1]
    assert "trade" not in session.statements[1]
    assert "order_intent" not in session.statements[1]
    assert session.parameters == {"workspace_id": WORKSPACE, "bot_id": BOT}
    assert session.events == ["execute", "execute"]


def test_guard_status_sets_cover_unknown_states() -> None:
    from trading_bot.application import bot_lifecycle_guard as module

    assert module.OPEN_ORDER_STATUSES == frozenset({"ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN"})
    assert module.HELD_RESERVATION_STATUSES == frozenset({"RESERVED", "UNKNOWN"})


def test_invisible_bot_is_not_found() -> None:
    guard, _ = _guard(False, 0, 0, 0)

    with pytest.raises(BotNotFlatError) as error:
        asyncio.run(guard.ensure_flat_for_disable(WORKSPACE, BOT))
    assert error.value.reason_code == "BOT_NOT_FOUND"


def test_guard_has_no_exit_only_mode() -> None:
    parameters = inspect.signature(BotLifecycleGuard.ensure_flat_for_disable).parameters
    assert set(parameters) == {"self", "workspace_id", "bot_id"}
