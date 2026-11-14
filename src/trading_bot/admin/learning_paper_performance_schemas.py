"""Schema HTTP cho chẩn đoán hiệu năng scorer trên dữ liệu PAPER."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PaperPerformanceAssessRequest(BaseModel):
    """Yêu cầu đánh giá một bot thuộc workspace hiện hành."""

    model_config = ConfigDict(extra="forbid")

    bot_id: UUID


class PaperPerformanceReportResponse(BaseModel):
    """Báo cáo PAPER summary-only, không có thành viên hoặc payload quyết định."""

    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    bot_id: UUID
    as_of: datetime
    status: str = Field(pattern="^(healthy|degraded|inconclusive)$")
    policy_version: str
    policy_hash: str = Field(pattern="^[0-9a-f]{64}$")
    target_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    baseline_report_hash: str = Field(pattern="^[0-9a-f]{64}$")
    report_hash: str = Field(pattern="^[0-9a-f]{64}$")
    report: dict[str, Any]
