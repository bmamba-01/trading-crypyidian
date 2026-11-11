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
from trading_bot.application.bot_dispatcher import (
    BotDecision,
    BotDecisionEvaluation,
    BotDecisionJob,
    BotDecisionStatus,
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
        resolved = await PostgresScorerRuntimeTargetRepository(session).resolve_active(
            workspace_id=job.workspace_id
        )
        if resolved is None:
            return None
        if self._policy_provider is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway chưa nhận policy runtime đã cấu hình."
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
        if evaluation.snapshot is None or evaluation.vector is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer candidate thiếu DecisionSnapshot hoặc vector strategy canonical."
            )
        if (
            evaluation.candidate_id is None
            or evaluation.candidate_id != incumbent.candidate_id
            or evaluation.snapshot.snapshot_id != incumbent.decision_snapshot_id
            or evaluation.vector.snapshot_id != evaluation.snapshot.snapshot_id
            or evaluation.vector.workspace_id != job.workspace_id
            or evaluation.vector.vector_hash == ""
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Scorer candidate không cùng snapshot/vector với BotDecision strategy."
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
                source_rights_hash=resolved.source_rights_fingerprint,
                is_out_of_distribution=is_out_of_distribution,
            ),
            base_artifact=resolved.base_artifact,
            scorer_artifact=resolved.scorer_artifact,
            policy=policy,
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


def _decision_payload(values: dict[str, object]) -> dict[str, object]:
    from trading_bot.application.bot_dispatcher import _decision_payload as canonical_payload

    return canonical_payload(values)


__all__ = ["PostgresScorerRuntimeCommitter"]
