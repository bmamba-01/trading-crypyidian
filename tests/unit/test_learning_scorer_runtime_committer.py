from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from tests.unit.test_signal_decision_repository import _decision, _job

from trading_bot.adapters.database.repositories import learning_scorer_runtime_committer as module
from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
    _final_decision,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.adapters.database.repositories.scorer_runtime_source_rights import (
    ScorerDecisionSourceLineageUnavailable,
    ScorerDecisionSourceRightsDenied,
)
from trading_bot.application.bot_dispatcher import BotDecisionStatus
from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
)

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _NestedTransaction:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def rollback(self) -> None:
        self.rolled_back = True


class _Session:
    def __init__(self) -> None:
        self.savepoints: list[_NestedTransaction] = []

    def begin_nested(self):
        savepoint = _NestedTransaction()
        self.savepoints.append(savepoint)
        return savepoint


async def test_disabled_deployment_is_inert_when_authoritative_resolver_returns_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            assert workspace_id == WORKSPACE
            return None

        async def resolve_active_for_job(self, *, workspace_id, job_id):
            assert workspace_id == WORKSPACE
            assert type(job_id) is UUID
            return None

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)

    result = await PostgresScorerRuntimeCommitter()(
        SimpleNamespace(),
        SimpleNamespace(workspace_id=WORKSPACE),
        SimpleNamespace(snapshot=None, vector=None),
        SimpleNamespace(),
    )

    assert result is None


async def test_active_target_without_configured_policy_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            assert workspace_id == WORKSPACE
            return SimpleNamespace(
                workspace_id=WORKSPACE,
                mode=ScorerDeploymentMode.SHADOW,
                deployment_event_id=UUID("44444444-4444-4444-8444-444444444444"),
                deployment_revision=1,
                active_pointer_version=1,
            )

        async def resolve_active_for_job(self, *, workspace_id, job_id):
            assert workspace_id == WORKSPACE
            assert type(job_id) is UUID
            return object()

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="policy runtime"):
        await PostgresScorerRuntimeCommitter()(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )


@pytest.mark.parametrize(
    ("mode", "scorer_decision", "expected_status"),
    [
        (ScorerDeploymentMode.PAPER_GATE, RuntimeScorerDecision.VETO, BotDecisionStatus.VETO),
        (
            ScorerDeploymentMode.PAPER_GATE,
            RuntimeScorerDecision.ABSTAIN,
            BotDecisionStatus.ABSTAIN,
        ),
    ],
)
async def test_paper_gate_maps_veto_and_abstain_without_changing_decision_identity(
    mode: ScorerDeploymentMode,
    scorer_decision: RuntimeScorerDecision,
    expected_status: BotDecisionStatus,
) -> None:
    incumbent = await _decision(_job())
    result = SimpleNamespace(decision=scorer_decision, reason_code="OUT_OF_DISTRIBUTION")

    final = _final_decision(incumbent, mode, result)  # type: ignore[arg-type]

    assert final.decision_id == incumbent.decision_id
    assert final.idempotency_key == incumbent.idempotency_key
    assert final.status is expected_status
    assert final.candidate_json is None
    assert final.decision_snapshot_id == incumbent.decision_snapshot_id
    assert final.decision_hash != incumbent.decision_hash
    assert f"SCORER_{scorer_decision.value}:OUT_OF_DISTRIBUTION" in final.gate_details


async def test_shadow_veto_observation_never_changes_strategy_decision() -> None:
    incumbent = await _decision(_job())
    result = SimpleNamespace(decision=RuntimeScorerDecision.VETO, reason_code="LOW_CONFIDENCE")

    final = _final_decision(
        incumbent,
        ScorerDeploymentMode.SHADOW,
        result,  # type: ignore[arg-type]
    )

    assert final is incumbent
    assert final.status is BotDecisionStatus.CANDIDATE
    assert final.decision_hash == incumbent.decision_hash


async def test_paper_gate_approve_keeps_strategy_candidate() -> None:
    incumbent = await _decision(_job())
    result = SimpleNamespace(decision=RuntimeScorerDecision.APPROVE, reason_code=None)

    final = _final_decision(
        incumbent,
        ScorerDeploymentMode.PAPER_GATE,
        result,  # type: ignore[arg-type]
    )

    assert final is incumbent


