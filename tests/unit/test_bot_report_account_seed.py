"""Repository đọc vốn ban đầu chỉ chọn các cột admin-web được cấp SELECT.

Xem migration d2e4f6a8b0c1.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository


class _Session:
    def __init__(self, row: object | None) -> None:
        self.row = row
        self.statements: list[object] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return SimpleNamespace(first=lambda: self.row)


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_account_seed_selects_only_granted_columns_and_formats_values() -> None:
    created = datetime(2026, 11, 14, 6, 13, tzinfo=UTC)
    session = _Session(
        SimpleNamespace(quote_asset="USDT", initial_equity=Decimal("10000.00"), created_at=created)
    )
    repository = BotReportRepository(session)  # type: ignore[arg-type]

    seed = asyncio.run(repository.load_account_seed(workspace_id=uuid4(), bot_id=uuid4()))

    assert seed == {
        "quote_asset": "USDT",
        "initial_equity": "10000.00",
        "created_at": created.isoformat(),
    }
    sql = _sql(session.statements[0])
    selected = sql.split("FROM", 1)[0]
    for column in ("quote_asset", "initial_equity", "created_at"):
        assert column in selected
    for forbidden in ("idempotency_key", "source_settings_hash", "trading_account_id", ".id"):
        assert forbidden not in selected
    assert "paper_account_seed.workspace_id =" in sql
    assert "paper_account_seed.bot_id =" in sql


def test_account_seed_missing_row_returns_none() -> None:
    repository = BotReportRepository(_Session(None))  # type: ignore[arg-type]

    assert asyncio.run(repository.load_account_seed(workspace_id=uuid4(), bot_id=uuid4())) is None
