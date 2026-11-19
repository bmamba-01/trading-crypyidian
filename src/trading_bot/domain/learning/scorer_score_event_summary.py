"""Tóm tắt score event đã khử payload để hiển thị trong admin."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

ScorerEventMode = Literal["SHADOW", "PAPER_GATE"]
ScorerEventDecision = Literal["APPROVE", "VETO", "ABSTAIN"]


@dataclass(frozen=True, slots=True)
class ScorerPaperEntryBlockAuditSummary:
    """Thông tin tối thiểu về lần operator chặn entry PAPER."""

    actor_user_id: UUID
    mfa_verified_at: datetime
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.actor_user_id) is not UUID or self.actor_user_id.int == 0:
            raise ValueError("actor_user_id audit chặn PAPER phải là UUID hợp lệ.")
        for name, value in (
            ("mfa_verified_at", self.mfa_verified_at),
            ("created_at", self.created_at),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} audit chặn PAPER phải có múi giờ.")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.mfa_verified_at > self.created_at:
            raise ValueError("MFA audit chặn PAPER không thể đến sau thời điểm ghi.")
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 500:
            raise ValueError("reason audit chặn PAPER không hợp lệ.")


@dataclass(frozen=True, slots=True)
class ScorerScoreEventSummary:
    """Một sự kiện scorer chỉ chứa các trường được phép hiển thị."""

    event_id: UUID
    created_at: datetime
    mode: ScorerEventMode
    decision: ScorerEventDecision
    reason_code: str | None
    probability: float | None
    confidence: float | None
    uncertainty: float | None
    bot_id: UUID | None = None
    bot_decision_id: UUID | None = None
    paper_entry_block: ScorerPaperEntryBlockAuditSummary | None = None

    def __post_init__(self) -> None:
        if type(self.event_id) is not UUID:
            raise ValueError("event_id phải là UUID.")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at score event phải có múi giờ.")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if self.mode not in {"SHADOW", "PAPER_GATE"}:
            raise ValueError("mode score event không hợp lệ.")
        if self.decision not in {"APPROVE", "VETO", "ABSTAIN"}:
            raise ValueError("decision score event không hợp lệ.")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str)
            or not self.reason_code.strip()
            or len(self.reason_code) > 80
        ):
            raise ValueError("reason_code score event không hợp lệ.")
        values = (self.probability, self.confidence, self.uncertainty)
        for name, value in zip(("probability", "confidence", "uncertainty"), values, strict=True):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} score event phải nằm trong [0, 1].")
        if self.decision == "ABSTAIN":
            if self.reason_code is None or any(value is not None for value in values):
                raise ValueError("ABSTAIN cần reason_code và không được có xác suất.")
        elif self.decision == "VETO":
            if self.reason_code is None or any(value is None for value in values):
                raise ValueError("VETO cần reason_code và đủ xác suất.")
        elif self.reason_code is not None or any(value is None for value in values):
            raise ValueError("APPROVE cần đủ xác suất và không có reason_code.")
        for name, identifier in (
            ("bot_id", self.bot_id),
            ("bot_decision_id", self.bot_decision_id),
        ):
            if identifier is not None and (type(identifier) is not UUID or identifier.int == 0):
                raise ValueError(f"{name} liên kết scorer không hợp lệ.")
        if (
            self.paper_entry_block is not None
            and type(self.paper_entry_block) is not ScorerPaperEntryBlockAuditSummary
        ):
            raise ValueError("paper_entry_block phải là audit đã kiểm tra.")


@dataclass(frozen=True, slots=True)
class ScorerScoreEventPage:
    """Một lát sự kiện scorer để phân trang ổn định theo số trang."""

    items: tuple[ScorerScoreEventSummary, ...]
    page: int
    has_next: bool

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            type(item) is not ScorerScoreEventSummary for item in self.items
        ):
            raise ValueError("items phải là tuple score event đã kiểm tra.")
        if type(self.page) is not int or self.page < 1:
            raise ValueError("page phải là số nguyên dương.")
        if type(self.has_next) is not bool:
            raise ValueError("has_next phải là boolean.")

    @property
    def has_previous(self) -> bool:
        """Cho biết trang trước có tồn tại theo chỉ số trang hiện tại."""
        return self.page > 1


__all__ = [
    "ScorerEventDecision",
    "ScorerEventMode",
    "ScorerPaperEntryBlockAuditSummary",
    "ScorerScoreEventPage",
    "ScorerScoreEventSummary",
]