async def test_missing_exact_lineage_returns_metadata_abstain_before_scorer_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    incumbent = await _decision(job)
    snapshot = SimpleNamespace(snapshot_id=incumbent.decision_snapshot_id)
    vector = SimpleNamespace(
        snapshot_id=incumbent.decision_snapshot_id,
        vector_hash="f" * 64,
        workspace_id=job.workspace_id,
        timeframe_lineage=(),
    )
    evaluation = SimpleNamespace(
        snapshot=snapshot,
        vector=vector,
        candidate_id=incumbent.candidate_id,
    )
    scorer_called = False
    gate_called = False

    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            assert workspace_id == job.workspace_id
            return SimpleNamespace(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.SHADOW,
                deployment_event_id=UUID("44444444-4444-4444-8444-444444444444"),
                deployment_revision=1,
                active_pointer_version=1,
            )

        async def resolve_active_for_job(self, *, workspace_id, job_id):
            raise AssertionError("Artifact resolver chạy trước khi lineage được xác minh.")

    class PolicyProvider:
        def get_policy(self, **_kwargs):
            return SimpleNamespace(policy_version="paper-v1", policy_hash="a" * 64)

    async def missing_lineage(*_args, **_kwargs):
        raise ScorerDecisionSourceLineageUnavailable("missing")

    async def unexpected_gate(*_args, **_kwargs):
        nonlocal gate_called
        gate_called = True
        return "c" * 64

    async def unexpected_persist(*_args, **_kwargs):
        raise AssertionError("Feature snapshot không được ghi khi source lineage thiếu.")

    async def unexpected_score(*_args, **_kwargs):
        nonlocal scorer_called
        scorer_called = True
        raise AssertionError("Scorer không được gọi khi lineage thiếu.")

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)
    monkeypatch.setattr(module, "stage_job_source_bars", missing_lineage)
    monkeypatch.setattr(module, "preflight_job_source_rights", unexpected_gate)
    monkeypatch.setattr(module, "persist_decision_feature_snapshots", unexpected_persist)
    monkeypatch.setattr(module, "authorize_job_source_scope", unexpected_gate)
    monkeypatch.setattr(module, "score_runtime_candidate_with_deadline", unexpected_score)

    commit = await PostgresScorerRuntimeCommitter(PolicyProvider())(
        _Session(),  # type: ignore[arg-type]
        job,
        evaluation,  # type: ignore[arg-type]
        incumbent,
    )

    assert commit is not None
    assert commit.score_event is None
    assert commit.decision.status is BotDecisionStatus.ABSTAIN
    assert commit.decision.reason_code == "SCORER_SOURCE_LINEAGE_MISSING"
    assert commit.decision.candidate_json is None
    assert gate_called is False
    assert scorer_called is False


async def test_missing_vector_validates_and_rolls_back_staging_without_loading_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    incumbent = await _decision(job)
    snapshot = SimpleNamespace(snapshot_id=incumbent.decision_snapshot_id)
    evaluation = SimpleNamespace(
        snapshot=snapshot,
        vector=None,
        candidate_id=incumbent.candidate_id,
    )
    staged_calls: list[object] = []
    artifact_called = False
    gate_called = False

    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            return SimpleNamespace(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.SHADOW,
                deployment_event_id=UUID("44444444-4444-4444-8444-444444444444"),
                deployment_revision=1,
                active_pointer_version=1,
            )

        async def resolve_active_for_job(self, **_kwargs):
            nonlocal artifact_called
            artifact_called = True
            raise AssertionError("Không được đọc model khi vector vắng mặt.")

    async def stage(_session, *, job, snapshot):
        staged_calls.append((job, snapshot))

    async def unexpected_gate(*_args, **_kwargs):
        nonlocal gate_called
        gate_called = True
        raise AssertionError("Không chạy rights/inference gate khi không có vector.")

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)
    monkeypatch.setattr(module, "stage_job_source_bars", stage)
    monkeypatch.setattr(module, "preflight_job_source_rights", unexpected_gate)
    monkeypatch.setattr(module, "authorize_job_source_scope", unexpected_gate)
    session = _Session()

    commit = await PostgresScorerRuntimeCommitter(object())(
        session,  # type: ignore[arg-type]
        job,
        evaluation,  # type: ignore[arg-type]
        incumbent,
    )

    assert commit is not None
    assert commit.score_event is None
    assert commit.decision.status is BotDecisionStatus.ABSTAIN
    assert commit.decision.reason_code == "SCORER_VECTOR_MISSING"
    assert commit.decision.candidate_json is None
    assert staged_calls == [(job, snapshot)]
    assert len(session.savepoints) == 1
    assert session.savepoints[0].rolled_back
    assert artifact_called is False
    assert gate_called is False


