"""Contract thuần cho phiên bản quyền, cấu hình nguồn và quota nghiên cứu."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

from trading_bot.domain.research.license_scope import SourceLicenseScope, scope_sha256
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_METHOD = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_MAX_COST_DIGITS = 128
_MAX_COST_EXPONENT = 128

_TRANSITIONS: dict[SourcePolicyState, frozenset[SourcePolicyState]] = {
    SourcePolicyState.DISCOVERED: frozenset(
        {SourcePolicyState.RIGHTS_PENDING, SourcePolicyState.BLOCKED}
    ),
    SourcePolicyState.RIGHTS_PENDING: frozenset(
        {SourcePolicyState.VERIFIED, SourcePolicyState.BLOCKED}
    ),
    SourcePolicyState.VERIFIED: frozenset(
        {
            SourcePolicyState.RIGHTS_PENDING,
            SourcePolicyState.BLOCKED,
            SourcePolicyState.REVOKED,
            SourcePolicyState.EXPIRED,
        }
    ),
    SourcePolicyState.BLOCKED: frozenset({SourcePolicyState.RIGHTS_PENDING}),
    SourcePolicyState.REVOKED: frozenset({SourcePolicyState.RIGHTS_PENDING}),
    SourcePolicyState.EXPIRED: frozenset({SourcePolicyState.RIGHTS_PENDING}),
}


class SourcePolicyTransitionError(RuntimeError):
    """Chuyển trạng thái nguồn không hợp lệ hoặc thiếu điều kiện review."""


class SourceQuotaExceeded(RuntimeError):
    """Ngân sách request, byte hoặc chi phí của cửa sổ đã hết."""


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _cost(value: Decimal | str, name: str) -> Decimal:
    if not isinstance(value, (Decimal, str)):
        raise ValueError(f"{name} phải là Decimal hoặc chuỗi thập phân.")
    if isinstance(value, str) and len(value) > _MAX_COST_DIGITS * 2:
        raise ValueError(f"{name} vượt giới hạn độ dài.")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} không hợp lệ.") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} phải hữu hạn và không âm.")
    parts = result.as_tuple()
    if not isinstance(parts.exponent, int) or abs(parts.exponent) > _MAX_COST_EXPONENT:
        raise ValueError(f"{name} vượt giới hạn precision hoặc exponent.")
    digits = parts.digits
    if not any(digits):
        return Decimal(0)
    exponent = parts.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    if len(digits) > _MAX_COST_DIGITS or abs(exponent) > _MAX_COST_EXPONENT:
        raise ValueError(f"{name} vượt giới hạn precision hoặc exponent.")
    return Decimal((parts.sign, digits, exponent))


def normalize_source_cost(value: Decimal | str, name: str = "cost") -> Decimal:
    """Kiểm tra và chuẩn hóa cost trước khi dùng trong policy hoặc quota ledger."""
    return _cost(value, name)


def _add_decimal_exact(left: Decimal, right: Decimal) -> Decimal:
    """Cộng hai Decimal trong giới hạn mà không làm tròn theo context toàn cục."""
    left = _cost(left, "left cost")
    right = _cost(right, "right cost")
    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    if not isinstance(left_tuple.exponent, int) or not isinstance(right_tuple.exponent, int):
        raise ValueError("Cost Decimal phải là giá trị hữu hạn.")
    exponent = min(left_tuple.exponent, right_tuple.exponent)
    left_digits = int("".join(str(digit) for digit in left_tuple.digits) or "0")
    right_digits = int("".join(str(digit) for digit in right_tuple.digits) or "0")
    left_coefficient = left_digits * 10 ** (left_tuple.exponent - exponent)
    right_coefficient = right_digits * 10 ** (right_tuple.exponent - exponent)
    if left_tuple.sign:
        left_coefficient = -left_coefficient
    if right_tuple.sign:
        right_coefficient = -right_coefficient
    coefficient = left_coefficient + right_coefficient
    sign = int(coefficient < 0)
    digits = tuple(int(digit) for digit in str(abs(coefficient)))
    return _cost(Decimal((sign, digits, exponent)), "total cost")


@dataclass(frozen=True, slots=True)
class SourceUsage:
    """Bộ đếm bất biến caller có thể lưu để kiểm quota theo cửa sổ."""

    window_started_at: datetime
    requests_used: int
    bytes_used: int
    cost_used: Decimal | str
    cost_unit: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "window_started_at", _utc(self.window_started_at, "window_started_at")
        )
        for name in ("requests_used", "bytes_used"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} phải là số nguyên không âm.")
        object.__setattr__(self, "cost_used", _cost(self.cost_used, "cost_used"))
        if not isinstance(self.cost_unit, str) or not self.cost_unit.strip():
            raise ValueError("cost_unit không được để trống.")


@dataclass(frozen=True, slots=True)
class SourceBudget:
    """Giới hạn quota và chi phí khai báo cho một cửa sổ nguồn."""

    window: timedelta
    max_requests: int
    max_bytes: int
    max_cost: Decimal | str
    cost_unit: str

    def __post_init__(self) -> None:
        if self.window <= timedelta(0):
            raise ValueError("window phải dương.")
        for name in ("max_requests", "max_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} phải là số nguyên dương.")
        object.__setattr__(self, "max_cost", _cost(self.max_cost, "max_cost"))
        if not isinstance(self.cost_unit, str) or not self.cost_unit.strip():
            raise ValueError("cost_unit không được để trống.")

    def consume(
        self,
        usage: SourceUsage | None,
        *,
        at: datetime,
        byte_count: int,
        cost: Decimal | str,
    ) -> SourceUsage:
        """Tạo bộ đếm mới hoặc từ chối trước khi vượt quota khai báo."""
        instant = _utc(at, "at")
        if type(byte_count) is not int or byte_count < 0:
            raise ValueError("byte_count phải là số nguyên không âm.")
        request_cost = _cost(cost, "cost")
        max_cost = _cost(self.max_cost, "max_cost")
        if request_cost > max_cost:
            raise SourceQuotaExceeded("cost request vượt ngân sách cost.")

        if usage is None:
            current = SourceUsage(instant, 0, 0, Decimal(0), self.cost_unit)
        else:
            if not isinstance(usage, SourceUsage):
                raise ValueError("usage phải là SourceUsage hoặc None.")
            if usage.cost_unit != self.cost_unit:
                raise ValueError("cost_unit của usage không khớp budget.")
            if instant < usage.window_started_at:
                raise ValueError("at không được trước window_started_at.")
            if instant - usage.window_started_at >= self.window:
                current = SourceUsage(instant, 0, 0, Decimal(0), self.cost_unit)
            else:
                current = usage

        requests_used = current.requests_used + 1
        bytes_used = current.bytes_used + byte_count
        cost_used = _add_decimal_exact(_cost(current.cost_used, "usage.cost_used"), request_cost)
        if requests_used > self.max_requests:
            raise SourceQuotaExceeded("request quota đã hết.")
        if bytes_used > self.max_bytes:
            raise SourceQuotaExceeded("byte quota đã hết.")
        if cost_used > max_cost:
            raise SourceQuotaExceeded("cost budget đã hết.")
        return SourceUsage(
            window_started_at=current.window_started_at,
            requests_used=requests_used,
            bytes_used=bytes_used,
            cost_used=cost_used,
            cost_unit=self.cost_unit,
        )


@dataclass(frozen=True, slots=True)
class SourceQuotaSnapshot:
    """Mức sử dụng quota hiện hành, được tính bằng đồng hồ database."""

    source_id: str
    policy_sequence: int
    as_of: datetime
    window_started_at: datetime
    budget: SourceBudget
    usage: SourceUsage


@dataclass(frozen=True, slots=True)
class SourcePolicyRevision:
    """Một snapshot append-only của quyền, endpoint, quota và người duyệt."""

    sequence: int
    policy: SourceRightsPolicy
    publisher: str
    endpoint_url: str
    access_method: str
    auth_scope: str
    attribution: str
    retention_ref: str
    budget: SourceBudget
    created_at: datetime
    actor_id: str
    reason: str
    approved_by: str | None = None
    provider: str | None = None
    product: str | None = None
    venue: str | None = None
    contract: str | None = None
    license_scope: SourceLicenseScope | None = None
    license_evidence_id: UUID | None = None
    license_digest: str | None = None
    license_scope_hash: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("sequence phải là số nguyên dương.")
        if not isinstance(self.policy, SourceRightsPolicy):
            raise ValueError("policy phải là SourceRightsPolicy.")
        for name in (
            "publisher",
            "endpoint_url",
            "auth_scope",
            "attribution",
            "retention_ref",
            "actor_id",
            "reason",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        try:
            parsed_endpoint = urlsplit(self.endpoint_url)
            endpoint_port = parsed_endpoint.port
            endpoint_valid = (
                parsed_endpoint.scheme == "https"
                and parsed_endpoint.hostname is not None
                and parsed_endpoint.username is None
                and parsed_endpoint.password is None
                and "#" not in self.endpoint_url
                and not any(
                    character.isspace() or ord(character) < 32 for character in self.endpoint_url
                )
                and (endpoint_port is None or 1 <= endpoint_port <= 65535)
            )
        except ValueError:
            endpoint_valid = False
        if not endpoint_valid:
            raise ValueError("endpoint_url phải dùng HTTPS và không có fragment.")
        if not isinstance(self.access_method, str) or not _METHOD.fullmatch(self.access_method):
            raise ValueError("access_method không hợp lệ.")
        if not isinstance(self.budget, SourceBudget):
            raise ValueError("budget phải là SourceBudget tường minh.")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.policy.state is SourcePolicyState.VERIFIED:
            if not self.policy.allowed_purposes:
                raise ValueError("Policy verified cần ít nhất một purpose được duyệt.")
            if not isinstance(self.approved_by, str) or not self.approved_by.strip():
                raise ValueError("approved_by bắt buộc cho policy verified.")
            if self.approved_by == self.actor_id:
                raise ValueError("approved_by phải khác actor_id để tách người duyệt.")
            if self.policy.reviewed_at is None or self.policy.reviewed_at > self.created_at:
                raise ValueError("Policy verified cần review không muộn hơn created_at.")
            if self.policy.valid_until is not None and self.policy.valid_until <= self.created_at:
                raise ValueError("valid_until phải sau created_at của policy verified.")
        elif self.approved_by is not None:
            raise ValueError("approved_by chỉ áp dụng cho policy verified.")

        identity = (self.provider, self.product, self.venue, self.contract)
        if any(value is not None for value in identity):
            if not all(
                isinstance(value, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]{0,127}", value)
                for value in identity
            ):
                raise ValueError("identity cần đủ provider/product/venue/contract ASCII chữ hoa.")
        scope = self.license_scope
        if scope is not None:
            if not isinstance(scope, SourceLicenseScope):
                raise ValueError("license_scope phải là SourceLicenseScope.")
            if scope.source_id != self.policy.source_id or identity != (
                scope.provider,
                scope.product,
                scope.venue,
                scope.contract,
            ):
                raise ValueError("license_scope không khớp source identity.")
            if self.attribution != scope.attribution:
                raise ValueError("attribution không khớp license_scope.")
            if not self.policy.allowed_purposes <= set(scope.allowed_purposes):
                raise ValueError("purpose vượt license_scope.")
        evidence = (self.license_evidence_id, self.license_digest, self.license_scope_hash)
        if any(value is not None for value in evidence):
            if (
                not isinstance(self.license_evidence_id, UUID)
                or scope is None
                or not isinstance(self.license_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.license_digest) is None
                or self.license_scope_hash != scope_sha256(scope)
            ):
                raise ValueError("license evidence thiếu hoặc digest/scope_hash không khớp.")

    @property
    def has_license_evidence(self) -> bool:
        """Metadata đủ để đối chiếu ledger; không tự xác nhận tồn tại hoặc quyền dùng."""
        return self.license_scope is not None and self.license_evidence_id is not None

    def require_license_evidence(self) -> None:
        """Chặn ghi VERIFIED mới thiếu metadata; constructor vẫn đọc được lịch sử cũ."""
        if self.state is SourcePolicyState.VERIFIED and not self.has_license_evidence:
            raise ValueError("Policy verified mới cần license evidence digest và scope.")

    @property
    def state(self) -> SourcePolicyState:
        """Trả trạng thái của policy snapshot."""
        return self.policy.state


def source_rights_hash(revision: SourcePolicyRevision) -> str:
    """Băm ổn định các điều kiện quyền và hạn mức đã được duyệt của policy."""
    if type(revision) is not SourcePolicyRevision:
        raise ValueError("revision phải là SourcePolicyRevision.")
    policy = revision.policy
    budget = revision.budget
    payload = {
        "policy_id": policy.policy_id,
        "source_id": policy.source_id,
        "sequence": revision.sequence,
        "state": policy.state.value,
        "allowed_purposes": sorted(purpose.value for purpose in policy.allowed_purposes),
        "evidence_ref": policy.evidence_ref,
        "reviewed_at": policy.reviewed_at.isoformat() if policy.reviewed_at else None,
        "valid_until": policy.valid_until.isoformat() if policy.valid_until else None,
        "approved_by": revision.approved_by,
        "publisher": revision.publisher,
        "endpoint_url": revision.endpoint_url,
        "access_method": revision.access_method,
        "auth_scope": revision.auth_scope,
        "attribution": revision.attribution,
        "retention_ref": revision.retention_ref,
        "budget": {
            "window": [budget.window.days, budget.window.seconds, budget.window.microseconds],
            "max_requests": budget.max_requests,
            "max_bytes": budget.max_bytes,
            "max_cost": str(budget.max_cost),
            "cost_unit": budget.cost_unit,
        },
    }
    if revision.license_evidence_id is not None:
        payload.update(
            {
                "license_evidence_id": str(revision.license_evidence_id),
                "license_digest": revision.license_digest,
                "license_scope_hash": revision.license_scope_hash,
            }
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceRegistryEntry:
    """Lịch sử bất biến của một nguồn; mọi thay đổi tạo policy revision mới."""

    source_id: str
    revisions: tuple[SourcePolicyRevision, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _TOKEN.fullmatch(self.source_id):
            raise ValueError("source_id không hợp lệ.")
        revisions = tuple(self.revisions)
        if not revisions:
            raise ValueError("Registry entry cần ít nhất một revision.")
        if revisions[0].state is not SourcePolicyState.DISCOVERED:
            raise ValueError("Revision đầu tiên phải ở trạng thái discovered.")
        for expected, revision in enumerate(revisions, start=1):
            if not isinstance(revision, SourcePolicyRevision) or revision.sequence != expected:
                raise ValueError("Revision phải liên tiếp và có thứ tự tăng dần.")
            if revision.policy.source_id != self.source_id:
                raise ValueError("policy.source_id không khớp registry entry.")
            if expected > 1:
                previous = revisions[expected - 2]
                for name in ("provider", "product", "venue", "contract"):
                    if getattr(previous, name) is not None and (
                        getattr(previous, name) != getattr(revision, name)
                    ):
                        raise ValueError("source identity bất biến khi đã được khai báo.")
                if revision.created_at < previous.created_at:
                    raise ValueError("created_at không được lùi giữa các revision.")
                if revision.state not in _TRANSITIONS[previous.state]:
                    raise ValueError("Lịch sử registry chứa transition không hợp lệ.")
        policy_ids = [revision.policy.policy_id for revision in revisions]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("policy_id phải khác nhau trong lịch sử append-only.")
        object.__setattr__(self, "revisions", revisions)

    @classmethod
    def discover(
        cls,
        *,
        source_id: str,
        publisher: str,
        endpoint_url: str,
        access_method: str,
        auth_scope: str,
        budget: SourceBudget,
        actor_id: str,
        at: datetime,
        reason: str,
        attribution: str = "not approved",
        retention_ref: str = "metadata-only-pending-review",
    ) -> SourceRegistryEntry:
        """Tạo candidate disabled; không fetch hoặc suy ra quyền từ public access."""
        policy = SourceRightsPolicy(
            policy_id=f"{source_id}:policy:1",
            source_id=source_id,
            state=SourcePolicyState.DISCOVERED,
            allowed_purposes=frozenset(),
        )
        revision = SourcePolicyRevision(
            sequence=1,
            policy=policy,
            publisher=publisher,
            endpoint_url=endpoint_url,
            access_method=access_method,
            auth_scope=auth_scope,
            attribution=attribution,
            retention_ref=retention_ref,
            budget=budget,
            created_at=at,
            actor_id=actor_id,
            reason=reason,
        )
        return cls(source_id=source_id, revisions=(revision,))

    @property
    def current(self) -> SourcePolicyRevision:
        """Snapshot mới nhất; các revision cũ vẫn còn trong tuple history."""
        return self.revisions[-1]

    def permits(self, purpose: SourceUsePurpose, *, at: datetime) -> bool:
        """Chỉ cho phép đúng purpose khi revision hiện tại đã duyệt và còn hạn."""
        current = self.current
        return current.state is SourcePolicyState.VERIFIED and current.policy.permits(
            purpose, at=at
        )

    def transition(
        self,
        *,
        policy_id: str,
        state: SourcePolicyState,
        actor_id: str,
        at: datetime,
        reason: str,
        allowed_purposes: frozenset[SourceUsePurpose] = frozenset(),
        evidence_ref: str | None = None,
        reviewed_at: datetime | None = None,
        valid_until: datetime | None = None,
        approved_by: str | None = None,
        source_id: str | None = None,
        publisher: str | None = None,
        endpoint_url: str | None = None,
        access_method: str | None = None,
        auth_scope: str | None = None,
        attribution: str | None = None,
        retention_ref: str | None = None,
        budget: SourceBudget | None = None,
        provider: str | None = None,
        product: str | None = None,
        venue: str | None = None,
        contract: str | None = None,
        license_scope: SourceLicenseScope | None = None,
        license_evidence_id: UUID | None = None,
        license_digest: str | None = None,
        license_scope_hash: str | None = None,
    ) -> SourceRegistryEntry:
        """Thêm policy revision sau khi kiểm tra trạng thái và dữ liệu phê duyệt."""
        current = self.current
        instant = _utc(at, "at")
        if not isinstance(state, SourcePolicyState):
            raise ValueError("state không hợp lệ.")
        if state not in _TRANSITIONS[current.state]:
            raise SourcePolicyTransitionError(
                f"Không cho phép transition {current.state.value} → {state.value}."
            )
        if policy_id in {revision.policy.policy_id for revision in self.revisions}:
            raise ValueError("policy_id phải mới để giữ lịch sử append-only.")
        if source_id is not None and source_id != self.source_id:
            raise ValueError("source_id không thể đổi trong cùng registry entry.")
        if instant < current.created_at:
            raise ValueError("at không được trước revision hiện tại.")
        if state is SourcePolicyState.EXPIRED:
            expiry = current.policy.valid_until
            if expiry is None or instant < expiry:
                raise SourcePolicyTransitionError("Chỉ chuyển expired sau valid_until.")

        policy = SourceRightsPolicy(
            policy_id=policy_id,
            source_id=self.source_id,
            state=state,
            allowed_purposes=allowed_purposes,
            evidence_ref=evidence_ref,
            reviewed_at=reviewed_at,
            valid_until=valid_until,
        )
        revision = SourcePolicyRevision(
            sequence=current.sequence + 1,
            policy=policy,
            provider=provider if provider is not None else current.provider,
            product=product if product is not None else current.product,
            venue=venue if venue is not None else current.venue,
            contract=contract if contract is not None else current.contract,
            license_scope=license_scope,
            license_evidence_id=license_evidence_id,
            license_digest=license_digest,
            license_scope_hash=license_scope_hash,
            publisher=publisher if publisher is not None else current.publisher,
            endpoint_url=endpoint_url if endpoint_url is not None else current.endpoint_url,
            access_method=access_method if access_method is not None else current.access_method,
            auth_scope=auth_scope if auth_scope is not None else current.auth_scope,
            attribution=attribution if attribution is not None else current.attribution,
            retention_ref=retention_ref if retention_ref is not None else current.retention_ref,
            budget=budget if budget is not None else current.budget,
            created_at=instant,
            actor_id=actor_id,
            reason=reason,
            approved_by=approved_by,
        )
        return SourceRegistryEntry(self.source_id, (*self.revisions, revision))


@dataclass(frozen=True, slots=True)
class ObservedSourcePolicy:
    """Policy history kèm receipt cho biết từng revision đã được quan sát sau commit."""

    history: SourceRegistryEntry
    known_at: Mapping[str, datetime]

    def __post_init__(self) -> None:
        if type(self.history) is not SourceRegistryEntry:
            raise ValueError("history phải là SourceRegistryEntry.")
        receipts = dict(self.known_at)
        expected_ids = {revision.policy.policy_id for revision in self.history.revisions}
        if set(receipts) != expected_ids:
            raise ValueError("known_at phải có receipt cho mọi policy revision.")
        normalized: dict[str, datetime] = {}
        previous: datetime | None = None
        for revision in self.history.revisions:
            policy = revision.policy
            known = _utc(receipts[policy.policy_id], "known_at")
            if known < revision.created_at or (policy.reviewed_at and known < policy.reviewed_at):
                raise ValueError("known_at không được trước policy revision hoặc review.")
            if previous is not None and known < previous:
                raise ValueError("known_at không được lùi giữa các policy revision.")
            normalized[policy.policy_id] = known
            previous = known
        object.__setattr__(self, "known_at", MappingProxyType(normalized))
