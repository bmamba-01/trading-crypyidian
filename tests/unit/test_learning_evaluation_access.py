"""Hợp đồng request cho receipt truy cập evaluator WP06."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.application.learning_evaluation import LearningEvaluationRequest
from trading_bot.application.learning_evaluation_access import evaluation_access_fingerprint
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _request(**changes: object) -> LearningEvaluationRequest:
    base = {
        "workspace_id": uuid4(),
        "manifest_hash": "a" * 64,
        "folds": (
            WalkForwardSplit(
                TemporalWindow(NOW, NOW + timedelta(hours=1)),
                TemporalWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
                TemporalWindow(NOW + timedelta(hours=2), NOW + timedelta(hours=3)),
            ),
        ),
        "evaluation_cutoff": NOW + timedelta(hours=3),
        "seed": 7,
        "idempotency_key": uuid4(),
    }
    base.update(changes)
    return LearningEvaluationRequest(**base)  # type: ignore[arg-type]


def test_access_fingerprint_is_stable_and_binds_workspace_manifest_split_and_seed() -> None:
    request = _request()

    assert evaluation_access_fingerprint(request) == evaluation_access_fingerprint(request)
    assert evaluation_access_fingerprint(request) != evaluation_access_fingerprint(
        _request(workspace_id=request.workspace_id, seed=8)
    )
    assert evaluation_access_fingerprint(request) != evaluation_access_fingerprint(
        _request(workspace_id=request.workspace_id, manifest_hash="b" * 64)
    )
    assert evaluation_access_fingerprint(request) != evaluation_access_fingerprint(
        _request(
            workspace_id=request.workspace_id,
            folds=(
                WalkForwardSplit(
                    TemporalWindow(NOW, NOW + timedelta(minutes=30)),
                    TemporalWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
                    TemporalWindow(NOW + timedelta(hours=2), NOW + timedelta(hours=3)),
                ),
            ),
        )
    )


def test_access_fingerprint_does_not_treat_run_id_as_payload_identity() -> None:
    request = _request()

    assert evaluation_access_fingerprint(request) == evaluation_access_fingerprint(
        _request(
            workspace_id=request.workspace_id,
            idempotency_key=uuid4(),
        )
    )


def test_access_fingerprint_rejects_non_authoritative_request() -> None:
    with pytest.raises(ValueError, match="LearningEvaluationRequest"):
        evaluation_access_fingerprint(object())  # type: ignore[arg-type]