async def test_noncanonical_feature_snapshot_abstains_before_artifact_or_scorer_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    incumbent = await _decision(job)
    snapshot = SimpleNamespace(snapshot_id=incumbent.decision_snapshot_id)
    vector = SimpleNamespace(
        snapshot_id=incumbent.decision_snapshot_id,
        vector_hash="f" * 64,
        workspace_id=job.workspace_id,
    )
    evaluation = SimpleNamespace(
        snapshot=snapshot,
        vector=vector,
        candidate_id=incumbent.candidate_id,
    )
    artifact_called = False
    score_called = False
    feature_writes: list[object] = []

    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            return SimpleNamespace(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.SHADOW,
                deployment_event_id=UUID("44444444-4444-4444-8444-444444444444"),
                deployment_revision=1,
                active_pointer_version=1,
            )

        async def resolve_active_for_job(self, *, workspace_id, job_id):
            nonlocal artifact_called
            artifact_called = True
            raise AssertionError("Artifact không được nạp trước khi snapshot canonical hợp lệ.")

    async def staged(*_args, **_kwargs):
        return ()

    async def preflight(*_args, **_kwargs):
        return "c" * 64

    async def reject_noncanonical(*_args, **_kwargs):
        raise ScorerDecisionSourceLineageUnavailable("snapshot canonical mismatch")

    async def unexpected_authorize(*_args, **_kwargs):
        raise AssertionError("Authorization receipt không được tạo cho snapshot sai.")

    async def unexpected_score(*_args, **_kwargs):
        nonlocal score_called
        score_called = True
        raise AssertionError("Scorer không được gọi cho snapshot canonical sai.")

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)
    monkeypatch.setattr(module, "stage_job_source_bars", staged)
    monkeypatch.setattr(module, "preflight_job_source_rights", preflight)
    monkeypatch.setattr(module, "persist_decision_feature_snapshots", reject_noncanonical)
    monkeypatch.setattr(module, "authorize_job_source_scope", unexpected_authorize)
    monkeypatch.setattr(module, "score_runtime_candidate_with_deadline", unexpected_score)

    commit = await PostgresScorerRuntimeCommitter(object())(
        _Session(),  # type: ignore[arg-type]
        job,
        evaluation,  # type: ignore[arg-type]
        incumbent,
    )

    assert commit is not None
    assert commit.score_event is None
    assert commit.decision.status is BotDecisionStatus.ABSTAIN
    assert commit.decision.candidate_json is None
    assert feature_writes == []
    assert artifact_called is False
    assert score_called is False


async def test_current_paper_rights_denial_aborts_before_scorer_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    incumbent = await _decision(job)
    snapshot = SimpleNamespace(snapshot_id=incumbent.decision_snapshot_id)
    vector = SimpleNamespace(
        snapshot_id=incumbent.decision_snapshot_id,
        vector_hash="f" * 64,
        workspace_id=job.workspace_id,
    )
    evaluation = SimpleNamespace(
        snapshot=snapshot,
        vector=vector,
        candidate_id=incumbent.candidate_id,
    )
    target = SimpleNamespace(policy_id="paper-v1", policy_hash="a" * 64)
    resolved = SimpleNamespace(
        target=target,
        source_rights_fingerprint="b" * 64,
        mode=ScorerDeploymentMode.SHADOW,
    )
    scorer_called = False

    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            assert workspace_id == job.workspace_id
            return SimpleNamespace(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.SHADOW,
                deployment_event_id=UUID("44444444-4444-4444-8444-444444444444"),
                deployment_revision=1,
                active_pointer_version=1,
            )

        async def resolve_active_for_job(self, *, workspace_id, job_id):
            assert workspace_id == job.workspace_id
            assert job_id == job.job_id
            return resolved

    class PolicyProvider:
        def get_policy(self, **_kwargs):
            return SimpleNamespace(policy_version="paper-v1", policy_hash="a" * 64)

    async def staged(*_args, **_kwargs):
        return ()

    async def denied(*_args, **_kwargs):
        raise ScorerDecisionSourceRightsDenied()

    async def unexpected_persist(*_args, **_kwargs):
        raise AssertionError("Feature snapshot không được lưu khi preflight rights denied.")

    async def unexpected_score(*_args, **_kwargs):
        nonlocal scorer_called
        scorer_called = True
        raise AssertionError("Scorer không được gọi khi quyền hiện hành bị từ chối.")

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)
    monkeypatch.setattr(module, "stage_job_source_bars", staged)
    monkeypatch.setattr(module, "preflight_job_source_rights", denied)
    monkeypatch.setattr(module, "persist_decision_feature_snapshots", unexpected_persist)
    monkeypatch.setattr(module, "score_runtime_candidate_with_deadline", unexpected_score)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="Current PAPER"):
        await PostgresScorerRuntimeCommitter(PolicyProvider())(
            _Session(),  # type: ignore[arg-type]
            job,
            evaluation,  # type: ignore[arg-type]
            incumbent,
        )

    assert scorer_called is False


