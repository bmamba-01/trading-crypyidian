from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.bot_scope_fakes import BotScopeSession
from tests.unit.test_paper_execution import _gateway

from trading_bot.adapters.database.models.paper_entities import PaperExecutionCheckpointRecord
from trading_bot.adapters.database.repositories.paper_checkpoints import (
    PaperCheckpointConflict,
    PaperCheckpointRepository,
)
from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


class FakeSession(BotScopeSession):
    def __init__(self, results: list[object | None]) -> None:
        self.results = iter(results)
        self.statements: list[str] = []
        self.added: list[object] = []

    async def execute(self, statement: Any, params: dict[str, object] | None = None) -> None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        if params is not None:
            self.statements.append(str(params))

    async def scalar(self, statement: Any) -> object | None:
        if self.is_bot_query(statement):
            return self.bot_identity(statement)
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.results)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _account():
    return SimpleNamespace(mode="PAPER", status="ENABLED")


def _checkpoint(*, revision: int, key: str, event_hash: str, snapshot: PaperExecutionSnapshot):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        account_id=uuid4(),
        mode="PAPER",
        revision=revision,
        event_key=key,
        event_hash=event_hash,
        event_scope=None,
        event_at=NOW,
        schema_version=snapshot.schema_version,
        policy_hash=snapshot.policy_hash,
        snapshot_hash=snapshot.content_hash,
        snapshot_payload=snapshot.payload,
        created_at=NOW,
    )


def test_paper_checkpoint_schema_is_account_scoped_append_only_and_versioned() -> None:
    table = PaperExecutionCheckpointRecord.__table__
    columns = set(table.columns.keys())
    unique_keys = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert {
        "workspace_id",
        "account_id",
        "revision",
        "event_key",
        "event_hash",
        "schema_version",
        "policy_hash",
        "snapshot_hash",
        "snapshot_payload",
    }.issubset(columns)
    assert ("workspace_id", "account_id", "revision") in unique_keys
    assert ("workspace_id", "account_id", "event_key") in unique_keys
    assert any(
        "workspace_id" in tuple(fk.parent.name for fk in constraint.elements)
        and "trading_account" in str(constraint.elements[0].target_fullname)
        for constraint in table.foreign_key_constraints
    )


@pytest.mark.asyncio
async def test_checkpoint_append_locks_account_and_assigns_first_revision() -> None:
    workspace_id = uuid4()
    account_id = uuid4()
    session = FakeSession([_account(), None, None])
    uow = SimpleNamespace(session=session, workspace_id=workspace_id)
    snapshot = _gateway().export_state()

    result = await PaperCheckpointRepository().append(
        uow,  # type: ignore[arg-type]
        account_id=account_id,
        event_key="submit:one",
        event_hash="a" * 64,
        snapshot=snapshot,
        occurred_at=NOW,
    )

    assert not result.duplicate
    assert result.revision == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, PaperExecutionCheckpointRecord)
    assert row.workspace_id == workspace_id
    assert row.account_id == account_id
    assert row.snapshot_payload == snapshot.payload
    assert any("pg_advisory_xact_lock" in statement for statement in session.statements)
    assert any("paper-checkpoint" in statement for statement in session.statements)


@pytest.mark.asyncio
async def test_checkpoint_duplicate_is_idempotent_but_conflicting_payload_rejects() -> None:
    account_id = uuid4()
    workspace_id = uuid4()
    snapshot = _gateway().export_state()
    existing = _checkpoint(revision=3, key="bar:one", event_hash="b" * 64, snapshot=snapshot)
    existing.account_id = account_id
    existing.workspace_id = workspace_id
    # Replay token None: kiểm "chưa từng có lease" (M2) -> lease None.
    same_session = FakeSession([_account(), existing, None, existing])
    same_uow = SimpleNamespace(session=same_session, workspace_id=workspace_id)
    result = await PaperCheckpointRepository().append(
        same_uow,  # type: ignore[arg-type]
        account_id=account_id,
        event_key="bar:one",
        event_hash="b" * 64,
        snapshot=snapshot,
        occurred_at=NOW,
    )
    assert result.duplicate
    assert result.revision == 3
    assert same_session.added == []

    changed_session = FakeSession([_account(), existing])
    changed_uow = SimpleNamespace(session=changed_session, workspace_id=workspace_id)
    with pytest.raises(PaperCheckpointConflict, match="payload khác"):
        await PaperCheckpointRepository().append(
            changed_uow,  # type: ignore[arg-type]
            account_id=account_id,
            event_key="bar:one",
            event_hash="c" * 64,
            snapshot=snapshot,
            occurred_at=NOW,
        )


@pytest.mark.asyncio
async def test_duplicate_historical_event_returns_latest_account_snapshot() -> None:
    account_id = uuid4()
    workspace_id = uuid4()
    historical_snapshot = _gateway().export_state()
    latest_snapshot = _gateway().export_state()
    existing = _checkpoint(
        revision=3, key="market:first", event_hash="b" * 64, snapshot=historical_snapshot
    )
    existing.account_id = account_id
    existing.workspace_id = workspace_id
    latest = _checkpoint(
        revision=5, key="market:last", event_hash="c" * 64, snapshot=latest_snapshot
    )
    latest.account_id = account_id
    latest.workspace_id = workspace_id
    session = FakeSession([_account(), existing, None, latest])
    uow = SimpleNamespace(session=session, workspace_id=workspace_id)

    result = await PaperCheckpointRepository().append(
        uow,  # type: ignore[arg-type]
        account_id=account_id,
        event_key="market:first",
        event_hash="b" * 64,
        snapshot=latest_snapshot,
        occurred_at=NOW,
    )

    assert result.duplicate
    assert result.revision == 5
    assert result.snapshot == latest_snapshot


@pytest.mark.asyncio
async def test_checkpoint_rejects_out_of_order_event_for_same_symbol() -> None:
    account_id = uuid4()
    workspace_id = uuid4()
    latest_snapshot = _gateway().export_state()
    latest = _checkpoint(
        revision=7,
        key="market:BTCUSDT:later",
        event_hash="d" * 64,
        snapshot=latest_snapshot,
    )
    latest.workspace_id = workspace_id
    latest.account_id = account_id
    latest.event_scope = "BTCUSDT"
    latest.event_at = NOW + timedelta(minutes=1)
    session = FakeSession([_account(), None, latest, latest])
    uow = SimpleNamespace(session=session, workspace_id=workspace_id)

    with pytest.raises(PaperCheckpointConflict, match="out-of-order"):
        await PaperCheckpointRepository().append(
            uow,  # type: ignore[arg-type]
            account_id=account_id,
            event_key="market:BTCUSDT:earlier",
            event_hash="e" * 64,
            snapshot=latest_snapshot,
            occurred_at=NOW,
            event_scope="BTCUSDT",
        )
