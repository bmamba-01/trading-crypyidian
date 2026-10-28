from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.adapters.database.repositories.workspace_settings import (
    SettingsVersionConflict,
    WorkspaceSettingsRepository,
    settings_hash,
)


class SequencedSession:
    def __init__(self, results: list[object | None]) -> None:
        self.results = iter(results)
        self.statements: list[str] = []
        self.parameters: list[dict[str, object]] = []

    async def scalar(self, statement: Any) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.results)

    async def execute(self, statement: Any, params: dict[str, object]) -> None:
        self.statements.append(str(statement))
        self.parameters.append(params)
        return None

    def add(self, record: object) -> None:
        del record

    async def flush(self) -> None:
        return None


def draft_record(parent_id: object, *, status: str = "DRAFT") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        parent_version_id=parent_id,
        status=status,
        revision=4,
    )


@pytest.mark.asyncio
async def test_publish_locks_workspace_and_rejects_a_parent_that_lost_head() -> None:
    workspace_id = uuid4()
    previous_head_id = uuid4()
    draft = draft_record(previous_head_id)
    concurrent_head = SimpleNamespace(id=uuid4())
    session = SequencedSession([SimpleNamespace(id=workspace_id), draft, concurrent_head])
    repository = WorkspaceSettingsRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SettingsVersionConflict, match="hiện hành đã thay đổi"):
        await repository.publish_draft(
            workspace_id=workspace_id,
            draft_id=draft.id,
            expected_revision=draft.revision,
            approved_by=uuid4(),
            expected_parent_version_id=previous_head_id,
        )

    assert "FOR UPDATE" in session.statements[0]
    assert "effective_at" in session.statements[2]
    assert len(session.statements) == 3


@pytest.mark.asyncio
async def test_runtime_lock_contract_uses_shared_workspace_advisory_lock() -> None:
    workspace_id = uuid4()
    session = SequencedSession([])
    repository = WorkspaceSettingsRepository(session)  # type: ignore[arg-type]

    await repository.lock_workspace(workspace_id)

    assert len(session.statements) == 1
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert session.parameters == [{"workspace_lock": f"workspace-settings:{workspace_id}"}]


@pytest.mark.asyncio
async def test_publish_preserves_parent_lineage_after_locked_head_check() -> None:
    workspace_id = uuid4()
    parent_id = uuid4()
    draft = draft_record(parent_id)
    current = SimpleNamespace(id=parent_id)
    published = SimpleNamespace(
        id=draft.id,
        parent_version_id=parent_id,
        status="PUBLISHED",
        revision=draft.revision,
    )
    session = SequencedSession(
        [SimpleNamespace(id=workspace_id), draft, current, draft.id, published]
    )
    repository = WorkspaceSettingsRepository(session)  # type: ignore[arg-type]

    result = await repository.publish_draft(
        workspace_id=workspace_id,
        draft_id=draft.id,
        expected_revision=draft.revision,
        approved_by=uuid4(),
        expected_parent_version_id=parent_id,
    )

    assert result.status == "PUBLISHED"
    assert result.parent_version_id == parent_id
    assert "parent_version_id" in session.statements[3]
    assert (
        "parent_version_id"
        not in session.statements[3].split(" SET ", 1)[-1].split(" WHERE ", 1)[0]
    )


@pytest.mark.asyncio
async def test_load_effective_settings_validates_hash_and_returns_published_version() -> None:
    workspace_id = uuid4()
    version_id = uuid4()
    payload = valid_payload()
    record = SimpleNamespace(
        id=version_id,
        workspace_id=workspace_id,
        settings_json=payload,
        settings_hash=settings_hash(payload),
        status="PUBLISHED",
        effective_at=datetime.now(UTC),
    )
    repository = WorkspaceSettingsRepository(SequencedSession([record]))  # type: ignore[arg-type]

    effective = await repository.load_effective_config(workspace_id=workspace_id)

    assert effective is not None
    assert effective.workspace_id == workspace_id
    assert effective.settings_version_id == version_id
    assert effective.settings.strategy.default_release == "h01-v1"


@pytest.mark.asyncio
async def test_load_effective_settings_fails_closed_on_content_hash_mismatch() -> None:
    payload = valid_payload()
    record = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        settings_json=payload,
        settings_hash="0" * 64,
        status="PUBLISHED",
        effective_at=datetime.now(UTC),
    )
    repository = WorkspaceSettingsRepository(SequencedSession([record]))  # type: ignore[arg-type]

    with pytest.raises(SettingsVersionConflict, match="Hash settings"):
        await repository.load_effective_config(workspace_id=record.workspace_id)


@pytest.mark.asyncio
async def test_publish_stamps_effective_time_with_database_clock() -> None:
    """Fix round 2 (Minor 3): effective_at do DB đóng dấu, cùng đồng hồ coordinator so sánh."""
    workspace_id = uuid4()
    parent_id = uuid4()
    draft = draft_record(parent_id)
    published = SimpleNamespace(id=draft.id, parent_version_id=parent_id, status="PUBLISHED")
    session = SequencedSession(
        [
            SimpleNamespace(id=workspace_id),
            draft,
            SimpleNamespace(id=parent_id),
            draft.id,
            published,
        ]
    )

    await WorkspaceSettingsRepository(session).publish_draft(  # type: ignore[arg-type]
        workspace_id=workspace_id,
        draft_id=draft.id,
        expected_revision=draft.revision,
        approved_by=uuid4(),
        expected_parent_version_id=parent_id,
    )

    update = session.statements[3]
    values = update.split(" SET ", 1)[-1].split(" WHERE ", 1)[0]
    assert "published_at=statement_timestamp()" in values.replace(" ", "")
    assert "effective_at=statement_timestamp()" in values.replace(" ", "")
