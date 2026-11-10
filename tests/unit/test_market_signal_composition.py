from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.application.market_signal_composition import _latest_bar_rows

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _ScalarRows:
    def __init__(self, values: tuple[object, ...]) -> None:
        self.values = values

    def all(self) -> tuple[object, ...]:
        return self.values


class _Session:
    def __init__(self, values_by_query: tuple[tuple[object, ...], ...]) -> None:
        self.values_by_query = values_by_query
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _ScalarRows(self.values_by_query[len(self.statements) - 1])


@pytest.mark.asyncio
async def test_latest_bar_rows_selects_bounded_times_then_preferred_revisions() -> None:
    base = datetime(2026, 11, 8, tzinfo=UTC)
    values_by_query = (
        (base,),
        (object(),),
        (base, base.replace(hour=1)),
        (object(), object()),
        (base,),
        (object(),),
    )
    session = _Session(values_by_query)

    result = await _latest_bar_rows(
        session,  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID,
        as_of=base,
        symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT"),
        timeframes=("1h",),
        max_bars_per_stream=256,
    )

    expected_rows = tuple(values_by_query[index] for index in (1, 3, 5))
    assert result == tuple(row for stream_rows in expected_rows for row in stream_rows)
    assert len(session.statements) == 6
    for index, symbol in enumerate(("BNBUSDT", "BTCUSDT", "ETHUSDT")):
        times_statement = session.statements[index * 2]
        revision_statement = session.statements[index * 2 + 1]
        times_sql = str(times_statement.compile(dialect=postgresql.dialect()))
        revision_sql = str(revision_statement.compile(dialect=postgresql.dialect()))
        assert symbol in times_statement.compile().params.values()
        assert symbol in revision_statement.compile().params.values()
        assert "SELECT DISTINCT market_bar.open_time" in times_sql
        assert "LIMIT" in times_sql
        assert "DISTINCT ON (market_bar.open_time)" in revision_sql
        assert revision_sql.lstrip().upper().startswith("SELECT ")
        for sql in (times_sql.lower(), revision_sql.lower()):
            assert "market_bar.workspace_id =" in sql
            assert "market_bar.symbol =" in sql
            assert "market_bar.timeframe =" in sql
            assert "market_bar.source in" in sql
            assert "market_bar.is_closed is true" in sql
            assert "market_bar.close_time <=" in sql
            assert "market_bar.available_at <=" in sql
        assert "order by market_bar.open_time desc" in times_sql.lower()
        revision_order = revision_sql.lower().split("order by", maxsplit=1)[1]
        assert revision_order.index("market_bar.open_time") < revision_order.index("case when")
        assert revision_order.index("case when") < revision_order.index(
            "market_bar.available_at desc"
        )
        assert revision_order.index("market_bar.available_at desc") < revision_order.index(
            "market_bar.revision desc"
        )
        assert revision_order.index("market_bar.revision desc") < revision_order.index(
            "market_bar.id desc"
        )
