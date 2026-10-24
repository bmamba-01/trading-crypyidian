from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from trading_bot.domain.events import EventEnvelope, SensitiveEventPayloadError, state_hash


def test_event_envelope_keeps_identity_and_point_in_time_fields() -> None:
    workspace_id = uuid4()
    occurred_at = datetime.now(UTC)
    envelope = EventEnvelope(
        event_type="risk.profile.created.v1",
        workspace_id=workspace_id,
        aggregate_type="risk_profile",
        aggregate_id=uuid4(),
        occurred_at=occurred_at,
        correlation_id=uuid4(),
        idempotency_key="risk-profile:version-1",
        schema_version=1,
        payload={"risk_profile_id": str(uuid4()), "version": 1},
    )

    assert envelope.workspace_id == workspace_id
    assert envelope.occurred_at == occurred_at
    assert envelope.schema_version == 1


@pytest.mark.parametrize(
    "field_name",
    [
        "api_secret",
        "binance_api_key",
        "authorization",
        "private_telegram_message",
        "raw_source_body",
    ],
)
def test_event_envelope_rejects_nested_secret_fields(field_name: str) -> None:
    with pytest.raises(SensitiveEventPayloadError):
        EventEnvelope(
            event_type="account.updated.v1",
            workspace_id=uuid4(),
            aggregate_type="account",
            aggregate_id=uuid4(),
            occurred_at=datetime.now(UTC),
            correlation_id=uuid4(),
            idempotency_key="account:1",
            schema_version=1,
            payload={"account": {field_name: "must never be serialized"}},
        )


def test_event_envelope_requires_timezone_aware_occurrence_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        EventEnvelope(
            event_type="risk.profile.created.v1",
            workspace_id=uuid4(),
            aggregate_type="risk_profile",
            aggregate_id=uuid4(),
            occurred_at=datetime(2026, 10, 22),  # noqa: DTZ001
            correlation_id=uuid4(),
            idempotency_key="risk-profile:version-1",
            schema_version=1,
            payload={},
        )


def test_state_hash_is_stable_and_does_not_return_the_original_state() -> None:
    first = state_hash({"status": "PAPER", "risk": {"fraction": "0.01"}})
    second = state_hash({"risk": {"fraction": "0.01"}, "status": "PAPER"})

    assert first == second
    assert len(first) == 64
    assert "PAPER" not in first


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 2), (2, 4), (3, 8), (10, 30)],
)
def test_retry_delay_uses_exponential_backoff_with_cap(attempt: int, expected: int) -> None:
    from trading_bot.application.outbox import retry_delay_seconds

    assert retry_delay_seconds(attempt, base_seconds=2, max_seconds=30) == expected


def test_retry_delay_rejects_invalid_limits() -> None:
    from trading_bot.application.outbox import retry_delay_seconds

    with pytest.raises(ValueError, match="backoff"):
        retry_delay_seconds(0, base_seconds=2, max_seconds=30)


def test_unit_of_work_rejects_event_from_another_workspace() -> None:
    from trading_bot.application.outbox import UnitOfWork

    workspace_id = uuid4()
    unit_of_work = UnitOfWork(cast(object, object()), workspace_id=workspace_id)
    envelope = EventEnvelope(
        event_type="risk.profile.created.v1",
        workspace_id=uuid4(),
        aggregate_type="risk_profile",
        aggregate_id=uuid4(),
        occurred_at=datetime.now(UTC),
        correlation_id=uuid4(),
        idempotency_key="risk-profile:version-1",
        schema_version=1,
        payload={},
    )

    with pytest.raises(ValueError, match="workspace"):
        unit_of_work.enqueue(envelope)
