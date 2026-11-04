"""Cổng giao dịch sàn (ExchangeGateway) trung lập và hợp đồng thăm dò sẵn sàng.

Định nghĩa các kiểu dữ liệu, báo cáo readiness độc lập và giao thức gateway
để tách biệt logic nghiệp vụ khỏi chi tiết giao vận sàn cụ thể.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class ReadinessStatus(StrEnum):
    """Trạng thái sẵn sàng của từng thành phần hoặc gateway tổng thể."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class ProbeCategory(StrEnum):
    """Phân loại thăm dò readiness độc lập theo ranh giới năng lực API."""

    PUBLIC_DATA = "PUBLIC_DATA"
    USER_DATA = "USER_DATA"
    TRADE_CAPABILITY = "TRADE_CAPABILITY"
    STREAM_AND_CLOCK = "STREAM_AND_CLOCK"


@dataclass(frozen=True, slots=True)
class ReadinessProbeResult:
    """Kết quả kiểm tra của một probe readiness đơn lẻ."""

    category: ProbeCategory
    status: ReadinessStatus
    message: str
    latency_ms: float | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GatewayReadinessReport:
    """Báo cáo readiness tổng hợp từ các probes độc lập, không che giấu lỗi."""

    overall_status: ReadinessStatus
    is_ready: bool
    probes: dict[ProbeCategory, ReadinessProbeResult]
    checked_at: datetime
    environment: str
    account_id: str


@dataclass(frozen=True, slots=True)
class GatewayEndpoints:
    """Cặp địa chỉ REST và WebSocket đã được xác thực an toàn."""

    rest_base_url: str
    ws_base_url: str


@dataclass(frozen=True, slots=True)
class SecretReference:
    """Tham chiếu bí mật an toàn; ẩn reference_id chi tiết khỏi log và repr."""

    reference_id: str = field(repr=False)
    key_alias: str

    def __repr__(self) -> str:
        masked_ref = f"***{self.reference_id[-6:]}" if len(self.reference_id) > 6 else "***"
        return f"SecretReference(key_alias='{self.key_alias}', ref='{masked_ref}')"

    def __str__(self) -> str:
        return self.__repr__()


class ExchangeGateway(Protocol):
    """Hợp đồng cổng giao dịch sàn trung lập với phạm vi account và mode tường minh."""

    @property
    def account_id(self) -> str:
        """Định danh tài khoản sở hữu gateway."""
        ...

    @property
    def mode(self) -> str:
        """Chế độ thực thi của gateway (DEMO, PAPER, LIVE)."""
        ...

    @property
    def endpoints(self) -> GatewayEndpoints:
        """Các endpoint giao tiếp đã qua kiểm duyệt allowlist."""
        ...

    def check_readiness(self) -> GatewayReadinessReport:
        """Thực hiện thăm dò toàn bộ probes và trả về báo cáo phân loại chi tiết."""
        ...
