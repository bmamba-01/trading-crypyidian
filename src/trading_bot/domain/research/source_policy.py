"""Hợp đồng thuần để kiểm tra quyền sử dụng từng nguồn nghiên cứu."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class SourcePolicyState(StrEnum):
    """Trạng thái duyệt quyền của một policy nguồn."""

    DISCOVERED = "discovered"
    RIGHTS_PENDING = "rights_pending"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SourceUsePurpose(StrEnum):
    """Mục đích sử dụng phải được cấp quyền độc lập."""

    METADATA = "metadata"
    DISPLAY = "display"
    EMBEDDING = "embedding"
    TRAINING = "training"
    REDISTRIBUTION = "redistribution"


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class SourceRightsPolicy:
    """Quyền có phiên bản cho một nguồn và các mục đích sử dụng cụ thể."""

    policy_id: str
    source_id: str
    state: SourcePolicyState
    allowed_purposes: frozenset[SourceUsePurpose]
    evidence_ref: str | None = None
    reviewed_at: datetime | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SourcePolicyState):
            raise ValueError("state không hợp lệ.")
        for name in ("policy_id", "source_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        purposes = frozenset(self.allowed_purposes)
        if any(not isinstance(purpose, SourceUsePurpose) for purpose in purposes):
            raise ValueError("allowed_purposes chứa mục đích không hợp lệ.")
        object.__setattr__(self, "allowed_purposes", purposes)

        if self.reviewed_at is not None:
            _require_aware(self.reviewed_at, "reviewed_at")
            object.__setattr__(self, "reviewed_at", self.reviewed_at.astimezone(UTC))
        if self.valid_until is not None:
            _require_aware(self.valid_until, "valid_until")
            object.__setattr__(self, "valid_until", self.valid_until.astimezone(UTC))
        if self.reviewed_at is not None and self.valid_until is not None:
            if self.valid_until <= self.reviewed_at:
                raise ValueError("valid_until phải sau reviewed_at.")

        if self.state is SourcePolicyState.VERIFIED and self.reviewed_at is None:
            raise ValueError("Policy verified cần reviewed_at.")
        if self.state is SourcePolicyState.VERIFIED and not (self.evidence_ref or "").strip():
            raise ValueError("evidence_ref không được để trống.")

    def permits(self, purpose: SourceUsePurpose, *, at: datetime) -> bool:
        """Chỉ cho phép đúng mục đích khi policy đã duyệt và còn hiệu lực."""
        _require_aware(at, "at")
        instant = at.astimezone(UTC)
        return (
            isinstance(purpose, SourceUsePurpose)
            and self.state is SourcePolicyState.VERIFIED
            and self.reviewed_at is not None
            and self.reviewed_at <= instant
            and (self.valid_until is None or instant < self.valid_until)
            and purpose in self.allowed_purposes
        )
