"""Projection aggregate-only của báo cáo kiểm định scorer đã lưu."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScorerReportSummary:
    """Siêu dữ liệu an toàn để hiển thị báo cáo scorer trong quản trị."""

    created_at: datetime
    evaluated_at: datetime
    status: str
    report_hash: str
    proof_hash: str
    scorer_artifact_hash: str
    base_artifact_hash: str
    policy_hash: str
    policy_version: str
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    vector_schema_version: str
    vector_schema_hash: str
    candidate_completeness_verified: bool
    scored_candidate_coverage_verified: bool
    test_sample_count: int
    opportunity_count: int
    class_support: tuple[tuple[int, int], ...]
    coverage: float
    abstain_count: int
    log_loss: float
    brier_score: float
    expected_calibration_error: float
    false_veto_rate: float
    candidate_retention: float | None
    candidate_count: int
    scored_candidate_count: int
    approved_candidate_count: int
