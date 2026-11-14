"""Adapter Python giữ operation và metadata tối thiểu từ SQL rights verifier."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from trading_bot.adapters.database.replay_training_rights import (
    DatabaseReplayTrainingRightsGate,
    ReplayTrainingOperation,
    training_scope_digest,
)

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BUNDLE_HASH = "1" * 64
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _row(child_hash: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "bundle_hash": BUNDLE_HASH,
        "child_dataset_hash": child_hash,
        "interval": "1h",
        "symbol": "BTCUSDT",
        "bars_content_hash": "2" * 64,
        "product": "FUTURES",
        "venue": "BINANCE",
        "contract": "USD_M_PERPETUAL",
        "data_start": NOW - timedelta(days=2),
        "data_end": NOW - timedelta(days=1),
        "policy_id": "BINANCE_USDM_REST:verified",
        "policy_fingerprint": "3" * 64,
        "bundle_policy_fingerprint": "4" * 64,
        "current_policy_fingerprint": "3" * 64,
        "terms_url": "https://example.invalid/terms",
        "verified_at": NOW - timedelta(days=3),
        "checked_at": NOW,
        "valid_until": NOW + timedelta(minutes=5),
        "license_evidence_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "evidence_digest": "5" * 64,
        "license_scope_hash": "6" * 64,
        "scope_hash": "7" * 64,
        "purpose": "training",
        "provider": "BINANCE_USDM_REST",
    }
    values.update(overrides)
    return values


class _Result:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> tuple[dict[str, object], ...]:
        return self._rows


class _Session:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows
        self.statement = ""
        self.parameters: dict[str, object] = {}

    async def execute(self, statement, parameters):
        self.statement = str(statement)
        self.parameters = parameters
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_verifier_returns_exact_child_receipts_without_evidence_payload() -> None:
    session = _Session((_row("a" * 64), _row("b" * 64)))
    gate = DatabaseReplayTrainingRightsGate(session)  # type: ignore[arg-type]

    receipts = await gate.authorize(
        workspace_id=WORKSPACE_ID,
        bundle_hash=BUNDLE_HASH,
        operation=ReplayTrainingOperation.TRAINING,
    )

    assert "wp06_authorize_replay_training_bundle" in session.statement
    assert session.parameters == {
        "workspace_id": str(WORKSPACE_ID),
        "bundle_hash": BUNDLE_HASH,
        "operation": "training",
    }
    assert {item.child_dataset_hash for item in receipts} == {"a" * 64, "b" * 64}
    assert all(item.policy_fingerprint == item.current_policy_fingerprint for item in receipts)
    assert all(item.bundle_policy_fingerprint == "4" * 64 for item in receipts)
    assert all(item.evidence_digest == "5" * 64 for item in receipts)
    assert "evidence_body" not in receipts[0].canonical_json
    assert len(training_scope_digest(receipts)) == 64


@pytest.mark.asyncio
async def test_verifier_receipt_pins_evaluation_operation_separately() -> None:
    session = _Session((_row("a" * 64),))
    receipts = await DatabaseReplayTrainingRightsGate(session).authorize(
        workspace_id=WORKSPACE_ID,
        bundle_hash=BUNDLE_HASH,
        operation=ReplayTrainingOperation.EVALUATION,
    )

    assert session.parameters["operation"] == "evaluation"
    assert receipts[0].purpose.value == "training"
    assert receipts[0].operation is ReplayTrainingOperation.EVALUATION
    assert receipts[0].scope_payload["operation"] == "evaluation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"workspace_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")},
        {"bundle_hash": "8" * 64},
        {"provider": "UNKNOWN_PROVIDER"},
        {"purpose": "backtest_research"},
        {"evidence_digest": "invalid"},
        {"policy_fingerprint": "9" * 64},
        {"data_start": NOW + timedelta(days=1)},
    ],
)
async def test_gate_rejects_malformed_or_mismatched_verifier_metadata(changed) -> None:
    row = _row("a" * 64, **changed)
    session = _Session((row,))

    with pytest.raises((PermissionError, ValueError)):
        await DatabaseReplayTrainingRightsGate(session).authorize(
            workspace_id=WORKSPACE_ID,
            bundle_hash=BUNDLE_HASH,
            operation=ReplayTrainingOperation.TRAINING,
        )


@pytest.mark.asyncio
async def test_gate_rejects_duplicate_child_rows() -> None:
    session = _Session((_row("a" * 64), _row("a" * 64)))

    with pytest.raises(PermissionError, match="child receipt trùng"):
        await DatabaseReplayTrainingRightsGate(session).authorize(
            workspace_id=WORKSPACE_ID,
            bundle_hash=BUNDLE_HASH,
            operation=ReplayTrainingOperation.TRAINING,
        )
