"""Ghi bộ dữ liệu QA vào PostgreSQL theo từng lớp (hợp đồng mở rộng cho các WP sau).

Mỗi lớp là một coroutine nhận `SeedContext` và chạy trong cùng một transaction do
`apply_dataset` mở. Lớp phải idempotent: tạo hàng theo ID tất định, hàng đã có phải khớp
identity của dataset (nếu không thì fail closed), field mutable được đưa về baseline, bảng
bất biến chỉ được kiểm tra. Mật khẩu chỉ đi qua `PasswordManager`, TOTP seed chỉ đi qua
`SecretProvider`; không có giá trị bí mật nào được ghi vào DB hoặc thông báo lỗi.
"""

from __future__ import annotations

import asyncio
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fixtures.qa_dataset import SEEDED_AT, QaDataset, QaMember, qa_id

from trading_bot.adapters.database.models import (
    BotConfigVersion,
    BotGrant,
    BotInstance,
    MfaFactor,
    PaperAccountSeed,
    RiskProfileVersion,
    RoleBinding,
    SecretReference,
    StrategyRelease,
    TradingAccount,
    UserAccount,
    UserMembership,
    Workspace,
    WorkspaceSettingsVersion,
)
from trading_bot.adapters.database.models.market_entities import MarketBar, MarketInstrument
from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionRepository,
)
from trading_bot.adapters.database.repositories.market_data import (
    MarketDataRepository,
    MarketRepositoryConflict,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.bot_config_resolver import (
    load_strategy_releases,
    validate_bot_overlay,
)
from trading_bot.application.runtime_risk_profile import load_risk_profile
from trading_bot.auth.security import PasswordManager
from trading_bot.config.bot_settings import BotConfigOverlay
from trading_bot.domain.market.ingestion import IngestionDisposition
from trading_bot.ports.secrets import SecretAccessError, SecretProvider

REQUIRED_TABLES = (
    "workspace",
    "user_account",
    "user_membership",
    "role_binding",
    "mfa_factor",
    "secret_reference",
    "trading_account",
    "bot_instance",
    "bot_grant",
    "risk_profile_version",
    "workspace_settings_version",
    "paper_account_seed",
    "strategy_release",
    "bot_config_version",
    "market_instrument",
    "market_bar",
)


class SeedGuardError(RuntimeError):
    """Chốt an toàn từ chối chạy seed hoặc dữ liệu hiện có xung đột với dataset QA."""


@dataclass(frozen=True, slots=True)
class DatabaseFacts:
    """Dữ kiện đọc từ database đích, dùng cho chốt an toàn dữ liệu trước khi ghi."""

    privileged: bool
    missing_tables: tuple[str, ...]
    non_paper_workspaces: int
    non_paper_accounts: int
    slug_owners: Mapping[str, UUID]
    email_owners: Mapping[str, UUID]
    account_name_owners: Mapping[tuple[UUID, str], UUID]
    settings_v1_owners: Mapping[UUID, UUID]


def validate_database_facts(facts: DatabaseFacts, dataset: QaDataset) -> None:
    """Fail closed nếu schema thiếu, database có dữ liệu không PAPER hoặc khóa bị chiếm."""
    if facts.missing_tables:
        raise SeedGuardError(
            "Schema thiếu bảng "
            f"{', '.join(facts.missing_tables)}; chạy migrator (alembic upgrade head) trước."
        )
    if not facts.privileged:
        raise SeedGuardError(
            "Seed cần role owner local (superuser/BYPASSRLS) giống migrator để ghi nhiều scope."
        )
    if facts.non_paper_workspaces or facts.non_paper_accounts:
        raise SeedGuardError(
            "Từ chối chạy: database có workspace/account không phải PAPER, không phải môi "
            "trường QA local."
        )
    expected_slugs = {workspace.slug: workspace.id for workspace in dataset.workspaces}
    expected_emails = {member.email: member.user_id for member in dataset.members}
    expected_accounts = {
        (dataset.workspace(account.workspace_key).id, account.name): account.id
        for account in dataset.accounts
    }
    expected_settings = {
        dataset.workspace(version.workspace_key).id: version.id
        for version in dataset.settings_versions
    }
    checks: tuple[tuple[str, Mapping[Any, UUID], Mapping[Any, UUID]], ...] = (
        ("slug workspace", facts.slug_owners, expected_slugs),
        ("email người dùng", facts.email_owners, expected_emails),
        ("tên account", facts.account_name_owners, expected_accounts),
        ("settings version 1", facts.settings_v1_owners, expected_settings),
    )
    for label, observed, expected in checks:
        for key, owner in observed.items():
            if expected.get(key) != owner:
                raise SeedGuardError(
                    f"Xung đột {label} {key!r}: đã thuộc bản ghi {owner} ngoài dataset QA; "
                    "từ chối ghi đè."
                )


@dataclass(frozen=True, slots=True)
class AccountCredential:
    """Mật khẩu và TOTP seed của một tài khoản QA; repr không lộ giá trị."""

    password: str = field(repr=False)
    totp_secret: str = field(repr=False)


@dataclass(slots=True)
class SeedContext:
    """Trạng thái dùng chung giữa các lớp seed trong cùng một transaction."""

    session: AsyncSession
    dataset: QaDataset
    credentials: Mapping[str, AccountCredential]
    secret_provider: SecretProvider
    passwords: PasswordManager
    now: datetime
    changes: Counter[str] = field(default_factory=Counter)
    bot_ids: dict[str, UUID] = field(default_factory=dict)
    stored_references: list[tuple[UUID, str]] = field(default_factory=list)
    superseded_references: list[tuple[UUID, str]] = field(default_factory=list)

    def workspace_id(self, workspace_key: str) -> UUID:
        return self.dataset.workspace(workspace_key).id


SeedLayer = Callable[[SeedContext], Awaitable[None]]


async def _ensure_row(
    context: SeedContext,
    model: Any,
    row_id: UUID,
    values: Mapping[str, Any],
    *,
    identity: Sequence[str],
    mutable: Sequence[str] = (),
) -> Any:
    """Tạo hàng theo ID tất định; hàng đã có phải khớp identity, field mutable về baseline."""
    table = model.__tablename__
    row = await context.session.get(model, row_id)
    if row is None:
        row = model(id=row_id, **values)
        context.session.add(row)
        await context.session.flush()
        context.changes[f"{table}.created"] += 1
        return row
    for name in identity:
        if getattr(row, name) != values[name]:
            raise SeedGuardError(
                f"Bản ghi {table} {row_id} đã có {name} khác dataset QA; từ chối ghi đè."
            )
    changed = False
    for name in mutable:
        if getattr(row, name) != values[name]:
            setattr(row, name, values[name])
            changed = True
    if changed:
        await context.session.flush()
        context.changes[f"{table}.updated"] += 1
    return row


async def _ensure_user(context: SeedContext, member: QaMember) -> None:
    credential = context.credentials[member.key]
    row = await context.session.get(UserAccount, member.user_id)
    if row is None:
        password_hash = await asyncio.to_thread(context.passwords.hash, credential.password)
        context.session.add(
            UserAccount(
                id=member.user_id,
                email_normalized=member.email,
                password_hash=password_hash,
                status=member.user_status,
                created_at=SEEDED_AT,
                updated_at=SEEDED_AT,
            )
        )
        await context.session.flush()
        context.changes["user_account.created"] += 1
        return
    if row.email_normalized != member.email:
        raise SeedGuardError(f"user_account {member.user_id} có email khác dataset QA.")
    changed = False
    # Tệp credentials là nguồn sự thật: mật khẩu đổi khi tệp được tạo lại.
    if not await asyncio.to_thread(
        context.passwords.verify, row.password_hash, credential.password
    ):
        row.password_hash = await asyncio.to_thread(context.passwords.hash, credential.password)
        context.changes["user_account.password_rotated"] += 1
        changed = True
    if row.status != member.user_status:
        row.status = member.user_status
        context.changes["user_account.updated"] += 1
        changed = True
    if changed:
        await context.session.flush()


async def _ensure_role_bindings(context: SeedContext, member: QaMember, workspace_id: UUID) -> None:
    session = context.session
    existing = list(
        await session.scalars(
            select(RoleBinding).where(
                RoleBinding.workspace_id == workspace_id,
                RoleBinding.membership_id == member.membership_id,
            )
        )
    )
    desired = {role.value for role in member.roles}
    for binding in existing:
        if binding.role not in desired:
            await session.delete(binding)
            context.changes["role_binding.deleted"] += 1
    present = {binding.role for binding in existing}
    for role in member.roles:
        if role.value not in present:
            session.add(
                RoleBinding(
                    id=member.role_binding_id(role),
                    workspace_id=workspace_id,
                    membership_id=member.membership_id,
                    role=role.value,
                    created_at=SEEDED_AT,
                )
            )
            context.changes["role_binding.created"] += 1
    await session.flush()


def _secret_matches(
    provider: SecretProvider, workspace_id: UUID, reference: str, expected: str
) -> bool:
    try:
        current = provider.resolve_mfa_secret(workspace_id, reference)
    except SecretAccessError:
        return False
    return secrets.compare_digest(current.encode("utf-8"), expected.encode("utf-8"))


async def _ensure_mfa(context: SeedContext, member: QaMember, workspace_id: UUID) -> None:
    """Một TOTP factor đã xác minh; seed chỉ đi qua SecretProvider, DB giữ reference."""
    session = context.session
    provider = context.secret_provider
    totp_secret = context.credentials[member.key].totp_secret
    others = list(
        await session.scalars(
            select(MfaFactor).where(
                MfaFactor.workspace_id == workspace_id,
                MfaFactor.membership_id == member.membership_id,
                MfaFactor.id != member.mfa_factor_id,
            )
        )
    )
    for other in others:
        # Login chọn factor đầu tiên; factor lạ làm đăng nhập QA không tất định.
        context.superseded_references.append((workspace_id, other.secret_reference))
        await session.delete(other)
        context.changes["mfa_factor.deleted"] += 1
    factor = await session.get(MfaFactor, member.mfa_factor_id)
    if factor is not None and (
        factor.workspace_id != workspace_id
        or factor.membership_id != member.membership_id
        or factor.factor_type != "TOTP"
    ):
        raise SeedGuardError(f"mfa_factor {factor.id} không khớp thành viên {member.key}.")
    current = factor is not None and _secret_matches(
        provider, workspace_id, factor.secret_reference, totp_secret
    )
    if factor is not None and current:
        reference = factor.secret_reference
    else:
        reference = provider.store_mfa_secret(workspace_id, totp_secret)
        context.stored_references.append((workspace_id, reference))
        if factor is not None:
            context.superseded_references.append((workspace_id, factor.secret_reference))
    await _ensure_secret_reference(context, member, workspace_id, reference)
    if factor is None:
        session.add(
            MfaFactor(
                id=member.mfa_factor_id,
                workspace_id=workspace_id,
                membership_id=member.membership_id,
                factor_type="TOTP",
                secret_reference=reference,
                verified_at=SEEDED_AT,
                last_used_counter=None,
                created_at=SEEDED_AT,
            )
        )
        context.changes["mfa_factor.created"] += 1
    elif not current or factor.verified_at is None:
        factor.secret_reference = reference
        factor.verified_at = SEEDED_AT
        factor.last_used_counter = None
        context.changes["mfa_factor.rotated"] += 1
    await session.flush()


async def _ensure_secret_reference(
    context: SeedContext, member: QaMember, workspace_id: UUID, reference: str
) -> None:
    row = await context.session.get(SecretReference, member.secret_reference_id)
    if row is None:
        context.session.add(
            SecretReference(
                id=member.secret_reference_id,
                workspace_id=workspace_id,
                provider="local_mfa",
                reference_key=reference,
                status="ACTIVE",
                created_at=SEEDED_AT,
            )
        )
        context.changes["secret_reference.created"] += 1
        return
    if row.workspace_id != workspace_id or row.provider != "local_mfa":
        raise SeedGuardError(f"secret_reference {row.id} không khớp dataset QA.")
    if row.reference_key != reference or row.status != "ACTIVE":
        row.reference_key = reference
        row.status = "ACTIVE"
        context.changes["secret_reference.updated"] += 1


async def apply_access_layer(context: SeedContext) -> None:
    """Workspace, người dùng, membership, role và MFA."""
    for workspace in context.dataset.workspaces:
        await _ensure_row(
            context,
            Workspace,
            workspace.id,
            {
                "name": workspace.name,
                "slug": workspace.slug,
                "mode": workspace.mode,
                "status": workspace.status,
                "created_at": SEEDED_AT,
            },
            identity=("slug", "mode"),
            mutable=("name", "status"),
        )
    for member in context.dataset.members:
        workspace_id = context.workspace_id(member.workspace_key)
        await _ensure_user(context, member)
        await _ensure_row(
            context,
            UserMembership,
            member.membership_id,
            {
                "workspace_id": workspace_id,
                "user_id": member.user_id,
                "status": member.membership_status,
                "created_at": SEEDED_AT,
            },
            identity=("workspace_id", "user_id"),
            mutable=("status",),
        )
        await _ensure_role_bindings(context, member, workspace_id)
        await _ensure_mfa(context, member, workspace_id)


async def apply_root_settings_layer(context: SeedContext) -> None:
    """Risk profile và WorkspaceSettings v1 đã publish (bất biến)."""
    dataset = context.dataset
    for profile in dataset.risk_profiles:
        await _ensure_row(
            context,
            RiskProfileVersion,
            profile.id,
            {
                "workspace_id": context.workspace_id(profile.workspace_key),
                "name": profile.name,
                "version": profile.version,
                "max_risk_fraction": profile.max_risk_fraction,
                "max_open_risk_fraction": profile.max_open_risk_fraction,
                "settings": profile.settings,
                "is_active": profile.is_active,
                "created_at": SEEDED_AT,
            },
            identity=(
                "workspace_id",
                "name",
                "version",
                "max_risk_fraction",
                "max_open_risk_fraction",
                "settings",
            ),
            mutable=("is_active",),
        )
    for version in dataset.settings_versions:
        owner = dataset.member(version.approved_by_member_key)
        creator = dataset.member(version.created_by_member_key)
        await _ensure_row(
            context,
            WorkspaceSettingsVersion,
            version.id,
            {
                "workspace_id": context.workspace_id(version.workspace_key),
                "version": version.version,
                "parent_version_id": None,
                "schema_version": version.settings.schema_version,
                "settings_json": version.settings_json,
                "settings_hash": version.settings_hash,
                "status": "PUBLISHED",
                "revision": 1,
                "created_by": creator.membership_id,
                "approved_by": owner.membership_id,
                "published_at": version.published_at,
                "effective_at": version.effective_at,
                "created_at": SEEDED_AT,
            },
            identity=("workspace_id", "version", "settings_hash", "status"),
        )


async def _ensure_bot_grants(context: SeedContext) -> None:
    """Grant còn hiệu lực đúng như dataset; grant lạ của thành viên QA bị thu hồi."""
    dataset = context.dataset
    session = context.session
    desired = {
        (
            context.bot_ids[grant.account_key],
            dataset.member(grant.member_key).membership_id,
            grant.permission.value,
        ): grant
        for grant in dataset.grants
    }
    rows = list(
        await session.scalars(
            select(BotGrant).where(
                BotGrant.workspace_id.in_([workspace.id for workspace in dataset.workspaces]),
                BotGrant.membership_id.in_([member.membership_id for member in dataset.members]),
            )
        )
    )
    history: Counter[tuple[UUID, UUID, str]] = Counter()
    active: set[tuple[UUID, UUID, str]] = set()
    for row in rows:
        key = (row.bot_id, row.membership_id, row.permission)
        history[key] += 1
        if row.revoked_at is not None:
            continue
        if key in desired:
            active.add(key)
        else:
            row.revoked_at = context.now
            context.changes["bot_grant.revoked"] += 1
    for key, grant in desired.items():
        if key in active:
            continue
        issued = history[key]
        # Lịch sử grant bất biến: cấp lại sau khi bị thu hồi tạo hàng mới, ID vẫn tất định.
        grant_id = (
            grant.id
            if issued == 0
            else qa_id(
                "bot_grant",
                f"{grant.account_key}:{grant.member_key}:{grant.permission.value}:reissue:{issued}",
            )
        )
        bot_id, membership_id, permission = key
        session.add(
            BotGrant(
                id=grant_id,
                workspace_id=context.workspace_id(grant.workspace_key),
                bot_id=bot_id,
                membership_id=membership_id,
                permission=permission,
                granted_by=dataset.member(grant.granted_by_member_key).membership_id,
                created_at=SEEDED_AT if issued == 0 else context.now,
            )
        )
        context.changes["bot_grant.created"] += 1
    await session.flush()


async def _ensure_paper_seed(context: SeedContext, seed_key: str) -> None:
    dataset = context.dataset
    seed = next(item for item in dataset.paper_seeds if item.account_key == seed_key)
    account = dataset.account(seed.account_key)
    workspace_id = context.workspace_id(seed.workspace_key)
    existing = await context.session.scalar(
        select(PaperAccountSeed).where(
            PaperAccountSeed.workspace_id == workspace_id,
            PaperAccountSeed.trading_account_id == account.id,
        )
    )
    if existing is not None:
        if (
            existing.quote_asset != seed.quote_asset
            or existing.initial_equity != seed.initial_equity
            or existing.idempotency_key != seed.idempotency_key
        ):
            raise SeedGuardError(f"paper_account_seed của account {account.name} khác dataset QA.")
        return
    context.session.add(
        PaperAccountSeed(
            id=seed.id,
            bot_id=context.bot_ids[seed.account_key],
            workspace_id=workspace_id,
            trading_account_id=account.id,
            quote_asset=seed.quote_asset,
            initial_equity=seed.initial_equity,
            source_settings_version_id=seed.settings_version_id,
            source_settings_hash=seed.settings_hash,
            idempotency_key=seed.idempotency_key,
            created_at=SEEDED_AT,
        )
    )
    await context.session.flush()
    context.changes["paper_account_seed.created"] += 1


async def apply_bots_layer(context: SeedContext) -> None:
    """PAPER account (trigger tạo bot), grant theo bot và seed vốn PAPER."""
    session = context.session
    for account in context.dataset.accounts:
        workspace_id = context.workspace_id(account.workspace_key)
        await _ensure_row(
            context,
            TradingAccount,
            account.id,
            {
                "workspace_id": workspace_id,
                "name": account.name,
                "venue": account.venue,
                "mode": account.mode,
                "environment": account.environment,
                "status": account.status,
                "max_notional": account.max_notional,
                "created_at": SEEDED_AT,
                "updated_at": SEEDED_AT,
            },
            identity=("workspace_id", "venue", "mode", "environment"),
            mutable=("name", "status", "max_notional"),
        )
        bot_id = await session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.trading_account_id == account.id,
            )
        )
        if bot_id is None:
            raise SeedGuardError(
                f"Trigger không tạo bot_instance cho account {account.name}; kiểm tra MB-01."
            )
        context.bot_ids[account.key] = bot_id
    await _ensure_bot_grants(context)
    for seed in context.dataset.paper_seeds:
        await _ensure_paper_seed(context, seed.account_key)


