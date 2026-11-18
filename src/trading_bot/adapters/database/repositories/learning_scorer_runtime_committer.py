"""Resolve scorer C4 và ghim score event cùng BotDecision trong một giao dịch."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import fields

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.bot_decisions import BotDecisionScoringCommit
from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
    PostgresScorerRuntimeTargetRepository,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.adapters.database.repositories.scorer_runtime_source_rights import (
    ScorerDecisionSourceLineageUnavailable,
    ScorerDecisionSourceRightsDenied,
    authorize_job_source_scope,
    combine_source_rights_fingerprints,
    persist_decision_feature_snapshots,
    preflight_job_source_rights,
    stage_job_source_bars,
)
from trading_bot.application.bot_dispatcher import (
    BotDecision,
    BotDecisionEvaluation,
    BotDecisionJob,
    BotDecisionStatus,
)
from trading_bot.application.learning_feature_vector import (
    LearningFeatureVectorBuildError,
    build_learning_feature_vector,
)
from trading_bot.application.learning_scorer_runtime import (
    LearningScorerRuntimeRequest,
    LearningScorerRuntimeResult,
    RuntimeScorerDecision,
    feature_vector_is_out_of_distribution,
    score_runtime_candidate_with_deadline,
)
from trading_bot.application.scorer_runtime_policy import ScorerRuntimePolicyProvider
from trading_bot.domain.learning.score_event import (
    LearningScoreDecision,
    LearningScoreEvent,
)
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode


class PostgresScorerRuntimeCommitter:
    """Ghim target/runtime score chính xác hoặc từ chối toàn bộ decision commit."""

    def __init__(self, policy_provider: ScorerRuntimePolicyProvider | None = None) -> None:
        self._policy_provider = policy_provider

    async def __call__(
        self,
        session: AsyncSession,
        job: BotDecisionJob,
        evaluation: BotDecisionEvaluation,
        incumbent: BotDecision,
    ) -> BotDecisionScoringCommit | None:
        target_repository = PostgresScorerRuntimeTargetRepository(session)
        metadata = await target_repository.resolve_active_metadata(workspace_id=job.workspace_id)
        if metadata is None:
            return None
        if self._policy_provider is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway chưa nhận policy runtime đã cấu hình."
            )
        if evaluation.snapshot is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer candidate thiếu DecisionSnapshot strategy canonical."
            )
        if evaluation.candidate_id is None or evaluation.candidate_id != incumbent.candidate_id:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer candidate không khớp BotDecision strategy."
            )
        if evaluation.snapshot.snapshot_id != incumbent.decision_snapshot_id:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer snapshot không khớp BotDecision strategy."
            )
        if evaluation.vector is None:
            vector_hash = hashlib.sha256(b"SCORER_VECTOR_MISSING").hexdigest()
            try:
                async with session.begin_nested() as savepoint:
                    await stage_job_source_bars(
                        session,
                        job=job,
                        snapshot=evaluation.snapshot,
                    )
                    await savepoint.rollback()
            except ScorerDecisionSourceLineageUnavailable:
                return _lineage_abstain_commit(
                    incumbent,
                    vector_hash=vector_hash,
                    decision_snapshot_id=evaluation.snapshot.snapshot_id,
                )
            return _vector_missing_commit(
                incumbent,
                vector_hash=vector_hash,
                decision_snapshot_id=evaluation.snapshot.snapshot_id,
            )
        if (
            evaluation.vector.snapshot_id != evaluation.snapshot.snapshot_id
            or evaluation.vector.workspace_id != job.workspace_id
            or evaluation.vector.vector_hash == ""
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Scorer candidate không cùng snapshot/vector với BotDecision strategy."
            )

        try:
            async with session.begin_nested():
                await stage_job_source_bars(
                    session,
                    job=job,
                    snapshot=evaluation.snapshot,
                )
                await preflight_job_source_rights(
                    session,
                    workspace_id=job.workspace_id,
                    job_id=job.job_id,
                )
                await persist_decision_feature_snapshots(
                    session,
                    job=job,
                    snapshot=evaluation.snapshot,
                )
                decision_rights_fingerprint = await authorize_job_source_scope(
                    session,
                    workspace_id=job.workspace_id,
                    job_id=job.job_id,
                )
        except ScorerDecisionSourceLineageUnavailable:
            # Thiếu identity không được đưa tới scorer CPU. Chỉ lưu quyết định ABSTAIN metadata.
            return _lineage_abstain_commit(
                incumbent,
                vector_hash=evaluation.vector.vector_hash,
                decision_snapshot_id=evaluation.snapshot.snapshot_id,
            )
        except ScorerDecisionSourceRightsDenied as error:
            raise ScorerDeploymentRightsUnavailable(
                "Current PAPER source-use rights bị từ chối đúng scope MarketBar."
            ) from error

        try:
            expected_vector = build_learning_feature_vector(
                evaluation.snapshot,
                strategy_release_id=incumbent.strategy_release_id,
                strategy_release_content_hash=incumbent.strategy_release_hash or "",
            )
        except LearningFeatureVectorBuildError as error:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer vector không thể dựng lại từ snapshot strategy canonical."
            ) from error
        if evaluation.vector != expected_vector:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer vector không khớp tái dựng canonical từ DecisionSnapshot strategy."
            )

        resolved = await target_repository.resolve_active_for_job(
            workspace_id=job.workspace_id,
            job_id=job.job_id,
        )
        if resolved is None:
            return None
        if (
            resolved.mode != metadata.mode
            or resolved.deployment_event_id != metadata.deployment_event_id
            or resolved.deployment_revision != metadata.deployment_revision
            or resolved.target.active_pointer_version != metadata.active_pointer_version
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Scorer deployment đổi giữa metadata preflight và artifact resolution."
            )
        policy = self._policy_provider.get_policy(
            workspace_id=job.workspace_id,
            policy_id=resolved.target.policy_id,
            expected_policy_hash=resolved.target.policy_hash,
        )
        if (
            policy is None
            or policy.policy_version != resolved.target.policy_id
            or policy.policy_hash != resolved.target.policy_hash
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway không có đúng policy version/hash đã ghim trong report."
            )

        source_rights_fingerprint = combine_source_rights_fingerprints(
            deployment_fingerprint=resolved.source_rights_fingerprint,
            decision_fingerprint=decision_rights_fingerprint,
        )

        # Ngưỡng OOD thuộc policy hash; schema/statistics thiếu hoặc sai luôn đóng.
        is_out_of_distribution = feature_vector_is_out_of_distribution(
            evaluation.vector,
            resolved.base_artifact,
            policy,
        )
        started = time.perf_counter()
        result = await score_runtime_candidate_with_deadline(
            request=LearningScorerRuntimeRequest(
                candidate_id=evaluation.candidate_id,
                vector=evaluation.vector,
                source_rights_eligible=True,
                source_rights_hash=source_rights_fingerprint,
                is_out_of_distribution=is_out_of_distribution,
            ),
            base_artifact=resolved.base_artifact,
            scorer_artifact=resolved.scorer_artifact,
            policy=policy,
        )
        try:
            rechecked_fingerprint = await authorize_job_source_scope(
                session,
                workspace_id=job.workspace_id,
                job_id=job.job_id,
            )
        except ScorerDecisionSourceLineageUnavailable as error:
            raise ScorerDeploymentRightsUnavailable(
                "Source lineage candidate biến mất trước khi append decision."
            ) from error
        except ScorerDecisionSourceRightsDenied as error:
            raise ScorerDeploymentRightsUnavailable(
                "Current PAPER rights bị thu hồi trước khi append decision."
            ) from error
        if rechecked_fingerprint != decision_rights_fingerprint:
            raise ScorerDeploymentRightsUnavailable(
                "Source rights fingerprint đổi trong scorer transaction."
            )
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        final_decision = _final_decision(incumbent, resolved.mode, result)
        vector = evaluation.vector
        timeframes = tuple(
            (item.timeframe, item.feature_snapshot_id) for item in vector.timeframe_lineage
        )
        source_digests = tuple(
            (item.timeframe, item.source_event_digest) for item in vector.timeframe_lineage
        )
        candidate_json = incumbent.candidate_json
        if candidate_json is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer candidate không có payload strategy canonical."
            )
        target = resolved.target
        if target.proof_id is None or target.proof_hash is None or target.proof_scope_hash is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway không trả proof TEST authoritative đầy đủ."
            )
        score_event = LearningScoreEvent.create(
            workspace_id=job.workspace_id,
            bot_id=job.bot_id,
            bot_decision_id=incumbent.decision_id,
            bot_decision_hash=final_decision.decision_hash,
            incumbent_decision_hash=incumbent.decision_hash,
            incumbent_status=incumbent.status.value,
            final_status=final_decision.status.value,
            candidate_id=evaluation.candidate_id,
            candidate_hash=hashlib.sha256(candidate_json.encode("utf-8")).hexdigest(),
            decision_snapshot_id=evaluation.snapshot.snapshot_id,
            vector_hash=vector.vector_hash,
            feature_schema_version=vector.schema_version,
            feature_schema_hash=vector.feature_schema_hash,
            timeframe_snapshot_ids=timeframes,
            source_event_digests=source_digests,
            source_rights_fingerprint=resolved.source_rights_fingerprint,
            decision_source_rights_fingerprint=decision_rights_fingerprint,
            mode=resolved.mode,
            deployment_event_id=resolved.deployment_event_id,
            deployment_revision=resolved.deployment_revision,
            registry_id=target.registry_id,
            registry_revision=target.registry_revision,
            active_pointer_version=target.active_pointer_version,
            scorer_artifact_id=target.scorer_artifact_id,
            scorer_artifact_hash=target.scorer_artifact_hash,
            base_artifact_hash=resolved.base_artifact_hash,
            evaluation_report_id=target.evaluation_report_id,
            evaluation_report_hash=target.evaluation_report_hash,
            evaluation_status=target.evaluation_status,
            report_scope_hash=target.report_scope_hash,
            proof_id=target.proof_id,
            proof_hash=target.proof_hash,
            proof_scope_hash=target.proof_scope_hash,
            candidate_completeness_verified=target.candidate_completeness_verified is True,
            policy_id=target.policy_id,
            policy_hash=target.policy_hash,
            decision=LearningScoreDecision(result.decision.value),
            reason_code=result.reason_code,
            class_probabilities=result.class_probabilities,
            probability=result.probability,
            confidence=result.confidence,
            uncertainty=result.uncertainty,
            model_latency_ms=latency_ms,
        )
        return BotDecisionScoringCommit(
            decision=final_decision,
            score_event=score_event,
            incumbent_decision=incumbent,
            vector_hash=vector.vector_hash,
            decision_snapshot_id=evaluation.snapshot.snapshot_id,
        )


def _final_decision(
    incumbent: BotDecision,
    mode: ScorerDeploymentMode,
    result: LearningScorerRuntimeResult,
) -> BotDecision:
    if mode is ScorerDeploymentMode.SHADOW or result.decision is RuntimeScorerDecision.APPROVE:
        return incumbent
    if mode is not ScorerDeploymentMode.PAPER_GATE:
        raise ScorerDeploymentRightsUnavailable("Scorer mode runtime không hỗ trợ.")
    if result.decision is RuntimeScorerDecision.VETO:
        status = BotDecisionStatus.VETO
        reason_code = "SCORER_VETO"
        detail_vi = "Scorer PAPER_GATE đã chặn candidate theo policy đã duyệt."
    else:
        status = BotDecisionStatus.ABSTAIN
        reason_code = "SCORER_ABSTAIN"
        detail_vi = "Scorer PAPER_GATE không đủ điều kiện để cho phép candidate."
    values = {field.name: getattr(incumbent, field.name) for field in fields(BotDecision)}
    values.pop("decision_hash")
    values["status"] = status
    values["reason_code"] = reason_code
    values["detail_vi"] = detail_vi
    values["candidate_json"] = None
    gate_code = result.reason_code or result.decision.value
    values["gate_details"] = (
        *incumbent.gate_details,
        f"SCORER_{result.decision.value}:{gate_code}",
    )
    canonical = json.dumps(
        _decision_payload(values),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    values["decision_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return BotDecision(**values)


def _lineage_abstain_commit(
    incumbent: BotDecision,
    *,
    vector_hash: str,
    decision_snapshot_id: str,
) -> BotDecisionScoringCommit:
    """Tạo ABSTAIN không giữ candidate hay score khi lineage nguồn chưa đủ."""
    values = {field.name: getattr(incumbent, field.name) for field in fields(BotDecision)}
    values.pop("decision_hash")
    values["status"] = BotDecisionStatus.ABSTAIN
    values["reason_code"] = "SCORER_SOURCE_LINEAGE_MISSING"
    values["detail_vi"] = "Không dùng scorer vì thiếu lineage nến nguồn đã xác minh."
    values["candidate_json"] = None
    values["gate_details"] = tuple(
        dict.fromkeys((*incumbent.gate_details, "SCORER_SOURCE_LINEAGE_MISSING"))
    )
    canonical = json.dumps(
        _decision_payload(values),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    values["decision_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    final_decision = BotDecision(**values)
    return BotDecisionScoringCommit(
        decision=final_decision,
        score_event=None,
        incumbent_decision=incumbent,
        vector_hash=vector_hash,
        decision_snapshot_id=decision_snapshot_id,
    )


def _vector_missing_commit(
    incumbent: BotDecision,
    *,
    vector_hash: str,
    decision_snapshot_id: str,
) -> BotDecisionScoringCommit:
    """Tạo ABSTAIN riêng khi lineage hợp lệ nhưng vector canonical không có."""
    values = {field.name: getattr(incumbent, field.name) for field in fields(BotDecision)}
    values.pop("decision_hash")
    values["status"] = BotDecisionStatus.ABSTAIN
    values["reason_code"] = "SCORER_VECTOR_MISSING"
    values["detail_vi"] = "Không dùng scorer vì thiếu vector canonical đã xác minh."
    values["candidate_json"] = None
    values["gate_details"] = tuple(
        dict.fromkeys((*incumbent.gate_details, "SCORER_VECTOR_MISSING"))
    )
    canonical = json.dumps(
        _decision_payload(values),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    values["decision_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    final_decision = BotDecision(**values)
    return BotDecisionScoringCommit(
        decision=final_decision,
        score_event=None,
        incumbent_decision=incumbent,
        vector_hash=vector_hash,
        decision_snapshot_id=decision_snapshot_id,
    )


def _decision_payload(values: dict[str, object]) -> dict[str, object]:
    from trading_bot.application.bot_dispatcher import _decision_payload as canonical_payload

    return canonical_payload(values)


__all__ = ["PostgresScorerRuntimeCommitter"]
