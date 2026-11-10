"""Rights gate nối Binance REST backfill với policy registry đã quan sát."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    ObservedSourcePolicy,
    source_rights_hash,
)

_SOURCE_ID = "BINANCE_USDM_REST"
_EVIDENCE_LEASE = timedelta(minutes=5)
_PURPOSE_MAP = {
    BackfillRightsPurpose.BACKTEST_RESEARCH: SourceUsePurpose.BACKTEST_RESEARCH,
    BackfillRightsPurpose.PAPER_DEMO: SourceUsePurpose.PAPER_DEMO,
}


class BackfillRightsRepository(Protocol):
    """Các đọc quyền phải nằm trong transaction/workspace của caller."""

    async def get_observed_entry(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
    ) -> ObservedSourcePolicy | None: ...

    async def database_clock(self, *, workspace_id: UUID) -> datetime: ...


class SourceRegistryBackfillRightsGate:
    """Chỉ cấp lease ngắn khi policy hiện hành và observer chain còn hợp lệ."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        repository: BackfillRightsRepository,
        source_id: str = _SOURCE_ID,
    ) -> None:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id không được để trống.")
        self._workspace_id = workspace_id
        self._repository = repository
        self._source_id = source_id

    async def authorize(
        self,
        *,
        request: RestBackfillRequest,
        now: datetime,
    ) -> BackfillRightsEvidence:
        """Đọc policy/receipt trước, rồi dùng DB clock để kiểm purpose và thời hạn."""
        if not isinstance(request, RestBackfillRequest):
            raise ValueError("request phải là RestBackfillRequest.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        purpose = _PURPOSE_MAP.get(request.purpose)
        if purpose is None:
            raise PermissionError("Backfill purpose chưa được nối với SourceUsePurpose.")

        observed = await self._repository.get_observed_entry(
            workspace_id=self._workspace_id,
            source_id=self._source_id,
        )
        if observed is None:
            raise PermissionError("Source policy thiếu chuỗi observer receipt đầy đủ.")
        if observed.history.source_id != self._source_id:
            raise PermissionError("Source policy không khớp nguồn backfill.")

        database_now = await self._repository.database_clock(workspace_id=self._workspace_id)
        if database_now.tzinfo is None or database_now.utcoffset() is None:
            raise PermissionError("Database clock không có múi giờ.")
        instant = database_now.astimezone(UTC)
        current = observed.history.current
        policy = current.policy
        if policy.source_id != self._source_id:
            raise PermissionError("Source policy không khớp nguồn backfill.")
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
            raise PermissionError("Policy hiện hành không cấp đúng mục đích backfill.")

        terms_url = policy.evidence_ref or ""
        terms = urlsplit(terms_url)
        if terms.scheme != "https" or not terms.netloc:
            raise PermissionError("Policy evidence cần URL điều khoản HTTPS đầy đủ.")

        lease_until = instant + _EVIDENCE_LEASE
        if policy.valid_until is not None:
            lease_until = min(lease_until, policy.valid_until)
        return BackfillRightsEvidence(
            workspace_id=self._workspace_id,
            provider="BINANCE_USDM_REST",
            purpose=request.purpose,
            terms_url=terms_url,
            policy_fingerprint=source_rights_hash(current),
            verified_at=policy.reviewed_at,
            valid_until=lease_until,
            checked_at=instant,
        )