async def apply_strategy_releases_layer(context: SeedContext) -> None:
    """Release H01/H03 PUBLISHED khớp hash registry, để resolver MB-02 chấp nhận overlay."""
    for release in context.dataset.strategy_releases:
        workspace_id = context.workspace_id(release.workspace_key)
        owner = await context.session.scalar(
            select(StrategyRelease.id).where(
                StrategyRelease.workspace_id == workspace_id,
                StrategyRelease.name == release.name,
                StrategyRelease.version == release.version,
            )
        )
        if owner is not None and owner != release.id:
            raise SeedGuardError(
                f"Xung đột strategy_release {release.name} v{release.version}: đã thuộc bản "
                f"ghi {owner} ngoài dataset QA; từ chối ghi đè."
            )
        await _ensure_row(
            context,
            StrategyRelease,
            release.id,
            {
                "workspace_id": workspace_id,
                "name": release.name,
                "version": release.version,
                "status": release.status,
                "recipe": {},
                "content_hash": release.content_hash,
                "created_at": SEEDED_AT,
            },
            identity=("workspace_id", "name", "version", "content_hash"),
            mutable=("status",),
        )


def bot_config_publication(
    head: Any, baseline_hash: str, *, effective_at: datetime, now: datetime
) -> tuple[UUID | None, datetime] | None:
    """Quyết định publish cho một bot: `None` nếu head đã là baseline.

    Chưa có version: publish baseline hiệu lực từ `effective_at` (mốc dataset). Head lệch
    baseline (vd. QA publish tay): publish version mới nối sau head, hiệu lực từ `now` vì
    lịch sử version bất biến và không được lùi hiệu lực trước head.
    """
    if head is None:
        return None, effective_at
    if head.overlay_hash == baseline_hash:
        return None
    return head.id, now


