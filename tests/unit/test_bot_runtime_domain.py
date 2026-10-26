"""MB05.1: domain thuần lease, fencing token, control và cổng entry (không đọc đồng hồ/DB)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.domain.execution.runtime_control import (
    MAX_LEASE_TTL,
    BotControlResult,
    BotControlState,
    BotEntryState,
    BotReadiness,
    ControlChangeKind,
    EntryBlockReason,
    EntryGate,
    EntryGateInput,
    FencingToken,
    LeaseGrant,
    LeaseSnapshot,
    RuntimeActivityStatus,
    RuntimeControlError,
    RuntimeControlSnapshot,
    RuntimeOperation,
    WorkspaceControlResult,
    WorkspaceControlState,
    WorkspaceKillState,
    decide_lease_acquire,
    decide_lease_renew,
    plan_bot_control_change,
    plan_workspace_control_change,
    verify_fence,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def test_fencing_token_is_positive_int_and_monotonic() -> None:
    first = FencingToken(1)
    second = first.next()

    assert second == FencingToken(2)
    assert first < second
    assert int(second) == 2
    for invalid in (0, -1, True, 1.5, "1"):
        with pytest.raises(RuntimeControlError) as error:
            FencingToken(invalid)  # type: ignore[arg-type]
        assert error.value.reason_code == "FENCING_TOKEN_INVALID"


def test_lease_grant_rejects_naive_past_and_overlong_expiry() -> None:
    grant = LeaseGrant(
        owner_id="runtime-a",
        fencing_token=FencingToken(1),
        granted_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        rehydrate_required=True,
    )
    assert grant.ttl == timedelta(seconds=30)

    cases = (
        (NOW.replace(tzinfo=None), NOW + timedelta(seconds=30), "LEASE_TIME_NOT_UTC"),
        (NOW, (NOW + timedelta(seconds=30)).replace(tzinfo=None), "LEASE_TIME_NOT_UTC"),
        (NOW, NOW, "LEASE_EXPIRY_NOT_FUTURE"),
        (NOW, NOW - timedelta(seconds=1), "LEASE_EXPIRY_NOT_FUTURE"),
        (NOW, NOW + MAX_LEASE_TTL + timedelta(microseconds=1), "LEASE_TTL_TOO_LONG"),
    )
    for granted_at, expires_at, code in cases:
        with pytest.raises(RuntimeControlError) as error:
            LeaseGrant(
                owner_id="runtime-a",
                fencing_token=FencingToken(1),
                granted_at=granted_at,
                expires_at=expires_at,
                rehydrate_required=False,
            )
        assert error.value.reason_code == code
    with pytest.raises(RuntimeControlError) as error:
        LeaseGrant(
            owner_id=" ",
            fencing_token=FencingToken(1),
            granted_at=NOW,
            expires_at=NOW + timedelta(seconds=5),
            rehydrate_required=False,
        )
    assert error.value.reason_code == "LEASE_OWNER_INVALID"


def _lease(owner: str = "runtime-a", token: int = 1, *, expires_in: int = 30) -> LeaseSnapshot:
    return LeaseSnapshot(
        owner_id=owner,
        fencing_token=FencingToken(token),
        expires_at=NOW + timedelta(seconds=expires_in),
    )


def test_acquire_first_time_issues_token_one_and_requires_hydrate() -> None:
    grant = decide_lease_acquire(
        None, owner_id="runtime-a", now=NOW, expires_at=NOW + timedelta(seconds=30)
    )

    assert grant.fencing_token == FencingToken(1)
    assert grant.rehydrate_required is True


def test_acquire_same_owner_before_expiry_keeps_token() -> None:
    grant = decide_lease_acquire(
        _lease(token=4), owner_id="runtime-a", now=NOW, expires_at=NOW + timedelta(seconds=60)
    )

    assert grant.fencing_token == FencingToken(4)
    assert grant.rehydrate_required is False


def test_acquire_other_owner_before_expiry_is_held_elsewhere() -> None:
    with pytest.raises(RuntimeControlError) as error:
        decide_lease_acquire(
            _lease(owner="runtime-a"),
            owner_id="runtime-b",
            now=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
    assert error.value.reason_code == "LEASE_HELD_ELSEWHERE"


def test_acquire_after_expiry_increments_token_for_new_and_same_owner() -> None:
    expired = _lease(owner="runtime-a", token=7, expires_in=0)

    takeover = decide_lease_acquire(
        expired, owner_id="runtime-b", now=NOW, expires_at=NOW + timedelta(seconds=30)
    )
    reacquire = decide_lease_acquire(
        expired, owner_id="runtime-a", now=NOW, expires_at=NOW + timedelta(seconds=30)
    )

    assert takeover.fencing_token == FencingToken(8)
    assert takeover.rehydrate_required is True
    assert reacquire.fencing_token == FencingToken(8)
    assert reacquire.rehydrate_required is True


def test_renew_requires_same_owner_token_and_live_lease() -> None:
    renewed = decide_lease_renew(
        _lease(token=3),
        owner_id="runtime-a",
        fencing_token=FencingToken(3),
        now=NOW,
        expires_at=NOW + timedelta(seconds=45),
    )
    assert renewed.fencing_token == FencingToken(3)
    assert renewed.rehydrate_required is False

    cases = (
        (None, "runtime-a", 3, "LEASE_NOT_HELD"),
        (_lease(owner="runtime-b", token=3), "runtime-a", 3, "STALE_FENCING_TOKEN"),
        (_lease(token=4), "runtime-a", 3, "STALE_FENCING_TOKEN"),
        (_lease(token=3, expires_in=0), "runtime-a", 3, "LEASE_EXPIRED"),
    )
    for current, owner, token, code in cases:
        with pytest.raises(RuntimeControlError) as error:
            decide_lease_renew(
                current,
                owner_id=owner,
                fencing_token=FencingToken(token),
                now=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )
        assert error.value.reason_code == code


def test_verify_fence_rejects_stale_owner_and_expired_lease() -> None:
    verify_fence(_lease(token=2), owner_id="runtime-a", fencing_token=FencingToken(2), now=NOW)
    cases = (
        (None, "runtime-a", 2, "LEASE_NOT_HELD"),
        (_lease(token=3), "runtime-a", 2, "STALE_FENCING_TOKEN"),
        (_lease(owner="runtime-b", token=2), "runtime-a", 2, "STALE_FENCING_TOKEN"),
        (_lease(token=2, expires_in=-1), "runtime-a", 2, "LEASE_EXPIRED"),
    )
    for current, owner, token, code in cases:
        with pytest.raises(RuntimeControlError) as error:
            verify_fence(current, owner_id=owner, fencing_token=FencingToken(token), now=NOW)
        assert error.value.reason_code == code


def _gate(**overrides: object) -> EntryGateInput:
    values: dict[str, object] = {
        "account_enabled": True,
        "bot_enabled": True,
        "workspace_kill_state": WorkspaceKillState.ACTIVE,
        "bot_entry_state": BotEntryState.ENABLED,
        "readiness": BotReadiness.READY,
        "lease_held": True,
        "open_incident": False,
        "activity_status": RuntimeActivityStatus.VERIFIED,
        "control_available": True,
    }
    values.update(overrides)
    return EntryGateInput(**values)  # type: ignore[arg-type]


def test_entry_gate_allows_only_when_every_condition_holds() -> None:
    decision = EntryGate.evaluate(_gate(), RuntimeOperation.NEW_ENTRY)

    assert decision.allowed is True
    assert decision.reason is None


def test_entry_gate_returns_first_reason_in_fixed_order() -> None:
    everything_wrong = _gate(
        account_enabled=False,
        bot_enabled=False,
        workspace_kill_state=WorkspaceKillState.KILLED,
        bot_entry_state=BotEntryState.PAUSED,
        readiness=BotReadiness.RECOVERY_REQUIRED,
        lease_held=False,
        open_incident=True,
        activity_status=RuntimeActivityStatus.UNVERIFIED,
        control_available=False,
    )
    expected_order = [
        EntryBlockReason.BOT_DISABLED,
        EntryBlockReason.WORKSPACE_KILLED,
        EntryBlockReason.BOT_PAUSED,
        EntryBlockReason.BOT_RECOVERY_REQUIRED,
        EntryBlockReason.LEASE_NOT_HELD,
        EntryBlockReason.EXTERNAL_ACTIVITY_UNRECONCILED,
        EntryBlockReason.ACCOUNT_ACTIVITY_UNVERIFIED,
        EntryBlockReason.RUNTIME_CONTROL_UNAVAILABLE,
    ]
    fixes: list[dict[str, object]] = [
        {"account_enabled": True, "bot_enabled": True},
        {"workspace_kill_state": WorkspaceKillState.ACTIVE},
        {"bot_entry_state": BotEntryState.ENABLED},
        {"readiness": BotReadiness.READY},
        {"lease_held": True},
        {"open_incident": False},
        {"activity_status": RuntimeActivityStatus.VERIFIED},
        {"control_available": True},
    ]
    current = everything_wrong
    observed = []
    for fix in fixes:
        decision = EntryGate.evaluate(current, RuntimeOperation.NEW_ENTRY)
        observed.append(decision.reason)
        current = replace(current, **fix)  # type: ignore[arg-type]
    assert observed == expected_order
    assert [reason.value for reason in EntryBlockReason] == [r.value for r in expected_order]


def test_entry_gate_disabled_account_maps_to_bot_disabled() -> None:
    decision = EntryGate.evaluate(_gate(account_enabled=False), RuntimeOperation.NEW_ENTRY)

    assert decision.reason is EntryBlockReason.BOT_DISABLED


def test_entry_gate_standby_and_starting_are_blocked_as_lease_not_held_or_recovery() -> None:
    standby = EntryGate.evaluate(
        _gate(readiness=BotReadiness.STANDBY, lease_held=False), RuntimeOperation.NEW_ENTRY
    )
    starting = EntryGate.evaluate(
        _gate(readiness=BotReadiness.STARTING), RuntimeOperation.NEW_ENTRY
    )

    assert standby.reason is EntryBlockReason.LEASE_NOT_HELD
    assert starting.reason is EntryBlockReason.BOT_RECOVERY_REQUIRED


def test_entry_gate_unknown_control_fails_closed_without_inventing_kill_state() -> None:
    decision = EntryGate.evaluate(
        _gate(workspace_kill_state=None, bot_entry_state=None, control_available=False),
        RuntimeOperation.NEW_ENTRY,
    )

    assert decision.reason is EntryBlockReason.RUNTIME_CONTROL_UNAVAILABLE


def test_entry_gate_accepts_mb04_budget_operation_values() -> None:
    from trading_bot.domain.risk.workspace_budget import BudgetOperation

    blocked = _gate(bot_entry_state=BotEntryState.PAUSED)

    assert EntryGate.evaluate(blocked, BudgetOperation.PROTECTIVE_EXIT).allowed is True
    assert EntryGate.evaluate(blocked, BudgetOperation.NEW_ENTRY).reason is (
        EntryBlockReason.BOT_PAUSED
    )


def test_entry_gate_never_blocks_protective_operations() -> None:
    blocked = _gate(
        bot_enabled=False,
        workspace_kill_state=WorkspaceKillState.KILLED,
        bot_entry_state=BotEntryState.PAUSED,
        open_incident=True,
        activity_status=RuntimeActivityStatus.UNVERIFIED,
        control_available=False,
    )
    for operation in (RuntimeOperation.PROTECTIVE_EXIT, RuntimeOperation.PROTECTIVE_MONITORING):
        decision = EntryGate.evaluate(blocked, operation)
        assert decision.allowed is True
        assert decision.reason is None


def test_control_snapshot_defaults_when_no_rows_and_exposes_revisions() -> None:
    snapshot = RuntimeControlSnapshot(workspace=None, bot=None)

    assert snapshot.workspace_kill_state is WorkspaceKillState.ACTIVE
    assert snapshot.bot_entry_state is BotEntryState.ENABLED
    assert snapshot.revisions() == {"workspace": 0, "bot": 0}

    killed = RuntimeControlSnapshot(
        workspace=WorkspaceControlState(revision=3, kill_state=WorkspaceKillState.KILLED),
        bot=BotControlState(revision=2, entry_state=BotEntryState.PAUSED),
    )
    assert killed.workspace_kill_state is WorkspaceKillState.KILLED
    assert killed.bot_entry_state is BotEntryState.PAUSED
    assert killed.revisions() == {"workspace": 3, "bot": 2}


def test_bot_control_change_first_insert_noop_and_revision_conflict() -> None:
    first = plan_bot_control_change(None, BotEntryState.PAUSED, expected_revision=None)
    assert first.kind is ControlChangeKind.INSERT
    assert first.next_revision == 1

    paused = BotControlState(revision=1, entry_state=BotEntryState.PAUSED)
    noop = plan_bot_control_change(paused, BotEntryState.PAUSED, expected_revision=None)
    assert noop.kind is ControlChangeKind.NOOP
    assert noop.next_revision == 1

    resume = plan_bot_control_change(paused, BotEntryState.ENABLED, expected_revision=1)
    assert resume.kind is ControlChangeKind.INSERT
    assert resume.next_revision == 2

    with pytest.raises(RuntimeControlError) as error:
        plan_bot_control_change(paused, BotEntryState.ENABLED, expected_revision=0)
    assert error.value.reason_code == "CONTROL_REVISION_CONFLICT"
    # Resume bắt buộc expected_revision (ruling 21).
    with pytest.raises(RuntimeControlError) as error:
        plan_bot_control_change(paused, BotEntryState.ENABLED, expected_revision=None)
    assert error.value.reason_code == "CONTROL_REVISION_REQUIRED"


def test_workspace_control_change_kill_noop_and_release_requires_revision() -> None:
    kill = plan_workspace_control_change(None, WorkspaceKillState.KILLED, expected_revision=None)
    assert (kill.kind, kill.next_revision) == (ControlChangeKind.INSERT, 1)

    killed = WorkspaceControlState(revision=1, kill_state=WorkspaceKillState.KILLED)
    noop = plan_workspace_control_change(killed, WorkspaceKillState.KILLED, expected_revision=None)
    assert noop.kind is ControlChangeKind.NOOP

    with pytest.raises(RuntimeControlError) as error:
        plan_workspace_control_change(killed, WorkspaceKillState.ACTIVE, expected_revision=None)
    assert error.value.reason_code == "CONTROL_REVISION_REQUIRED"
    release = plan_workspace_control_change(killed, WorkspaceKillState.ACTIVE, expected_revision=1)
    assert (release.kind, release.next_revision) == (ControlChangeKind.INSERT, 2)
    # Release khi chưa từng kill là noop (trạng thái đích đã đạt).
    already_active = plan_workspace_control_change(
        None, WorkspaceKillState.ACTIVE, expected_revision=0
    )
    assert already_active.kind is ControlChangeKind.NOOP


def test_control_results_are_immutable_value_objects() -> None:
    bot_id, workspace_id = uuid4(), uuid4()
    bot_result = BotControlResult(
        bot_id=bot_id, entry_state=BotEntryState.PAUSED, revision=1, changed=True
    )
    workspace_result = WorkspaceControlResult(
        workspace_id=workspace_id, kill_state=WorkspaceKillState.KILLED, revision=1, changed=True
    )

    with pytest.raises(AttributeError):
        bot_result.revision = 2  # type: ignore[misc]
    with pytest.raises(AttributeError):
        workspace_result.changed = False  # type: ignore[misc]
