"""Ca sử dụng quản lý bản nháp và phiên bản cấu hình workspace."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    RiskProfileVersion,
    TradingAccount,
    WorkspaceSettingsVersion,
)
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord, TradeRecord
from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
from trading_bot.adapters.database.repositories.workspace_settings import (
    SettingsReferenceNotFound,
    SettingsVersionConflict,
    WorkspaceSettingsRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    risk_limits_from_profile,
)
from trading_bot.application.workspace_risk import assess_risk_capacity
from trading_bot.auth.bot_policy import WORKSPACE_BOT_ROLES
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.config.workspace_settings import WorkspaceSettings
from trading_bot.domain.events import EventEnvelope
from trading_bot.domain.risk.workspace_budget import CapacityStatus
from trading_bot.domain.strategy.registry import StrategyRegistry, UnknownStrategyReleaseError

_SECRET_KEY = re.compile(r"(?:secret|token|password|credential|api[_-]?key|cookie)", re.I)


class SettingsAdminError(Exception):
    """Lỗi nghiệp vụ có mã HTTP ổn định cho API quản trị."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class SettingsAdminService:
    """Thực hiện thay đổi cấu hình trong cùng transaction với audit và outbox."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_current(self, principal: Principal) -> dict[str, Any] | None:
        async with self._uow(principal) as unit:
            record = await WorkspaceSettingsRepository(unit.session).get_effective(
                workspace_id=principal.workspace_id
            )
            return serialize_version(record)

    async def list_versions(self, principal: Principal) -> list[dict[str, Any]]:
        async with self._uow(principal) as unit:
            records = await WorkspaceSettingsRepository(unit.session).list_versions(
                workspace_id=principal.workspace_id
            )
            return [
                serialized
                for record in records
                if (serialized := serialize_version(record)) is not None
            ]

    async def create_draft(
        self,
        principal: Principal,
        *,
        settings: WorkspaceSettings | None,
        expected_parent_version_id: UUID | None,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        async with self._uow(principal, request_id=request_id) as unit:
            repository = WorkspaceSettingsRepository(unit.session)
            current = await repository.get_effective(workspace_id=principal.workspace_id)
            if current is not None and expected_parent_version_id != current.id:
                raise SettingsAdminError(409, "Phiên bản cấu hình hiện hành đã thay đổi.")
            if current is None and expected_parent_version_id is not None:
                raise SettingsAdminError(409, "Phiên bản cha không còn hiện hành.")
            selected_settings = settings
            if selected_settings is None:
                if current is None:
                    raise SettingsAdminError(409, "Cần cung cấp cấu hình đầu tiên cho workspace.")
                selected_settings = WorkspaceSettings.model_validate(current.settings_json)
            self._require_risk_change_access(principal, current, selected_settings)
            try:
                record = await repository.create_draft(
                    workspace_id=principal.workspace_id,
                    settings=selected_settings,
                    created_by=principal.membership_id,
                    parent_version_id=current.id if current else None,
                )
            except SettingsReferenceNotFound as error:
                raise SettingsAdminError(422, str(error)) from error
            self._record_change(unit, "settings.draft_created", record, None)
            return serialize_version(record) or {}

    async def update_draft(
        self,
        principal: Principal,
        *,
        draft_id: UUID,
        expected_revision: int,
        settings: WorkspaceSettings,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        async with self._uow(principal, request_id=request_id) as unit:
            repository = WorkspaceSettingsRepository(unit.session)
            records = await repository.list_versions(workspace_id=principal.workspace_id)
            draft = next((item for item in records if item.id == draft_id), None)
            if draft is None or draft.status != "DRAFT":
                raise SettingsAdminError(404, "Không tìm thấy bản nháp có thể sửa.")
            if draft.revision != expected_revision:
                raise SettingsAdminError(409, "Bản nháp đã thay đổi; hãy tải lại trước khi sửa.")
            parent = next((item for item in records if item.id == draft.parent_version_id), None)
            parent_settings = (
                WorkspaceSettings.model_validate(parent.settings_json) if parent else None
            )
            self._require_risk_change_access(principal, parent, settings, parent_settings)
            try:
                updated = await repository.update_draft(
                    workspace_id=principal.workspace_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    settings=settings,
                )
            except SettingsReferenceNotFound as error:
                raise SettingsAdminError(422, str(error)) from error
            except SettingsVersionConflict as error:
                raise SettingsAdminError(409, str(error)) from error
            self._record_change(unit, "settings.draft_updated", updated, draft.settings_json)
            return serialize_version(updated) or {}

    async def validate_draft(self, principal: Principal, *, draft_id: UUID) -> dict[str, Any]:
        async with self._uow(principal) as unit:
            record = await self._find_version(
                WorkspaceSettingsRepository(unit.session), principal.workspace_id, draft_id
            )
            if record.status != "DRAFT":
                raise SettingsAdminError(409, "Chỉ có thể kiểm định bản nháp.")
            try:
                settings = WorkspaceSettings.model_validate(record.settings_json)
            except Exception as error:
                return {"valid": False, "errors": _validation_errors(error), "warnings": []}
            try:
                StrategyRegistry().get_release(settings.strategy.default_release)
            except UnknownStrategyReleaseError:
                return {
                    "valid": False,
                    "errors": [
                        {
                            "field": "strategy.default_release",
                            "message": "Strategy release không tồn tại trong registry hiện hành.",
                        }
                    ],
                    "warnings": [],
                }
            risk_profile = await unit.session.scalar(
                select(RiskProfileVersion).where(
                    RiskProfileVersion.workspace_id == principal.workspace_id,
                    RiskProfileVersion.id == settings.risk.profile_version_id,
                )
            )
            if risk_profile is None:
                return {
                    "valid": False,
                    "errors": [
                        {
                            "field": "risk.profile_version_id",
                            "message": "Risk profile không tồn tại trong workspace hiện tại.",
                        }
                    ],
                    "warnings": [],
                }
            try:
                risk_limits_from_profile(risk_profile)
            except RiskProfileRuntimeError as error:
                return {
                    "valid": False,
                    "errors": [
                        {
                            "field": "risk.profile_version_id",
                            "message": str(error),
                        }
                    ],
                    "warnings": [],
                }
            return {"valid": True, "errors": [], "warnings": []}

    async def publish_draft(
        self,
        principal: Principal,
        *,
        draft_id: UUID,
        expected_revision: int,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        async with self._uow(principal, request_id=request_id) as unit:
            repository = WorkspaceSettingsRepository(unit.session)
            await repository.lock_workspace(principal.workspace_id)
            record = await self._find_version(repository, principal.workspace_id, draft_id)
            if record.status != "DRAFT":
                raise SettingsAdminError(409, "Bản nháp không còn ở trạng thái có thể publish.")
            settings = WorkspaceSettings.model_validate(record.settings_json)
            try:
                StrategyRegistry().get_release(settings.strategy.default_release)
            except UnknownStrategyReleaseError as error:
                raise SettingsAdminError(
                    422, "Strategy release không tồn tại trong registry hiện hành."
                ) from error
            records = await repository.list_versions(workspace_id=principal.workspace_id)
            parent = next((item for item in records if item.id == record.parent_version_id), None)
            self._require_risk_change_access(
                principal,
                parent,
                settings,
                WorkspaceSettings.model_validate(parent.settings_json) if parent else None,
            )
            current = await repository.get_effective(workspace_id=principal.workspace_id)
            if (current.id if current else None) != record.parent_version_id:
                raise SettingsAdminError(
                    409, "Bản nháp dựa trên cấu hình cũ; hãy tạo bản nháp mới."
                )
            current_risk = (
                WorkspaceSettings.model_validate(current.settings_json).risk if current else None
            )
            risk_changed = current_risk != settings.risk
            if risk_changed:
                await repository.lock_workspace_risk(principal.workspace_id)
            await self._guard_paper_policy_change(
                unit,
                workspace_id=principal.workspace_id,
                current_settings=(
                    WorkspaceSettings.model_validate(current.settings_json) if current else None
                ),
                next_settings=settings,
                principal=principal,
            )
            risk_profile = await unit.session.scalar(
                select(RiskProfileVersion).where(
                    RiskProfileVersion.workspace_id == principal.workspace_id,
                    RiskProfileVersion.id == settings.risk.profile_version_id,
                )
            )
            if risk_profile is None:
                raise SettingsAdminError(
                    422, "Risk profile không tồn tại trong workspace hiện tại."
                )
            try:
                runtime_profile = risk_limits_from_profile(risk_profile)
            except RiskProfileRuntimeError as error:
                raise SettingsAdminError(422, str(error)) from error
            try:
                published = await repository.publish_draft(
                    workspace_id=principal.workspace_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    approved_by=principal.membership_id,
                    expected_parent_version_id=record.parent_version_id,
                )
            except SettingsVersionConflict as error:
                raise SettingsAdminError(409, str(error)) from error
            self._record_change(
                unit,
                "settings.published",
                published,
                current.settings_json if current else None,
            )
            response = serialize_version(published) or {}
            if risk_changed:
                response["capacity"] = await self._record_capacity(
                    unit,
                    principal,
                    published,
                    settings,
                    runtime_profile.limits.account_max_open_risk_fraction,
                )
            return response

    async def rollback(
        self,
        principal: Principal,
        *,
        version: int,
        reason: str,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        async with self._uow(principal, request_id=request_id) as unit:
            repository = WorkspaceSettingsRepository(unit.session)
            await repository.lock_workspace(principal.workspace_id)
            records = await repository.list_versions(workspace_id=principal.workspace_id)
            target = next((item for item in records if item.version == version), None)
            if target is None or target.status != "PUBLISHED":
                raise SettingsAdminError(404, "Không tìm thấy phiên bản đã publish để rollback.")
            current = await repository.get_effective(workspace_id=principal.workspace_id)
            settings = WorkspaceSettings.model_validate(target.settings_json)
            current_risk = (
                WorkspaceSettings.model_validate(current.settings_json).risk if current else None
            )
            risk_changed = current_risk != settings.risk
            if risk_changed:
                await repository.lock_workspace_risk(principal.workspace_id)
            await self._guard_paper_policy_change(
                unit,
                workspace_id=principal.workspace_id,
                current_settings=(
                    WorkspaceSettings.model_validate(current.settings_json) if current else None
                ),
                next_settings=settings,
                principal=principal,
            )
            self._require_risk_change_access(
                principal,
                current,
                settings,
                WorkspaceSettings.model_validate(current.settings_json) if current else None,
            )
            try:
                draft = await repository.create_draft(
                    workspace_id=principal.workspace_id,
                    settings=settings,
                    created_by=principal.membership_id,
                    parent_version_id=current.id if current else None,
                )
            except SettingsReferenceNotFound as error:
                raise SettingsAdminError(422, str(error)) from error
            try:
                published = await repository.publish_draft(
                    workspace_id=principal.workspace_id,
                    draft_id=draft.id,
                    expected_revision=draft.revision,
                    approved_by=principal.membership_id,
                    expected_parent_version_id=current.id if current else None,
                )
            except SettingsVersionConflict as error:
                raise SettingsAdminError(409, str(error)) from error
            unit.record_audit(
                action="settings.rolled_back",
                target_type="workspace_settings_version",
                target_id=published.id,
                reason=reason,
                before_state=current.settings_json if current else None,
                after_state=published.settings_json,
            )
            self._enqueue(unit, "settings.rolled_back", published, {"version": version})
            response = serialize_version(published) or {}
            if risk_changed:
                runtime_profile = await self._load_runtime_profile(unit, principal, settings)
                response["capacity"] = await self._record_capacity(
                    unit,
                    principal,
                    published,
                    settings,
                    runtime_profile.limits.account_max_open_risk_fraction,
                )
            return response

    @staticmethod
    async def _load_runtime_profile(
        unit: UnitOfWork, principal: Principal, settings: WorkspaceSettings
    ) -> Any:
        record = await unit.session.scalar(
            select(RiskProfileVersion).where(
                RiskProfileVersion.workspace_id == principal.workspace_id,
                RiskProfileVersion.id == settings.risk.profile_version_id,
            )
        )
        if record is None:
            raise SettingsAdminError(422, "Risk profile không tồn tại trong workspace hiện tại.")
        try:
            return risk_limits_from_profile(record)
        except RiskProfileRuntimeError as error:
            raise SettingsAdminError(422, str(error)) from error

    @staticmethod
    async def _record_capacity(
        unit: UnitOfWork,
        principal: Principal,
        published: WorkspaceSettingsVersion,
        settings: WorkspaceSettings,
        account_fraction: Decimal,
    ) -> dict[str, Any]:
        """I5/contract §6: đọc exposure dưới L1+L2 đang giữ, ghi breach nếu cap mới thấp hơn.

        Publish vẫn có hiệu lực; trạng thái `CAPACITY_BREACH_AFTER_TIGHTENING` được audit và đưa
        vào outbox, entry mới bị coordinator chặn tới khi exposure xuống dưới cap; protective
        monitoring giữ nguyên, không auto-flatten. Role thay đổi risk (owner/risk_admin) thấy mọi
        bot qua RLS nên phép cộng theo account là toàn workspace.
        """
        if not principal.roles & WORKSPACE_BOT_ROLES:
            raise SettingsAdminError(
                403, "Cần role có quyền xem toàn bộ bot để đánh giá capacity sau khi siết cap."
            )
        assessment = await assess_risk_capacity(
            unit.session,
            workspace_id=principal.workspace_id,
            workspace_fraction=settings.risk.workspace_max_open_risk_fraction,
            account_fraction=account_fraction,
            max_mark_age=timedelta(seconds=settings.risk.max_mark_age_seconds),
            as_of=datetime.now(UTC),
        )
        payload = assessment.payload()
        action = (
            "risk.capacity_breach_after_tightening"
            if assessment.status is CapacityStatus.CAPACITY_BREACH_AFTER_TIGHTENING
            else "risk.capacity_assessed"
        )
        unit.record_audit(
            action=action,
            target_type="workspace_settings_version",
            target_id=published.id,
            reason=assessment.summary(),
            after_state=payload,
        )
        SettingsAdminService._enqueue(
            unit, action, published, {"version": published.version, "capacity": payload}
        )
        return payload

    @staticmethod
    async def _guard_paper_policy_change(
        unit: UnitOfWork,
        *,
        workspace_id: UUID,
        current_settings: WorkspaceSettings | None,
        next_settings: WorkspaceSettings,
        principal: Principal,
    ) -> None:
        """Không đổi fill/recovery profile khi checkpoint còn phụ thuộc lineage cũ."""
        if current_settings is None or (
            current_settings.paper_execution == next_settings.paper_execution
        ):
            return
        # RLS bot-scoped chỉ cho role workspace-wide thấy mọi bot; role khác sẽ đếm thiếu.
        if not principal.roles & WORKSPACE_BOT_ROLES:
            raise SettingsAdminError(
                403, "Cần role có quyền xem toàn bộ bot để đổi PAPER execution profile."
            )
        active_intents = await unit.session.scalar(
            select(func.count(OrderIntentRecord.id)).where(
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.mode == "PAPER",
                OrderIntentRecord.status.in_(("ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN")),
            )
        )
        open_trades = await unit.session.scalar(
            select(func.count(TradeRecord.id)).where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.status == "OPEN",
            )
        )
        active_reservations = await unit.session.scalar(
            select(func.count(RiskReservationRecord.id))
            .join(
                TradingAccount,
                (TradingAccount.workspace_id == RiskReservationRecord.workspace_id)
                & (TradingAccount.id == RiskReservationRecord.account_id),
            )
            .where(
                RiskReservationRecord.workspace_id == workspace_id,
                TradingAccount.mode == "PAPER",
                RiskReservationRecord.status.in_(("RESERVED", "UNKNOWN")),
            )
        )
        if active_intents or open_trades or active_reservations:
            raise SettingsAdminError(
                409,
                "Không thể đổi PAPER execution/recovery profile khi còn intent, trade hoặc "
                "risk reservation chưa kết thúc.",
            )

    def _uow(self, principal: Principal, *, request_id: UUID | None = None) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        )

    async def _find_version(
        self, repository: WorkspaceSettingsRepository, workspace_id: UUID, version_id: UUID
    ) -> WorkspaceSettingsVersion:
        record = next(
            (
                item
                for item in await repository.list_versions(workspace_id=workspace_id)
                if item.id == version_id
            ),
            None,
        )
        if record is None:
            raise SettingsAdminError(404, "Không tìm thấy phiên bản cấu hình.")
        return record

    @staticmethod
    def _require_risk_change_access(
        principal: Principal,
        parent: WorkspaceSettingsVersion | None,
        settings: WorkspaceSettings,
        parent_settings: WorkspaceSettings | None = None,
    ) -> None:
        if parent_settings is None and parent is not None:
            parent_settings = WorkspaceSettings.model_validate(parent.settings_json)
        before = parent_settings.risk if parent_settings else None
        if settings.risk != before:
            if not authorize(principal, Permission.RISK_PROFILE_WRITE):
                raise SettingsAdminError(403, "Cần quyền thay đổi hồ sơ rủi ro.")
            now = datetime.now(UTC)
            if principal.mfa_verified_at > now:
                raise SettingsAdminError(403, "Thời điểm xác thực MFA không hợp lệ.")
            if now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
                raise SettingsAdminError(403, "Cần xác thực MFA gần đây.")

    @staticmethod
    def _record_change(
        unit: UnitOfWork,
        action: str,
        record: WorkspaceSettingsVersion,
        before: object | None,
    ) -> None:
        unit.record_audit(
            action=action,
            target_type="workspace_settings_version",
            target_id=record.id,
            before_state=before,
            after_state=record.settings_json,
        )
        SettingsAdminService._enqueue(unit, action, record, {"version": record.version})

    @staticmethod
    def _enqueue(
        unit: UnitOfWork,
        event_type: str,
        record: WorkspaceSettingsVersion,
        payload: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC)
        unit.enqueue(
            EventEnvelope(
                event_type=event_type,
                workspace_id=unit.workspace_id,
                aggregate_type="workspace_settings_version",
                aggregate_id=record.id,
                occurred_at=now,
                correlation_id=unit.request_id,
                idempotency_key=f"{event_type}:{record.id}",
                schema_version=1,
                payload=payload,
            )
        )


def serialize_version(record: WorkspaceSettingsVersion | None) -> dict[str, Any] | None:
    """Tạo phản hồi công khai, loại bỏ đệ quy trường có tên giống thông tin bí mật."""
    if record is None:
        return None
    return {
        "id": str(record.id),
        "version": record.version,
        "parent_version_id": str(record.parent_version_id) if record.parent_version_id else None,
        "status": record.status,
        "revision": record.revision,
        "settings_hash": record.settings_hash,
        "settings": redact(record.settings_json),
        "created_by": str(record.created_by),
        "approved_by": str(record.approved_by) if record.approved_by else None,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "effective_at": record.effective_at.isoformat() if record.effective_at else None,
    }


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[đã ẩn]" for key in value if _SECRET_KEY.search(str(key))} | {
            key: redact(child) for key, child in value.items() if not _SECRET_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    return value


def _validation_errors(error: Exception) -> list[dict[str, str]]:
    errors = getattr(error, "errors", None)
    if errors is None:
        return [{"field": "settings", "message": "Cấu hình không hợp lệ."}]
    return [
        {
            "field": ".".join(str(part) for part in detail.get("loc", ())) or "settings",
            "message": str(detail.get("msg", "Cấu hình không hợp lệ.")),
        }
        for detail in errors()
    ]
