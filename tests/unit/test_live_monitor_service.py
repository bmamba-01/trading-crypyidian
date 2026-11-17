"""Unit test cho LiveMonitorService (LIVE-MON, CRY-429): quyền, con trỏ và ghép trạng thái."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from trading_bot.adapters.database.repositories.live_monitor import EVENT_SOURCE_LIMIT
from trading_bot.application.live_monitor import LiveMonitorService
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.monitoring.live_monitor import LiveCursor

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT_A = UUID("11111111-1111-4111-8111-111111111111")
BOT_B = UUID("22222222-2222-4222-8222-222222222222")
BOT_DENIED = UUID("33333333-3333-4333-8333-333333333333")
BOT_FOREIGN = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = Principal(
    user_id=UUID(int=1),
    membership_id=UUID(int=2),
    workspace_id=WORKSPACE,
    roles=frozenset({Role.ANALYST_REVIEWER}),
    mfa_verified_at=NOW,
)


class FakeUnit:
    last: FakeUnit

    def __init__(self, sessions: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session = object()
        FakeUnit.last = self

    async def __aenter__(self) -> FakeUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeGrants:
    def __init__(self, session: object) -> None:
        pass

    async def require(self, principal: Principal, *, bot_id: UUID, permission: str) -> None:
        if bot_id == BOT_DENIED:
            raise PermissionError("no grant")


class FakeRepository:
    rows: list[dict[str, Any]] = []
    calls: list[tuple[str, Any]] = []

    def __init__(self, session: object) -> None:
        pass

    async def events(self, *, workspace_id: UUID, bot_ids: list[UUID], since: Any) -> Any:
        FakeRepository.calls.append(("events", (list(bot_ids), since)))
        return list(FakeRepository.rows)

    async def database_now(self) -> datetime:
        return NOW + timedelta(milliseconds=5)

    async def open_positions(self, *, workspace_id: UUID, bot_ids: list[UUID]) -> Any:
        FakeRepository.calls.append(("open_positions", list(bot_ids)))
        return [
            {
                "trade_id": "t1",
                "bot_id": BOT_A,
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quote_asset": "USDT",
                "opened_at": NOW - timedelta(minutes=5),
                "entry_fills": [{"price": Decimal("100"), "quantity": Decimal("1")}],
                "exit_quantity": Decimal(0),
                "initial_stop": "90",
                "current_stop": None,
                "target": "120",
                "funding_total": Decimal("0"),
                "fee_total": Decimal("0.1"),
            }
        ]

    async def latest_prices(self, *, workspace_id: UUID, symbols: list[str]) -> Any:
        return {"BTCUSDT": {"price": Decimal("95"), "at": NOW, "source": "MARK"}}

    async def equity_inputs(self, *, workspace_id: UUID, bot_ids: Any, day_start: Any) -> Any:
        return {
            "seeds": {
                BOT_A: {"initial_equity": Decimal("1000"), "quote_asset": "USDT"},
                BOT_B: {"initial_equity": Decimal("500"), "quote_asset": "USDT"},
            },
            "per_bot": {
                BOT_A: {
                    "realized": Decimal("20"),
                    "peak_realized": Decimal("40"),
                    "closed_count": 3,
                }
            },
            "curve": [(NOW - timedelta(hours=1), Decimal("20"))],
            "before": None,
            "today": {BOT_A: Decimal("-15")},
        }

    async def feed_clock(self, *, workspace_id: UUID) -> Any:
        return {"latest_bar_close_at": NOW - timedelta(seconds=30), "latest_mark_at": None}

    async def outbox_stats(self, *, workspace_id: UUID) -> Any:
        return {"pending": 0, "oldest": None, "unconsumed_pending": 7}

    async def runtime_heartbeats(self, *, workspace_id: UUID, bot_ids: Any) -> Any:
        return {BOT_A: NOW - timedelta(seconds=10)}


def _service() -> LiveMonitorService:
    FakeRepository.calls = []
    return LiveMonitorService(
        sessions=object(),  # type: ignore[arg-type]
        unit_of_work=FakeUnit,
        repository=FakeRepository,
        grants=FakeGrants,
        clock=lambda: NOW,
    )


RUNTIME = {
    str(BOT_A): {
        "id": str(BOT_A),
        "name": "A",
        "status": "ENABLED",
        "entry_state": "ENABLED",
        "readiness": "READY",
        "entry_allowed": True,
        "workspace_kill_state": "ACTIVE",
    },
    str(BOT_B): {"id": str(BOT_B), "name": "B", "status": "ENABLED", "entry_allowed": False},
    str(BOT_DENIED): {"id": str(BOT_DENIED), "name": "D", "status": "ENABLED"},
}


def _read(**kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "bot_ids": [BOT_A, BOT_B, BOT_DENIED, BOT_FOREIGN, BOT_A],
        "since": None,
        "include_state": False,
        "runtime": RUNTIME,
    }
    return asyncio.run(_service().read(PRINCIPAL, **(params | kwargs)))


def test_only_visible_and_granted_bots_are_read_in_read_only_snapshot() -> None:
    FakeRepository.rows = []
    result = _read()
    assert result["bot_ids"] == [str(BOT_A), str(BOT_B)]
    assert FakeRepository.calls[0] == ("events", ([BOT_A, BOT_B], None))
    assert FakeUnit.last.kwargs["snapshot_read_only"] is True
    assert FakeUnit.last.kwargs["workspace_id"] == WORKSPACE
    assert FakeUnit.last.kwargs["actor_membership_id"] == PRINCIPAL.membership_id
    assert result["state"] is None
    # Lượt đầu không có sự kiện: con trỏ là giờ DB.
    assert result["cursor"] == LiveCursor(NOW + timedelta(milliseconds=5)).encode()


def test_since_queries_overlap_floor_and_keeps_cursor_without_new_rows() -> None:
    FakeRepository.rows = []
    since = LiveCursor(NOW - timedelta(minutes=1))
    result = _read(since=since)
    assert FakeRepository.calls[0][1][1] == since.query_floor()
    assert result["cursor"] == since.encode()


def _row(source: str, index: int, at: datetime) -> dict[str, Any]:
    return {
        "source": source,
        "id": f"{source}{index}",
        "bot_id": BOT_A,
        "recorded_at": at,
        "occurred_at": at,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quantity": Decimal("1"),
    }


def test_cursor_is_capped_by_source_that_hit_limit() -> None:
    base = NOW - timedelta(minutes=10)
    rows = [
        _row("order", index, base + timedelta(seconds=index)) for index in range(EVENT_SOURCE_LIMIT)
    ]
    rows.append(_row("funding", 0, NOW))
    FakeRepository.rows = rows
    result = _read(since=LiveCursor(base - timedelta(seconds=1)))
    capped = base + timedelta(seconds=EVENT_SOURCE_LIMIT - 1)
    assert result["cursor"] == LiveCursor(capped).encode()
    assert len(result["events"]) == EVENT_SOURCE_LIMIT + 1


def test_state_combines_positions_equity_health_and_bots() -> None:
    FakeRepository.rows = []
    state = _read(include_state=True)["state"]
    assert "_db_now" not in state
    (position,) = state["positions"]
    assert position["unrealized_pnl"] == "-5"
    assert position["r_multiple"] == "-0.50"
    bots = {bot["id"]: bot for bot in state["bots"]}
    assert set(bots) == {str(BOT_A), str(BOT_B)}
    assert bots[str(BOT_A)]["equity"]["equity"] == "1015"  # 1000 + 20 − 5
    assert bots[str(BOT_A)]["equity"]["peak"] == "1040"
    assert bots[str(BOT_A)]["heartbeat_age_seconds"] == 10
    assert bots[str(BOT_A)]["heartbeat_stale"] is False
    assert bots[str(BOT_B)]["heartbeat_stale"] is True
    assert bots[str(BOT_A)]["net_today"] == "-15"
    assert state["equity"]["equity"] == "1515"  # 1500 + 20 − 5
    assert state["equity"]["net_today"] == "-15"
    assert state["health"]["outbox"]["unconsumed_pending"] == 7
    assert state["health"]["clock"]["skew_ms"] == 5
    assert state["health"]["breaker"]["state"] == "CLOSED"
    assert state["workspace"]["kill_state"] == "ACTIVE"
