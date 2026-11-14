"""Quản trị policy nguồn offline theo Principal và repository chỉ ghi policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol
from uuid import UUID, uuid4

from starlette.requests import ClientDisconnect

from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.research.license_scope import SourceLicenseScope, scope_sha256
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourcePolicyRevision,
    SourceQuotaSnapshot,
    SourceRegistryEntry,
)

LICENSE_EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
LICENSE_EVIDENCE_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/html",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


class LicenseEvidenceUploadError(ValueError):
    """Lỗi upload an toàn, chỉ mang mã HTTP và thông báo chung."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SourceLicenseEvidence:
    evidence_id: UUID
    source_id: str
    policy_id: str
    idempotency_key: UUID
    evidence_ref: str
    digest: str
    byte_count: int
    media_type: str
    license_scope: SourceLicenseScope
    actor_id: str
    created_at: datetime

    @property
    def scope_hash(self) -> str:
        return scope_sha256(self.license_scope)


@dataclass(frozen=True, slots=True)
class SourceCreateCommand:
    source_id: str
    publisher: str
    endpoint_url: str
    access_method: str
    auth_scope: str
    budget: SourceBudget
    reason: str
    attribution: str = "not approved"
    retention_ref: str = "metadata-only-pending-review"


@dataclass(frozen=True, slots=True)
class SourceRevisionCommand:
    state: SourcePolicyState
    reason: str
    evidence_ref: str | None = None
    allowed_purposes: frozenset[SourceUsePurpose] = frozenset()
    valid_until: datetime | None = None
    publisher: str | None = None
    endpoint_url: str | None = None
    access_method: str | None = None
    auth_scope: str | None = None
    attribution: str | None = None
    retention_ref: str | None = None
    budget: SourceBudget | None = None
    provider: str | None = None
    product: str | None = None
    venue: str | None = None
    contract: str | None = None
    license_scope: SourceLicenseScope | None = None


@dataclass(frozen=True, slots=True)
class SourceApprovalCommand:
    allowed_purposes: frozenset[SourceUsePurpose]
    valid_until: datetime | None
    reason: str
    license_evidence_id: UUID | None = None


class SourcePolicyAppend(Protocol):
    revision: SourcePolicyRevision
    is_duplicate: bool


class SourcePolicyAdminRepository(Protocol):
    async def evidence_by_id(
        self, *, workspace_id: UUID, source_id: str, evidence_id: UUID
    ) -> SourceLicenseEvidence | None: ...

    async def append_license_evidence(
        self, *, workspace_id: UUID, evidence: SourceLicenseEvidence
    ) -> SourceLicenseEvidence: ...
    async def evidence_for_pending(
        self, *, workspace_id: UUID, source_id: str, policy_id: str
    ) -> SourceLicenseEvidence | None: ...

    async def list_entries(self, *, workspace_id: UUID) -> tuple[SourceRegistryEntry, ...]: ...
    async def get_entry(
        self, *, workspace_id: UUID, source_id: str
    ) -> SourceRegistryEntry | None: ...
    async def get_quota_snapshot(
        self, *, workspace_id: UUID, source_id: str
    ) -> SourceQuotaSnapshot | None: ...
    async def get_source_snapshot(
        self, *, workspace_id: UUID, source_id: str
    ) -> tuple[SourceRegistryEntry, SourceQuotaSnapshot | None] | None: ...
    async def membership_id_for_user(self, *, workspace_id: UUID, user_id: UUID) -> UUID | None: ...
    async def find_revisions_by_idempotency_key(
        self, *, workspace_id: UUID, key: UUID
    ) -> tuple[SourcePolicyRevision, ...]: ...
    async def append_revision_and_observe(
        self, *, workspace_id: UUID, revision: SourcePolicyRevision
    ) -> SourcePolicyAppend: ...


class SourcePolicyAdminService:
    """Port chỉ quản trị policy; không chứa thao tác reserve quota."""

    def __init__(self, repository: SourcePolicyAdminRepository) -> None:
        self._repository = repository

    def for_principal(self, principal: Principal) -> PrincipalSourcePolicyAdminService:
        if not isinstance(principal, Principal):
            raise TypeError("principal phải đến từ authentication boundary.")
        return PrincipalSourcePolicyAdminService(self._repository, principal)