async def apply_bot_configs_layer(context: SeedContext) -> None:
    """Config bot PUBLISHED cho bot A/B qua repository MB-02 (DRAFT → validate → PUBLISHED)."""
    session = context.session
    dataset = context.dataset
    repository = BotConfigVersionRepository(session)
    for config in dataset.bot_configs:
        workspace_id = context.workspace_id(config.workspace_key)
        bot_id = context.bot_ids[config.account_key]
        account = await session.get(TradingAccount, dataset.account(config.account_key).id)
        if account is None:
            raise SeedGuardError(f"Thiếu account {config.account_key} cho cấu hình bot QA.")
        await repository.lock_bot(workspace_id=workspace_id, bot_id=bot_id)
        head = await repository.get_head(workspace_id=workspace_id, bot_id=bot_id)
        decision = bot_config_publication(
            head, config.overlay_hash, effective_at=config.effective_at, now=context.now
        )
        if decision is None:
            continue
        parent_id, effective_at = decision
        root = await WorkspaceSettingsRepository(session).load_effective_config(
            workspace_id=workspace_id, as_of=effective_at
        )
        if root is None:
            raise SeedGuardError("Root settings QA chưa hiệu lực; không thể publish config bot.")
        overlay = BotConfigOverlay.model_validate(config.overlay_json)
        issues = validate_bot_overlay(
            overlay,
            workspace_settings=root,
            account=account,
            root_risk_profile=await load_risk_profile(
                session, workspace_id=workspace_id, profile_id=root.settings.risk.profile_version_id
            ),
            strategy_releases=await load_strategy_releases(session, workspace_id=workspace_id),
        )
        if issues:
            fields = ", ".join(issue.field for issue in issues)
            raise SeedGuardError(f"Overlay QA của bot {config.account_key} không hợp lệ: {fields}.")
        draft = await repository.create_draft(
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account.id,
            overlay=overlay,
            base_settings_version_id=root.settings_version_id,
            base_settings_hash=root.settings_hash,
            created_by=dataset.member(config.created_by_member_key).membership_id,
            parent_version_id=parent_id,
        )
        await repository.publish_draft(
            workspace_id=workspace_id,
            bot_id=bot_id,
            draft_id=draft.id,
            expected_revision=draft.revision,
            expected_parent_version_id=parent_id,
            approved_by=dataset.member(config.approved_by_member_key).membership_id,
            base_settings_version_id=root.settings_version_id,
            base_settings_hash=root.settings_hash,
            effective_at=effective_at,
        )
        context.changes["bot_config_version.published"] += 1


