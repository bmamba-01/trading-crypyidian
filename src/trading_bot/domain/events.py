import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "apikey",
        "apisecret",
        "authorization",
        "credential",
        "credentials",
        "password",
        "privatetelegrammessage",
        "privatekey",
        "rawbody",
        "rawsourcebody",
        "secret",
        "token",
    }
)


class SensitiveEventPayloadError(ValueError):
    """Lỗi khi payload có trường thường chứa thông tin bí mật hoặc nội dung riêng tư."""


def state_hash(value: Any) -> str:
    """Băm trạng thái JSON chuẩn hóa để audit không lưu bản chụp dữ liệu."""
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Trạng thái audit phải chuyển được thành JSON hợp lệ") from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Phong bì sự kiện bất biến, có định danh và thời điểm phục vụ truy vết."""

    event_type: str
    workspace_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    occurred_at: datetime
    correlation_id: UUID
    idempotency_key: str
    schema_version: int
    payload: Mapping[str, Any]
    event_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.event_type.strip() or not self.aggregate_type.strip():
            raise ValueError("Loại sự kiện và loại aggregate không được để trống")
        if not self.idempotency_key.strip():
            raise ValueError("Idempotency key không được để trống")
        if self.schema_version < 1:
            raise ValueError("Schema version phải lớn hơn 0")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Thời điểm sự kiện phải có timezone")
        if not isinstance(self.payload, Mapping):
            raise TypeError("Payload sự kiện phải là mapping")
        _validate_payload(self.payload, "payload")
        try:
            json.dumps(self.payload, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Payload phải chuyển được thành JSON hợp lệ") from error


def _validate_payload(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Tên trường payload phải là chuỗi: {path}")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in SENSITIVE_PAYLOAD_KEYS or normalized.endswith(
                ("apikey", "apisecret", "credential", "password", "privatekey", "secret", "token")
            ):
                raise SensitiveEventPayloadError(
                    f"Payload không được chứa trường nhạy cảm: {path}.{key}"
                )
            _validate_payload(nested, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, nested in enumerate(value):
            _validate_payload(nested, f"{path}[{index}]")
