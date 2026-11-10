"""Ca sử dụng phát sự kiện khi policy nguồn đã tới hạn theo đồng hồ cơ sở dữ liệu."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class ExpiredSourcePolicyEventWriter(Protocol):
    """Port ghi batch expiry bằng DB clock trong transaction của caller."""

    async def enqueue_expired_policy_events(
        self, *, workspace_id: UUID, batch_size: int
    ) -> int: ...


class SourcePolicyExpiryScanner:
    """Phát tối đa một batch event, không tự đổi trạng thái policy hoặc dùng app clock."""

    def __init__(self, writer: ExpiredSourcePolicyEventWriter) -> None:
        self._writer = writer

    async def scan_workspace(self, workspace_id: UUID, *, batch_size: int = 100) -> int:
        """Quét workspace tường minh; caller lặp theo lịch cho tới khi batch trả về 0."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("batch_size phải là số nguyên từ 1 đến 500.")
        emitted = await self._writer.enqueue_expired_policy_events(
            workspace_id=workspace_id,
            batch_size=batch_size,
        )
        if type(emitted) is not int or not 0 <= emitted <= batch_size:
            raise RuntimeError("repository trả số expiry event ngoài batch size.")
        return emitted
