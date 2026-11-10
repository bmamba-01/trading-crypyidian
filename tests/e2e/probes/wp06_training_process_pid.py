from __future__ import annotations

import json
import os
from threading import Event, Timer
from uuid import uuid4

from trading_bot.application.learning_training_batch import (
    AuthorizedTrainerBatch,
    AuthorizedTrainerSample,
)
from trading_bot.application.learning_training_process import (
    LearningCpuTrainingProcessRunner,
    LearningTrainingProcessFailure,
    LearningTrainingProcessFailureCode,
)
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)


def _batch() -> AuthorizedTrainerBatch:
    request = LearningResourceRequest(
        training_samples=3,
        validation_samples=3,
        feature_count=1,
        max_steps=30,
        max_runtime_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    samples = tuple(
        AuthorizedTrainerSample(
            sample_id=f"{role}-{label}",
            role=role,
            strategy_release_id=None,
            strategy_release_content_hash=None,
            vector_schema_version=None,
            vector_schema_hash=None,
            vector_hash=None,
            features=(("momentum", float(label)),),
            label_value=label,
            sample_hash=f"{label + 1:x}" * 64,
        )
        for role in ("train", "validation")
        for label in (-1, 0, 1)
    )
    return AuthorizedTrainerBatch(
        attempt_id=uuid4(),
        normalized_dataset_id=uuid4(),
        workspace_id=uuid4(),
        manifest_hash="a" * 64,
        split_policy_version="split-v2",
        fold_plan_hash="b" * 64,
        fold_id="c" * 64,
        approval_hash="d" * 64,
        seed=19,
        trainer_version="cpu-logistic-v1",
        dependency_fingerprint="e" * 64,
        feature_schema_hash="f" * 64,
        attempt_content_hash="1" * 64,
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
        samples=samples,
    )


def main() -> None:
    cancellation = Event()
    timer = Timer(0.05, cancellation.set)
    timer.start()
    try:
        LearningCpuTrainingProcessRunner().train(_batch(), cancel_event=cancellation)
    except LearningTrainingProcessFailure as error:
        timer.join(timeout=1)
        if error.code is not LearningTrainingProcessFailureCode.CANCELLED:
            raise AssertionError("PID probe chỉ chấp nhận kết quả cancelled.") from None
        process_id = error.process_id
        if type(process_id) is not int or process_id <= 0 or process_id == os.getpid():
            raise AssertionError("Runner không trả PID child bị kết thúc.") from None
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("PID child còn tồn tại sau khi worker hủy.")
        print(json.dumps({"cancelled": True, "child_pid_reaped": process_id}))
        return
    raise AssertionError("Runner không nhận tín hiệu hủy.")


if __name__ == "__main__":
    main()