async def test_post_inference_rights_denial_aborts_commit_after_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    incumbent = await _decision(job)
    snapshot = SimpleNamespace(snapshot_id=incumbent.decision_snapshot_id)
    vector = SimpleNamespace(
        snapshot_id=incumbent.decision_snapshot_id,
        vector_hash="f" * 64,
        workspace_id=job.workspace_id,
        timeframe_lineage=(),
    )
    evaluation = SimpleNamespace(
        snapshot=snapshot,
        vector=vector,
        candidate_id=incumbent.candidate_id,
    )
    target = SimpleNamespace(
        policy_id="paper-v1",
        policy_hash="a" * 64,
        proof_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        proof_hash="1" * 64,
        proof_scope_hash="2" * 64,
        registry_id=UUID("11111111-1111-4111-8111-111111111111"),
        registry_revision=1,
        active_pointer_version=1,
        scorer_artifact_id=UUID("22222222-2222-4222-8222-222222222222"),
        scorer_artifact_hash="3" * 64,
        evaluation_report_id=UUID("33333333-3333-4333-8333-333333333333"),
        evaluation_report_hash="4" * 64,
        evaluation_status="ACCEPTED_SHADOW_ONLY",
        candidate_completeness_verified=True,
    )
    resolved = SimpleNamespace(
        target=target,
        source_rights_fingerprint="b" * 64,
        mode=ScorerDeploymentMode.SHADOW,
        base_artifact=object(),
        base_artifact_hash="5" * 64,
        scorer_artifact=object(),
        deployment_event_id=UUID("44444444-4444-4444-8444-444444444444"),
        deployment_revision=1,
    )
    gate_calls = 0
    score_called = False

    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_metadata(self, *, workspace_id):
            assert workspace_id == job.workspace_id
            return SimpleNamespace(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.SHADOW,
                deployment_event_id=resolved.deployment_event_id,
                deployment_revision=resolved.deployment_revision,
                active_pointer_version=target.active_pointer_version,
            )

        async def resolve_active_for_job(self, *, workspace_id, job_id):
            assert workspace_id == job.workspace_id
            assert job_id == job.job_id
            return resolved

    class PolicyProvider:
        def get_policy(self, **_kwargs):
            return SimpleNamespace(policy_version="paper-v1", policy_hash="a" * 64)

    async def staged(*_args, **_kwargs):
        return ()

    async def preflight(*_args, **_kwargs):
        return "c" * 64

    async def persisted(*_args, **_kwargs):
        return None

    async def gate(*_args, **_kwargs):
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 1:
            return "c" * 64
        raise ScorerDecisionSourceRightsDenied("WP06_SCORER_JOB_SOURCE_REVOKED")

    async def scored(*_args, **_kwargs):
        nonlocal score_called
        score_called = True
        return SimpleNamespace(decision=RuntimeScorerDecision.ABSTAIN, reason_code="LOW_CONFIDENCE")

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)
    monkeypatch.setattr(module, "stage_job_source_bars", staged)
    monkeypatch.setattr(module, "preflight_job_source_rights", preflight)
    monkeypatch.setattr(module, "persist_decision_feature_snapshots", persisted)
    monkeypatch.setattr(module, "authorize_job_source_scope", gate)
    monkeypatch.setattr(module, "build_learning_feature_vector", lambda *_args, **_kwargs: vector)
    monkeypatch.setattr(
        module,
        "LearningScorerRuntimeRequest",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(module, "feature_vector_is_out_of_distribution", lambda *_args: False)
    monkeypatch.setattr(module, "score_runtime_candidate_with_deadline", scored)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="thu hồi"):
        await PostgresScorerRuntimeCommitter(PolicyProvider())(
            _Session(),  # type: ignore[arg-type]
            job,
            evaluation,  # type: ignore[arg-type]
            incumbent,
        )

    assert score_called is True
    assert gate_calls == 2
