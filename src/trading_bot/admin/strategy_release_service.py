"""Dịch vụ quản trị phát hành chiến lược (Strategy Release) trong workspace (SC-3 / CRY-34).

Cung cấp các ca sử dụng cho Admin Web:
1. Liệt kê toàn bộ release từ `strategy_registry()` (catalog sản phẩm: rule H0x/H18–H22 và
   Strategy Cards L0–L6) kèm trạng thái hiện hành trong workspace (`PUBLISHED`, `RETIRED`,
   `UNPUBLISHED` hoặc `HASH_MISMATCH`).
2. Chủ sở hữu (Owner) có xác thực MFA gần đây thực hiện công bố (`PUBLISHED`) hoặc thu hồi
   (`RETIRED`) bản phát hành kèm lý do tiếng Việt, ghi `audit_event` trong cùng transaction.
3. Đối soát nghiêm ngặt mã băm (`content_hash`) trực tiếp từ catalog: mọi yêu cầu gửi mã băm lệch
   catalog hoặc rule ngoài catalog đều bị từ chối fail-closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent, StrategyRelease
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.auth.action_challenge import (
    ACTION_STRATEGY_RELEASE_PUBLISH,
    ACTION_STRATEGY_RELEASE_RETIRE,
    ActionChallengeError,
    ActionChallengeService,
    StepUpProof,
    strategy_release_payload,
    verify_service_step_up,
)
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.events import state_hash
from trading_bot.domain.strategy.registry import (
    StrategyRegistry,
    UnknownStrategyReleaseError,
)
from trading_bot.domain.strategy.registry import (
    StrategyRelease as RegisteredRelease,
)

WORKSPACE_RELEASE_STATUS_LABELS_VI: dict[str, str] = {
    "PUBLISHED": "Đã công bố (PUBLISHED)",
    "RETIRED": "Đã thu hồi (RETIRED)",
    "UNPUBLISHED": "Chưa công bố",
    "HASH_MISMATCH": "Lệch mã băm catalog (cần công bố lại)",
}


class StrategyReleaseAdminError(Exception):
    """Lỗi nghiệp vụ khi quản trị `strategy_release` từ Admin Web/API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        reason_code: str = "STRATEGY_RELEASE_ERROR",
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CatalogReleaseView:
    """Mô hình hiển thị an toàn của một release trong catalog kèm trạng thái tại workspace."""

    release_id: str
    strategy_id: str
    name_vi: str
    rule_summary_vi: str
    recipe_version: str
    rule_version: str
    catalog_status: str
    content_hash: str
    short_hash: str
    workspace_release_id: str | None
    workspace_version: int | None
    workspace_status: str
    workspace_status_label_vi: str
    workspace_content_hash: str | None
    hash_matches_catalog: bool
    is_published: bool
    is_retired: bool
    kind: str
    created_at_formatted: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Chuyển đổi sang từ điển JSON phục vụ REST API và kiểm thử."""
        return {
            "release_id": self.release_id,
            "strategy_id": self.strategy_id,
            "name_vi": self.name_vi,
            "rule_summary_vi": self.rule_summary_vi,
            "recipe_version": self.recipe_version,
            "rule_version": self.rule_version,
            "catalog_status": self.catalog_status,
            "content_hash": self.content_hash,
            "short_hash": self.short_hash,
            "workspace_release_id": self.workspace_release_id,
            "workspace_version": self.workspace_version,
            "workspace_status": self.workspace_status,
            "workspace_status_label_vi": self.workspace_status_label_vi,
            "workspace_content_hash": self.workspace_content_hash,
            "hash_matches_catalog": self.hash_matches_catalog,
            "is_published": self.is_published,
            "is_retired": self.is_retired,
            "kind": self.kind,
            "created_at": self.created_at_formatted,
        }


def _short_hash(digest: str) -> str:
    if len(digest) >= 14:
        return f"{digest[:8]}...{digest[-6:]}"
    return digest


def _require_owner_and_recent_mfa(principal: Principal) -> None:
    """Bắt buộc vai trò OWNER và phiên xác thực MFA còn trong cửa sổ hiệu lực."""
    if Role.OWNER not in principal.roles:
        raise StrategyReleaseAdminError(
            403,
            "Chỉ chủ sở hữu (owner) mới được công bố hoặc thu hồi bản phát hành chiến lược.",
            reason_code="CONTROL_PERMISSION_DENIED",
        )
    if principal.mfa_verified_at is None:
        raise StrategyReleaseAdminError(
            403,
            "Cần xác thực MFA gần đây.",
            reason_code="RECENT_MFA_REQUIRED",
        )
    now = datetime.now(UTC)
    if principal.mfa_verified_at > now:
        raise StrategyReleaseAdminError(
            403,
            "Thời điểm xác thực MFA không hợp lệ.",
            reason_code="RECENT_MFA_REQUIRED",
        )
    if now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise StrategyReleaseAdminError(
            403,
            "Cần xác thực MFA gần đây.",
            reason_code="RECENT_MFA_REQUIRED",
        )


def _validate_reason_vi(reason: str | None) -> str:
    """Kiểm tra lý do tiếng Việt hợp lệ (3–500 ký tự sau khi cắt khoảng trắng)."""
    cleaned = reason.strip() if isinstance(reason, str) else ""
    if not 3 <= len(cleaned) <= 500:
        raise StrategyReleaseAdminError(
            422,
            "Lý do thao tác cần từ 3 đến 500 ký tự tiếng Việt rõ ràng.",
            reason_code="CONTROL_REASON_REQUIRED",
        )
    return cleaned


def _build_recipe_payload(
    registry: StrategyRegistry, registered: RegisteredRelease
) -> dict[str, Any]:
    """Đóng gói metadata recipe chuẩn hóa của bản phát hành từ catalog."""
    rule = registry.describe_rule(registered.release_id)
    recipe_obj = getattr(rule, "recipe", None)
    if recipe_obj is not None and hasattr(recipe_obj, "__dataclass_fields__"):
        manifest: Any = {
            name: getattr(recipe_obj, name) for name in recipe_obj.__dataclass_fields__
        }
    else:
        manifest = getattr(rule, "recipe_manifest", None) or {}
    return {
        "release_id": registered.release_id,
        "strategy_id": registered.strategy_id,
        "recipe_version": registered.recipe_version,
        "rule_version": registered.rule_version,
        "recipe_manifest": manifest,
    }


def _serialize_release_row(row: StrategyRelease) -> dict[str, Any]:
    """Tuần tự hóa trạng thái hàng `strategy_release` để băm `before_hash` / `after_hash`."""
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "name": row.name,
        "version": row.version,
        "status": row.status,
        "content_hash": row.content_hash,
        "recipe": dict(row.recipe or {}),
    }


def _clone_release_row(row: StrategyRelease) -> StrategyRelease:
    return StrategyRelease(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        version=row.version,
        status=row.status,
        recipe=dict(row.recipe or {}),
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


class StrategyReleaseAdminService:
    """Dịch vụ quản trị công bố và thu hồi `strategy_release` theo workspace."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        registry: StrategyRegistry | None = None,
        initial_releases: Sequence[StrategyRelease] = (),
        workspace_scoped: bool | None = None,
        action_challenge_service: ActionChallengeService | None = None,
        require_action_step_up: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry or strategy_registry()
        self._workspace_scoped = workspace_scoped
        self._action_challenge_service = action_challenge_service
        self._require_action_step_up = require_action_step_up
        self._in_memory_releases: dict[UUID, list[StrategyRelease]] = {}
        self._in_memory_audits: list[AuditEvent] = []
        for row in initial_releases:
            bucket = self._in_memory_releases.setdefault(row.workspace_id, [])
            bucket.append(_clone_release_row(row))

    @property
    def audit_events(self) -> tuple[AuditEvent, ...]:
        """Danh sách sự kiện audit ghi nhận trong chế độ in-memory (phục vụ kiểm thử)."""
        return tuple(self._in_memory_audits)

    def has_workspace_state(self, workspace_id: UUID) -> bool:
        """Cho biết service có kết nối DB thật hoặc đã có dữ liệu release của workspace."""
        if self._workspace_scoped is not None:
            return self._workspace_scoped
        if self._session_factory is not None:
            return True
        return workspace_id in self._in_memory_releases

    def _uow(self, principal: Principal, *, request_id: UUID | None = None) -> UnitOfWork:
        if self._session_factory is None:
            raise RuntimeError("UnitOfWork chỉ dùng khi có session_factory.")
        return UnitOfWork(
            self._session_factory,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        )

    def _resolve_catalog_release(
        self,
        release_id: str | None,
        *,
        expected_content_hash: str | None = None,
    ) -> RegisteredRelease:
        """Tra cứu release từ catalog sản phẩm và từ chối mọi mã băm lệch catalog."""
        clean_id = release_id.strip() if isinstance(release_id, str) else ""
        if not clean_id:
            raise StrategyReleaseAdminError(
                422,
                "Thiếu mã phát hành chiến lược (release_id).",
                reason_code="UNKNOWN_STRATEGY_RELEASE",
            )
        try:
            registered = self._registry.get_release(clean_id)
        except UnknownStrategyReleaseError as error:
            raise StrategyReleaseAdminError(
                404,
                f"Mã phát hành '{clean_id}' không tồn tại trong danh mục sản phẩm (catalog).",
                reason_code="UNKNOWN_STRATEGY_RELEASE",
            ) from error

        # Luôn đối chiếu với catalog sản phẩm chuẩn `strategy_registry()`
        try:
            authoritative = strategy_registry().get_release(clean_id)
        except UnknownStrategyReleaseError as error:
            raise StrategyReleaseAdminError(
                404,
                f"Mã phát hành '{clean_id}' không thuộc danh mục sản phẩm chuẩn.",
                reason_code="UNKNOWN_STRATEGY_RELEASE",
            ) from error

        if registered.content_hash != authoritative.content_hash:
            raise StrategyReleaseAdminError(
                409,
                "Mã băm của bản phát hành lệch với danh mục sản phẩm chuẩn hiện hành.",
                reason_code="CATALOG_HASH_MISMATCH",
            )

        if expected_content_hash is not None:
            given_hash = expected_content_hash.strip()
            if not given_hash or given_hash != registered.content_hash:
                raise StrategyReleaseAdminError(
                    409,
                    "Mã băm (content_hash) không khớp với danh mục sản phẩm hiện hành; "
                    "từ chối thao tác.",
                    reason_code="CATALOG_HASH_MISMATCH",
                )
        return registered

    def _to_view(
        self,
        registered: RegisteredRelease,
        workspace_rows: Sequence[StrategyRelease],
    ) -> CatalogReleaseView:
        """Tổng hợp trạng thái hiện tại trong workspace của một release trong catalog."""
        candidates = sorted(
            (row for row in workspace_rows if row.name == registered.release_id),
            key=lambda r: (
                r.status == "PUBLISHED" and r.content_hash == registered.content_hash,
                r.status == "PUBLISHED",
                r.version,
                r.created_at or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        matched_published = next(
            (
                row
                for row in candidates
                if row.status == "PUBLISHED" and row.content_hash == registered.content_hash
            ),
            None,
        )
        mismatched_published = next(
            (
                row
                for row in candidates
                if row.status == "PUBLISHED" and row.content_hash != registered.content_hash
            ),
            None,
        )
        retired_row = next((row for row in candidates if row.status == "RETIRED"), None)

        chosen: StrategyRelease | None = None
        if matched_published is not None:
            chosen = matched_published
            workspace_status = "PUBLISHED"
            hash_matches = True
        elif mismatched_published is not None:
            chosen = mismatched_published
            workspace_status = "HASH_MISMATCH"
            hash_matches = False
        elif retired_row is not None:
            chosen = retired_row
            workspace_status = "RETIRED"
            hash_matches = retired_row.content_hash == registered.content_hash
        elif candidates:
            chosen = candidates[0]
            workspace_status = "UNPUBLISHED"
            hash_matches = chosen.content_hash == registered.content_hash
        else:
            workspace_status = "UNPUBLISHED"
            hash_matches = True

        created_str: str | None = None
        if chosen is not None and isinstance(chosen.created_at, datetime):
            created_str = chosen.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

        kind = "strategy_card" if registered.release_id.startswith("sc-") else "built_in_rule"
        return CatalogReleaseView(
            release_id=registered.release_id,
            strategy_id=registered.strategy_id,
            name_vi=registered.name_vi,
            rule_summary_vi=registered.rule_summary_vi,
            recipe_version=registered.recipe_version,
            rule_version=registered.rule_version,
            catalog_status=registered.status,
            content_hash=registered.content_hash,
            short_hash=_short_hash(registered.content_hash),
            workspace_release_id=str(chosen.id) if chosen is not None else None,
            workspace_version=chosen.version if chosen is not None else None,
            workspace_status=workspace_status,
            workspace_status_label_vi=WORKSPACE_RELEASE_STATUS_LABELS_VI.get(
                workspace_status, "Chưa công bố"
            ),
            workspace_content_hash=chosen.content_hash if chosen is not None else None,
            hash_matches_catalog=hash_matches,
            is_published=(workspace_status == "PUBLISHED" and hash_matches),
            is_retired=(workspace_status == "RETIRED"),
            kind=kind,
            created_at_formatted=created_str,
        )

    async def _load_workspace_rows(self, principal: Principal) -> list[StrategyRelease]:
        if self._session_factory is not None:
            async with self._uow(principal) as unit:
                rows = await unit.session.scalars(
                    select(StrategyRelease).where(
                        StrategyRelease.workspace_id == principal.workspace_id
                    )
                )
                return list(rows.all())
        return [
            _clone_release_row(row)
            for row in self._in_memory_releases.get(principal.workspace_id, [])
        ]

    async def list_releases(self, principal: Principal) -> list[CatalogReleaseView]:
        """Liệt kê tất cả release từ catalog kèm trạng thái hiện hành trong workspace."""
        if not authorize(principal, Permission.WORKSPACE_READ):
            raise StrategyReleaseAdminError(
                403,
                "Không đủ quyền xem danh sách phát hành chiến lược.",
                reason_code="CONTROL_PERMISSION_DENIED",
            )
        workspace_rows = await self._load_workspace_rows(principal)
        return [
            self._to_view(registered, workspace_rows)
            for registered in self._registry.list_releases()
        ]

    async def list_published_release_ids(self, principal: Principal) -> tuple[str, ...]:
        """Danh sách `release_id` đang ở trạng thái `PUBLISHED` và khớp hash catalog."""
        views = await self.list_releases(principal)
        return tuple(view.release_id for view in views if view.is_published)

    async def _verify_step_up(
        self,
        principal: Principal,
        *,
        action: str,
        release_id: str,
        expected_content_hash: str | None,
        reason: str,
        step_up: StepUpProof | None = None,
    ) -> None:
        if Role.OWNER not in principal.roles:
            raise StrategyReleaseAdminError(
                403,
                "Chỉ chủ sở hữu (owner) mới được công bố hoặc thu hồi bản phát hành chiến lược.",
                reason_code="CONTROL_PERMISSION_DENIED",
            )
        try:
            await verify_service_step_up(
                principal,
                action_challenge_service=self._action_challenge_service,
                require_action_step_up=self._require_action_step_up,
                action=action,
                payload=strategy_release_payload(
                    release_id=release_id,
                    expected_content_hash=expected_content_hash,
                    reason=reason,
                ),
                step_up=step_up,
                fallback_recent_mfa=lambda: _require_owner_and_recent_mfa(principal),
            )
        except ActionChallengeError as error:
            raise StrategyReleaseAdminError(
                error.status_code, error.message, reason_code=error.code
            ) from error

    async def publish_release(
        self,
        principal: Principal,
        *,
        release_id: str,
        reason: str,
        expected_content_hash: str | None = None,
        request_id: UUID | None = None,
        step_up: StepUpProof | None = None,
    ) -> CatalogReleaseView:
        """Công bố một `strategy_release` từ catalog vào workspace (Owner + Step-Up/MFA + Audit)."""
        clean_reason = _validate_reason_vi(reason)
        registered = self._resolve_catalog_release(
            release_id, expected_content_hash=expected_content_hash
        )
        await self._verify_step_up(
            principal,
            action=ACTION_STRATEGY_RELEASE_PUBLISH,
            release_id=registered.release_id,
            expected_content_hash=expected_content_hash,
            reason=clean_reason,
            step_up=step_up,
        )
        recipe_payload = _build_recipe_payload(self._registry, registered)

        if self._session_factory is not None:
            async with self._uow(principal, request_id=request_id) as unit:
                await WorkspaceSettingsRepository(unit.session).lock_workspace(
                    principal.workspace_id
                )
                rows_result = await unit.session.scalars(
                    select(StrategyRelease)
                    .where(
                        StrategyRelease.workspace_id == principal.workspace_id,
                        StrategyRelease.name == registered.release_id,
                    )
                    .with_for_update()
                )
                existing_rows = list(rows_result.all())
                target, before_state, after_state = self._apply_publish(
                    principal.workspace_id,
                    registered,
                    recipe_payload,
                    existing_rows,
                )
                if target not in existing_rows:
                    unit.session.add(target)
                unit.record_audit(
                    action="strategy_release.published",
                    target_type="strategy_release",
                    target_id=target.id,
                    reason=clean_reason,
                    before_state=before_state,
                    after_state=after_state,
                )
                all_rows_result = await unit.session.scalars(
                    select(StrategyRelease).where(
                        StrategyRelease.workspace_id == principal.workspace_id,
                        StrategyRelease.name == registered.release_id,
                    )
                )
                updated_rows = list(all_rows_result.all())
                return self._to_view(registered, updated_rows)

        # Chế độ in-memory có snapshot rollback nguyên tử khi phát sinh lỗi
        ws_id = principal.workspace_id
        snapshot_rows = [_clone_release_row(row) for row in self._in_memory_releases.get(ws_id, [])]
        snapshot_audits = list(self._in_memory_audits)
        try:
            bucket = self._in_memory_releases.setdefault(ws_id, [])
            existing_rows = [row for row in bucket if row.name == registered.release_id]
            target, before_state, after_state = self._apply_publish(
                ws_id,
                registered,
                recipe_payload,
                existing_rows,
            )
            if target not in existing_rows:
                bucket.append(target)
            self._record_in_memory_audit(
                principal=principal,
                action="strategy_release.published",
                target_id=target.id,
                reason=clean_reason,
                before_state=before_state,
                after_state=after_state,
                request_id=request_id,
            )
            return self._to_view(
                registered,
                [row for row in bucket if row.name == registered.release_id],
            )
        except BaseException:
            self._in_memory_releases[ws_id] = snapshot_rows
            self._in_memory_audits = snapshot_audits
            raise

    @staticmethod
    def _apply_publish(
        workspace_id: UUID,
        registered: RegisteredRelease,
        recipe_payload: dict[str, Any],
        existing_rows: Sequence[StrategyRelease],
    ) -> tuple[StrategyRelease, dict[str, Any] | None, dict[str, Any]]:
        """Cập nhật hoặc tạo mới hàng `strategy_release` khớp mã băm catalog."""
        latest_existing = max(existing_rows, key=lambda r: r.version, default=None)
        before_state = (
            _serialize_release_row(latest_existing) if latest_existing is not None else None
        )

        # Thu hồi các hàng cũ lệch hash đang PUBLISHED để đảm bảo chỉ có duy nhất 1 hàng active
        for row in existing_rows:
            if row.content_hash != registered.content_hash and row.status == "PUBLISHED":
                row.status = "RETIRED"

        matched = next(
            (row for row in existing_rows if row.content_hash == registered.content_hash),
            None,
        )
        if matched is not None:
            before_state = _serialize_release_row(matched)
            matched.status = "PUBLISHED"
            matched.recipe = dict(recipe_payload)
            target = matched
        else:
            next_version = max((row.version for row in existing_rows), default=0) + 1
            target = StrategyRelease(
                id=uuid4(),
                workspace_id=workspace_id,
                name=registered.release_id,
                version=next_version,
                status="PUBLISHED",
                recipe=dict(recipe_payload),
                content_hash=registered.content_hash,
                created_at=datetime.now(UTC),
            )
        after_state = _serialize_release_row(target)
        return target, before_state, after_state

    async def retire_release(
        self,
        principal: Principal,
        *,
        release_id: str,
        reason: str,
        expected_content_hash: str | None = None,
        request_id: UUID | None = None,
        step_up: StepUpProof | None = None,
    ) -> CatalogReleaseView:
        """Thu hồi (`RETIRED`) một `strategy_release` đang công bố trong workspace."""
        clean_reason = _validate_reason_vi(reason)
        registered = self._resolve_catalog_release(
            release_id, expected_content_hash=expected_content_hash
        )
        await self._verify_step_up(
            principal,
            action=ACTION_STRATEGY_RELEASE_RETIRE,
            release_id=registered.release_id,
            expected_content_hash=expected_content_hash,
            reason=clean_reason,
            step_up=step_up,
        )

        if self._session_factory is not None:
            async with self._uow(principal, request_id=request_id) as unit:
                await WorkspaceSettingsRepository(unit.session).lock_workspace(
                    principal.workspace_id
                )
                rows_result = await unit.session.scalars(
                    select(StrategyRelease)
                    .where(
                        StrategyRelease.workspace_id == principal.workspace_id,
                        StrategyRelease.name == registered.release_id,
                    )
                    .with_for_update()
                )
                existing_rows = list(rows_result.all())
                target, before_state, after_state = self._apply_retire(registered, existing_rows)
                unit.record_audit(
                    action="strategy_release.retired",
                    target_type="strategy_release",
                    target_id=target.id,
                    reason=clean_reason,
                    before_state=before_state,
                    after_state=after_state,
                )
                return self._to_view(registered, existing_rows)

        ws_id = principal.workspace_id
        snapshot_rows = [_clone_release_row(row) for row in self._in_memory_releases.get(ws_id, [])]
        snapshot_audits = list(self._in_memory_audits)
        try:
            bucket = self._in_memory_releases.setdefault(ws_id, [])
            existing_rows = [row for row in bucket if row.name == registered.release_id]
            target, before_state, after_state = self._apply_retire(registered, existing_rows)
            self._record_in_memory_audit(
                principal=principal,
                action="strategy_release.retired",
                target_id=target.id,
                reason=clean_reason,
                before_state=before_state,
                after_state=after_state,
                request_id=request_id,
            )
            return self._to_view(registered, existing_rows)
        except BaseException:
            self._in_memory_releases[ws_id] = snapshot_rows
            self._in_memory_audits = snapshot_audits
            raise

    @staticmethod
    def _apply_retire(
        registered: RegisteredRelease,
        existing_rows: Sequence[StrategyRelease],
    ) -> tuple[StrategyRelease, dict[str, Any], dict[str, Any]]:
        """Chuyển trạng thái hàng `PUBLISHED` sang `RETIRED`."""
        published_rows = [row for row in existing_rows if row.status == "PUBLISHED"]
        if not published_rows:
            raise StrategyReleaseAdminError(
                409,
                f"Bản phát hành '{registered.release_id}' hiện chưa được công bố hoặc đã thu hồi "
                "trong không gian làm việc này.",
                reason_code="RELEASE_NOT_PUBLISHED",
            )
        target = next(
            (row for row in published_rows if row.content_hash == registered.content_hash),
            published_rows[0],
        )
        before_state = _serialize_release_row(target)
        for row in published_rows:
            row.status = "RETIRED"
        after_state = _serialize_release_row(target)
        return target, before_state, after_state

    def _record_in_memory_audit(
        self,
        *,
        principal: Principal,
        action: str,
        target_id: UUID,
        reason: str,
        before_state: object | None,
        after_state: object | None,
        request_id: UUID | None,
    ) -> None:
        self._in_memory_audits.append(
            AuditEvent(
                id=uuid4(),
                workspace_id=principal.workspace_id,
                actor_membership_id=principal.membership_id,
                action=action,
                target_type="strategy_release",
                target_id=target_id,
                reason=reason,
                before_hash=state_hash(before_state) if before_state is not None else None,
                after_hash=state_hash(after_state) if after_state is not None else None,
                request_id=request_id or uuid4(),
                occurred_at=datetime.now(UTC),
            )
        )
