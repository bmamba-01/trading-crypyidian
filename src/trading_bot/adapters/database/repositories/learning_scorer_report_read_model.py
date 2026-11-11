"""Truy vấn báo cáo scorer mới nhất dưới dạng projection aggregate-only."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import LearningScorerTestEvaluationReportRecord
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_METRIC_KEYS = frozenset(
    {
        "evaluated_at",
        "manifest_hash",
        "fold_plan_hash",
        "fold_id",
        "vector_schema_version",
        "vector_schema_hash",
        "class_support",
        "coverage",
        "abstain_count",
        "log_loss",
        "brier_score",
        "expected_calibration_error",
        "false_veto_rate",
        "candidate_retention",
        "candidate_count",
        "scored_candidate_count",
        "approved_candidate_count",
    }
)


class LearningScorerReportReadRepository:
    """Đọc một báo cáo gần nhất, không tải JSON/canonical payload đầy đủ."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest(self, *, workspace_id: UUID) -> ScorerReportSummary | None:
        if type(workspace_id) is not UUID:
            raise ValueError("Workspace phải là UUID.")

        report = LearningScorerTestEvaluationReportRecord
        report_metadata = func.jsonb_build_object(
            *(value for key in sorted(_METRIC_KEYS) for value in (key, report.report[key]))
        ).label("report_metadata")
        statement = (
            select(
                report.id,
                report.created_at,
                report.report_hash,
                report.proof_hash,
                report.scorer_artifact_hash,
                report.base_artifact_hash,
                report.policy_hash,
                report.policy_version,
                report.evaluation_status,
                report.candidate_completeness_verified,
                report.scored_candidate_coverage_verified,
                report.test_sample_count,
                report.opportunity_count,
                report_metadata,
            )
            .where(report.workspace_id == workspace_id)
            .order_by(report.created_at.desc(), report.id.desc())
            .limit(1)
        )
        result = await self._session.execute(statement)
        row = result.mappings().first()
        if row is None:
            return None
        return _summary_from_row(row)


def _summary_from_row(row: object) -> ScorerReportSummary:
    if not hasattr(row, "__getitem__"):
        raise ValueError("Projection báo cáo scorer không hợp lệ.")
    metadata = row["report_metadata"]
    if type(metadata) is not dict or set(metadata) != _METRIC_KEYS:
        raise ValueError("Projection metadata báo cáo scorer sai schema.")

    status = _strict_text(row["evaluation_status"], "evaluation_status")
    if status not in {
        "INCONCLUSIVE",
        "ACCEPTED_SHADOW_ONLY",
        "ACCEPTED_PAPER_GATE_ELIGIBLE",
    }:
        raise ValueError("Trạng thái báo cáo scorer không hợp lệ.")

    candidate_retention = metadata["candidate_retention"]
    parsed_retention = (
        None
        if candidate_retention is None
        else _strict_bounded_number(candidate_retention, "candidate_retention")
    )
    created_at = row["created_at"]
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("created_at báo cáo scorer phải có múi giờ.")

    return ScorerReportSummary(
        evaluation_report_id=_strict_uuid(row["id"], "report id"),
        created_at=created_at.astimezone(UTC),
        evaluated_at=_strict_datetime(metadata["evaluated_at"], "evaluated_at"),
        status=status,
        report_hash=_strict_hash(row["report_hash"], "report_hash"),
        proof_hash=_strict_hash(row["proof_hash"], "proof_hash"),
        scorer_artifact_hash=_strict_hash(row["scorer_artifact_hash"], "scorer_artifact_hash"),
        base_artifact_hash=_strict_hash(row["base_artifact_hash"], "base_artifact_hash"),
        policy_hash=_strict_hash(row["policy_hash"], "policy_hash"),
        policy_version=_strict_text(row["policy_version"], "policy_version"),
        manifest_hash=_strict_hash(metadata["manifest_hash"], "manifest_hash"),
        fold_plan_hash=_strict_hash(metadata["fold_plan_hash"], "fold_plan_hash"),
        fold_id=_strict_text(metadata["fold_id"], "fold_id"),
        vector_schema_version=_strict_text(
            metadata["vector_schema_version"], "vector_schema_version"
        ),
        vector_schema_hash=_strict_hash(metadata["vector_schema_hash"], "vector_schema_hash"),
        candidate_completeness_verified=_strict_bool(
            row["candidate_completeness_verified"], "candidate_completeness_verified"
        ),
        scored_candidate_coverage_verified=_strict_bool(
            row["scored_candidate_coverage_verified"], "scored_candidate_coverage_verified"
        ),
        test_sample_count=_strict_nonnegative_int(row["test_sample_count"], "test_sample_count"),
        opportunity_count=_strict_nonnegative_int(row["opportunity_count"], "opportunity_count"),
        class_support=_strict_class_support(metadata["class_support"]),
        coverage=_strict_bounded_number(metadata["coverage"], "coverage"),
        abstain_count=_strict_nonnegative_int(metadata["abstain_count"], "abstain_count"),
        log_loss=_strict_nonnegative_number(metadata["log_loss"], "log_loss"),
        brier_score=_strict_nonnegative_number(metadata["brier_score"], "brier_score"),
        expected_calibration_error=_strict_bounded_number(
            metadata["expected_calibration_error"], "expected_calibration_error"
        ),
        false_veto_rate=_strict_bounded_number(metadata["false_veto_rate"], "false_veto_rate"),
        candidate_retention=parsed_retention,
        candidate_count=_strict_nonnegative_int(metadata["candidate_count"], "candidate_count"),
        scored_candidate_count=_strict_nonnegative_int(
            metadata["scored_candidate_count"], "scored_candidate_count"
        ),
        approved_candidate_count=_strict_nonnegative_int(
            metadata["approved_candidate_count"], "approved_candidate_count"
        ),
    )


def _strict_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} phải là chuỗi không rỗng.")
    return value


def _strict_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} phải là SHA-256.")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} phải là bool.")
    return value


def _strict_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} phải là số nguyên không âm.")
    return value


def _strict_number(value: object, name: str) -> float:
    if type(value) is int:
        parsed = float(value)
    elif type(value) is float:
        parsed = value
    else:
        raise ValueError(f"{name} phải là số hữu hạn.")
    if not math.isfinite(parsed):
        raise ValueError(f"{name} phải là số hữu hạn.")
    return parsed


def _strict_bounded_number(value: object, name: str) -> float:
    parsed = _strict_number(value, name)
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{name} phải nằm trong khoảng 0..1.")
    return parsed


def _strict_nonnegative_number(value: object, name: str) -> float:
    parsed = _strict_number(value, name)
    if parsed < 0.0:
        raise ValueError(f"{name} không được âm.")
    return parsed


def _strict_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} phải là thời điểm ISO-8601.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} không phải thời điểm ISO-8601.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return parsed.astimezone(UTC)


def _strict_uuid(value: object, name: str) -> UUID:
    if type(value) is not UUID:
        raise ValueError(f"{name} phải là UUID.")
    return value


def _strict_class_support(value: object) -> tuple[tuple[int, int], ...]:
    if type(value) is not list:
        raise ValueError("class_support phải là danh sách.")
    pairs: list[tuple[int, int]] = []
    for item in value:
        if (
            type(item) is not list
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not int
            or item[1] < 0
        ):
            raise ValueError("class_support chứa phần tử sai schema.")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


__all__ = ["LearningScorerReportReadRepository"]
