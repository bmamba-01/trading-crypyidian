"""Projection aggregate-only của báo cáo kiểm định scorer đã lưu."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ScorerReportSummary:
    """Siêu dữ liệu an toàn để hiển thị báo cáo scorer trong quản trị."""

    evaluation_report_id: UUID
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
    confusion_matrix: tuple[tuple[int, ...], ...] | None
    coverage: float
    abstain_count: int
    log_loss: float
    brier_score: float
    expected_calibration_error: float
    false_veto_count: int
    false_veto_rate: float
    candidate_retention: float | None
    candidate_count: int
    scored_candidate_count: int
    approved_candidate_count: int
    no_candidate_count: int
    input_excluded_count: int
    not_run_count: int | None = None

    @property
    def positive_class_support(self) -> int:
        """Số mẫu có nhãn PnL dương trong báo cáo TEST đã lưu."""
        return dict(self.class_support).get(1, 0)

    @property
    def positive_class_false_positive_count(self) -> int | None:
        """Số mẫu khác lớp dương nhưng bị dự đoán thành lớp PnL dương."""
        if self.confusion_matrix is None:
            return None
        return self.confusion_matrix[0][2] + self.confusion_matrix[1][2]

    @property
    def positive_class_false_negative_count(self) -> int | None:
        """Số mẫu PnL dương bị dự đoán sang lớp khác hoặc bị ABSTAIN."""
        if self.confusion_matrix is None:
            return None
        row = self.confusion_matrix[2]
        return row[0] + row[1] + row[3]
