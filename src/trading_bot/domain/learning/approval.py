"""Hợp đồng phê duyệt bất biến cho split trước khi niêm phong manifest."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LearningSplitApproval:
    """Ghim người duyệt, manifest dự kiến, policy, fold plan và bằng chứng MFA."""

    workspace_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    actor_membership_id: UUID
    idempotency_key: UUID
    reason: str
    approved_at: datetime
    mfa_verified_at: datetime

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (
                self.workspace_id,
                self.actor_membership_id,
                self.idempotency_key,
            )
        ):
            raise ValueError("Định danh phê duyệt phải là UUID.")
        for name in ("manifest_hash", "fold_plan_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} phải là SHA-256 dạng hex thường.")
        if (
            not isinstance(self.split_policy_version, str)
            or not self.split_policy_version.strip()
            or len(self.split_policy_version) > 128
        ):
            raise ValueError("split_policy_version không hợp lệ.")
        if (
            not isinstance(self.reason, str)
            or not 12 <= len(self.reason.strip()) <= 500
            or any(ord(char) < 32 and char not in "\t\n\r" for char in self.reason)
        ):
            raise ValueError("Lý do phê duyệt phải có 12–500 ký tự hiển thị.")
        approved_at = _timestamp(self.approved_at, "approved_at")
        mfa_verified_at = _timestamp(self.mfa_verified_at, "mfa_verified_at")
        if mfa_verified_at > approved_at or approved_at - mfa_verified_at > timedelta(minutes=15):
            raise ValueError("Bằng chứng MFA không còn mới tại thời điểm phê duyệt.")
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "mfa_verified_at", mfa_verified_at)

    @property
    def canonical_json(self) -> str:
        """Trả payload ổn định để so sánh retry và kiểm mã băm."""
        return json.dumps(
            {
                "actor_membership_id": str(self.actor_membership_id),
                "approved_at": self.approved_at.isoformat(),
                "fold_plan_hash": self.fold_plan_hash,
                "idempotency_key": str(self.idempotency_key),
                "manifest_hash": self.manifest_hash,
                "mfa_verified_at": self.mfa_verified_at.isoformat(),
                "reason": self.reason.strip(),
                "split_policy_version": self.split_policy_version,
                "workspace_id": str(self.workspace_id),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