async def apply_market_layer(context: SeedContext) -> None:
    """Instrument và nến qua MarketDataRepository (idempotency key, revision 1)."""
    repository = MarketDataRepository(context.session)
    instrument_ids: dict[tuple[str, str], UUID] = {}
    try:
        for instrument in context.dataset.instruments:
            result = await repository.append_instrument(
                workspace_id=context.workspace_id(instrument.workspace_key),
                observation=instrument.observation,
                idempotency_key=instrument.idempotency_key,
                received_at=instrument.received_at,
                available_at=instrument.available_at,
            )
            if result.disposition is not IngestionDisposition.DUPLICATE:
                context.changes["market_instrument.created"] += 1
            key = (instrument.workspace_key, instrument.observation.instrument.symbol)
            instrument_ids[key] = result.record_id
        for item in context.dataset.bars:
            result = await repository.append_bar(
                workspace_id=context.workspace_id(item.workspace_key),
                instrument_id=instrument_ids[(item.workspace_key, item.bar.symbol)],
                bar=item.bar,
                version=item.version,
            )
            if result.disposition is IngestionDisposition.ACCEPT:
                context.changes["market_bar.created"] += 1
            elif result.disposition is not IngestionDisposition.DUPLICATE:
                raise SeedGuardError(f"Nến QA {item.version.logical_key} bị xem là revision mới.")
    except MarketRepositoryConflict as error:
        raise SeedGuardError(f"Dữ liệu thị trường QA xung đột: {error}") from error


