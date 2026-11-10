from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.application.dataset_rights_invalidation import RightsChangeEvent


def event(*, event_type: str = "research.rights_changed.v1", payload=None):
    workspace_id = uuid4()
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        event_type=event_type,
        aggregate_type="source_policy_revision",
        occurred_at=datetime(2026, 11, 3, tzinfo=UTC),
        schema_version=1,
        payload=payload
        or {
            "workspace_id": str(workspace_id),
            "source_id": "source-1",
            "change_type": "policy_revision",
            "policy_sequence": 4,
            "policy_state": "revoked",
            "reason_code": "policy_revoked",
        },
    )


def test_rights_change_event_accepts_policy_revision_contract() -> None:
    row = event()

    parsed = RightsChangeEvent.from_outbox(row)

    assert parsed.workspace_id == row.workspace_id
    assert parsed.event_id == row.id
    assert parsed.source_id == "source-1"
    assert parsed.item_id is None
    assert parsed.policy_sequence == 4
    assert parsed.change_type == "policy_revision"


def test_rights_change_event_rejects_cross_workspace_payload() -> None:
    row = event(
        payload={
            "workspace_id": str(uuid4()),
            "source_id": "source-1",
            "change_type": "policy_revision",
            "policy_sequence": 4,
            "policy_state": "revoked",
            "reason_code": "policy_revoked",
        }
    )

    with pytest.raises(ValueError, match="workspace"):
        RightsChangeEvent.from_outbox(row)


def test_rights_change_event_rejects_aggregate_and_change_type_mismatch() -> None:
    row = event()
    row.payload = {
        "workspace_id": str(row.workspace_id),
        "source_id": "source-1",
        "item_id": "item-1",
        "change_type": "item_takedown",
        "reason_code": "rights_withdrawn",
    }

    with pytest.raises(ValueError, match="aggregate"):
        RightsChangeEvent.from_outbox(row)


def test_rights_change_event_accepts_item_revision_contract() -> None:
    row = event()
    row.aggregate_type = "source_item_revision"
    row.payload = {
        "workspace_id": str(row.workspace_id),
        "source_id": "source-1",
        "item_id": "item-1",
        "change_type": "item_revision",
        "revision_id": "revision-2",
        "item_sequence": 2,
    }

    parsed = RightsChangeEvent.from_outbox(row)

    assert parsed.item_id == "item-1"
    assert parsed.revision_id == "revision-2"
    assert parsed.invalidation_reason_code == "source_item_superseded"


def test_rights_change_event_accepts_takedown_contract() -> None:
    row = event()
    row.aggregate_type = "source_item_takedown"
    row.payload = {
        "workspace_id": str(row.workspace_id),
        "source_id": "source-1",
        "item_id": "item-1",
        "change_type": "item_takedown",
        "reason_code": "rights_withdrawn",
    }

    parsed = RightsChangeEvent.from_outbox(row)

    assert parsed.item_id == "item-1"
    assert parsed.invalidation_reason_code == "source_item_takedown"
