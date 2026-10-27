"""MB07a.1: service vận hành bot dùng chung cho Admin API và Telegram (ruling 31–34)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from tests.unit.bot_operations_fakes import (
    BOT_A,
    BOT_B,
    FOREIGN_BOT,
    WORKSPACE,
    build_operations,
    lock_timeout_error,
    member,
    seeded_store,
)

from trading_bot.application.bot_lifecycle_guard import BotNotFlatError
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.domain.execution.runtime_control import BotEntryState, WorkspaceKillState


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _code(coroutine: Any) -> str:
    with pytest.raises((BotRuntimeControlError, BotNotFlatError)) as error:
        _run(coroutine)
    return str(error.value.reason_code)


# --- trạng thái -----------------------------------------------------------------------------
def test_status_of_visible_bot_reports_control_readiness_and_workspace_kill() -> None:
    store = seeded_store()
    owner = member(store, "owner")
    store.readiness[BOT_A] = ("READY", None)
    ops = build_operations(store).for_principal(owner)
    _run(ops.pause_bot(BOT_A, "Bảo trì"))

    status = _run(ops.bot_status(BOT_A))

    assert status.bot_id == BOT_A
    assert status.entry_state is BotEntryState.PAUSED
    assert status.control_revision == 1
    assert status.readiness == "READY"
    assert status.workspace_kill_state is WorkspaceKillState.ACTIVE
    assert status.entry_allowed is False
    assert status.as_dict()["entry_state"] == "PAUSED"


def test_entry_allowed_only_when_enabled_ready_unpaused_and_not_killed() -> None:
    store = seeded_store()
    owner = member(store, "owner")
    store.readiness[BOT_A] = ("READY", None)
    ops = build_operations(store).for_principal(owner)

    assert _run(ops.bot_status(BOT_A)).entry_allowed is True
    _run(ops.kill_workspace("Sự cố"))
    assert _run(ops.bot_status(BOT_A)).entry_allowed is False
    assert _run(ops.bot_status(BOT_B)).entry_allowed is False  # chưa READY


@pytest.mark.parametrize("bot_id", [BOT_B, FOREIGN_BOT], ids=["ungranted", "unknown"])
def test_status_outside_scope_is_not_found_without_leaking(bot_id: Any) -> None:
    store = seeded_store()
    operator = member(store, "operator")
    ops = build_operations(store).for_principal(operator)

    assert _code(ops.bot_status(bot_id)) == "BOT_NOT_FOUND"


def test_status_requires_bot_read_grant_even_when_row_visible() -> None:
    store = seeded_store()
    operator = member(store, "operator")
    # RLS cho thấy bot B (ví dụ grant khác) nhưng không có `bot:read`.
    store.visible[operator.membership_id].add(BOT_B)
    ops = build_operations(store).for_principal(operator)

    assert _code(ops.bot_status(BOT_B)) == "BOT_NOT_FOUND"


def test_other_workspace_principal_sees_nothing() -> None:
    store = seeded_store()
    outsider = member(store, "other_workspace_owner")
    store.visible[outsider.membership_id] = {BOT_A, BOT_B}  # RLS vẫn lọc theo workspace
    ops = build_operations(store).for_principal(outsider)

    assert _run(ops.list_bots()) == []
    assert _code(ops.bot_status(BOT_A)) == "BOT_NOT_FOUND"


def test_list_bots_filters_by_read_grant_and_workspace_status_lists_same_bots() -> None:
    store = seeded_store()
    operator = member(store, "operator")
    owner = member(store, "owner")
    service = build_operations(store)

    assert [item.bot_id for item in _run(service.for_principal(operator).list_bots())] == [BOT_A]
    assert [item.bot_id for item in _run(service.for_principal(owner).list_bots())] == [
        BOT_A,
        BOT_B,
    ]
    workspace = _run(service.for_principal(operator).workspace_status())
    assert workspace.kill_state is WorkspaceKillState.ACTIVE
    assert [item.bot_id for item in workspace.bots] == [BOT_A]
    assert workspace.as_dict()["workspace_id"] == str(WORKSPACE)


# --- control ủy quyền cho MB-05 ---------------------------------------------------------------
def test_control_operations_delegate_to_runtime_control_service_with_audit() -> None:
    store = seeded_store()
    risk_admin = member(store, "risk_admin")
    ops = build_operations(store).for_principal(risk_admin)

    paused = _run(ops.pause_bot(BOT_A, "Bảo trì"))
    resumed = _run(ops.resume_bot(BOT_A, "Xong", expected_revision=1))
    killed = _run(ops.kill_workspace("Sự cố"))
    released = _run(ops.release_workspace("Hết sự cố", expected_revision=1))

    assert (paused.revision, resumed.revision, killed.revision, released.revision) == (1, 2, 1, 2)
    assert [audit["action"] for audit in store.audits] == [
        "bot.runtime.paused",
        "bot.runtime.resumed",
        "workspace.runtime.killed",
        "workspace.runtime.released",
    ]


# --- disable (ruling 29, 34) ----------------------------------------------------------------
@pytest.mark.parametrize(
    "kind", ["risk_admin", "strategy_admin", "operator", "analyst_reviewer", "operator_ungranted"]
)
def test_disable_is_owner_only(kind: str) -> None:
    store = seeded_store()
    actor = member(store, kind)
    ops = build_operations(store).for_principal(actor)

    assert _code(ops.disable_bot(BOT_A, "Ngừng")) == "CONTROL_PERMISSION_DENIED"
    assert store.bots[BOT_A]["status"] == "ENABLED"
    assert "guard:L1" not in store.events
    assert store.audits[-1]["action"] == "bot.lifecycle.disable_denied"
    assert store.audits[-1]["reason"] == "CONTROL_PERMISSION_DENIED"


def test_disable_requires_recent_mfa_and_reason() -> None:
    store = seeded_store()
    stale = member(store, "owner", mfa_age=timedelta(minutes=16))
    owner = member(store, "owner")
    service = build_operations(store)

    assert _code(service.for_principal(stale).disable_bot(BOT_A, "Ngừng")) == (
        "RECENT_MFA_REQUIRED"
    )
    assert _code(service.for_principal(owner).disable_bot(BOT_A, "  ")) == (
        "CONTROL_REASON_REQUIRED"
    )
    assert store.bots[BOT_A]["status"] == "ENABLED"


def test_disable_unknown_bot_is_not_found() -> None:
    store = seeded_store()
    owner = member(store, "owner")

    code = _code(build_operations(store).for_principal(owner).disable_bot(FOREIGN_BOT, "Ngừng"))

    assert code == "BOT_NOT_FOUND"


def test_disable_not_flat_bot_is_rejected_with_counts_and_audited() -> None:
    store = seeded_store()
    owner = member(store, "owner")
    store.not_flat[BOT_A] = (1, 2, 1)
    ops = build_operations(store).for_principal(owner)

    with pytest.raises(BotNotFlatError) as error:
        _run(ops.disable_bot(BOT_A, "Ngừng"))

    assert (error.value.open_positions, error.value.open_orders) == (1, 2)
    assert error.value.held_reservations == 1
    assert store.bots[BOT_A]["status"] == "ENABLED"
    assert "account:disabled" not in store.events
    assert store.audits[-1]["action"] == "bot.lifecycle.disable_denied"
    assert store.audits[-1]["reason"] == "BOT_NOT_FLAT"
    assert store.audits[-1]["actor"] == owner.membership_id


def test_disable_flat_bot_runs_guard_before_account_write_then_noop() -> None:
    store = seeded_store()
    owner = member(store, "owner")
    ops = build_operations(store).for_principal(owner)

    first = _run(ops.disable_bot(BOT_A, "Ngừng vĩnh viễn"))
    second = _run(ops.disable_bot(BOT_A, "Ngừng vĩnh viễn"))

    assert (first.changed, first.status) == (True, "DISABLED")
    assert (second.changed, second.status) == (False, "DISABLED")
    assert store.bots[BOT_A]["status"] == "DISABLED"
    assert store.bots[BOT_B]["status"] == "ENABLED"
    assert store.events.index("guard:L1") < store.events.index("account:lock")
    assert [audit["action"] for audit in store.audits] == ["bot.disabled", "bot.disabled.noop"]
    assert store.audits[0]["reason"] == "Ngừng vĩnh viễn"
    assert store.audits[0]["target_id"] == BOT_A


# --- fix round 1 (Important #1, ruling 40/41): lock_timeout 55P03 → RUNTIME_CONTROL_UNAVAILABLE
CONTROL_CALLS = {
    "pause": lambda ops: ops.pause_bot(BOT_A, "Bảo trì"),
    "resume": lambda ops: ops.resume_bot(BOT_A, "Xong", expected_revision=1),
    "kill": lambda ops: ops.kill_workspace("Sự cố"),
    "release": lambda ops: ops.release_workspace("Hết", expected_revision=1),
    "disable": lambda ops: ops.disable_bot(BOT_A, "Ngừng"),
}


@pytest.mark.parametrize("call", CONTROL_CALLS.values(), ids=CONTROL_CALLS.keys())
def test_transient_lock_timeout_becomes_unavailable_with_denied_audit(call: Any) -> None:
    store = seeded_store()
    owner = member(store, "owner")
    store.fail_with = lock_timeout_error()
    ops = build_operations(store).for_principal(owner)

    with pytest.raises(BotRuntimeControlError) as error:
        _run(call(ops))

    assert error.value.reason_code == "RUNTIME_CONTROL_UNAVAILABLE"
    # Không giữ chuỗi exception chứa câu SQL/params (ruling 41).
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert "SQL-PARAM-MUST-NOT-LEAK" not in str(error.value)
    denied = store.audits[-1]
    assert denied["action"] in {"bot.runtime.control_denied", "bot.lifecycle.disable_denied"}
    assert denied["reason"] == "RUNTIME_CONTROL_UNAVAILABLE"
    assert denied["actor"] == owner.membership_id
    assert "SQL-PARAM-MUST-NOT-LEAK" not in repr(store.audits)


@pytest.mark.parametrize(
    "call",
    [
        lambda ops: ops.bot_status(BOT_A),
        lambda ops: ops.list_bots(),
        lambda ops: ops.workspace_status(),
    ],
    ids=["status", "list", "workspace"],
)
def test_transient_error_on_read_paths_is_unavailable(call: Any) -> None:
    store = seeded_store()
    owner = member(store, "owner")
    store.fail_with = lock_timeout_error()
    ops = build_operations(store).for_principal(owner)

    with pytest.raises(BotRuntimeControlError) as error:
        _run(call(ops))

    assert error.value.reason_code == "RUNTIME_CONTROL_UNAVAILABLE"
    assert error.value.__cause__ is None


def test_non_transient_database_error_is_not_masked() -> None:
    store = seeded_store()
    owner = member(store, "owner")
    store.fail_with = DBAPIError("SELECT 1", {}, Exception("boom"))
    ops = build_operations(store).for_principal(owner)

    with pytest.raises(DBAPIError):
        _run(ops.pause_bot(BOT_A, "Bảo trì"))
