from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.cli.learning_trainer import _parser
from trading_bot.domain.learning.jobs import (
    LearningTrainingJobClaim,
    LearningTrainingJobRecoveryAction,
    recover_learning_training_job,
)


def test_expired_job_without_receipt_can_be_requeued() -> None:
    action = recover_learning_training_job(has_receipt=False, has_artifact=False)

    assert action is LearningTrainingJobRecoveryAction.REQUEUE


def test_expired_job_with_consumed_receipt_fails_instead_of_reusing_batch() -> None:
    action = recover_learning_training_job(has_receipt=True, has_artifact=False)

    assert action is LearningTrainingJobRecoveryAction.FAIL_ATTEMPT


def test_expired_job_with_persisted_artifact_is_already_succeeded() -> None:
    action = recover_learning_training_job(has_receipt=True, has_artifact=True)

    assert action is LearningTrainingJobRecoveryAction.ALREADY_SUCCEEDED


def test_artifact_without_access_receipt_is_rejected_as_inconsistent() -> None:
    with pytest.raises(ValueError, match="receipt"):
        recover_learning_training_job(has_receipt=False, has_artifact=True)


def test_job_claim_requires_positive_generation_and_aware_expiry() -> None:
    values = {
        "job_id": uuid4(),
        "workspace_id": uuid4(),
        "attempt_id": uuid4(),
        "worker_id": "trainer-01",
        "generation": 0,
        "lease_until": datetime.now(UTC) + timedelta(minutes=5),
    }

    with pytest.raises(ValueError, match="generation"):
        LearningTrainingJobClaim(**values)


def test_worker_cli_supports_claiming_one_durable_job() -> None:
    workspace_id = uuid4()

    arguments = _parser().parse_args(
        ["work-once", "--workspace-id", str(workspace_id), "--worker-id", "worker-test"]
    )

    assert arguments.command == "work-once"
    assert arguments.workspace_id == workspace_id
    assert arguments.worker_id == "worker-test"
