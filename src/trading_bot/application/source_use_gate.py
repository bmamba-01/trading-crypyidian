"""Cổng quyền dùng nguồn chung (AI-4): policy Source Registry hiện hành phải cho phép mục đích.

Cùng các kiểm tra của rights gate Binance REST nhưng cho nguồn không phải nến (lịch vĩ mô, tin
tức...): chuỗi observer receipt đầy đủ, policy ``verified``, đã review, chưa hết hạn theo đồng hồ
DB, cấp đúng ``SourceUsePurpose`` và có URL điều khoản HTTPS. Thiếu bất kỳ điều kiện nào thì từ
chối (fail-closed), collector không được tải nguồn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from trading_bot.application.binance_rest_backfill_rights import BackfillRightsRepository
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_registry import source_rights_hash


@dataclass(frozen=True, slots=True)
class SourceUseEvidence:
    """Bằng chứng quyền tại thời điểm kiểm (lưu cùng dữ liệu đã thu)."""

    workspace_id: UUID
    source_id: str
    purpose: SourceUsePurpose
    policy_id: str
    policy_fingerprint: str
    terms_url: str
    checked_at: datetime


class SourceUseGate:
    """Kiểm quyền một nguồn cho một mục đích trong transaction/workspace của caller."""

    def __init__(
        self, *, workspace_id: UUID, source_id: str, repository: BackfillRightsRepository
    ) -> None:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id không được để trống.")
        self._workspace_id = workspace_id
        self._source_id = source_id
        self._repository = repository

    async def authorize(self, purpose: SourceUsePurpose) -> SourceUseEvidence:
        observed = await self._repository.get_observed_entry(
            workspace_id=self._workspace_id, source_id=self._source_id
        )
        if observed is None:
            raise PermissionError("Nguồn chưa có policy với chuỗi observer receipt đầy đủ.")
        database_now = await self._repository.database_clock(workspace_id=self._workspace_id)
        if database_now.tzinfo is None or database_now.utcoffset() is None:
            raise PermissionError("Database clock không có múi giờ.")
        instant = database_now.astimezone(UTC)
        current = observed.history.current
        policy = current.policy
        if observed.history.source_id != self._source_id or policy.source_id != self._source_id:
            raise PermissionError("Policy không khớp nguồn.")
        known_at = observed.known_at.get(policy.policy_id)
        if known_at is None or known_at >= instant:
            raise PermissionError("Policy hiện hành chưa có observer receipt khả dụng.")
        if policy.state.value != "verified":
            raise PermissionError("Policy hiện hành chưa được verified.")
        if policy.valid_until is not None and instant >= policy.valid_until:
            raise PermissionError("Policy quyền dữ liệu đã hết hạn.")
        if policy.reviewed_at is None or policy.reviewed_at > instant:
            raise PermissionError("Policy chưa được review tại database clock.")
        if purpose not in policy.allowed_purposes or not observed.history.permits(
            purpose, at=instant
        ):
            raise PermissionError("Policy hiện hành không cấp mục đích này.")
        terms_url = policy.evidence_ref or ""
        terms = urlsplit(terms_url)
        if terms.scheme != "https" or not terms.netloc:
            raise PermissionError("Policy cần URL điều khoản HTTPS đầy đủ.")
        return SourceUseEvidence(
            workspace_id=self._workspace_id,
            source_id=self._source_id,
            purpose=purpose,
            policy_id=policy.policy_id,
            policy_fingerprint=source_rights_hash(current),
            terms_url=terms_url,
            checked_at=instant,
        )


__all__ = ["SourceUseEvidence", "SourceUseGate"]
