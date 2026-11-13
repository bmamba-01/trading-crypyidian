"""Schema API giới hạn metadata/policy nguồn; không nhận danh tính từ request."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_bot.application.source_policy_admin import (
    SourceApprovalCommand,
    SourceCreateCommand,
    SourceLicenseEvidence,
    SourceRevisionCommand,
)
from trading_bot.domain.research.license_scope import SourceLicenseScope, canonical_scope_json
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourcePolicyRevision,
    SourceQuotaSnapshot,
    SourceRegistryEntry,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and "#" not in value
            and not any(char.isspace() or ord(char) < 32 for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("endpoint_url phải dùng HTTPS an toàn.")
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("valid_until phải có múi giờ.")
    return value


class SourceBudgetRequest(StrictRequest):
    window_seconds: int = Field(gt=0, le=31_536_000)
    max_requests: int = Field(gt=0)
    max_bytes: int = Field(gt=0)
    max_cost: Decimal = Field(ge=0, allow_inf_nan=False, strict=False)
    cost_unit: str = Field(min_length=1, max_length=32)

    @field_validator("max_cost", mode="before")
    @classmethod
    def decimal_string_only(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("max_cost phải là chuỗi thập phân chính xác.")
        return value

    def to_domain(self) -> SourceBudget:
        return SourceBudget(
            window=timedelta(seconds=self.window_seconds),
            max_requests=self.max_requests,
            max_bytes=self.max_bytes,
            max_cost=self.max_cost,
            cost_unit=self.cost_unit,
        )


class SourceCreateRequest(StrictRequest):
    source_id: str = Field(min_length=1, max_length=128)
    publisher: str = Field(min_length=1, max_length=512)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    access_method: str = Field(min_length=1, max_length=32)
    auth_scope: str = Field(min_length=1, max_length=512)
    budget: SourceBudgetRequest
    reason: str = Field(min_length=1, max_length=2048)
    attribution: str = Field(default="not approved", min_length=1, max_length=2048)
    retention_ref: str = Field(default="metadata-only-pending-review", min_length=1, max_length=512)

    @field_validator("source_id")
    @classmethod
    def valid_source_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
            raise ValueError("source_id không hợp lệ.")
        return value

    @field_validator("access_method")
    @classmethod
    def valid_access_method(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", value) is None:
            raise ValueError("access_method không hợp lệ.")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        return _endpoint(value)

    def to_command(self) -> SourceCreateCommand:
        return SourceCreateCommand(
            source_id=self.source_id,
            publisher=self.publisher,
            endpoint_url=self.endpoint_url,
            access_method=self.access_method,
            auth_scope=self.auth_scope,
            budget=self.budget.to_domain(),
            reason=self.reason,
            attribution=self.attribution,
            retention_ref=self.retention_ref,
        )


class SourceLicenseScopeRequest(StrictRequest):
    scope_version: Literal["wp06-license-scope-v1"] = "wp06-license-scope-v1"
    source_id: str
    provider: str
    product: str
    venue: str
    contract: str
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    data_start: datetime
    data_end: datetime
    allowed_purposes: tuple[SourceUsePurpose, ...]
    allowed_outputs: tuple[str, ...]
    max_retention_days: int | None = Field(strict=True)
    attribution: str
    derived_use: tuple[str, ...]

    def to_domain(self) -> SourceLicenseScope:
        return SourceLicenseScope(**self.model_dump(exclude={"scope_version"}))

    @model_validator(mode="after")
    def validate_scope(self) -> SourceLicenseScopeRequest:
        self.to_domain()
        return self


class SourceRevisionRequest(StrictRequest):
    state: Literal["rights_pending", "blocked", "revoked"]
    reason: str = Field(min_length=1, max_length=2048)
    evidence_ref: str | None = Field(default=None, max_length=2048)
    allowed_purposes: frozenset[SourceUsePurpose] = frozenset()
    valid_until: datetime | None = None
    publisher: str | None = Field(default=None, min_length=1, max_length=512)
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2048)
    access_method: str | None = Field(default=None, min_length=1, max_length=32)
    auth_scope: str | None = Field(default=None, min_length=1, max_length=512)
    attribution: str | None = Field(default=None, min_length=1, max_length=2048)
    retention_ref: str | None = Field(default=None, min_length=1, max_length=512)
    budget: SourceBudgetRequest | None = None
    provider: str | None = None
    product: str | None = None
    venue: str | None = None
    contract: str | None = None
    license_scope: SourceLicenseScopeRequest | None = None

    @field_validator("endpoint_url")
    @classmethod
    def valid_endpoint(cls, value: str | None) -> str | None:
        return _endpoint(value) if value is not None else None

    @field_validator("valid_until")
    @classmethod
    def valid_expiry(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def disabled_has_only_reason(self) -> SourceRevisionRequest:
        if self.state in {"blocked", "revoked"} and self.model_fields_set - {"state", "reason"}:
            raise ValueError("Policy bị vô hiệu chỉ nhận state và reason.")
        if self.state == "rights_pending":
            scope = self.license_scope
            if (
                scope is None
                or not self.evidence_ref
                or not self.evidence_ref.strip()
                or any(
                    getattr(self, name) != getattr(scope, name)
                    for name in ("provider", "product", "venue", "contract")
                )
            ):
                raise ValueError("Pending cần identity/scope/reference khớp chính xác.")
        return self

    def to_command(self) -> SourceRevisionCommand:
        return SourceRevisionCommand(
            state=SourcePolicyState(self.state),
            reason=self.reason,
            evidence_ref=self.evidence_ref,
            allowed_purposes=self.allowed_purposes,
            valid_until=self.valid_until,
            publisher=self.publisher,
            endpoint_url=self.endpoint_url,
            access_method=self.access_method,
            auth_scope=self.auth_scope,
            attribution=self.attribution,
            retention_ref=self.retention_ref,
            budget=self.budget.to_domain() if self.budget else None,
            provider=self.provider,
            product=self.product,
            venue=self.venue,
            contract=self.contract,
            license_scope=self.license_scope.to_domain() if self.license_scope else None,
        )


class SourceApprovalRequest(StrictRequest):
    license_evidence_id: UUID | None = None
    allowed_purposes: frozenset[SourceUsePurpose] = Field(min_length=1)
    valid_until: datetime | None = None
    reason: str = Field(min_length=1, max_length=2048)

    @field_validator("valid_until")
    @classmethod
    def valid_expiry(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    def to_command(self) -> SourceApprovalCommand:
        return SourceApprovalCommand(
            allowed_purposes=self.allowed_purposes,
            valid_until=self.valid_until,
            reason=self.reason,
            license_evidence_id=self.license_evidence_id,
        )


class SourceBudgetResponse(BaseModel):
    window_seconds: int
    max_requests: int
    max_bytes: int
    max_cost: Decimal
    cost_unit: str

    @classmethod
    def from_domain(cls, budget: SourceBudget) -> SourceBudgetResponse:
        return cls(
            window_seconds=int(budget.window.total_seconds()),
            max_requests=budget.max_requests,
            max_bytes=budget.max_bytes,
            max_cost=Decimal(str(budget.max_cost)),
            cost_unit=budget.cost_unit,
        )


class SourceRevisionResponse(BaseModel):
    policy_id: str
    source_id: str
    sequence: int
    state: SourcePolicyState
    allowed_purposes: tuple[SourceUsePurpose, ...]
    evidence_ref: str | None
    reviewed_at: datetime | None
    valid_until: datetime | None
    publisher: str
    endpoint_url: str
    access_method: str
    auth_scope: str
    attribution: str
    retention_ref: str
    budget: SourceBudgetResponse
    created_at: datetime
    actor_id: str
    reason: str
    approved_by: str | None
    provider: str | None
    product: str | None
    venue: str | None
    contract: str | None
    license_scope: SourceLicenseScopeRequest | None
    license_evidence_id: UUID | None
    license_digest: str | None
    license_scope_hash: str | None

    @classmethod
    def from_domain(cls, revision: SourcePolicyRevision) -> SourceRevisionResponse:
        policy = revision.policy
        return cls(
            policy_id=policy.policy_id,
            source_id=policy.source_id,
            sequence=revision.sequence,
            state=policy.state,
            allowed_purposes=tuple(sorted(policy.allowed_purposes)),
            evidence_ref=policy.evidence_ref,
            reviewed_at=policy.reviewed_at,
            valid_until=policy.valid_until,
            publisher=revision.publisher,
            endpoint_url=revision.endpoint_url,
            access_method=revision.access_method,
            auth_scope=revision.auth_scope,
            attribution=revision.attribution,
            retention_ref=revision.retention_ref,
            budget=SourceBudgetResponse.from_domain(revision.budget),
            created_at=revision.created_at,
            actor_id=revision.actor_id,
            reason=revision.reason,
            approved_by=revision.approved_by,
            provider=revision.provider,
            product=revision.product,
            venue=revision.venue,
            contract=revision.contract,
            license_scope=SourceLicenseScopeRequest.model_validate_json(
                canonical_scope_json(revision.license_scope)
            )
            if revision.license_scope
            else None,
            license_evidence_id=revision.license_evidence_id,
            license_digest=revision.license_digest,
            license_scope_hash=revision.license_scope_hash,
        )


class SourceEntryResponse(BaseModel):
    source_id: str
    current: SourceRevisionResponse
    revisions: tuple[SourceRevisionResponse, ...]

    @classmethod
    def from_domain(cls, entry: SourceRegistryEntry) -> SourceEntryResponse:
        return cls(
            source_id=entry.source_id,
            current=SourceRevisionResponse.from_domain(entry.current),
            revisions=tuple(SourceRevisionResponse.from_domain(item) for item in entry.revisions),
        )


class SourceQuotaResponse(BaseModel):
    source_id: str
    policy_sequence: int
    as_of: datetime
    window_started_at: datetime
    budget: SourceBudgetResponse
    requests_used: int
    bytes_used: int
    cost_used: Decimal

    @classmethod
    def from_domain(cls, snapshot: SourceQuotaSnapshot) -> SourceQuotaResponse:
        return cls(
            source_id=snapshot.source_id,
            policy_sequence=snapshot.policy_sequence,
            as_of=snapshot.as_of,
            window_started_at=snapshot.window_started_at,
            budget=SourceBudgetResponse.from_domain(snapshot.budget),
            requests_used=snapshot.usage.requests_used,
            bytes_used=snapshot.usage.bytes_used,
            cost_used=Decimal(str(snapshot.usage.cost_used)),
        )


class SourceDetailResponse(SourceEntryResponse):
    quota: SourceQuotaResponse | None

    @classmethod
    def from_snapshot(
        cls, entry: SourceRegistryEntry, quota: SourceQuotaSnapshot | None
    ) -> SourceDetailResponse:
        return cls(
            source_id=entry.source_id,
            current=SourceRevisionResponse.from_domain(entry.current),
            revisions=tuple(SourceRevisionResponse.from_domain(item) for item in entry.revisions),
            quota=SourceQuotaResponse.from_domain(quota) if quota else None,
        )


class SourceLicenseEvidenceResponse(BaseModel):
    evidence_id: UUID
    source_id: str
    policy_id: str
    evidence_ref: str
    digest: str
    byte_count: int
    media_type: str
    scope_version: str
    license_scope: SourceLicenseScopeRequest
    scope_hash: str
    actor_id: str
    created_at: datetime

    @classmethod
    def from_domain(cls, evidence: SourceLicenseEvidence) -> SourceLicenseEvidenceResponse:
        return cls(
            evidence_id=evidence.evidence_id,
            source_id=evidence.source_id,
            policy_id=evidence.policy_id,
            evidence_ref=evidence.evidence_ref,
            digest=evidence.digest,
            byte_count=evidence.byte_count,
            media_type=evidence.media_type,
            scope_version=evidence.license_scope.scope_version,
            license_scope=SourceLicenseScopeRequest.model_validate(
                json.loads(canonical_scope_json(evidence.license_scope))
            ),
            scope_hash=evidence.scope_hash,
            actor_id=evidence.actor_id,
            created_at=evidence.created_at,
        )
