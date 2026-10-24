from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


class SecretAccessError(PermissionError):
    """Báo từ chối thao tác với tham chiếu bí mật."""


@dataclass(frozen=True, slots=True)
class SecretLease:
    reference: str
    workspace_id: UUID
    expires_in_seconds: int
    value: str = field(repr=False)


class SecretProvider(Protocol):
    """Hợp đồng lưu và phân phối bí mật theo workspace và danh tính process."""

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str: ...

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str: ...

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None: ...

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease: ...
