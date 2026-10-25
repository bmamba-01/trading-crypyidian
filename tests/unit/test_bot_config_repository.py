"""Repository phiên bản cấu hình bot: scope workspace+bot, lock, revision và publish."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionConflict,
    BotConfigVersionRepository,
    request_fingerprint,
)
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash


class RecordingSession:
    """Session giả ghi câu lệnh; trả kết quả theo hàng đợi để kiểm tra luồng repository."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.statements: list[str] = []
        self.params: list[dict[str, object]] = []
        self.added: list[object] = []

    def _record(self, statement: object, params: object = None) -> None:
        compiled = statement.compile()  # type: ignore[attr-defined]
        self.statements.append(str(compiled))
        self.params.append(dict(compiled.params) | dict(params or {}))  # type: ignore[call-overload]

    async def scalar(self, statement: object) -> object:
        self._record(statement)
        return self.results.pop(0) if self.results else None

    async def execute(self, statement: object, params: object = None) -> None:
        self._record(statement, params)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


async def test_effective_lookup_is_scoped_to_workspace_bot_and_published_rows() -> None:
    session = RecordingSession()
    workspace_id, bot_id = uuid4(), uuid4()
    as_of = datetime(2026, 10, 24, tzinfo=UTC)

    await BotConfigVersionRepository(session).get_effective(  # type: ignore[arg-type]
        workspace_id=workspace_id, bot_id=bot_id, as_of=as_of
    )

    statement = session.statements[0]
    assert "bot_config_version.workspace_id = " in statement
    assert "bot_config_version.bot_id = " in statement
    assert "bot_config_version.status = " in statement
    assert "bot_config_version.effective_at <= " in statement
    assert "ORDER BY bot_config_version.version DESC" in statement
    assert {workspace_id, bot_id, "PUBLISHED", as_of} <= set(session.params[0].values())


async def test_lock_bot_uses_transaction_advisory_lock_scoped_by_workspace_and_bot() -> None:
    session = RecordingSession()
    workspace_id, bot_id = uuid4(), uuid4()

    await BotConfigVersionRepository(session).lock_bot(  # type: ignore[arg-type]
        workspace_id=workspace_id, bot_id=bot_id
    )

    assert "pg_advisory_xact_lock" in session.statements[0]
    assert f"bot-config:{workspace_id}:{bot_id}" in session.params[0].values()


async def test_create_draft_numbers_versions_and_hashes_canonical_overlay() -> None:
    session = RecordingSession(4)
    overlay = BotConfigOverlay.model_validate(
        {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.0050"}}
    )
    membership_id, parent_id, base_id = uuid4(), uuid4(), uuid4()

    record = await BotConfigVersionRepository(session).create_draft(  # type: ignore[arg-type]
        workspace_id=uuid4(),
        bot_id=uuid4(),
        account_id=uuid4(),
        overlay=overlay,
        base_settings_version_id=base_id,
        base_settings_hash="c" * 64,
        created_by=membership_id,
        parent_version_id=parent_id,
    )

    assert record.version == 4
    assert record.status == "DRAFT"
    assert record.revision == 1
    assert record.overlay_json == canonical_overlay(overlay)
    assert record.overlay_hash == overlay_hash(canonical_overlay(overlay))
    assert record.created_by == record.updated_by == membership_id
    assert record.parent_version_id == parent_id
    assert record.base_settings_version_id == base_id
    assert session.added == [record]


async def test_update_draft_conflicts_when_revision_or_status_changed() -> None:
    session = RecordingSession(None)

    with pytest.raises(BotConfigVersionConflict):
        await BotConfigVersionRepository(session).update_draft(  # type: ignore[arg-type]
            workspace_id=uuid4(),
            bot_id=uuid4(),
            draft_id=uuid4(),
            expected_revision=1,
            overlay=BotConfigOverlay.model_validate({"schema_version": 1}),
            base_settings_version_id=uuid4(),
            base_settings_hash="c" * 64,
            updated_by=uuid4(),
        )

    assert "bot_config_version.revision = " in session.statements[0]
    assert "bot_config_version.status = " in session.statements[0]


def _draft(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid4(),
        "status": "DRAFT",
        "revision": 2,
        "parent_version_id": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("draft_changes", "head"),
    [
        ({"status": "PUBLISHED"}, None),
        ({"revision": 3}, None),
        ({}, SimpleNamespace(id=uuid4())),
    ],
)
async def test_publish_rejects_stale_revision_status_or_parent(
    draft_changes: dict[str, object], head: object
) -> None:
    draft = _draft(**draft_changes)
    session = RecordingSession(draft, head)

    with pytest.raises(BotConfigVersionConflict):
        await BotConfigVersionRepository(session).publish_draft(  # type: ignore[arg-type]
            workspace_id=uuid4(),
            bot_id=uuid4(),
            draft_id=draft.id,  # type: ignore[arg-type]
            expected_revision=2,
            expected_parent_version_id=None,
            approved_by=uuid4(),
            base_settings_version_id=uuid4(),
            base_settings_hash="c" * 64,
        )


async def test_publish_sets_effective_time_and_approver_when_parent_is_head() -> None:
    draft = _draft()
    published = SimpleNamespace(id=draft.id, status="PUBLISHED")
    session = RecordingSession(draft, None, draft.id, published)
    approver = uuid4()
    effective_at = datetime(2026, 10, 24, tzinfo=UTC) + timedelta(minutes=1)

    result = await BotConfigVersionRepository(session).publish_draft(  # type: ignore[arg-type]
        workspace_id=uuid4(),
        bot_id=uuid4(),
        draft_id=draft.id,  # type: ignore[arg-type]
        expected_revision=2,
        expected_parent_version_id=None,
        approved_by=approver,
        base_settings_version_id=uuid4(),
        base_settings_hash="c" * 64,
        effective_at=effective_at,
    )

    assert result is published
    update_params = session.params[2]
    assert update_params["status"] == "PUBLISHED"
    assert update_params["approved_by"] == approver
    assert update_params["effective_at"] == effective_at
    assert update_params["published_at"] == effective_at


def test_request_fingerprint_is_order_independent_and_payload_sensitive() -> None:
    first = request_fingerprint("publish", {"draft_id": "a", "expected_revision": 1})
    same = request_fingerprint("publish", {"expected_revision": 1, "draft_id": "a"})
    other = request_fingerprint("publish", {"draft_id": "a", "expected_revision": 2})
    other_operation = request_fingerprint("rollback", {"draft_id": "a", "expected_revision": 1})

    assert first == same
    assert len({first, other, other_operation}) == 3
