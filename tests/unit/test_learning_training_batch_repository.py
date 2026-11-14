"""Kiểm tra ánh xạ lỗi gateway trainer thành xung đột fail-closed."""

from __future__ import annotations

import pytest

from trading_bot.adapters.database.repositories.learning_training_batches import (
    _training_batch_failure_code,
)


@pytest.mark.parametrize(
    "message",
    [
        "WP06_TRAINING_REPLAY_CURRENT_POLICY_DENIED",
        "WP06_TRAINING_REPLAY_EVIDENCE_DENIED",
        "WP06_TRAINING_REPLAY_SCOPE_NOT_COVERED",
        "WP06_TRAINING_REPLAY_MARKET_BAR_SET_MISMATCH",
    ],
)
def test_replay_source_denials_map_to_public_fail_closed_code(message: str) -> None:
    assert _training_batch_failure_code(message) == "WP06_TRAINING_BATCH_SOURCE_RIGHTS_FAIL_CLOSED"


def test_unrelated_database_errors_are_not_mapped_to_source_rights_denial() -> None:
    assert _training_batch_failure_code("connection lost") is None
