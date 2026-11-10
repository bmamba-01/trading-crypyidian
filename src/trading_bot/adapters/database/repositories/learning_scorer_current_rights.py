"""Ranh giới metadata-only cho việc tái kiểm quyền scorer hiện hành."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACCEPTED = frozenset({"ACCEPTED_SHADOW_ONLY", "ACCEPTED_PAPER_GATE_ELIGIBLE"})


@dataclass(frozen=True, slots=True)
class ScorerDeploymentRightsEvidence:
    """Kết quả kiểm quyền và lineage, không bao gồm membership hoặc source payload."""

    workspace_id: UUID
    scorer_artifact_id: UUID
    evaluation_report_id: UUID
    shadow_eligible: bool
    paper_gate_eligible: bool
    evaluation_status: str
    candidate_completeness_verified: bool
    scored_candidate_coverage_verified: bool
    fingerprint: str
    checked_at: datetime
    artifact_hash: str
    report_hash: str
    proof_hash: str
    policy_hash: str
    bundle_hash: str
    source_scope_hash: str
    training_membership_digest: str
    validation_membership_digest: str

    def __post_init__(self) -> None:
        for name in ("workspace_id", "scorer_artifact_id", "evaluation_report_id"):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} phải là UUID authoritative.")
        if type(self.shadow_eligible) is not bool or type(self.paper_gate_eligible) is not bool:
            raise ValueError("Cờ eligibility phải là bool do database trả về.")
        if type(self.candidate_completeness_verified) is not bool:
            raise ValueError("candidate_completeness_verified phải là bool authoritative.")
        if type(self.scored_candidate_coverage_verified) is not bool:
            raise ValueError("scored_candidate_coverage_verified phải là bool authoritative.")
        if self.evaluation_status not in {*_ACCEPTED, "INCONCLUSIVE"}:
            raise ValueError("evaluation_status scorer không được hỗ trợ.")
        if self.evaluation_status == "INCONCLUSIVE" and (
            self.shadow_eligible or self.paper_gate_eligible
        ):
            raise ValueError("INCONCLUSIVE không thể eligible cho deployment scorer.")
        if self.evaluation_status in _ACCEPTED and not self.shadow_eligible:
            raise ValueError("Report ACCEPTED phải eligible cho SHADOW sau khi kiểm rights.")
        expected_paper_gate = (
            self.evaluation_status == "ACCEPTED_PAPER_GATE_ELIGIBLE"
            and self.candidate_completeness_verified
            and self.scored_candidate_coverage_verified
        )
        if self.paper_gate_eligible is not expected_paper_gate:
            raise ValueError("PAPER_GATE cần report ACCEPTED, completeness và coverage.")
        if not isinstance(self.checked_at, datetime) or self.checked_at.tzinfo is None:
            raise ValueError("checked_at phải là timestamp database có múi giờ.")
        if self.checked_at.utcoffset() is None:
            raise ValueError("checked_at phải có UTC offset.")
        for name in (
            "fingerprint",
            "artifact_hash",
            "report_hash",
            "proof_hash",
            "policy_hash",
            "bundle_hash",
            "source_scope_hash",
            "training_membership_digest",
            "validation_membership_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")


class LearningScorerCurrentRightsRepository:
    """Gọi gateway PostgreSQL; ứng dụng không đọc assignment/source payload trực tiếp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def verify_deployment_rights(
        self,
        *,
        workspace_id: UUID,
        scorer_artifact_id: UUID,
        evaluation_report_id: UUID,
    ) -> ScorerDeploymentRightsEvidence:
        """Kiểm report và rights hiện hành trong transaction READ COMMITTED caller."""
        if any(
            type(value) is not UUID
            for value in (workspace_id, scorer_artifact_id, evaluation_report_id)
        ):
            raise ValueError("Workspace, artifact và report phải là UUID authoritative.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        isolation = await self._session.scalar(text("SHOW transaction_isolation"))
        if str(isolation).lower() != "read committed":
            raise RuntimeError("Scorer rights recheck cần transaction READ COMMITTED.")
        result = await self._session.execute(
            text(
                "SELECT * FROM public.wp06_verify_scorer_deployment_rights("
                "target_workspace => CAST(:workspace_id AS uuid), "
                "target_scorer_artifact_id => CAST(:scorer_artifact_id AS uuid), "
                "target_evaluation_report_id => CAST(:evaluation_report_id AS uuid))"
            ),
            {
                "workspace_id": str(workspace_id),
                "scorer_artifact_id": str(scorer_artifact_id),
                "evaluation_report_id": str(evaluation_report_id),
            },
        )
        row = result.mappings().one()
        evidence = ScorerDeploymentRightsEvidence(**dict(row))
        if (
            evidence.workspace_id != workspace_id
            or evidence.scorer_artifact_id != scorer_artifact_id
            or evidence.evaluation_report_id != evaluation_report_id
        ):
            raise ValueError("Scorer rights gateway trả scope khác request.")
        return evidence


__all__ = [
    "LearningScorerCurrentRightsRepository",
    "ScorerDeploymentRightsEvidence",
]
