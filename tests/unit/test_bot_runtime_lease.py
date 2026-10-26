"""MB05.3: repository lease (session giả) — CAS theo đồng hồ DB, token đơn điệu, không commit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from tests.unit.runtime_fakes import RecordingSession

from trading_bot.adapters.database.models import BotRuntimeLeaseRecord
from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
from trading_bot.domain.execution.runtime_control import (
    MAX_LEASE_TTL,
    FencingToken,
    RuntimeControlError,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
WORKSPACE, BOT, ACCOUNT = uuid4(), uuid4(), uuid4()


def _record(owner: str = "runtime-a", token: int = 1, *, expires_in: int = 30) -> object:
    return BotRuntimeLeaseRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        owner_id=owner,
        fencing_token=token,
        acquired_at=NOW - timedelta(minutes=1),
        renewed_at=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(seconds=expires_in),
    )


def _acquire(session: RecordingSession, owner: str, *, ttl: timedelta = timedelta(seconds=30)):
    return asyncio.run(BotRuntimeLease(session).acquire_grant(WORKSPACE, BOT, owner, NOW + ttl))


def test_first_acquire_inserts_token_one_using_db_clock_and_locks_row() -> None:
    session = RecordingSession(scalar_results=[None, NOW, ACCOUNT])

    grant = _acquire(session, "runtime-a")

    assert grant.fencing_token == FencingToken(1)
    assert grant.rehydrate_required is True
    assert grant.granted_at == NOW
    # Khóa hàng lease trước, đọc đồng hồ DB sau (chờ khóa không làm `now` cũ).
    assert "FOR UPDATE" in session.statements[0]
    assert "clock_timestamp()" in session.statements[1]
    [row] = session.added
    assert (row.workspace_id, row.bot_id, row.account_id) == (WORKSPACE, BOT, ACCOUNT)
    assert (row.owner_id, row.fencing_token) == ("runtime-a", 1)
    assert row.expires_at == NOW + timedelta(seconds=30)
    assert session.flushes == 1


def test_interface_acquire_returns_fencing_token() -> None:
    session = RecordingSession(scalar_results=[None, NOW, ACCOUNT])

    token = asyncio.run(
        BotRuntimeLease(session).acquire(WORKSPACE, BOT, "runtime-a", NOW + timedelta(seconds=30))
    )

    assert token == FencingToken(1)


def test_same_owner_before_expiry_keeps_token() -> None:
    record = _record(token=5)
    session = RecordingSession(scalar_results=[record, NOW])

    grant = _acquire(session, "runtime-a", ttl=timedelta(seconds=60))

    assert grant.fencing_token == FencingToken(5)
    assert grant.rehydrate_required is False
    assert record.fencing_token == 5
    assert record.expires_at == NOW + timedelta(seconds=60)
    assert session.added == []


def test_other_owner_before_expiry_is_rejected_without_mutation() -> None:
    record = _record(owner="runtime-a", token=2)
    session = RecordingSession(scalar_results=[record, NOW])

    with pytest.raises(RuntimeControlError) as error:
        _acquire(session, "runtime-b")

    assert error.value.reason_code == "LEASE_HELD_ELSEWHERE"
    assert (record.owner_id, record.fencing_token) == ("runtime-a", 2)
    assert session.flushes == 0


def test_other_owner_after_expiry_takes_over_with_next_token() -> None:
    record = _record(owner="runtime-a", token=2, expires_in=0)
    session = RecordingSession(scalar_results=[record, NOW])

    grant = _acquire(session, "runtime-b")

    assert grant.fencing_token == FencingToken(3)
    assert grant.rehydrate_required is True
    assert (record.owner_id, record.fencing_token) == ("runtime-b", 3)


def test_same_owner_after_expiry_gets_next_token_and_must_rehydrate() -> None:
    record = _record(owner="runtime-a", token=7, expires_in=-10)
    session = RecordingSession(scalar_results=[record, NOW])

    grant = _acquire(session, "runtime-a")

    assert grant.fencing_token == FencingToken(8)
    assert grant.rehydrate_required is True


@pytest.mark.parametrize(
    "ttl",
    [MAX_LEASE_TTL + timedelta(seconds=1), timedelta(seconds=0), timedelta(seconds=-5)],
    ids=["over-max", "now", "past"],
)
def test_expiry_outside_db_window_is_rejected_before_any_write(ttl: timedelta) -> None:
    session = RecordingSession(scalar_results=[None, NOW, ACCOUNT])

    with pytest.raises(RuntimeControlError) as error:
        _acquire(session, "runtime-a", ttl=ttl)

    assert error.value.reason_code in {"LEASE_TTL_TOO_LONG", "LEASE_EXPIRY_NOT_FUTURE"}
    assert session.added == []
    assert session.flushes == 0


def test_concurrent_first_insert_loser_is_held_elsewhere() -> None:
    session = RecordingSession(scalar_results=[None, NOW, ACCOUNT])
    session.flush_error = IntegrityError("INSERT", {}, Exception("uq_bot_runtime_lease_bot"))

    with pytest.raises(RuntimeControlError) as error:
        _acquire(session, "runtime-b")

    assert error.value.reason_code == "LEASE_HELD_ELSEWHERE"


def test_db_trigger_rejection_maps_to_safe_reason_code() -> None:
    record = _record(owner="runtime-a", token=2, expires_in=0)
    session = RecordingSession(scalar_results=[record, NOW])
    session.flush_error = IntegrityError(
        "UPDATE", {}, Exception("MB05_LEASE_TOKEN_INVALID\nCONTEXT: PL/pgSQL")
    )

    with pytest.raises(RuntimeControlError) as error:
        _acquire(session, "runtime-b")

    assert error.value.reason_code == "LEASE_TOKEN_INVALID"


def test_unknown_bot_cannot_acquire_lease() -> None:
    session = RecordingSession(scalar_results=[None, NOW, None])

    with pytest.raises(RuntimeControlError) as error:
        _acquire(session, "runtime-a")

    assert error.value.reason_code == "BOT_NOT_FOUND"


def test_renew_keeps_token_and_rejects_stale_or_expired() -> None:
    record = _record(token=4)
    session = RecordingSession(scalar_results=[record, NOW])
    grant = asyncio.run(
        BotRuntimeLease(session).renew(
            WORKSPACE, BOT, "runtime-a", FencingToken(4), NOW + timedelta(seconds=40)
        )
    )
    assert grant.fencing_token == FencingToken(4)
    assert record.expires_at == NOW + timedelta(seconds=40)

    for current, token, code in (
        (_record(token=5), 4, "STALE_FENCING_TOKEN"),
        (_record(token=4, expires_in=0), 4, "LEASE_EXPIRED"),
        (None, 4, "LEASE_NOT_HELD"),
    ):
        session = RecordingSession(scalar_results=[current, NOW])
        with pytest.raises(RuntimeControlError) as error:
            asyncio.run(
                BotRuntimeLease(session).renew(
                    WORKSPACE, BOT, "runtime-a", FencingToken(token), NOW + timedelta(seconds=30)
                )
            )
        assert error.value.reason_code == code
        assert session.flushes == 0


def test_release_if_owner_only_releases_live_lease_of_same_owner_and_token() -> None:
    record = _record(token=3)
    session = RecordingSession(scalar_results=[record, NOW])
    assert asyncio.run(
        BotRuntimeLease(session).release_if_owner(WORKSPACE, BOT, "runtime-a", FencingToken(3))
    )
    assert record.expires_at == NOW

    for current in (_record(owner="runtime-b", token=3), _record(token=4), None):
        session = RecordingSession(scalar_results=[current, NOW])
        assert (
            asyncio.run(
                BotRuntimeLease(session).release_if_owner(
                    WORKSPACE, BOT, "runtime-a", FencingToken(3)
                )
            )
            is False
        )
        assert session.flushes == 0


def test_repository_exposes_no_delete_and_never_commits() -> None:
    public = {name for name in dir(BotRuntimeLease) if not name.startswith("_")}

    assert not {name for name in public if "delete" in name}
    assert {"acquire", "acquire_grant", "renew", "release_if_owner", "current"} <= public
