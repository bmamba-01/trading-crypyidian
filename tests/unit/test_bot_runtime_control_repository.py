"""MB05.3: control append-only, đọc control trong transaction, state có fence và incident."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from tests.unit.runtime_fakes import RecordingSession

from trading_bot.adapters.database.models import (
    AccountActivityIncidentRecord,
    BotRuntimeControlRecord,
    BotRuntimeLeaseRecord,
    BotRuntimeStateRecord,
    WorkspaceRuntimeControlRecord,
)
from trading_bot.adapters.database.repositories.account_activity_incidents import (
    AccountActivityIncidentRepository,
)
from trading_bot.adapters.database.repositories.bot_runtime_controls import (
    BotRuntimeControlRepository,
    read_runtime_control,
)
from trading_bot.adapters.database.repositories.bot_runtime_states import (
    BotRuntimeStateRepository,
)
from trading_bot.domain.execution.runtime_control import (
    BotEntryState,
    BotReadiness,
    FencingToken,
    RuntimeActivityStatus,
    RuntimeControlError,
    RuntimeFence,
    WorkspaceKillState,
)
from trading_bot.ports.account_activity import (
    ActivityClassification,
    ActivityVerdict,
    ObservedOrder,
    ObservedPosition,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
WORKSPACE, BOT, ACCOUNT, MEMBER = uuid4(), uuid4(), uuid4(), uuid4()
FENCE = RuntimeFence("runtime-a", FencingToken(2))


def _bot_control(revision: int, state: str) -> BotRuntimeControlRecord:
    return BotRuntimeControlRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        revision=revision,
        entry_state=state,
        reason_code="OPERATOR_PAUSE",
        reason_text="Tạm dừng",
        changed_by=MEMBER,
        changed_at=NOW,
        correlation_id=uuid4(),
    )


def _workspace_control(revision: int, state: str) -> WorkspaceRuntimeControlRecord:
    return WorkspaceRuntimeControlRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        revision=revision,
        kill_state=state,
        reason_code="RISK_KILL",
        reason_text="Kill",
        changed_by=MEMBER,
        changed_at=NOW,
        correlation_id=uuid4(),
    )


def _pause(session: RecordingSession, **overrides: object):
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "bot_id": BOT,
        "target": BotEntryState.PAUSED,
        "expected_revision": None,
        "changed_by": MEMBER,
        "reason_code": "OPERATOR_PAUSE",
        "reason_text": "Tạm dừng để kiểm tra",
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return asyncio.run(BotRuntimeControlRepository(session).append_bot_control(**values))  # type: ignore[arg-type]


def test_first_pause_inserts_revision_one_after_taking_l1() -> None:
    session = RecordingSession(scalar_results=[None, ACCOUNT])

    result = _pause(session)

    assert (result.revision, result.entry_state, result.changed) == (1, BotEntryState.PAUSED, True)
    assert "pg_advisory_xact_lock" in session.statements[0]
    [row] = session.added
    assert isinstance(row, BotRuntimeControlRecord)
    assert (row.revision, row.entry_state, row.account_id) == (1, "PAUSED", ACCOUNT)
    assert row.changed_by == MEMBER


def test_each_change_inserts_next_revision_and_current_is_max_revision() -> None:
    session = RecordingSession(scalar_results=[_bot_control(1, "PAUSED"), ACCOUNT])

    result = _pause(session, target=BotEntryState.ENABLED, expected_revision=1)

    assert (result.revision, result.entry_state, result.changed) == (2, BotEntryState.ENABLED, True)
    assert session.added[0].revision == 2
    assert "ORDER BY bot_runtime_control.revision DESC" in session.statements[1]


def test_pause_when_already_paused_is_noop_without_new_row() -> None:
    session = RecordingSession(scalar_results=[_bot_control(3, "PAUSED")])

    result = _pause(session)

    assert (result.revision, result.changed) == (3, False)
    assert session.added == []
    assert session.flushes == 0


def test_expected_revision_mismatch_and_duplicate_revision_conflict() -> None:
    session = RecordingSession(scalar_results=[_bot_control(2, "PAUSED")])
    with pytest.raises(RuntimeControlError) as error:
        _pause(session, target=BotEntryState.ENABLED, expected_revision=1)
    assert error.value.reason_code == "CONTROL_REVISION_CONFLICT"

    session = RecordingSession(scalar_results=[None, ACCOUNT])
    session.flush_error = IntegrityError("INSERT", {}, Exception("uq_bot_runtime_control_revision"))
    with pytest.raises(RuntimeControlError) as error:
        _pause(session)
    assert error.value.reason_code == "CONTROL_REVISION_CONFLICT"


def test_control_requires_reason() -> None:
    for overrides in ({"reason_code": " "}, {"reason_text": ""}, {"reason_text": "x" * 501}):
        with pytest.raises(RuntimeControlError) as error:
            _pause(RecordingSession(scalar_results=[None, ACCOUNT]), **overrides)
        assert error.value.reason_code == "CONTROL_REASON_REQUIRED"


def test_workspace_kill_noop_and_release_revision() -> None:
    repository_session = RecordingSession(scalar_results=[None])
    killed = asyncio.run(
        BotRuntimeControlRepository(repository_session).append_workspace_control(
            workspace_id=WORKSPACE,
            target=WorkspaceKillState.KILLED,
            expected_revision=None,
            changed_by=MEMBER,
            reason_code="RISK_KILL",
            reason_text="Kill switch",
            correlation_id=uuid4(),
        )
    )
    assert (killed.revision, killed.kill_state, killed.changed) == (
        1,
        WorkspaceKillState.KILLED,
        True,
    )
    assert isinstance(repository_session.added[0], WorkspaceRuntimeControlRecord)

    noop_session = RecordingSession(scalar_results=[_workspace_control(1, "KILLED")])
    noop = asyncio.run(
        BotRuntimeControlRepository(noop_session).append_workspace_control(
            workspace_id=WORKSPACE,
            target=WorkspaceKillState.KILLED,
            expected_revision=None,
            changed_by=MEMBER,
            reason_code="RISK_KILL",
            reason_text="Kill lần nữa",
            correlation_id=uuid4(),
        )
    )
    assert (noop.revision, noop.changed) == (1, False)
    assert noop_session.added == []


def test_repository_has_no_update_or_delete_control_methods() -> None:
    public = {name for name in dir(BotRuntimeControlRepository) if not name.startswith("_")}

    assert not {name for name in public if "update" in name or "delete" in name}


def test_read_runtime_control_in_transaction_defaults_and_fails_closed() -> None:
    session = RecordingSession(scalar_results=[None, None])
    snapshot = asyncio.run(read_runtime_control(session, workspace_id=WORKSPACE, bot_id=BOT))
    assert snapshot.revisions() == {"workspace": 0, "bot": 0}
    assert snapshot.workspace_kill_state is WorkspaceKillState.ACTIVE

    session = RecordingSession(
        scalar_results=[_workspace_control(4, "KILLED"), _bot_control(2, "PAUSED")]
    )
    snapshot = asyncio.run(read_runtime_control(session, workspace_id=WORKSPACE, bot_id=BOT))
    assert snapshot.revisions() == {"workspace": 4, "bot": 2}
    assert snapshot.bot_entry_state is BotEntryState.PAUSED

    class _Broken(RecordingSession):
        async def scalar(self, statement: object, params: object = None) -> object:
            raise OperationalError("SELECT", {}, Exception("permission denied"))

    with pytest.raises(RuntimeControlError) as error:
        asyncio.run(read_runtime_control(_Broken(), workspace_id=WORKSPACE, bot_id=BOT))
    assert error.value.reason_code == "RUNTIME_CONTROL_UNAVAILABLE"


def _lease() -> BotRuntimeLeaseRecord:
    return BotRuntimeLeaseRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        owner_id="runtime-a",
        fencing_token=2,
        acquired_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )


def test_state_upsert_runs_fence_first_and_writes_current_token() -> None:
    session = RecordingSession(scalar_results=[_lease(), NOW, None])

    record = asyncio.run(
        BotRuntimeStateRepository(session).upsert(
            fence=FENCE,
            workspace_id=WORKSPACE,
            bot_id=BOT,
            account_id=ACCOUNT,
            readiness=BotReadiness.READY,
            reason_code=None,
            heartbeat_at=NOW,
            activity_status=RuntimeActivityStatus.VERIFIED,
        )
    )

    assert "FOR SHARE" in session.statements[0]
    assert record.fencing_token == 2
    assert record.readiness == "READY"
    assert record.activity_status == "VERIFIED"
    assert session.added == [record]


def test_state_upsert_updates_existing_row_and_rejects_unsafe_reason() -> None:
    existing = BotRuntimeStateRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        readiness="STARTING",
        heartbeat_at=NOW,
        fencing_token=1,
    )
    session = RecordingSession(scalar_results=[_lease(), NOW, existing])
    record = asyncio.run(
        BotRuntimeStateRepository(session).upsert(
            fence=FENCE,
            workspace_id=WORKSPACE,
            bot_id=BOT,
            account_id=ACCOUNT,
            readiness=BotReadiness.RECOVERY_REQUIRED,
            reason_code="PAPER_RECOVERY_CONFLICT",
            heartbeat_at=NOW,
        )
    )
    assert record is existing
    assert (record.readiness, record.fencing_token) == ("RECOVERY_REQUIRED", 2)
    assert session.added == []

    with pytest.raises(RuntimeControlError) as error:
        asyncio.run(
            BotRuntimeStateRepository(RecordingSession([_lease(), NOW, None])).upsert(
                fence=FENCE,
                workspace_id=WORKSPACE,
                bot_id=BOT,
                account_id=ACCOUNT,
                readiness=BotReadiness.READY,
                reason_code="Traceback (most recent call last): secret",
                heartbeat_at=NOW,
            )
        )
    assert error.value.reason_code == "STATE_REASON_INVALID"


def test_state_upsert_with_stale_fence_writes_nothing() -> None:
    session = RecordingSession(scalar_results=[_lease(), NOW])

    with pytest.raises(RuntimeControlError) as error:
        asyncio.run(
            BotRuntimeStateRepository(session).upsert(
                fence=RuntimeFence("runtime-a", FencingToken(1)),
                workspace_id=WORKSPACE,
                bot_id=BOT,
                account_id=ACCOUNT,
                readiness=BotReadiness.READY,
                reason_code=None,
                heartbeat_at=NOW,
            )
        )
    assert error.value.reason_code == "STALE_FENCING_TOKEN"
    assert session.added == []


def _external() -> ActivityClassification:
    return ActivityClassification(
        verdict=ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED,
        reason_code="EXTERNAL_ACTIVITY_UNRECONCILED",
        foreign_orders=(ObservedOrder("manual-1", "BTCUSDT", "BUY", Decimal("0.1"), "NEW"),),
        foreign_positions=(ObservedPosition("ETHUSDT", "SHORT", Decimal("2")),),
        fingerprint="f" * 64,
        snapshot_hash="a" * 64,
    )


def _incident(status: str = "OPEN") -> AccountActivityIncidentRecord:
    return AccountActivityIncidentRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        status=status,
        source="SCRIPTED",
        snapshot_hash="a" * 64,
        foreign_orders=[],
        foreign_positions=[],
        fingerprint="f" * 64,
        fencing_token=2,
    )


def test_open_incident_is_unique_per_open_fingerprint_and_fenced() -> None:
    session = RecordingSession(scalar_results=[_lease(), NOW, None])
    record, created = asyncio.run(
        AccountActivityIncidentRepository(session).open_incident(
            fence=FENCE,
            workspace_id=WORKSPACE,
            bot_id=BOT,
            account_id=ACCOUNT,
            classification=_external(),
            source="SCRIPTED",
            cursor="c-9",
        )
    )
    assert created is True
    assert "FOR SHARE" in session.statements[0]
    assert record.fencing_token == 2
    assert record.status == "OPEN"
    assert record.foreign_orders == [
        {
            "client_order_id": "manual-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.1",
            "status": "NEW",
        }
    ]
    assert record.foreign_positions == [{"symbol": "ETHUSDT", "side": "SHORT", "quantity": "2"}]

    existing = _incident()
    session = RecordingSession(scalar_results=[_lease(), NOW, existing])
    again, created = asyncio.run(
        AccountActivityIncidentRepository(session).open_incident(
            fence=FENCE,
            workspace_id=WORKSPACE,
            bot_id=BOT,
            account_id=ACCOUNT,
            classification=_external(),
            source="SCRIPTED",
            cursor="c-10",
        )
    )
    assert (again, created) == (existing, False)
    assert session.added == []


def test_open_incident_rejects_non_external_classification() -> None:
    clean = ActivityClassification(verdict=ActivityVerdict.CLEAN, reason_code=None)

    with pytest.raises(RuntimeControlError) as error:
        asyncio.run(
            AccountActivityIncidentRepository(RecordingSession()).open_incident(
                fence=FENCE,
                workspace_id=WORKSPACE,
                bot_id=BOT,
                account_id=ACCOUNT,
                classification=clean,
                source="SCRIPTED",
                cursor=None,
            )
        )
    assert error.value.reason_code == "INCIDENT_CLASSIFICATION_INVALID"


def test_resolution_only_open_to_resolved_with_allowed_code_and_reason() -> None:
    incident = _incident()
    session = RecordingSession(scalar_results=[incident])
    resolved = asyncio.run(
        AccountActivityIncidentRepository(session).resolve(
            workspace_id=WORKSPACE,
            bot_id=BOT,
            incident_id=incident.id,
            resolution_code="FALSE_POSITIVE_CONFIRMED",
            reason="Đã xác minh với sàn",
            resolved_by=MEMBER,
            resolved_at=NOW,
        )
    )
    assert resolved.status == "RESOLVED"
    assert (resolved.resolved_by, resolved.resolution_code) == (
        MEMBER,
        "FALSE_POSITIVE_CONFIRMED",
    )
    assert "FOR UPDATE" in session.statements[0]

    cases = (
        (_incident("RESOLVED"), "FALSE_POSITIVE_CONFIRMED", "ok", "INCIDENT_NOT_OPEN"),
        (None, "FALSE_POSITIVE_CONFIRMED", "ok", "INCIDENT_NOT_FOUND"),
        (_incident(), "ADOPTED_BY_BOT", "ok", "INCIDENT_RESOLUTION_CODE_INVALID"),
        (_incident(), "FALSE_POSITIVE_CONFIRMED", " ", "INCIDENT_RESOLUTION_REASON_REQUIRED"),
    )
    for current, code, reason, expected in cases:
        with pytest.raises(RuntimeControlError) as error:
            asyncio.run(
                AccountActivityIncidentRepository(RecordingSession([current])).resolve(
                    workspace_id=WORKSPACE,
                    bot_id=BOT,
                    incident_id=uuid4(),
                    resolution_code=code,
                    reason=reason,
                    resolved_by=MEMBER,
                    resolved_at=NOW,
                )
            )
        assert error.value.reason_code == expected
