"""Dịch vụ Application lưu và đọc báo cáo kiểm định scalping SIG-5 từ PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.scalping_validation_reports import (
    ScalpingValidationReportRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize


class ScalpingValidationReportError(Exception):
    """Lỗi nghiệp vụ hoặc phân quyền khi đọc/lưu báo cáo kiểm định SIG-5."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def verify_validation_report_hash(report_body: dict[str, Any]) -> None:
    """Xác minh ``validation_hash`` khớp với nội dung canonical của báo cáo SIG-5."""
    required = {
        "costs_version",
        "release_id",
        "release_content_hash",
        "dataset_hashes",
        "symbol",
        "evaluation_start",
        "range_end",
        "scenarios",
        "reports",
        "verdict",
        "validation_hash",
    }
    missing = sorted(required - set(report_body))
    if missing:
        raise ScalpingValidationReportError(
            422,
            "VALIDATION_REPORT_INCOMPLETE",
            f"Báo cáo kiểm định thiếu trường bắt buộc: {', '.join(missing)}.",
        )
    expected_hash = str(report_body["validation_hash"])
    canonical_body = {key: value for key, value in report_body.items() if key != "validation_hash"}
    payload_bytes = json.dumps(
        canonical_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    computed = hashlib.sha256(payload_bytes).hexdigest()
    if computed != expected_hash:
        raise ScalpingValidationReportError(
            422,
            "VALIDATION_HASH_MISMATCH",
            "Mã băm validation_hash không khớp nội dung báo cáo kiểm định.",
        )


class ScalpingValidationReportService:
    """Quản lý lưu trữ bền vững và truy vấn báo cáo kiểm định SIG-5 theo workspace."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = ScalpingValidationReportRepository,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work = unit_of_work
        self._repository = repository

    def _require(self, principal: Principal, permission: Permission) -> None:
        try:
            authorize(principal, permission, raise_error=True)
        except PermissionError as error:
            raise ScalpingValidationReportError(
                403,
                "VALIDATION_REPORT_FORBIDDEN",
                f"Thiếu quyền {permission.value} cho báo cáo kiểm định.",
            ) from error

    async def save_report(
        self, principal: Principal, *, report_body: dict[str, Any]
    ) -> dict[str, Any]:
        self._require(principal, Permission.STRATEGY_WRITE)
        verify_validation_report_hash(report_body)
        now = datetime.now(UTC)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repo = cast(ScalpingValidationReportRepository, self._repository(unit.session))
            return await repo.save_report(
                workspace_id=principal.workspace_id,
                report_body=report_body,
                actor_user_id=principal.user_id,
                now=now,
            )

    async def list_reports(
        self,
        principal: Principal,
        *,
        release_id: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._require(principal, Permission.WORKSPACE_READ)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repo = cast(ScalpingValidationReportRepository, self._repository(unit.session))
            return await repo.list_reports(
                workspace_id=principal.workspace_id,
                release_id=release_id,
                symbol=symbol,
                limit=limit,
            )
