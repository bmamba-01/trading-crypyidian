"""Hợp đồng nhãn tạo từ outcome replay đã hoàn tất."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


def _timestamp(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} phải là SHA-256 dạng hex chữ thường.")
    return value


@dataclass(frozen=True, slots=True)
class OutcomeLabelInput:
    """Thông tin do caller cung cấp để đánh giá một outcome đã đóng."""

    sample_id: str
    outcome_id: str
    outcome_hash: str
    recipe_hash: str
    expected_recipe_hash: str
    decision_at: datetime
    feature_available_at: datetime
    opened_at: datetime
    closed_at: datetime
    net_pnl: Decimal | str
    outcome_complete: bool
    fills_complete: bool
    fees_complete: bool
    funding_complete: bool
    source_dependent: bool
    source_rights_eligible: bool | None
    source_rights_hash: str | None
    label_version: str

    def __post_init__(self) -> None:
        for name in ("sample_id", "outcome_id", "label_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        for name in ("outcome_hash", "recipe_hash", "expected_recipe_hash"):
            _sha256(getattr(self, name), name)
        for name in ("decision_at", "feature_available_at", "opened_at", "closed_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name))
        if self.closed_at < self.opened_at:
            raise ValueError("closed_at không được trước opened_at.")
        if not isinstance(self.net_pnl, (Decimal, str)):
            raise ValueError("net_pnl phải là Decimal hoặc chuỗi thập phân.")
        try:
            net_pnl = Decimal(self.net_pnl)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("net_pnl không phải số thập phân hợp lệ.") from error
        if not net_pnl.is_finite():
            raise ValueError("net_pnl phải hữu hạn.")
        object.__setattr__(self, "net_pnl", net_pnl)
        for name in (
            "outcome_complete",
            "fills_complete",
            "fees_complete",
            "funding_complete",
            "source_dependent",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} phải là boolean.")
        if self.source_dependent:
            if type(self.source_rights_eligible) is not bool:
                raise ValueError("source_rights_eligible phải là boolean khi có phụ thuộc nguồn.")
        elif self.source_rights_eligible is not None or self.source_rights_hash is not None:
            raise ValueError("Quyền nguồn không áp dụng khi outcome không phụ thuộc nguồn.")
        if self.source_rights_hash is not None:
            _sha256(self.source_rights_hash, "source_rights_hash")
        if (
            self.source_dependent
            and self.source_rights_eligible
            and self.source_rights_hash is None
        ):
            raise ValueError("source_rights_hash bắt buộc khi rights được xác nhận.")


@dataclass(frozen=True, slots=True)
class OutcomeLabel:
    """Nhãn ba lớp theo dấu net PnL, gắn với outcome và recipe nguồn."""

    sample_id: str
    outcome_id: str
    outcome_hash: str
    recipe_hash: str
    label_version: str
    decision_at: datetime
    feature_available_at: datetime
    outcome_start: datetime
    outcome_end: datetime
    net_pnl: Decimal
    value: int
    source_rights_hash: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class OutcomeLabelResult:
    """Nhãn hợp lệ hoặc các lý do loại xác định, không giữ provisional label."""

    label: OutcomeLabel | None
    exclusion_reasons: tuple[str, ...]


def build_outcome_label(source: OutcomeLabelInput) -> OutcomeLabelResult:
    """Tạo nhãn sign-net-PnL khi outcome/chi phí/rights đủ điều kiện."""
    net_pnl = Decimal(source.net_pnl)
    reasons: list[str] = []
    if not all(
        (
            source.outcome_complete,
            source.fills_complete,
            source.fees_complete,
            source.funding_complete,
        )
    ):
        reasons.append("incomplete_outcome")
    if source.recipe_hash != source.expected_recipe_hash:
        reasons.append("recipe_mismatch")
    if source.feature_available_at > source.decision_at:
        reasons.append("feature_not_available_at_decision")
    if source.opened_at < source.decision_at:
        reasons.append("outcome_started_before_decision")
    if source.closed_at <= source.decision_at:
        reasons.append("outcome_not_after_decision")
    if source.source_dependent and not source.source_rights_eligible:
        reasons.append("source_rights_not_eligible")
    if reasons:
        return OutcomeLabelResult(None, tuple(reasons))

    value = (net_pnl > 0) - (net_pnl < 0)
    payload = {
        "sample_id": source.sample_id,
        "outcome_id": source.outcome_id,
        "outcome_hash": source.outcome_hash,
        "recipe_hash": source.recipe_hash,
        "label_version": source.label_version,
        "decision_at": source.decision_at.isoformat(),
        "feature_available_at": source.feature_available_at.isoformat(),
        "outcome_start": source.opened_at.isoformat(),
        "outcome_end": source.closed_at.isoformat(),
        "net_pnl": str(net_pnl),
        "value": value,
        "source_dependent": source.source_dependent,
        "source_rights_hash": source.source_rights_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return OutcomeLabelResult(
        OutcomeLabel(
            sample_id=source.sample_id,
            outcome_id=source.outcome_id,
            outcome_hash=source.outcome_hash,
            recipe_hash=source.recipe_hash,
            label_version=source.label_version,
            decision_at=source.decision_at,
            feature_available_at=source.feature_available_at,
            outcome_start=source.opened_at,
            outcome_end=source.closed_at,
            net_pnl=net_pnl,
            value=value,
            source_rights_hash=source.source_rights_hash,
            content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ),
        (),
    )
