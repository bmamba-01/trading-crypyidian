"""Port AI Analyst (AI-2): engine phân tích Run Report, thay được giữa Claude/Codex/giả lập."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """Một lượt phân tích: prompt đã dựng, schema đầu ra, session để tiếp tục (nếu có)."""

    topic_key: str
    prompt: str
    schema: Mapping[str, Any]
    session_id: str | None = None
    resume: bool = False
    timeout_seconds: float = 600.0
    max_budget_usd: float = 1.0


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Kết quả thô kèm thông tin kiểm toán; ``structured_output`` None khi engine không trả."""

    engine: str
    structured_output: Mapping[str, Any] | None
    raw_stdout: str
    session_id: str | None
    exit_code: int
    cli_version: str | None = None
    model: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    error: str | None = None


class AnalystEngine(Protocol):
    name: str

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """Chạy một lượt phân tích đồng bộ (tiến trình con có timeout)."""
        ...
