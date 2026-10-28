"""Kho lưu phiên bản cấu hình workspace với tenant scope tường minh."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    RiskProfileVersion,
    Workspace,
    WorkspaceSettingsVersion,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings


class SettingsVersionConflict(RuntimeError):
    """Bản nháp đã thay đổi hoặc không còn ở trạng thái có thể sửa."""


class SettingsReferenceNotFound(ValueError):
    """Cấu hình trỏ tới hồ sơ rủi ro không tồn tại trong workspace hiện tại."""


def canonical_settings(settings: WorkspaceSettings) -> dict[str, Any]:
    """Trả cấu hình JSON ổn định, dùng cùng biểu diễn cho lưu trữ và băm."""
    return settings.model_dump(mode="json")


def settings_hash(settings_json: dict[str, Any]) -> str:
    """Tính SHA-256 trên JSON UTF-8 đã sắp xếp khóa và chuẩn hóa dấu cách."""
    canonical = json.dumps(settings_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkspaceSettingsRepository:
    """Các thao tác DB luôn nhận workspace_id và không tự commit transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_draft(
        self,
        *,
        workspace_id: UUID,
        settings: WorkspaceSettings,
        created_by: UUID,
        parent_version_id: UUID | None = None,
    ) -> WorkspaceSettingsVersion:
        await self._lock_workspace(workspace_id)
        await self._validate_risk_reference(workspace_id, settings.risk.profile_version_id)
        if parent_version_id is not None:
            parent = await self._session.scalar(
                select(WorkspaceSettingsVersion).where(
                    WorkspaceSettingsVersion.workspace_id == workspace_id,
                    WorkspaceSettingsVersion.id == parent_version_id,
                    WorkspaceSettingsVersion.status == "PUBLISHED",
                )
            )
            if parent is None:
                raise SettingsReferenceNotFound(
                    "Phiên bản cha không tồn tại hoặc chưa được publish."
                )

        next_version = await self._session.scalar(
            select(func.coalesce(func.max(WorkspaceSettingsVersion.version), 0) + 1).where(
                WorkspaceSettingsVersion.workspace_id == workspace_id
            )
        )
        payload = canonical_settings(settings)
        record = WorkspaceSettingsVersion(
            workspace_id=workspace_id,
            version=int(next_version or 1),
            parent_version_id=parent_version_id,
            schema_version=settings.schema_version,
            settings_json=payload,
            settings_hash=settings_hash(payload),
            status="DRAFT",
            revision=1,
            created_by=created_by,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def update_draft(
        self,
        *,
        workspace_id: UUID,
        draft_id: UUID,
        expected_revision: int,
        settings: WorkspaceSettings,
    ) -> WorkspaceSettingsVersion:
        await self._validate_risk_reference(workspace_id, settings.risk.profile_version_id)
        payload = canonical_settings(settings)
        updated_id = await self._session.scalar(
            update(WorkspaceSettingsVersion)
            .where(
                WorkspaceSettingsVersion.workspace_id == workspace_id,
                WorkspaceSettingsVersion.id == draft_id,
                WorkspaceSettingsVersion.status == "DRAFT",
                WorkspaceSettingsVersion.revision == expected_revision,
            )
            .values(
                settings_json=payload,
                settings_hash=settings_hash(payload),
                schema_version=settings.schema_version,
                revision=expected_revision + 1,
            )
            .returning(WorkspaceSettingsVersion.id)
        )
        if updated_id is None:
            raise SettingsVersionConflict("Bản nháp đã đổi hoặc không còn ở trạng thái DRAFT.")
        record = await self._get_version(workspace_id, draft_id)
        assert record is not None
        return record

    async def publish_draft(
        self,
        *,
        workspace_id: UUID,
        draft_id: UUID,
        expected_revision: int,
        approved_by: UUID,
        expected_parent_version_id: UUID | None = None,
        effective_at: datetime | None = None,
    ) -> WorkspaceSettingsVersion:
        await self._lock_workspace(workspace_id)
        draft = await self._get_version(workspace_id, draft_id)
        if draft is None or draft.status != "DRAFT" or draft.revision != expected_revision:
            raise SettingsVersionConflict("Bản nháp đã đổi hoặc không còn ở trạng thái DRAFT.")
        if draft.parent_version_id != expected_parent_version_id:
            raise SettingsVersionConflict("Phiên bản cha của bản nháp không khớp.")
        current = await self.get_effective(workspace_id=workspace_id)
        current_id = current.id if current is not None else None
        if current_id != expected_parent_version_id:
            raise SettingsVersionConflict("Phiên bản cấu hình hiện hành đã thay đổi.")

        # Fix round 2 (Minor 3): đóng dấu bằng đồng hồ DB (cùng nguồn clock_timestamp() mà
        # coordinator so sánh), không bằng đồng hồ process admin.
        now = func.statement_timestamp()
        updated_id = await self._session.scalar(
            update(WorkspaceSettingsVersion)
            .where(
                WorkspaceSettingsVersion.workspace_id == workspace_id,
                WorkspaceSettingsVersion.id == draft_id,
                WorkspaceSettingsVersion.status == "DRAFT",
                WorkspaceSettingsVersion.revision == expected_revision,
                WorkspaceSettingsVersion.parent_version_id == expected_parent_version_id,
            )
            .values(
                status="PUBLISHED",
                approved_by=approved_by,
                published_at=now,
                effective_at=effective_at or now,
            )
            .returning(WorkspaceSettingsVersion.id)
        )
        if updated_id is None:
            raise SettingsVersionConflict("Bản nháp đã đổi hoặc không còn ở trạng thái DRAFT.")
        record = await self._get_version(workspace_id, draft_id)
        assert record is not None
        return record

    async def get_effective(
        self, *, workspace_id: UUID, as_of: datetime | None = None
    ) -> WorkspaceSettingsVersion | None:
        # Không có as_of: so với đồng hồ DB, cùng nguồn với statement_timestamp() khi publish.
        point = as_of if as_of is not None else func.clock_timestamp()
        return await self._session.scalar(
            select(WorkspaceSettingsVersion)
            .where(
                WorkspaceSettingsVersion.workspace_id == workspace_id,
                WorkspaceSettingsVersion.status == "PUBLISHED",
                WorkspaceSettingsVersion.effective_at <= point,
            )
            .order_by(WorkspaceSettingsVersion.version.desc())
            .limit(1)
        )

    async def load_effective_config(
        self, *, workspace_id: UUID, as_of: datetime | None = None
    ) -> PublishedWorkspaceSettings | None:
        """Tải cấu hình effective, kiểm tra hash rồi trả phiên bản runtime đã ghim."""
        record = await self.get_effective(workspace_id=workspace_id, as_of=as_of)
        if record is None:
            return None
        if record.status != "PUBLISHED" or record.effective_at is None:
            raise SettingsVersionConflict(
                "Runtime chỉ được nạp settings đã publish và có hiệu lực."
            )
        if settings_hash(record.settings_json) != record.settings_hash:
            raise SettingsVersionConflict("Hash settings đã lưu không khớp nội dung cấu hình.")
        settings = WorkspaceSettings.model_validate(record.settings_json)
        return PublishedWorkspaceSettings(
            workspace_id=record.workspace_id,
            settings_version_id=record.id,
            settings_hash=record.settings_hash,
            settings=settings,
            effective_at=record.effective_at,
        )

    async def list_versions(self, *, workspace_id: UUID) -> list[WorkspaceSettingsVersion]:
        records = await self._session.scalars(
            select(WorkspaceSettingsVersion)
            .where(WorkspaceSettingsVersion.workspace_id == workspace_id)
            .order_by(WorkspaceSettingsVersion.version.desc())
        )
        return list(records)

    async def _get_version(
        self, workspace_id: UUID, version_id: UUID
    ) -> WorkspaceSettingsVersion | None:
        return await self._session.scalar(
            select(WorkspaceSettingsVersion).where(
                WorkspaceSettingsVersion.workspace_id == workspace_id,
                WorkspaceSettingsVersion.id == version_id,
            )
        )

    async def _lock_workspace(self, workspace_id: UUID) -> None:
        workspace = await self._session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise SettingsReferenceNotFound("Workspace không tồn tại.")

    async def lock_workspace(self, workspace_id: UUID) -> None:
        """Tuần tự hóa runtime/publish mà không cần runtime role cập nhật workspace."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:workspace_lock, 0))"),
            {"workspace_lock": f"workspace-settings:{workspace_id}"},
        )

    async def lock_workspace_risk(self, workspace_id: UUID) -> None:
        """Tuần tự hóa publish risk và reservation trên cùng workspace."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:risk_lock, 0))"),
            {"risk_lock": f"workspace-risk:{workspace_id}"},
        )

    async def _validate_risk_reference(self, workspace_id: UUID, profile_id: UUID) -> None:
        profile = await self._session.scalar(
            select(RiskProfileVersion).where(
                RiskProfileVersion.workspace_id == workspace_id,
                RiskProfileVersion.id == profile_id,
            )
        )
        if profile is None:
            raise SettingsReferenceNotFound("Risk profile không tồn tại trong workspace hiện tại.")
