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
from trading_bot.auth.action_challenge import (
    ACTION_BOT_CONFIG_PUBLISH,
    ACTION_BOT_CONFIG_ROLLBACK,
    ACTION_BOT_CREATE,
    ActionChallengeError,
    ActionChallengeService,
    StepUpProof,
    bot_config_publish_payload,
    bot_config_rollback_payload,
    bot_create_payload,
    resolve_step_up_proof,
    verify_service_step_up,
)
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.config.bot_settings import (
    BotConfigOverlay,
    canonical_overlay,
    decimal_text,
    exit_section,
    overlay_hash,
    risk_section,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.events import EventEnvelope

EXIT_BACKTEST_REQUIRED_MESSAGE = (
    "Chưa có backtest bản gốc vs bản nháp này. Chạy "
    "`uv run python scripts/run_proposal_backtest.py --bot-draft <bot_id>` rồi kiểm định lại."
)


class BotAdminError(Exception):
    """Lỗi nghiệp vụ có mã HTTP ổn định; ``errors`` liệt kê trường vi phạm nếu có."""

    def __init__(
        self,
        status_code: int,
        message: str,
        errors: Sequence[dict[str, str]] = (),
        *,
        code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.errors = list(errors)
        self.code = code
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

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        action_challenge_service: ActionChallengeService | None = None,
        require_action_step_up: bool = False,
    ) -> None:
        self._sessions = sessions
        self._action_challenge_service = action_challenge_service
        self._require_action_step_up = require_action_step_up

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
        step_up: StepUpProof | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Tạo PAPER account để trigger MB-01 tạo bot; cùng tên + thuộc tính là replay."""
        if Role.OWNER not in principal.roles:
            raise BotAdminError(403, "Chỉ owner được tạo bot và account PAPER mới.")
        if not max_notional.is_finite() or max_notional <= 0:
            raise BotAdminError(422, "max_notional phải hữu hạn và dương.")
        await self._verify_step_up(
            principal,
            action=ACTION_BOT_CREATE,
            payload=bot_create_payload(name=name, max_notional=max_notional),
            risk_or_exit_changed=True,
            step_up=step_up,
        )
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
            root_release = root.published.settings.strategy.default_release
            if overlay.strategy is not None and overlay.strategy.release_id != root_release:
                # MB-05 ruling 28: cảnh báo không chặn; runtime entry cần fan-out MB-05b.
                warnings.append(
                    {
                        "field": "strategy.release_id",
                        "code": "BOT_RELEASE_DIFFERS_FROM_ROOT",
                        "message": (
                            "Release của bot khác strategy.default_release của root: publish vẫn "
                            "được, nhưng runtime PAPER từ chối entry của bot "
                            "(PAPER_BOT_STRATEGY_FANOUT_REQUIRED) tới khi MB-05b nối fan-out."
                        ),
                    }
                )
            if exit_changed(overlay, head) and (
                await find_exit_backtest(
                    unit.session,
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    draft=draft,
                    head=head,
                )
                is None
            ):
                warnings.append(
                    {
                        "field": "exit",
                        "code": "EXIT_BACKTEST_REQUIRED",
                        "message": EXIT_BACKTEST_REQUIRED_MESSAGE,
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
        step_up: StepUpProof | None = None,
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
            if _overlay_high_risk_changed(overlay, head):
                await self._verify_step_up(
                    principal,
                    action=ACTION_BOT_CONFIG_PUBLISH,
                    payload=bot_config_publish_payload(
                        bot_id=bot_id,
                        draft_id=draft_id,
                        expected_revision=expected_revision,
                        overlay_hash=draft.overlay_hash,
                    ),
                    risk_or_exit_changed=_risk_or_exit_changed(overlay, head),
                    step_up=step_up,
                )
            exit_backtest = None
            if exit_changed(overlay, head):
                # CRY-428 (owner 17/11/2026): đổi thông số thoát phải có backtest gốc vs mới của
                # đúng bản nháp; owner tự quyết dù kết luận thế nào.
                exit_backtest = await find_exit_backtest(
                    unit.session,
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    draft=draft,
                    head=head,
                )
                if exit_backtest is None:
                    raise BotAdminError(
                        409,
                        "Đổi thông số thoát lệnh cần backtest bản gốc vs bản nháp này trước khi "
                        "publish.",
                        [{"field": "exit", "message": EXIT_BACKTEST_REQUIRED_MESSAGE}],
                    )
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
            if exit_backtest is not None:
                unit.record_audit(
                    action="bot.config.exit_backtest_accepted",
                    target_type="bot_config_version",
                    target_id=published.id,
                    # Audit chỉ lưu hash trạng thái; reason giữ mã backtest để truy vết trực tiếp.
                    reason=(
                        f"proposal_backtest={exit_backtest.id} verdict={exit_backtest.verdict}"
                    ),
                    after_state={
                        "proposal_backtest_id": str(exit_backtest.id),
                        "verdict": exit_backtest.verdict,
                        "exit": exit_section(overlay),
                    },
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
        step_up: StepUpProof | None = None,
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
            if _overlay_high_risk_changed(overlay, head):
                await self._verify_step_up(
                    principal,
                    action=ACTION_BOT_CONFIG_ROLLBACK,
                    payload=bot_config_rollback_payload(
                        bot_id=bot_id,
                        version=version,
                        expected_current_version_id=expected_current_version_id,
                        target_overlay_hash=target.overlay_hash,
                        reason=reason,
                    ),
                    risk_or_exit_changed=_risk_or_exit_changed(overlay, head),
                    step_up=step_up,
                )
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

    # --- Đề xuất AI đã backtest (AI-3) ------------------------------------------------------

    async def list_proposal_backtests(
        self, principal: Principal, *, bot_id: UUID, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Kết quả backtest đề xuất AI của bot, mới nhất trước (chỉ đọc)."""
        from sqlalchemy import select

        from trading_bot.adapters.database.models import ProposalBacktestRecord
        from trading_bot.application.proposal_backtest import DRAFTABLE_VERDICTS

        async with self._uow(principal, bot_id=bot_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.READ)
            rows = await unit.session.scalars(
                select(ProposalBacktestRecord)
                .where(
                    ProposalBacktestRecord.workspace_id == principal.workspace_id,
                    ProposalBacktestRecord.bot_id == bot_id,
                )
                .order_by(ProposalBacktestRecord.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "id": str(row.id),
                    "created_at": row.created_at,
                    "verdict": row.verdict,
                    "changes": list(row.proposal.get("changes") or []),
                    "rationale": str(row.proposal.get("rationale") or ""),
                    "baseline": dict(row.result.get("baseline") or {}),
                    "variant": dict(row.result.get("variant") or {}),
                    "notes": list(row.result.get("notes") or []),
                    "window": row.result.get("window"),
                    "source": row.source,
                    "draft_id": row.proposal.get("draft_id"),
                    # Kết quả backtest bản nháp của owner (CRY-428) không tạo nháp mới được.
                    "draftable": row.source == "AI_PROPOSAL" and row.verdict in DRAFTABLE_VERDICTS,
                }
                for row in rows.all()
            ]

    async def create_draft_from_proposal(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        backtest_id: UUID,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Bản nháp = cấu hình đang publish + thay đổi AI đã backtest; không tự publish.

        Kết quả backtest phải còn khớp cấu hình hiện hành (nếu không phải backtest lại). Quyền,
        idempotency (mỗi kết quả một bản nháp) và MFA khi đổi khối risk đi qua ``create_draft``.
        """
        from sqlalchemy import select

        from trading_bot.adapters.database.models import ProposalBacktestRecord
        from trading_bot.application.proposal_backtest import draft_overlay_from_backtest

        async with self._uow(principal, bot_id=bot_id) as unit:
            await self._require(unit, principal, bot_id, BotPermission.CONFIG_WRITE)
            record = await unit.session.scalar(
                select(ProposalBacktestRecord).where(
                    ProposalBacktestRecord.workspace_id == principal.workspace_id,
                    ProposalBacktestRecord.bot_id == bot_id,
                    ProposalBacktestRecord.id == backtest_id,
                )
            )
            if record is None or record.source != "AI_PROPOSAL":
                raise BotAdminError(404, "Không tìm thấy kết quả backtest đề xuất của bot này.")
            head = await BotConfigVersionRepository(unit.session).get_head(
                workspace_id=principal.workspace_id, bot_id=bot_id
            )
            if head is None:
                raise BotAdminError(409, "Bot chưa có cấu hình đã publish.")
            try:
                payload = draft_overlay_from_backtest(
                    head.overlay_json, record.proposal, record.result
                )
                overlay = BotConfigOverlay.model_validate(payload)
            except (ValueError, ValidationError) as error:
                raise BotAdminError(409, str(error).splitlines()[0][:300]) from error
            parent_id = head.id
        return await self.create_draft(
            principal,
            bot_id=bot_id,
            overlay=overlay,
            expected_parent_version_id=parent_id,
            idempotency_key=f"ai3-proposal:{backtest_id}",
            request_id=request_id,
        )

    # --- Helpers ----------------------------------------------------------------------------

    async def _verify_step_up(
        self,
        principal: Principal,
        *,
        action: str,
        payload: dict[str, Any],
        risk_or_exit_changed: bool,
        step_up: StepUpProof | None = None,
    ) -> None:
        proof = resolve_step_up_proof(step_up)
        if proof is None and not self._require_action_step_up and not risk_or_exit_changed:
            return
        try:
            await verify_service_step_up(
                principal,
                action_challenge_service=self._action_challenge_service,
                require_action_step_up=self._require_action_step_up,
                action=action,
                payload=payload,
                step_up=proof,
                fallback_recent_mfa=lambda: _require_recent_mfa(principal),
            )
        except ActionChallengeError as error:
            raise BotAdminError(error.status_code, error.message, code=error.code) from error

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
        settings = WorkspaceSettingsRepository(unit.session)
        await settings.lock_workspace(principal.workspace_id)
        await settings.lock_workspace_risk(principal.workspace_id)
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


def _risk_or_exit_changed(overlay: BotConfigOverlay, baseline: BotConfigVersion | None) -> bool:
    before = baseline.overlay_json.get("risk", {}) if baseline is not None else {}
    return risk_section(overlay) != before or exit_changed(overlay, baseline)


def _overlay_high_risk_changed(
    overlay: BotConfigOverlay, baseline: BotConfigVersion | None
) -> bool:
    """Đổi risk, exit, strategy hoặc universe đều thuộc phạm vi step-up SEC-20."""
    if baseline is None:
        return True
    before = baseline.overlay_json
    after = canonical_overlay(overlay)
    return (
        _risk_or_exit_changed(overlay, baseline)
        or after.get("strategy") != before.get("strategy")
        or after.get("universe") != before.get("universe")
    )


def _require_mfa_for_risk_change(
    principal: Principal, overlay: BotConfigOverlay, baseline: BotConfigVersion | None
) -> None:
    """Đổi khối risk hoặc exit (CRY-428) so với version cha/hiện hành bắt buộc MFA gần đây."""
    if _risk_or_exit_changed(overlay, baseline):
        _require_recent_mfa(principal)


def exit_changed(overlay: BotConfigOverlay, baseline: BotConfigVersion | None) -> bool:
    """Khối ``exit`` (ghi đè thoát lệnh) khác bản đang publish."""
    before = exit_section(baseline.overlay_json) if baseline is not None else {}
    return exit_section(overlay) != before


async def find_exit_backtest(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    bot_id: UUID,
    draft: BotConfigVersion,
    head: BotConfigVersion | None,
) -> Any:
    """Backtest OWNER_DRAFT của đúng bản nháp này trên đúng bản đang publish (mới nhất)."""
    from sqlalchemy import select

    from trading_bot.adapters.database.models import ProposalBacktestRecord

    return await session.scalar(
        select(ProposalBacktestRecord)
        .where(
            ProposalBacktestRecord.workspace_id == workspace_id,
            ProposalBacktestRecord.bot_id == bot_id,
            ProposalBacktestRecord.source == "OWNER_DRAFT",
            ProposalBacktestRecord.proposal["draft_overlay_hash"].astext == draft.overlay_hash,
            ProposalBacktestRecord.proposal["head_overlay_hash"].astext
            == (head.overlay_hash if head is not None else ""),
        )
        .order_by(ProposalBacktestRecord.created_at.desc())
        .limit(1)
    )


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
