"""Service quyền nguồn theo Principal đã xác thực và quota kiểm tra tại operation boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    SourcePolicyRevision,
)


class SourceQuotaReservation(Protocol):
    """Kết quả quota của repository port, không kéo adapter vào application."""

    event_id: UUID
    window_started_at: datetime
    policy_sequence: int
    is_duplicate: bool


class SourcePolicyAppend(Protocol):
    """Kết quả append policy của repository port."""

    revision: SourcePolicyRevision
    is_duplicate: bool


class SourceRegistryRepository(Protocol):
    """Các thao tác persistence chạy trong transaction do caller sở hữu."""

    async def append_revision_and_observe(
        self, *, workspace_id: UUID, revision: SourcePolicyRevision
    ) -> SourcePolicyAppend: ...

    async def reserve_quota(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        idempotency_key: str,
        purpose: SourceUsePurpose,
        reserved_bytes: int,
        reserved_cost: Decimal | str,
    ) -> SourceQuotaReservation: ...


class SourceRegistryService:
    """Tạo facade theo Principal; repository vẫn thực thi policy/quota atomically."""

    def __init__(
        self,
        repository: SourceRegistryRepository,
    ) -> None:
        if not callable(getattr(repository, "append_revision_and_observe", None)):
            raise TypeError("Source registry port bắt buộc append_revision_and_observe sau commit.")
        validate_port = getattr(repository, "validate_application_port", None)
        if validate_port is not None:
            validate_port()
        self._repository = repository

    def for_principal(self, principal: Principal) -> PrincipalSourceRegistryService:
        if not isinstance(principal, Principal):
            raise TypeError("principal phải đến từ authentication boundary.")
        return PrincipalSourceRegistryService(self, principal)


class PrincipalSourceRegistryService:
    """Ghi provenance từ Principal; không nhận actor/approver identity từ request."""

    def __init__(self, service: SourceRegistryService, principal: Principal) -> None:
        self._service = service
        self._principal = principal

    async def append_revision(
        self,
        candidate: SourcePolicyRevision,
        *,
        reviewer: Principal | None = None,
    ) -> SourcePolicyRevision:
        """Ghi revision với actor/reviewer xác thực; repository cấp timestamp DB."""
        actor = self._principal
        _require_permission(actor, Permission.RESEARCH_SOURCE_WRITE)
        if not isinstance(candidate, SourcePolicyRevision):
            raise TypeError("candidate phải là SourcePolicyRevision.")
        if candidate.policy.state is SourcePolicyState.VERIFIED:
            if reviewer is None:
                raise PermissionError("Policy verified cần Principal reviewer riêng.")
            _require_permission(reviewer, Permission.RESEARCH_SOURCE_APPROVE)
            if reviewer.workspace_id != actor.workspace_id:
                raise PermissionError("actor và reviewer phải thuộc cùng workspace.")
            if reviewer.user_id == actor.user_id or reviewer.membership_id == actor.membership_id:
                raise PermissionError("reviewer phải khác người tạo policy.")
            approver_id: str | None = str(reviewer.user_id)
        else:
            if reviewer is not None:
                raise ValueError("reviewer chỉ áp dụng khi policy verified.")
            approver_id = None
        revision = replace(
            candidate,
            actor_id=str(actor.user_id),
            approved_by=approver_id,
        )
        result = await self._service._repository.append_revision_and_observe(
            workspace_id=actor.workspace_id, revision=revision
        )
        return result.revision

    async def reserve_usage(
        self,
        *,
        source_id: str,
        idempotency_key: str,
        purpose: SourceUsePurpose,
        reserved_bytes: int,
        reserved_cost: Decimal | str,
    ) -> SourceQuotaReservation:
        """Reserve quota trước fetch; repository kiểm rights/purpose bằng đồng hồ DB."""
        principal = self._principal
        _require_permission(principal, Permission.RESEARCH_SOURCE_USE)
        if type(purpose) is not SourceUsePurpose:
            raise ValueError("purpose phải là SourceUsePurpose tường minh.")
        return await self._service._repository.reserve_quota(
            workspace_id=principal.workspace_id,
            source_id=source_id,
            idempotency_key=idempotency_key,
            purpose=purpose,
            reserved_bytes=reserved_bytes,
            reserved_cost=reserved_cost,
        )


def _require_permission(principal: Principal, permission: Permission) -> None:
    if not isinstance(principal, Principal) or not authorize(principal, permission):
        raise PermissionError(f"Principal không có quyền {permission.value}.")
