"""Ca sử dụng quản trị bot và vòng đời cấu hình overlay theo bot.

Mỗi thao tác kiểm workspace + ``BotPermission`` trước khi chạm dữ liệu, ghi audit và
outbox trong cùng UnitOfWork, và không bao giờ ghi vào ``workspace_settings_version``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    BotConfigVersion,
    BotInstance,
    StrategyRelease,
    TradingAccount,
)
from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionConflict,
    BotConfigVersionRepository,
    request_fingerprint,
)
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_instances import BotInstanceRepository
from trading_bot.adapters.database.repositories.workspace_settings import (
    SettingsVersionConflict,
    WorkspaceSettingsRepository,
)
from trading_bot.admin.settings_service import redact
from trading_bot.application.bot_config_resolver import (
    OverlayIssue,
    load_strategy_releases,
    validate_bot_overlay,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    RuntimeRiskProfile,
    load_risk_profile,
)
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.config.bot_settings import (
    BotConfigOverlay,
    canonical_overlay,
    decimal_text,
    overlay_hash,
    risk_section,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.events import EventEnvelope


class BotAdminError(Exception):
    """Lỗi nghiệp vụ có mã HTTP ổn định; ``errors`` liệt kê trường vi phạm nếu có."""

    def __init__(
        self, status_code: int, message: str, errors: Sequence[dict[str, str]] = ()
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.errors = list(errors)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RootContext:
    """Root settings đã publish cùng hard caps và release của workspace để validate overlay."""

    published: PublishedWorkspaceSettings
    risk_profile: RuntimeRiskProfile
    releases: Sequence[StrategyRelease]


async def load_root_context(session: AsyncSession, *, workspace_id: UUID) -> RootContext | None:
    """Nạp root hiện hành đã kiểm hash; thiếu hoặc hỏng thì trả None để từ chối thao tác."""
    try:
        published = await WorkspaceSettingsRepository(session).load_effective_config(
            workspace_id=workspace_id
        )
    except SettingsVersionConflict:
        return None
    if published is None:
        return None
    try:
        risk_profile = await load_risk_profile(
            session,
            workspace_id=workspace_id,
            profile_id=published.settings.risk.profile_version_id,
        )
    except RiskProfileRuntimeError:
        return None
    releases = await load_strategy_releases(session, workspace_id=workspace_id)
    return RootContext(published, risk_profile, releases)


class BotConfigAdminService:
    """Quản trị bot và overlay; mutation cấu hình hỗ trợ Idempotency-Key tùy chọn."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # --- Bot -------------------------------------------------------------------------------

    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        async with self._uow(principal) as unit:
            grants = BotGrantRepository(unit.session)
            versions = BotConfigVersionRepository(unit.session)
            visible: list[dict[str, Any]] = []
            rows = await BotInstanceRepository(unit.session).list_with_accounts(
                workspace_id=principal.workspace_id
            )
            for bot, account in rows:
                try:
                    await grants.require(principal, bot_id=bot.id, permission=BotPermission.READ)
                except PermissionError:
                    continue
                head = await versions.get_head(workspace_id=principal.workspace_id, bot_id=bot.id)
                visible.append(serialize_bot(bot, account, head))
            return visible

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        async with self._uow(principal, bot_id=bot_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.READ)
            bot, account = await self._bot(unit, principal, bot_id)
            versions = await BotConfigVersionRepository(unit.session).list_versions(
                workspace_id=principal.workspace_id, bot_id=bot_id
            )
            head = next((row for row in versions if row.status == "PUBLISHED"), None)
            return serialize_bot(bot, account, head) | {
                "versions": [serialize_bot_version(row) for row in versions]
            }

    async def create_bot(
        self,
        principal: Principal,
        *,
        name: str,
        max_notional: Decimal,
        request_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Tạo PAPER account để trigger MB-01 tạo bot; cùng tên + thuộc tính là replay."""
        if Role.OWNER not in principal.roles:
            raise BotAdminError(403, "Chỉ owner được tạo bot và account PAPER mới.")
        _require_recent_mfa(principal)
        if not max_notional.is_finite() or max_notional <= 0:
            raise BotAdminError(422, "max_notional phải hữu hạn và dương.")
        async with self._uow(principal, request_id=request_id) as unit:
            bots = BotInstanceRepository(unit.session)
            existing = await bots.get_account_by_name(
                workspace_id=principal.workspace_id, name=name
            )
            if existing is not None:
                return await self._replay_bot(unit, principal, existing, max_notional), False
            try:
                bot, account = await bots.create_paper_account(
                    workspace_id=principal.workspace_id, name=name, max_notional=max_notional
                )
            except IntegrityError as error:
                raise BotAdminError(409, "Tên bot/account đã tồn tại trong workspace.") from error
            state = serialize_bot(bot, account, None)
            unit.record_audit(
                action="bot.created",
                target_type="bot_instance",
                target_id=bot.id,
                after_state=state,
            )
            _enqueue(
                unit,
                "bot.created",
                aggregate_type="bot_instance",
                aggregate_id=bot.id,
                key=f"bot.created:{bot.id}",
                payload={"bot_id": str(bot.id), "account_id": str(account.id), "mode": "PAPER"},
            )
            return state, True

    async def _replay_bot(
        self,
        unit: UnitOfWork,
        principal: Principal,
        account: TradingAccount,
        max_notional: Decimal,
    ) -> dict[str, Any]:
        bot = await BotInstanceRepository(unit.session).get_for_account(
            workspace_id=principal.workspace_id, account_id=account.id
        )
        if (
            bot is None
            or account.mode != "PAPER"
            or account.venue != "binance_usdm"
            or account.max_notional != max_notional
        ):
            raise BotAdminError(409, "Tên bot/account đã tồn tại với thuộc tính khác.")
        return serialize_bot(bot, account, None)

    # --- Draft ------------------------------------------------------------------------------

    async def create_draft(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        overlay: BotConfigOverlay,
        expected_parent_version_id: UUID | None,
        idempotency_key: str | None = None,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        fingerprint = request_fingerprint(
            "draft.create",
            {
                "bot_id": str(bot_id),
                "expected_parent_version_id": str(expected_parent_version_id),
                "overlay_hash": overlay_hash(canonical_overlay(overlay)),
            },
        )
        async with self._uow(principal, bot_id=bot_id, request_id=request_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.CONFIG_WRITE)
            _, account = await self._bot(unit, principal, bot_id)
            versions = BotConfigVersionRepository(unit.session)
            await versions.lock_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
            replay = await self._replay(
                versions, principal, bot_id, "draft.create", idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            head = await versions.get_head(workspace_id=principal.workspace_id, bot_id=bot_id)
            if (head.id if head is not None else None) != expected_parent_version_id:
                raise BotAdminError(409, "Phiên bản cấu hình bot hiện hành đã thay đổi.")
            root = await self._root(unit, principal)
            _require_mfa_for_risk_change(principal, overlay, head)
            record = await versions.create_draft(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                account_id=account.id,
                overlay=overlay,
                base_settings_version_id=root.published.settings_version_id,
                base_settings_hash=root.published.settings_hash,
                created_by=principal.membership_id,
                parent_version_id=head.id if head is not None else None,
            )
            await self._remember(
                versions, principal, record, "draft.create", idempotency_key, fingerprint
            )
            _record_change(unit, "bot.config.draft_created", record, None)
            return serialize_bot_version(record)

    async def update_draft(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        draft_id: UUID,
        expected_revision: int,
        overlay: BotConfigOverlay,
        idempotency_key: str | None = None,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        fingerprint = request_fingerprint(
            "draft.update",
            {
                "bot_id": str(bot_id),
                "draft_id": str(draft_id),
                "expected_revision": expected_revision,
                "overlay_hash": overlay_hash(canonical_overlay(overlay)),
            },
        )
        async with self._uow(principal, bot_id=bot_id, request_id=request_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.CONFIG_WRITE)
            await self._bot(unit, principal, bot_id)
            versions = BotConfigVersionRepository(unit.session)
            await versions.lock_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
            replay = await self._replay(
                versions, principal, bot_id, "draft.update", idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            draft = await versions.get(
                workspace_id=principal.workspace_id, bot_id=bot_id, version_id=draft_id
            )
            if draft is None or draft.status != "DRAFT":
                raise BotAdminError(404, "Không tìm thấy bản nháp có thể sửa.")
            if draft.revision != expected_revision:
                raise BotAdminError(409, "Bản nháp đã thay đổi; hãy tải lại trước khi sửa.")
            parent = (
                await versions.get(
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    version_id=draft.parent_version_id,
                )
                if draft.parent_version_id is not None
                else None
            )
            root = await self._root(unit, principal)
            _require_mfa_for_risk_change(principal, overlay, parent)
            before = dict(draft.overlay_json)
            try:
                updated = await versions.update_draft(
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    overlay=overlay,
                    base_settings_version_id=root.published.settings_version_id,
                    base_settings_hash=root.published.settings_hash,
                    updated_by=principal.membership_id,
                )
            except BotConfigVersionConflict as error:
                raise BotAdminError(409, str(error)) from error
            await self._remember(
                versions, principal, updated, "draft.update", idempotency_key, fingerprint
            )
            _record_change(unit, "bot.config.draft_updated", updated, before)
            return serialize_bot_version(updated)

    async def validate_draft(
        self, principal: Principal, *, bot_id: UUID, draft_id: UUID
    ) -> dict[str, Any]:
        async with self._uow(principal, bot_id=bot_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.CONFIG_WRITE)
            _, account = await self._bot(unit, principal, bot_id)
            versions = BotConfigVersionRepository(unit.session)
            draft = await versions.get(
                workspace_id=principal.workspace_id, bot_id=bot_id, version_id=draft_id
            )
            if draft is None:
                raise BotAdminError(404, "Không tìm thấy phiên bản cấu hình bot.")
            if draft.status != "DRAFT":
                raise BotAdminError(409, "Chỉ có thể kiểm định bản nháp.")
            root = await load_root_context(unit.session, workspace_id=principal.workspace_id)
            if root is None:
                return {
                    "valid": False,
                    "errors": [{"field": "root", "message": "Workspace chưa có root settings."}],
                    "warnings": [],
                }
            try:
                overlay = BotConfigOverlay.model_validate(draft.overlay_json)
            except ValidationError:
                return {
                    "valid": False,
                    "errors": [{"field": "overlay", "message": "Overlay đã lưu không hợp lệ."}],
                    "warnings": [],
                }
            issues = _issues(overlay, root, account)
            head = await versions.get_head(workspace_id=principal.workspace_id, bot_id=bot_id)
            warnings = []
            if (head.id if head is not None else None) != draft.parent_version_id:
                warnings.append(
                    {
                        "field": "parent_version_id",
                        "message": "Bản nháp dựa trên phiên bản cũ; publish sẽ bị từ chối.",
                    }
                )
            return {"valid": not issues, "errors": _issue_dicts(issues), "warnings": warnings}

    # --- Publish / rollback -----------------------------------------------------------------

    async def publish(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        draft_id: UUID,
        expected_revision: int,
        idempotency_key: str | None = None,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        fingerprint = request_fingerprint(
            "publish",
            {
                "bot_id": str(bot_id),
                "draft_id": str(draft_id),
                "expected_revision": expected_revision,
            },
        )
        async with self._uow(principal, bot_id=bot_id, request_id=request_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.CONFIG_PUBLISH)
            _, account = await self._bot(unit, principal, bot_id)
            versions = await self._lock_for_publish(unit, principal, bot_id)
            replay = await self._replay(
                versions, principal, bot_id, "publish", idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            draft = await versions.get(
                workspace_id=principal.workspace_id, bot_id=bot_id, version_id=draft_id
            )
            if draft is None:
                raise BotAdminError(404, "Không tìm thấy bản nháp cấu hình bot.")
            if draft.status != "DRAFT" or draft.revision != expected_revision:
                raise BotAdminError(409, "Bản nháp không còn ở trạng thái/revision có thể publish.")
            head = await versions.get_head(workspace_id=principal.workspace_id, bot_id=bot_id)
            if (head.id if head is not None else None) != draft.parent_version_id:
                raise BotAdminError(409, "Bản nháp dựa trên cấu hình cũ; hãy tạo bản nháp mới.")
            overlay = _parse_stored(draft)
            root = await self._root(unit, principal)
            _raise_issues(_issues(overlay, root, account))
            _require_mfa_for_risk_change(principal, overlay, head)
            published = await self._publish(
                versions, principal, draft, expected_revision, head, root
            )
            await self._remember(
                versions, principal, published, "publish", idempotency_key, fingerprint
            )
            _record_change(
                unit,
                "bot.config.published",
                published,
                head.overlay_json if head is not None else None,
            )
            return serialize_bot_version(published)

    async def rollback(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        version: int,
        expected_current_version_id: UUID | None,
        reason: str,
        idempotency_key: str | None = None,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        fingerprint = request_fingerprint(
            "rollback",
            {
                "bot_id": str(bot_id),
                "version": version,
                "expected_current_version_id": str(expected_current_version_id),
                "reason": reason,
            },
        )
        async with self._uow(principal, bot_id=bot_id, request_id=request_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.CONFIG_PUBLISH)
            _, account = await self._bot(unit, principal, bot_id)
            versions = await self._lock_for_publish(unit, principal, bot_id)
            replay = await self._replay(
                versions, principal, bot_id, "rollback", idempotency_key, fingerprint
            )
            if replay is not None:
                return replay
            target = await versions.get_by_number(
                workspace_id=principal.workspace_id, bot_id=bot_id, version=version
            )
            if target is None or target.status != "PUBLISHED":
                raise BotAdminError(404, "Không tìm thấy phiên bản đã publish để rollback.")
            head = await versions.get_head(workspace_id=principal.workspace_id, bot_id=bot_id)
            if head is None or head.id != expected_current_version_id:
                raise BotAdminError(409, "Phiên bản cấu hình bot hiện hành đã thay đổi.")
            if head.id == target.id:
                raise BotAdminError(409, "Phiên bản này đang là cấu hình hiện hành.")
            overlay = _parse_stored(target)
            root = await self._root(unit, principal)
            _raise_issues(_issues(overlay, root, account))
            _require_mfa_for_risk_change(principal, overlay, head)
            draft = await versions.create_draft(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                account_id=account.id,
                overlay=overlay,
                base_settings_version_id=root.published.settings_version_id,
                base_settings_hash=root.published.settings_hash,
                created_by=principal.membership_id,
                parent_version_id=head.id,
                source_version_id=target.id,
            )
            published = await self._publish(versions, principal, draft, 1, head, root)
            await self._remember(
                versions, principal, published, "rollback", idempotency_key, fingerprint
            )
            unit.record_audit(
                action="bot.config.rolled_back",
                target_type="bot_config_version",
                target_id=published.id,
                reason=reason,
                before_state=head.overlay_json,
                after_state=published.overlay_json,
            )
            _enqueue_version(unit, "bot.config.rolled_back", published)
            return serialize_bot_version(published)

    # --- Helpers ----------------------------------------------------------------------------

    def _uow(
        self,
        principal: Principal,
        *,
        bot_id: UUID | None = None,
        request_id: UUID | None = None,
    ) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        )

    @staticmethod
    async def _require(
        unit: UnitOfWork, principal: Principal, bot_id: UUID, permission: BotPermission
    ) -> None:
        try:
            await BotGrantRepository(unit.session).require(
                principal, bot_id=bot_id, permission=permission
            )
        except PermissionError as error:
            raise BotAdminError(403, "Không đủ quyền cho thao tác bot này.") from error

    @staticmethod
    async def _bot(
        unit: UnitOfWork, principal: Principal, bot_id: UUID
    ) -> tuple[BotInstance, TradingAccount]:
        found = await BotInstanceRepository(unit.session).get_with_account(
            workspace_id=principal.workspace_id, bot_id=bot_id
        )
        if found is None:
            raise BotAdminError(404, "Không tìm thấy bot trong workspace.")
        return found

    @staticmethod
    async def _root(unit: UnitOfWork, principal: Principal) -> RootContext:
        root = await load_root_context(unit.session, workspace_id=principal.workspace_id)
        if root is None:
            raise BotAdminError(409, "Workspace chưa có root settings hợp lệ đã publish.")
        return root

    @staticmethod
    async def _lock_for_publish(
        unit: UnitOfWork, principal: Principal, bot_id: UUID
    ) -> BotConfigVersionRepository:
        # Cùng lock với root publish và runtime decision rồi mới lock bot (thứ tự cố định).
        await WorkspaceSettingsRepository(unit.session).lock_workspace(principal.workspace_id)
        versions = BotConfigVersionRepository(unit.session)
        await versions.lock_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
        return versions

    @staticmethod
    async def _publish(
        versions: BotConfigVersionRepository,
        principal: Principal,
        draft: BotConfigVersion,
        expected_revision: int,
        head: BotConfigVersion | None,
        root: RootContext,
    ) -> BotConfigVersion:
        try:
            return await versions.publish_draft(
                workspace_id=principal.workspace_id,
                bot_id=draft.bot_id,
                draft_id=draft.id,
                expected_revision=expected_revision,
                expected_parent_version_id=head.id if head is not None else None,
                approved_by=principal.membership_id,
                base_settings_version_id=root.published.settings_version_id,
                base_settings_hash=root.published.settings_hash,
            )
        except BotConfigVersionConflict as error:
            raise BotAdminError(409, str(error)) from error

    @staticmethod
    async def _replay(
        versions: BotConfigVersionRepository,
        principal: Principal,
        bot_id: UUID,
        operation: str,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            return None
        previous = await versions.find_request(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        if previous is None:
            return None
        if previous.request_hash != fingerprint:
            raise BotAdminError(422, "Idempotency-Key đã được dùng cho yêu cầu khác.")
        record = await versions.get(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            version_id=previous.bot_config_version_id,
        )
        if record is None:
            raise BotAdminError(409, "Không đọc lại được kết quả của Idempotency-Key.")
        return serialize_bot_version(record)

    @staticmethod
    async def _remember(
        versions: BotConfigVersionRepository,
        principal: Principal,
        record: BotConfigVersion,
        operation: str,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> None:
        if idempotency_key is None:
            return
        await versions.record_request(
            workspace_id=principal.workspace_id,
            bot_id=record.bot_id,
            account_id=record.account_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=fingerprint,
            version_id=record.id,
            created_by=principal.membership_id,
        )


def _require_recent_mfa(principal: Principal) -> None:
    now = datetime.now(UTC)
    if principal.mfa_verified_at > now:
        raise BotAdminError(403, "Thời điểm xác thực MFA không hợp lệ.")
    if now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise BotAdminError(403, "Cần xác thực MFA gần đây.")


def _require_mfa_for_risk_change(
    principal: Principal, overlay: BotConfigOverlay, baseline: BotConfigVersion | None
) -> None:
    """Đổi khối risk so với version cha/hiện hành bắt buộc MFA gần đây."""
    before = baseline.overlay_json.get("risk", {}) if baseline is not None else {}
    if risk_section(overlay) != before:
        _require_recent_mfa(principal)


def _parse_stored(record: BotConfigVersion) -> BotConfigOverlay:
    try:
        return BotConfigOverlay.model_validate(record.overlay_json)
    except ValidationError as error:
        raise BotAdminError(422, "Overlay đã lưu không hợp lệ.") from error


def _issues(
    overlay: BotConfigOverlay, root: RootContext, account: TradingAccount
) -> tuple[OverlayIssue, ...]:
    return validate_bot_overlay(
        overlay,
        workspace_settings=root.published,
        account=account,
        root_risk_profile=root.risk_profile,
        strategy_releases=root.releases,
    )


def _issue_dicts(issues: Sequence[OverlayIssue]) -> list[dict[str, str]]:
    return [{"field": issue.field, "message": issue.message} for issue in issues]


def _raise_issues(issues: Sequence[OverlayIssue]) -> None:
    if issues:
        raise BotAdminError(422, "Overlay vi phạm root settings.", _issue_dicts(issues))


def _record_change(
    unit: UnitOfWork, action: str, record: BotConfigVersion, before: object | None
) -> None:
    unit.record_audit(
        action=action,
        target_type="bot_config_version",
        target_id=record.id,
        before_state=before,
        after_state=record.overlay_json,
    )
    _enqueue_version(unit, action, record)


def _enqueue_version(unit: UnitOfWork, event_type: str, record: BotConfigVersion) -> None:
    _enqueue(
        unit,
        event_type,
        aggregate_type="bot_config_version",
        aggregate_id=record.id,
        # Revision trong key để nhiều lần sửa cùng draft không đụng unique outbox.
        key=f"{event_type}:{record.id}:{record.revision}",
        payload={
            "bot_id": str(record.bot_id),
            "account_id": str(record.account_id),
            "version": record.version,
            "revision": record.revision,
            "status": record.status,
            "overlay_hash": record.overlay_hash,
            "base_settings_version_id": str(record.base_settings_version_id),
        },
    )


def _enqueue(
    unit: UnitOfWork,
    event_type: str,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    key: str,
    payload: dict[str, Any],
) -> None:
    unit.enqueue(
        EventEnvelope(
            event_type=event_type,
            workspace_id=unit.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=datetime.now(UTC),
            correlation_id=unit.request_id,
            idempotency_key=key,
            schema_version=1,
            payload=payload,
        )
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_id(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def serialize_bot_version(record: BotConfigVersion) -> dict[str, Any]:
    """Phản hồi công khai của một version; overlay đi qua bộ lọc khóa bí mật."""
    return {
        "id": str(record.id),
        "bot_id": str(record.bot_id),
        "account_id": str(record.account_id),
        "version": record.version,
        "parent_version_id": _optional_id(record.parent_version_id),
        "source_version_id": _optional_id(record.source_version_id),
        "status": record.status,
        "revision": record.revision,
        "overlay_hash": record.overlay_hash,
        "overlay": redact(record.overlay_json),
        "base_settings_version_id": str(record.base_settings_version_id),
        "base_settings_hash": record.base_settings_hash,
        "created_by": str(record.created_by),
        "updated_by": str(record.updated_by),
        "approved_by": _optional_id(record.approved_by),
        "published_at": _iso(record.published_at),
        "effective_at": _iso(record.effective_at),
    }


def serialize_bot(
    bot: BotInstance, account: TradingAccount, head: BotConfigVersion | None
) -> dict[str, Any]:
    return {
        "id": str(bot.id),
        "name": bot.name,
        "status": bot.status,
        "account": {
            "id": str(account.id),
            "name": account.name,
            "mode": account.mode,
            "venue": account.venue,
            "environment": account.environment,
            "status": account.status,
            "max_notional": decimal_text(account.max_notional),
        },
        "current_config": serialize_bot_version(head) if head is not None else None,
    }