# Thứ tự lớp là hợp đồng mở rộng: WP sau chèn lớp mới (vd. cấu hình bot sau "bots",
# evidence dùng chung sau "market") thay vì sửa lớp hiện có. Xem docs/testing/qa-test-data.md.
SEED_LAYERS: tuple[tuple[str, SeedLayer], ...] = (
    ("access", apply_access_layer),
    ("root_settings", apply_root_settings_layer),
    ("strategy_releases", apply_strategy_releases_layer),
    ("bots", apply_bots_layer),
    ("bot_configs", apply_bot_configs_layer),
    ("market", apply_market_layer),
)
_COUNTED_MODELS: tuple[Any, ...] = (
    UserMembership,
    RoleBinding,
    MfaFactor,
    SecretReference,
    TradingAccount,
    BotInstance,
    BotGrant,
    RiskProfileVersion,
    WorkspaceSettingsVersion,
    PaperAccountSeed,
    StrategyRelease,
    BotConfigVersion,
    MarketInstrument,
    MarketBar,
)


async def collect_database_facts(session: AsyncSession, dataset: QaDataset) -> DatabaseFacts:
    privileged = bool(
        await session.scalar(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    )
    missing = []
    for table in REQUIRED_TABLES:
        if await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None:
            missing.append(table)
    if missing:
        return DatabaseFacts(privileged, tuple(missing), 0, 0, {}, {}, {}, {})
    workspace_ids = [workspace.id for workspace in dataset.workspaces]
    non_paper_workspaces = await session.scalar(
        select(func.count()).select_from(Workspace).where(Workspace.mode != "PAPER")
    )
    non_paper_accounts = await session.scalar(
        select(func.count()).select_from(TradingAccount).where(TradingAccount.mode != "PAPER")
    )
    slug_rows = await session.execute(
        select(Workspace.slug, Workspace.id).where(
            Workspace.slug.in_([workspace.slug for workspace in dataset.workspaces])
        )
    )
    email_rows = await session.execute(
        select(UserAccount.email_normalized, UserAccount.id).where(
            UserAccount.email_normalized.in_([member.email for member in dataset.members])
        )
    )
    account_rows = await session.execute(
        select(TradingAccount.workspace_id, TradingAccount.name, TradingAccount.id).where(
            TradingAccount.workspace_id.in_(workspace_ids),
            TradingAccount.name.in_([account.name for account in dataset.accounts]),
        )
    )
    settings_rows = await session.execute(
        select(WorkspaceSettingsVersion.workspace_id, WorkspaceSettingsVersion.id).where(
            WorkspaceSettingsVersion.workspace_id.in_(workspace_ids),
            WorkspaceSettingsVersion.version == 1,
        )
    )
    return DatabaseFacts(
        privileged=privileged,
        missing_tables=(),
        non_paper_workspaces=int(non_paper_workspaces or 0),
        non_paper_accounts=int(non_paper_accounts or 0),
        slug_owners=dict(slug_rows.all()),
        email_owners=dict(email_rows.all()),
        account_name_owners={
            (workspace_id, name): row_id for workspace_id, name, row_id in account_rows.all()
        },
        settings_v1_owners=dict(settings_rows.all()),
    )


async def count_dataset_rows(session: AsyncSession, dataset: QaDataset) -> dict[str, int]:
    workspace_ids = [workspace.id for workspace in dataset.workspaces]
    counts = {
        "workspace": await session.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.id.in_(workspace_ids))
        ),
        "user_account": await session.scalar(
            select(func.count())
            .select_from(UserAccount)
            .where(UserAccount.id.in_([member.user_id for member in dataset.members]))
        ),
    }
    for model in _COUNTED_MODELS:
        counts[model.__tablename__] = await session.scalar(
            select(func.count()).select_from(model).where(model.workspace_id.in_(workspace_ids))
        )
    return {table: int(value or 0) for table, value in counts.items()}


