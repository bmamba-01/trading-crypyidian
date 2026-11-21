"""Sự kiện audit bất biến khi người có thẩm quyền chặn một entry PAPER."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from trading_bot.auth.policy import RECENT_MFA_LIFETIME

_SHA256 = re.compile(r"[0-9a-f]{64}")


def normalize_block_reason(reason: str) -> str:
    """Cắt khoảng trắng và kiểm tra giới hạn 1–500 ký tự của lý do đã chuẩn hóa."""
    if not isinstance(reason, str) or any(
        ord(char) < 32 and char not in "\t\n\r" for char in reason
    ):
        raise ValueError("reason không được chứa ký tự điều khiển ẩn.")
    normalized = reason.strip()
    if not 1 <= len(normalized) <= 500:
        raise ValueError("reason phải có từ 1 đến 500 ký tự sau khi cắt khoảng trắng.")
    return normalized


@dataclass(frozen=True, slots=True)
class LearningPaperEntryBlockEvent:
    """Ghim quyết định, phiên bản scorer và dữ liệu MFA tại thời điểm chặn."""

    event_id: UUID
    workspace_id: UUID
    bot_id: UUID
    bot_decision_id: UUID
    bot_decision_hash: str
    candidate_id: str
    score_event_id: UUID
    score_event_hash: str
    deployment_event_id: UUID
    deployment_revision: int
    registry_id: UUID
    registry_revision: int
    active_pointer_version: int
    scorer_artifact_id: UUID
    scorer_artifact_hash: str
    actor_user_id: UUID
    mfa_verified_at: datetime
    reason: str
    created_at: datetime
    idempotency_key: UUID

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "workspace_id",
            "bot_id",
            "bot_decision_id",
            "score_event_id",
            "deployment_event_id",
            "registry_id",
            "scorer_artifact_id",
            "actor_user_id",
            "idempotency_key",
        ):
            value = getattr(self, name)
            if type(value) is not UUID or value.int == 0:
                raise ValueError(f"{name} phải là UUID khác zero.")
        for name in (
            "bot_decision_hash",
            "candidate_id",
            "score_event_hash",
            "scorer_artifact_hash",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
        for name, minimum in (
            ("deployment_revision", 1),
            ("registry_revision", 1),
            ("active_pointer_version", 0),
        ):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} phải là số nguyên từ {minimum} trở lên.")
        for name in ("created_at", "mfa_verified_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError(f"{name} phải có múi giờ.")
            object.__setattr__(self, name, value.astimezone(UTC))
        if (
            self.mfa_verified_at > self.created_at
            or self.created_at - self.mfa_verified_at > RECENT_MFA_LIFETIME
        ):
            raise ValueError("MFA phải còn hiệu lực trong 15 phút và không được ở tương lai.")
        object.__setattr__(self, "reason", normalize_block_reason(self.reason))
