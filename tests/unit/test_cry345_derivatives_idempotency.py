"""Unit test cho CRY-345: idempotency ON CONFLICT DO NOTHING cho derivatives_worker & RLS."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.models import DerivativesSnapshotRecord
from trading_bot.workers.derivatives_worker import _DerivativesStore


@pytest.mark.asyncio
async def test_derivatives_store_insert_uses_on_conflict_do_nothing() -> None:
    """Chạy lại cùng (workspace_id, symbol, collected_at) không trùng và không vỡ transaction."""
    ws_id = uuid4()
    executed_sql: list[str] = []

    class _FakeSession:
        async def execute(self, stmt: Any) -> None:
            compiled = stmt.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
            executed_sql.append(str(compiled))

        async def flush(self) -> None:
            return None

    store = _DerivativesStore(_FakeSession(), ws_id)  # type: ignore[arg-type]
    now = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)
    record = {
        "workspace_id": ws_id,
        "symbol": "BTCUSDT",
        "collected_at": now,
        "policy_fingerprint": "a" * 64,
        "payload_sha256": "b" * 64,
        "mark_price": Decimal("95000.0"),
        "funding_rate": Decimal("0.0001"),
        "next_funding_time": now,
        "premium_time": now,
    }
    await store.insert(record)
    await store.insert(record)

    assert len(executed_sql) == 2
    assert "ON CONFLICT ON CONSTRAINT uq_derivatives_snapshot_point DO NOTHING" in executed_sql[0]
    assert DerivativesSnapshotRecord.__tablename__ == "derivatives_snapshot"
