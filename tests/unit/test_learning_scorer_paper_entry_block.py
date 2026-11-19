"""Kiểm quyền và dữ liệu audit cho thao tác chặn entry PAPER."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from trading_bot.auth.policy import Permission, Principal, Role, authorize


def _event(**overrides):
    from trading_bot.domain.learning.paper_entry_block import LearningPaperEntryBlockEvent

    created_at = datetime(2026, 11, 18, 6, tzinfo=UTC)
    return LearningPaperEntryBlockEvent(
        **(
            {
                "event_id": UUID(int=1),
                "workspace_id": UUID(int=2),
                "bot_id": UUID(int=3),
                "bot_decision_id": UUID(int=4),
                "bot_decision_hash": "a" * 64,
                "candidate_id": "b" * 64,
                "score_event_id": UUID(int=5),
                "score_event_hash": "c" * 64,
                "deployment_event_id": UUID(int=6),
                "deployment_revision": 7,
                "registry_id": UUID(int=8),
                "registry_revision": 9,
                "active_pointer_version": 10,
                "scorer_artifact_id": UUID(int=11),
                "scorer_artifact_hash": "d" * 64,
                "actor_user_id": UUID(int=12),
                "mfa_verified_at": created_at - timedelta(minutes=15),
                "reason": "Điều kiện thị trường thay đổi",
                "created_at": created_at,
                "idempotency_key": UUID(int=13),
            }
            | overrides
        )
    )


@pytest.mark.parametrize("role", list(Role))
def test_paper_block_permission_is_limited_to_owner_and_risk_admin(role: Role) -> None:
    principal = Principal(
        UUID(int=12), UUID(int=14), UUID(int=2), frozenset({role}), datetime.now(UTC)
    )

    assert authorize(principal, Permission.LEARNING_PAPER_ENTRY_BLOCK) is (
        role in {Role.OWNER, Role.RISK_ADMIN}
    )


def test_block_event_is_immutable() -> None:
    event = _event()

    with pytest.raises(FrozenInstanceError):
        event.reason = "Lý do đã sửa"


def test_reason_limit_applies_after_normalization() -> None:
    event = _event(reason="\n\u00a0" + "ệ" * 500 + "\t ")

    assert event.reason == "ệ" * 500


@pytest.mark.parametrize("reason", ["", " \n\u2003", "ệ" * 501, "a\x00b", "\x01a"])
def test_rejects_invalid_block_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        _event(reason=reason)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("idempotency_key", UUID(int=0)),
        ("candidate_id", "candidate"),
        ("score_event_hash", "C" * 64),
        ("deployment_revision", 0),
        ("registry_revision", True),
        ("active_pointer_version", -1),
    ],
)
def test_rejects_invalid_audit_metadata(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _event(**{field: value})


def test_preserves_audit_instants_in_utc() -> None:
    event = _event(
        mfa_verified_at=datetime(2026, 11, 18, 12, 45, tzinfo=timezone(timedelta(hours=7)))
    )

    assert event.mfa_verified_at == datetime(2026, 11, 18, 5, 45, tzinfo=UTC)
    assert event.mfa_verified_at.tzinfo is UTC


@pytest.mark.parametrize("offset", [timedelta(seconds=1), -timedelta(minutes=15, seconds=1)])
def test_rejects_mfa_outside_audit_window(offset: timedelta) -> None:
    created_at = datetime(2026, 11, 18, 6, tzinfo=UTC)

    with pytest.raises(ValueError, match="MFA"):
        _event(created_at=created_at, mfa_verified_at=created_at + offset)


def test_rejects_audit_timestamp_without_timezone() -> None:
    event = _event()

    with pytest.raises(ValueError, match="created_at"):
        replace(event, created_at=event.created_at.replace(tzinfo=None))
