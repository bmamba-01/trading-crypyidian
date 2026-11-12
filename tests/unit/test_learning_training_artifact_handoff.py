"""Kiểm hợp đồng metadata artifact và cách ly workspace của trainer admin."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.learning_training_admin import (
    LearningTrainingAdminRepository,
    LearningTrainingJobSnapshot,
)
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings


def _job() -> LearningTrainingJobSnapshot:
    now = datetime.now(UTC)
    return LearningTrainingJobSnapshot(
        job_id=uuid4(),
        attempt_id=uuid4(),
        normalized_dataset_id=uuid4(),
        manifest_hash="b" * 64,
        fold_plan_hash="c" * 64,
        fold_id="d" * 64,
        status="succeeded",
        stage="complete",
        progress=100,
        attempt_count=1,
        lease_generation=1,
        cancel_requested=False,
        error_code=None,
        training_samples=12,
        validation_samples=3,
        feature_count=8,
        created_at=now,
        updated_at=now,
    )


def test_training_api_returns_artifact_lineage_without_model_or_sample_payload() -> None:
    artifact_id = uuid4()
    workspace_id = uuid4()
    result = SimpleNamespace(
        artifact_id=artifact_id,
        artifact_hash="a" * 64,
        artifact_schema_version="cpu-artifact-v1",
        trainer_version="cpu-trainer-v1",
        approval_hash="e" * 64,
        batch_hash="f" * 64,
        feature_schema_hash="1" * 64,
        attempt_content_hash="2" * 64,
        scorer_state="abstain",
        calibrated=False,
        canonical_artifact="MODEL_PAYLOAD_SECRET",
        artifact={"weights": [123]},
        samples=[{"features": [456], "label_value": 1}],
    )
    snapshot = SimpleNamespace(**(asdict(_job()) | {"artifact_result": result}))
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )

    class _Facade:
        async def list_jobs(self) -> tuple[object, ...]:
            return (snapshot,)

        async def list_eligible_folds(self) -> tuple[()]:
            return ()

    class _Service:
        def for_principal(self, value: Principal) -> _Facade:
            assert value.workspace_id == workspace_id
            return _Facade()

    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    app.state.learning_training_admin_service = _Service()
    response = TestClient(app).get("/api/v1/learning-training/jobs")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["jobs"][0]
    assert payload.get("artifact_result") == {
        "artifact_id": str(artifact_id),
        "artifact_hash": "a" * 64,
        "artifact_schema_version": "cpu-artifact-v1",
        "trainer_version": "cpu-trainer-v1",
        "approval_hash": "e" * 64,
        "batch_hash": "f" * 64,
        "feature_schema_hash": "1" * 64,
        "attempt_content_hash": "2" * 64,
        "scorer_state": "abstain",
        "calibrated": False,
    }
    assert all(
        term not in response.text
        for term in (
            "canonical_artifact",
            "MODEL_PAYLOAD_SECRET",
            "weights",
            'samples": [',
            "features",
            "label_value",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("single_job", [False, True])
async def test_training_query_selects_only_scoped_succeeded_artifact_metadata(
    single_job: bool,
) -> None:
    class _Result:
        def all(self) -> list[object]:
            return []

        def one_or_none(self) -> None:
            return None

    class _Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object, *_args: object, **_kwargs: object) -> _Result:
            self.statements.append(statement)
            return _Result()

    workspace_id = uuid4()
    session = _Session()
    repository = LearningTrainingAdminRepository(session)  # type: ignore[arg-type]
    if single_job:
        assert await repository.get_job_snapshot(workspace_id=workspace_id, job_id=uuid4()) is None
    else:
        assert await repository.list_jobs(workspace_id=workspace_id) == ()
    query = session.statements[-1].compile(dialect=postgresql.dialect())
    sql = str(query)
    assert "learning_training_artifact.id" in sql
    assert "learning_training_artifact.artifact_hash" in sql
    assert "learning_training_artifact.workspace_id = learning_training_job.workspace_id" in sql
    assert "learning_training_artifact.attempt_id = learning_training_job.attempt_id" in sql
    assert "learning_training_job.status =" in sql
    assert "succeeded" in query.params.values()
    assert workspace_id in query.params.values()
    assert all(
        term not in sql
        for term in (
            "learning_training_artifact.canonical_artifact",
            "learning_training_artifact.artifact,",
            "learning_normalized_sample",
            "learning_fold_assignment",
        )
    )