class PrincipalSourcePolicyAdminService:
    def __init__(self, repository: SourcePolicyAdminRepository, principal: Principal) -> None:
        self._repository = repository
        self._principal = principal

    def _read(self) -> None:
        if not (
            authorize(self._principal, Permission.RESEARCH_SOURCE_WRITE)
            or authorize(self._principal, Permission.RESEARCH_SOURCE_APPROVE)
        ):
            raise PermissionError("Không có quyền đọc source policy.")

    def _mutation(self, permission: Permission) -> None:
        if not authorize(self._principal, permission):
            raise PermissionError("Không có quyền sửa source policy.")
        at = self._principal.mfa_verified_at
        now = datetime.now(UTC)
        if at.tzinfo is None or at > now or now - at > RECENT_MFA_LIFETIME:
            raise PermissionError("Cần xác thực MFA gần đây.")

    async def list_sources(self) -> tuple[SourceRegistryEntry, ...]:
        self._read()
        return await self._repository.list_entries(workspace_id=self._principal.workspace_id)

    async def get_source(
        self, source_id: str
    ) -> tuple[SourceRegistryEntry, SourceQuotaSnapshot | None] | None:
        self._read()
        return await self._repository.get_source_snapshot(
            workspace_id=self._principal.workspace_id, source_id=source_id
        )

    async def get_pending_license_evidence(
        self, source_id: str, policy_id: str
    ) -> SourceLicenseEvidence | None:
        """Trả metadata của đúng pending hiện hành, không lộ row hoặc bytes của ledger."""
        self._read()
        entry = await self._repository.get_entry(
            workspace_id=self._principal.workspace_id,
            source_id=source_id,
        )
        if (
            entry is None
            or entry.current.state is not SourcePolicyState.RIGHTS_PENDING
            or entry.current.policy.policy_id != policy_id
        ):
            return None
        return await self._repository.evidence_for_pending(
            workspace_id=self._principal.workspace_id,
            source_id=source_id,
            policy_id=policy_id,
        )

    async def _append(self, revision: SourcePolicyRevision) -> SourcePolicyRevision:
        result = await self._repository.append_revision_and_observe(
            workspace_id=self._principal.workspace_id, revision=revision
        )
        return result.revision

    async def create_source(
        self, command: SourceCreateCommand, idempotency_key: UUID
    ) -> SourcePolicyRevision:
        self._mutation(Permission.RESEARCH_SOURCE_WRITE)
        _require_key(idempotency_key)
        entry = await self._repository.get_entry(
            workspace_id=self._principal.workspace_id, source_id=command.source_id
        )
        candidate = SourceRegistryEntry.discover(
            source_id=command.source_id,
            publisher=command.publisher,
            endpoint_url=command.endpoint_url,
            access_method=command.access_method,
            auth_scope=command.auth_scope,
            budget=command.budget,
            actor_id=_actor_id(self._principal),
            at=entry.revisions[0].created_at if entry else datetime.now(UTC),
            reason=command.reason,
            attribution=command.attribution,
            retention_ref=command.retention_ref,
        ).current
        if entry is not None:
            _assert_same(entry.revisions[0], candidate)
        return await self._append(candidate)

    async def submit_pending(
        self, source_id: str, command: SourceRevisionCommand, idempotency_key: UUID
    ) -> SourcePolicyRevision:
        self._mutation(Permission.RESEARCH_SOURCE_WRITE)
        if command.state is not SourcePolicyState.RIGHTS_PENDING:
            raise ValueError("Chỉ nhận revision rights_pending.")
        scope = command.license_scope
        if (
            not isinstance(scope, SourceLicenseScope)
            or not command.evidence_ref
            or not command.evidence_ref.strip()
            or scope.source_id != source_id
            or any(
                getattr(command, name) != getattr(scope, name)
                for name in ("provider", "product", "venue", "contract")
            )
        ):
            raise ValueError("Pending cần scope/reference và identity khớp chính xác.")
        return await self._change(source_id, command, idempotency_key)

    async def change_disabled_state(
        self, source_id: str, state: SourcePolicyState, reason: str, idempotency_key: UUID
    ) -> SourcePolicyRevision:
        self._mutation(Permission.RESEARCH_SOURCE_WRITE)
        if state not in (SourcePolicyState.BLOCKED, SourcePolicyState.REVOKED):
            raise ValueError("Chỉ nhận blocked hoặc revoked.")
        return await self._change(source_id, SourceRevisionCommand(state, reason), idempotency_key)

    async def _change(
        self, source_id: str, command: SourceRevisionCommand, idempotency_key: UUID
    ) -> SourcePolicyRevision:
        entry = await self._repository.get_entry(
            workspace_id=self._principal.workspace_id, source_id=source_id
        )
        if entry is None:
            raise LookupError("Không tìm thấy source.")
        policy_id = _policy_id(idempotency_key, (source_id, command))
        existing = await self._find_retry(entry, idempotency_key, policy_id)
        base = (
            SourceRegistryEntry(source_id, entry.revisions[: existing.sequence - 1])
            if existing and existing.sequence > 1
            else entry
        )
        if existing is not None and existing.sequence == 1:
            raise ValueError("idempotency key đã dùng cho candidate.")
        at = existing.created_at if existing else datetime.now(UTC)
        candidate = base.transition(
            policy_id=policy_id,
            state=command.state,
            actor_id=_actor_id(self._principal),
            at=at,
            reason=command.reason,
            allowed_purposes=command.allowed_purposes,
            evidence_ref=command.evidence_ref,
            valid_until=command.valid_until,
            publisher=command.publisher,
            endpoint_url=command.endpoint_url,
            access_method=command.access_method,
            auth_scope=command.auth_scope,
            attribution=command.attribution,
            retention_ref=command.retention_ref,
            budget=command.budget,
            provider=command.provider,
            product=command.product,
            venue=command.venue,
            contract=command.contract,
            license_scope=command.license_scope,
        ).current
        if existing:
            _assert_same(existing, candidate)
        return await self._append(candidate)

    async def approve_pending(
        self,
        source_id: str,
        pending_policy_id: str,
        command: SourceApprovalCommand,
        idempotency_key: UUID,
    ) -> SourcePolicyRevision:
        self._mutation(Permission.RESEARCH_SOURCE_APPROVE)
        entry = await self._repository.get_entry(
            workspace_id=self._principal.workspace_id, source_id=source_id
        )
        if entry is None:
            raise LookupError("Không tìm thấy source.")
        policy_id = _policy_id(idempotency_key, (source_id, pending_policy_id, command))
        existing = await self._find_retry(entry, idempotency_key, policy_id)
        base = (
            SourceRegistryEntry(source_id, entry.revisions[: existing.sequence - 1])
            if existing and existing.sequence > 1
            else entry
        )
        pending = base.current
        if (
            pending.policy.policy_id != pending_policy_id
            or pending.state is not SourcePolicyState.RIGHTS_PENDING
        ):
            raise ValueError("Pending revision không còn là phiên bản hiện hành.")
        if await self._same_actor(pending.actor_id):
            raise PermissionError("Reviewer phải khác người gửi policy.")
        pending_evidence = await self._repository.evidence_for_pending(
            workspace_id=self._principal.workspace_id,
            source_id=source_id,
            policy_id=pending_policy_id,
        )
        evidence = pending_evidence
        if command.license_evidence_id is not None:
            if not isinstance(command.license_evidence_id, UUID):
                raise ValueError("license_evidence_id phải là UUID.")
            evidence = await self._repository.evidence_by_id(
                workspace_id=self._principal.workspace_id,
                source_id=source_id,
                evidence_id=command.license_evidence_id,
            )
            if evidence is None:
                raise LookupError("Không tìm thấy evidence trong workspace/source.")
        if (
            evidence is None
            or evidence.source_id != source_id
            or evidence.license_scope != pending.license_scope
            or evidence.evidence_ref != pending.policy.evidence_ref
            or not command.allowed_purposes.issubset(evidence.license_scope.allowed_purposes)
        ):
            raise ValueError("Cần evidence cùng pending scope/reference và purpose phù hợp.")
        if pending_evidence is not None and any(
            getattr(pending_evidence, name) != getattr(evidence, name)
            for name in ("digest", "byte_count", "media_type", "evidence_ref", "license_scope")
        ):
            raise ValueError("Evidence được chọn khác payload đã upload cho pending.")
        if await self._same_actor(evidence.actor_id):
            raise PermissionError("Reviewer phải khác người upload evidence.")
        at = existing.created_at if existing else datetime.now(UTC)
        reviewed = existing.policy.reviewed_at if existing else at
        candidate = base.transition(
            policy_id=policy_id,
            state=SourcePolicyState.VERIFIED,
            actor_id=pending.actor_id,
            at=at,
            reason=command.reason,
            allowed_purposes=command.allowed_purposes,
            evidence_ref=pending.policy.evidence_ref,
            reviewed_at=reviewed,
            valid_until=command.valid_until,
            approved_by=str(self._principal.user_id),
            license_scope=evidence.license_scope,
            license_evidence_id=evidence.evidence_id,
            license_digest=evidence.digest,
            license_scope_hash=evidence.scope_hash,
        ).current
        if existing:
            _assert_same(existing, candidate)
        return await self._append(candidate)

    async def upload_license_evidence(
        self,
        source_id: str,
        policy_id: str,
        stream: AsyncIterator[bytes],
        *,
        media_type: str,
        content_length: str | None,
        idempotency_key: UUID,
    ) -> SourceLicenseEvidence:
        """Băm từng chunk rồi bỏ bytes; chỉ append sau khi luồng hoàn tất."""
        self._mutation(Permission.RESEARCH_SOURCE_WRITE)
        _require_key(idempotency_key)
        if media_type not in LICENSE_EVIDENCE_MEDIA_TYPES:
            raise LicenseEvidenceUploadError(415, "Loại tài liệu không được hỗ trợ.")
        length = None
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdecimal():
                raise LicenseEvidenceUploadError(400, "Content-Length không hợp lệ.")
            # Không chuyển số nguyên quá dài do caller cung cấp.
            normalized = content_length.lstrip("0") or "0"
            if len(normalized) > 8 or int(normalized) > LICENSE_EVIDENCE_MAX_BYTES:
                raise LicenseEvidenceUploadError(413, "Tài liệu vượt 10 MiB.")
            length = int(normalized)
        entry = await self._repository.get_entry(
            workspace_id=self._principal.workspace_id,
            source_id=source_id,
        )
        if entry is None:
            raise LookupError("Không tìm thấy nguồn.")
        pending = entry.current
        if (
            pending.state is not SourcePolicyState.RIGHTS_PENDING
            or pending.policy.policy_id != policy_id
            or pending.license_scope is None
            or not pending.policy.evidence_ref
        ):
            raise ValueError("Pending revision không còn hiện hành hoặc thiếu scope/reference.")
        digest = hashlib.sha256()
        count = 0
        try:
            async for chunk in stream:
                count += len(chunk)
                if count > LICENSE_EVIDENCE_MAX_BYTES:
                    raise LicenseEvidenceUploadError(413, "Tài liệu vượt 10 MiB.")
                digest.update(chunk)
        except ClientDisconnect as error:
            raise LicenseEvidenceUploadError(400, "Upload bị gián đoạn.") from error
        if count == 0 or (length is not None and count != length):
            raise LicenseEvidenceUploadError(400, "Body rỗng hoặc Content-Length không khớp.")
        return await self._repository.append_license_evidence(
            workspace_id=self._principal.workspace_id,
            evidence=SourceLicenseEvidence(
                uuid4(),
                source_id,
                policy_id,
                idempotency_key,
                pending.policy.evidence_ref,
                digest.hexdigest(),
                count,
                media_type,
                pending.license_scope,
                _actor_id(self._principal),
                datetime.now(UTC),
            ),
        )

    async def _find_retry(
        self, entry: SourceRegistryEntry, key: UUID, policy_id: str
    ) -> SourcePolicyRevision | None:
        matches = await self._repository.find_revisions_by_idempotency_key(
            workspace_id=self._principal.workspace_id, key=key
        )
        if len(matches) > 1:
            raise ValueError("idempotency key có nhiều revision; từ chối thao tác.")
        if not matches:
            return None
        existing = matches[0]
        if existing.policy.source_id != entry.source_id or existing.policy.policy_id != policy_id:
            raise ValueError("idempotency key đã có payload khác.")
        return existing

    async def _same_actor(self, actor_id: str) -> bool:
        parts = actor_id.split(":")
        if len(parts) == 2:
            try:
                actor_user, actor_membership = UUID(parts[0]), UUID(parts[1])
            except ValueError as error:
                raise PermissionError("actor_id không hợp lệ; từ chối duyệt.") from error
        elif len(parts) == 1:
            try:
                actor_user = UUID(parts[0])
            except ValueError as error:
                raise PermissionError("actor_id legacy không hợp lệ; từ chối duyệt.") from error
            resolved = await self._repository.membership_id_for_user(
                workspace_id=self._principal.workspace_id, user_id=actor_user
            )
            if resolved is None:
                raise PermissionError("Không tìm được membership của actor legacy; từ chối duyệt.")
            actor_membership = resolved
        else:
            raise PermissionError("actor_id không hợp lệ; từ chối duyệt.")
        return (
            actor_user == self._principal.user_id
            or actor_membership == self._principal.membership_id
        )


def _require_key(key: UUID) -> None:
    if not isinstance(key, UUID):
        raise ValueError("idempotency_key phải là UUID.")


def _canonical(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        # Giữ key retry của approval cũ khi chưa chọn evidence ID tường minh.
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in fields(value)
            if not (
                isinstance(value, SourceApprovalCommand)
                and field.name == "license_evidence_id"
                and value.license_evidence_id is None
            )
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp phải có múi giờ.")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, timedelta):
        return [value.days, value.seconds, value.microseconds]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (frozenset, set)):
        return sorted((_canonical(item) for item in value), key=str)
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("Payload command chứa kiểu không hợp lệ.")


def _policy_id(key: UUID, payload: object) -> str:
    _require_key(key)
    canonical = json.dumps(
        _canonical(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"policy:{key.hex}:{digest}"


def _actor_id(principal: Principal) -> str:
    return f"{principal.user_id}:{principal.membership_id}"


def _assert_same(stored: SourcePolicyRevision, candidate: SourcePolicyRevision) -> None:
    if stored != candidate:
        raise ValueError("idempotency key đã có payload khác.")
