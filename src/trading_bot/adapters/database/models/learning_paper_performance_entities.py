"""ORM lưu báo cáo chẩn đoán hiệu năng PAPER chỉ gồm số liệu tổng hợp."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

_FORBIDDEN_REPORT_KEYS = frozenset(
    {
        "candidate_id",
        "decision_id",
        "decision_payload",
        "feature_payload",
        "features",
        "labels",
        "probabilities",
        "trade_id",
        "trade_payload",
    }
)


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(set(value) & _FORBIDDEN_REPORT_KEYS) or any(
            _contains_forbidden_key(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


class LearningPaperPerformanceReportRecord(UuidPrimaryKey, CreatedAt, Base):
    """Báo cáo bất biến, một bot/target và hai cohort aggregate-only."""

    __tablename__ = "learning_paper_performance_report"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            ondelete="RESTRICT",
            name="fk_learning_paper_performance_bot",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "baseline_report_id"],
            [
                "learning_scorer_test_evaluation_report.workspace_id",
                "learning_scorer_test_evaluation_report.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_paper_performance_baseline_report",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_paper_performance_workspace_id"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_paper_performance_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "target_fingerprint",
            "policy_hash",
            "as_of",
            "membership_digest",
            name="uq_learning_paper_performance_snapshot",
        ),
        CheckConstraint(
            "status IN ('healthy','degraded','inconclusive')",
            name="learning_paper_performance_status_valid",
        ),
        CheckConstraint(
            "policy_hash ~ '^[0-9a-f]{64}$' "
            "AND target_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND baseline_report_hash ~ '^[0-9a-f]{64}$' "
            "AND membership_digest ~ '^[0-9a-f]{64}$' "
            "AND report_hash ~ '^[0-9a-f]{64}$' "
            "AND idempotency_key IS NOT NULL",
            name="learning_paper_performance_hashes_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(report) = 'object' AND length(canonical_report) > 0 "
            "AND NOT (report ?| ARRAY['candidate_id','decision_id','decision_payload',"
            "'feature_payload','features','labels','probabilities','trade_id','trade_payload'])",
            name="learning_paper_performance_report_aggregate_only",
        ),
        Index(
            "ix_learning_paper_performance_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    baseline_report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    baseline_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_report: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_report(
        cls,
        *,
        report_id: UUID,
        workspace_id: UUID,
        bot_id: UUID,
        baseline_report_id: UUID,
        baseline_report_hash: str,
        policy_version: str,
        policy_hash: str,
        target_fingerprint: str,
        membership_digest: str,
        as_of: datetime,
        status: str,
        idempotency_key: UUID,
        report: dict[str, Any],
        canonical_report: str,
        report_hash: str,
    ) -> LearningPaperPerformanceReportRecord:
        """Tạo bản ghi từ JSON aggregate đã canonical hóa và băm ở application."""
        values = (report_id, workspace_id, bot_id, baseline_report_id, idempotency_key)
        if any(type(value) is not UUID for value in values):
            raise ValueError("Các định danh report phải là UUID authoritative.")
        for name, value in (
            ("baseline_report_hash", baseline_report_hash),
            ("policy_hash", policy_hash),
            ("target_fingerprint", target_fingerprint),
            ("membership_digest", membership_digest),
            ("report_hash", report_hash),
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"{name} phải là SHA-256 lowercase.")
            try:
                int(value, 16)
            except ValueError as error:
                raise ValueError(f"{name} phải là SHA-256 lowercase.") from error
            if value != value.lower():
                raise ValueError(f"{name} phải là SHA-256 lowercase.")
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of phải có timezone.")
        if status not in {"healthy", "degraded", "inconclusive"}:
            raise ValueError("status report không hợp lệ.")
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ValueError("policy_version không được để trống.")
        canonical = json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical != canonical_report:
            raise ValueError("canonical_report không khớp JSON aggregate.")
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != report_hash:
            raise ValueError("report_hash không khớp canonical_report.")
        if _contains_forbidden_key(report):
            raise ValueError("Report không được chứa quyết định, nhãn hoặc payload thô.")
        return cls(
            id=report_id,
            workspace_id=workspace_id,
            bot_id=bot_id,
            baseline_report_id=baseline_report_id,
            baseline_report_hash=baseline_report_hash,
            policy_version=policy_version,
            policy_hash=policy_hash,
            target_fingerprint=target_fingerprint,
            membership_digest=membership_digest,
            as_of=as_of,
            status=status,
            idempotency_key=idempotency_key,
            report_hash=report_hash,
            report=report,
            canonical_report=canonical,
        )