def releasable_references(
    superseded: Sequence[tuple[UUID, str]], live_references: set[str]
) -> list[tuple[UUID, str]]:
    """Chỉ trả reference không còn hàng nào trỏ tới, mỗi reference một lần.

    Tệp TOTP được định vị chỉ theo reference (không theo workspace), nên một factor lạ
    trỏ cùng tệp với factor thật không được làm xóa tệp đang dùng.
    """
    releasable: list[tuple[UUID, str]] = []
    seen: set[str] = set()
    for workspace_id, reference in superseded:
        if reference in live_references or reference in seen:
            continue
        seen.add(reference)
        releasable.append((workspace_id, reference))
    return releasable


async def collect_live_mfa_references(session: AsyncSession) -> set[str]:
    """Mọi reference MFA còn được tham chiếu trong toàn database (sau các lớp seed)."""
    factor_references = await session.scalars(select(MfaFactor.secret_reference))
    secret_references = await session.scalars(
        select(SecretReference.reference_key).where(SecretReference.provider == "local_mfa")
    )
    return set(factor_references.all()) | set(secret_references.all())


async def apply_dataset(
    sessions: async_sessionmaker[AsyncSession],
    dataset: QaDataset,
    credentials: Mapping[str, AccountCredential],
    *,
    secret_provider: SecretProvider,
    passwords: PasswordManager | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ghi mọi lớp trong một transaction; lỗi giữa chừng xóa TOTP vừa mã hóa."""
    context: SeedContext | None = None
    try:
        async with sessions() as session, session.begin():
            # Tuần tự hóa hai lần seed chạy đồng thời trên cùng database.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended('crypidian-qa-seed', 0))")
            )
            validate_database_facts(await collect_database_facts(session, dataset), dataset)
            context = SeedContext(
                session=session,
                dataset=dataset,
                credentials=credentials,
                secret_provider=secret_provider,
                passwords=passwords or PasswordManager(),
                now=now or datetime.now(UTC),
            )
            for _name, layer in SEED_LAYERS:
                await layer(context)
            counts = await count_dataset_rows(session, dataset)
            await session.flush()
            # Xác định tệp được phép xóa trong cùng transaction, trước commit.
            releasable = releasable_references(
                context.superseded_references, await collect_live_mfa_references(session)
            )
    except BaseException:
        if context is not None:
            for workspace_id, reference in context.stored_references:
                secret_provider.delete_mfa_secret(workspace_id, reference)
        raise
    assert context is not None
    # Chỉ xóa tệp TOTP bị thay sau khi commit thành công và không còn hàng nào trỏ tới.
    for workspace_id, reference in releasable:
        secret_provider.delete_mfa_secret(workspace_id, reference)
    return {
        "dataset_version": dataset.version,
        "workspaces": {
            workspace.key: {"id": str(workspace.id), "slug": workspace.slug}
            for workspace in dataset.workspaces
        },
        "bots": {key: str(bot_id) for key, bot_id in sorted(context.bot_ids.items())},
        "counts": counts,
        "changes": {key: value for key, value in sorted(context.changes.items()) if value},
    }
