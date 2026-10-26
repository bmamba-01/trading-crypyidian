"""Hợp nhất root settings đã publish với overlay bot đã publish thành cấu hình hiệu lực.

Resolver thuần (không I/O) để kết quả xác định và kiểm chứng được bằng hash; hàm async
``load_effective_bot_config`` chỉ nạp input trong transaction do caller quản lý.
Mọi vi phạm đều fail-closed: không tự kẹp (clamp) hay bỏ qua giá trị overlay.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotConfigVersion,
    BotInstance,
    StrategyRelease,
    TradingAccount,
)
from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionRepository,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    SettingsVersionConflict,
    WorkspaceSettingsRepository,
)
from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    RuntimeRiskProfile,
    load_risk_profile,
)
from trading_bot.config.bot_settings import (
    RISK_FIELDS,
    BotConfigOverlay,
    decimal_text,
    overlay_hash,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.risk.planner import RiskLimits
from trading_bot.domain.strategy.contracts import StrategySelection
from trading_bot.domain.strategy.registry import (
    StrategyRegistry,
    UnknownStrategyReleaseError,
)
from trading_bot.domain.strategy.registry import (
    StrategyRelease as RegisteredRelease,
)

EFFECTIVE_CONFIG_SCHEMA = "effective-bot-config-v1"
# Venue lưu ở TradingAccount (chữ thường) và ở root settings (chữ hoa) phải chỉ cùng một sàn.
_ACCOUNT_VENUES = {"BINANCE_USDM": "binance_usdm"}
_PARAMETERIZED_RELEASES = frozenset({"h03-v1"})


class ReleaseCatalog(Protocol):
    """Nguồn metadata release; mặc định là registry built-in."""

    def get_release(self, release_id: str) -> RegisteredRelease: ...


@dataclass(frozen=True, slots=True)
class OverlayIssue:
    """Một vi phạm có đường dẫn trường để Admin API hiển thị."""

    field: str
    message: str


class BotConfigResolutionError(ValueError):
    """Không thể tạo cấu hình hiệu lực; runtime phải từ chối entry mới."""

    def __init__(self, code: str, issues: Sequence[OverlayIssue] = ()) -> None:
        self.code = code
        self.issues = tuple(issues)
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EffectiveStrategy:
    """Strategy hiệu lực của bot cùng hash release đã duyệt trong workspace."""

    release_id: str
    release_content_hash: str
    workspace_release_id: UUID
    parameters: tuple[tuple[str, int], ...]
    entry_timeframe: str
    confirmation_timeframe: str


@dataclass(frozen=True, slots=True)
class EffectiveBotConfig:
    """Cấu hình hiệu lực bất biến của một bot tại thời điểm resolve.

    ``effective_hash`` phủ toàn bộ lineage và nội dung đã hợp nhất, không phủ
    ``resolved_at`` nên cùng cặp version luôn cho cùng hash.
    """

    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    settings_version_id: UUID
    settings_hash: str
    bot_config_version_id: UUID
    bot_config_hash: str
    risk_profile_version_id: UUID
    effective_at: datetime
    resolved_at: datetime
    strategy: EffectiveStrategy
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    risk_limits: RiskLimits
    effective_hash: str

    def strategy_selection(self) -> StrategySelection:
        """Hợp đồng strategy runtime; version ghim là root để khớp PlannerSettings."""
        return StrategySelection(
            settings_version_id=str(self.settings_version_id),
            active_release_ids=(self.strategy.release_id,),
            published=True,
            parameters=self.strategy.parameters,
        )

    def verify_hash(self) -> bool:
        return compute_effective_hash(self) == self.effective_hash


def compute_effective_hash(config: EffectiveBotConfig) -> str:
    """Tính lại SHA-256 trên payload canonical để kiểm chứng cấu hình hiệu lực."""
    limits = config.risk_limits
    payload: dict[str, Any] = {
        "schema": EFFECTIVE_CONFIG_SCHEMA,
        "workspace_id": str(config.workspace_id),
        "bot_id": str(config.bot_id),
        "account_id": str(config.account_id),
        "settings_version_id": str(config.settings_version_id),
        "settings_hash": config.settings_hash,
        "bot_config_version_id": str(config.bot_config_version_id),
        "bot_config_hash": config.bot_config_hash,
        "risk_profile_version_id": str(config.risk_profile_version_id),
        "effective_at": config.effective_at.isoformat(),
        "strategy": {
            "release_id": config.strategy.release_id,
            "release_content_hash": config.strategy.release_content_hash,
            "workspace_release_id": str(config.strategy.workspace_release_id),
            "parameters": [[key, value] for key, value in config.strategy.parameters],
            "entry_timeframe": config.strategy.entry_timeframe,
            "confirmation_timeframe": config.strategy.confirmation_timeframe,
        },
        "universe": {"symbols": list(config.symbols), "timeframes": list(config.timeframes)},
        "risk_limits": {
            **{
                name: (decimal_text(val) if (val := getattr(limits, name)) is not None else None)
                for name in RISK_FIELDS
                if name != "max_positions"
            },
            "account_max_open_risk_fraction": decimal_text(limits.account_max_open_risk_fraction),
            "max_positions": limits.max_positions,
            "max_account_age_seconds": int(limits.max_account_age.total_seconds()),
            "max_data_age_seconds": int(limits.max_data_age.total_seconds()),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Evaluation:
    issues: tuple[OverlayIssue, ...]
    strategy: EffectiveStrategy | None
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    risk_limits: RiskLimits | None


def validate_bot_overlay(
    overlay: BotConfigOverlay,
    *,
    workspace_settings: PublishedWorkspaceSettings,
    account: TradingAccount,
    root_risk_profile: RuntimeRiskProfile,
    strategy_releases: Sequence[StrategyRelease],
    registry: ReleaseCatalog | None = None,
) -> tuple[OverlayIssue, ...]:
    """Liệt kê mọi vi phạm của overlay so với root; rỗng nghĩa là hợp lệ."""
    return _evaluate(
        overlay,
        workspace_settings=workspace_settings,
        account=account,
        root_risk_profile=root_risk_profile,
        strategy_releases=strategy_releases,
        registry=registry or StrategyRegistry(),
    ).issues


def resolve_effective_bot_config(
    workspace_settings: PublishedWorkspaceSettings,
    bot_version: BotConfigVersion,
    account: TradingAccount,
    as_of: datetime,
    *,
    root_risk_profile: RuntimeRiskProfile,
    strategy_releases: Sequence[StrategyRelease],
    registry: ReleaseCatalog | None = None,
) -> EffectiveBotConfig:
    """Resolve root version + bot version đã publish + release đã duyệt tại ``as_of``."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise BotConfigResolutionError("AS_OF_NAIVE")
    root_effective_at = workspace_settings.effective_at
    if root_effective_at is None or root_effective_at > as_of:
        raise BotConfigResolutionError("ROOT_SETTINGS_NOT_EFFECTIVE")
    if bot_version.status != "PUBLISHED":
        raise BotConfigResolutionError("BOT_CONFIG_NOT_PUBLISHED")
    bot_effective_at = bot_version.effective_at
    if bot_effective_at is None or bot_effective_at > as_of:
        raise BotConfigResolutionError("BOT_CONFIG_NOT_EFFECTIVE")
    _require_scope(workspace_settings, bot_version, account, root_risk_profile)
    if overlay_hash(bot_version.overlay_json) != bot_version.overlay_hash:
        raise BotConfigResolutionError("BOT_CONFIG_HASH_MISMATCH")
    try:
        overlay = BotConfigOverlay.model_validate(bot_version.overlay_json)
    except ValidationError:
        raise BotConfigResolutionError("BOT_CONFIG_INVALID") from None
    if overlay.schema_version != bot_version.schema_version:
        raise BotConfigResolutionError("BOT_CONFIG_INVALID")
    evaluation = _evaluate(
        overlay,
        workspace_settings=workspace_settings,
        account=account,
        root_risk_profile=root_risk_profile,
        strategy_releases=strategy_releases,
        registry=registry or StrategyRegistry(),
    )
    if evaluation.issues or evaluation.strategy is None or evaluation.risk_limits is None:
        raise BotConfigResolutionError("BOT_CONFIG_ROOT_MISMATCH", evaluation.issues)
    config = EffectiveBotConfig(
        workspace_id=workspace_settings.workspace_id,
        bot_id=bot_version.bot_id,
        account_id=account.id,
        settings_version_id=workspace_settings.settings_version_id,
        settings_hash=workspace_settings.settings_hash,
        bot_config_version_id=bot_version.id,
        bot_config_hash=bot_version.overlay_hash,
        risk_profile_version_id=root_risk_profile.profile_id,
        effective_at=max(root_effective_at, bot_effective_at),
        resolved_at=as_of,
        strategy=evaluation.strategy,
        symbols=evaluation.symbols,
        timeframes=evaluation.timeframes,
        risk_limits=evaluation.risk_limits,
        effective_hash="",
    )
    return replace(config, effective_hash=compute_effective_hash(config))


async def load_effective_bot_config(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    account_id: UUID,
    as_of: datetime,
    published: PublishedWorkspaceSettings | None = None,
    registry: ReleaseCatalog | None = None,
) -> EffectiveBotConfig | None:
    """Nạp input theo workspace+account rồi resolve; ``None`` khi bot chưa có version publish.

    Bot chưa có version publish dùng root như MB-01 (overlay rỗng ngầm định).
    """
    account = await session.scalar(
        select(TradingAccount).where(
            TradingAccount.workspace_id == workspace_id, TradingAccount.id == account_id
        )
    )
    if account is None:
        raise BotConfigResolutionError("ACCOUNT_NOT_FOUND")
    bot = await session.scalar(
        select(BotInstance).where(
            BotInstance.workspace_id == workspace_id,
            BotInstance.trading_account_id == account_id,
        )
    )
    if bot is None:
        raise BotConfigResolutionError("BOT_NOT_FOUND")
    version = await BotConfigVersionRepository(session).get_effective(
        workspace_id=workspace_id, bot_id=bot.id, as_of=as_of
    )
    if version is None:
        return None
    root = published
    if root is None:
        try:
            root = await WorkspaceSettingsRepository(session).load_effective_config(
                workspace_id=workspace_id, as_of=as_of
            )
        except SettingsVersionConflict:
            raise BotConfigResolutionError("ROOT_SETTINGS_INVALID") from None
    if root is None:
        raise BotConfigResolutionError("ROOT_SETTINGS_NOT_EFFECTIVE")
    try:
        risk_profile = await load_risk_profile(
            session, workspace_id=workspace_id, profile_id=root.settings.risk.profile_version_id
        )
    except RiskProfileRuntimeError:
        raise BotConfigResolutionError("ROOT_RISK_PROFILE_INVALID") from None
    releases = await load_strategy_releases(session, workspace_id=workspace_id)
    return resolve_effective_bot_config(
        root,
        version,
        account,
        as_of,
        root_risk_profile=risk_profile,
        strategy_releases=releases,
        registry=registry,
    )


async def load_strategy_releases(
    session: AsyncSession, *, workspace_id: UUID
) -> list[StrategyRelease]:
    """Đọc mọi release của workspace (kể cả chưa active) để báo lỗi chính xác."""
    rows = await session.scalars(
        select(StrategyRelease).where(StrategyRelease.workspace_id == workspace_id)
    )
    return list(rows.all())


def _require_scope(
    root: PublishedWorkspaceSettings,
    version: BotConfigVersion,
    account: TradingAccount,
    risk_profile: RuntimeRiskProfile,
) -> None:
    expected_venue = _ACCOUNT_VENUES.get(root.settings.market_data.venue)
    if (
        version.workspace_id != root.workspace_id
        or account.workspace_id != root.workspace_id
        or risk_profile.workspace_id != root.workspace_id
        or version.account_id != account.id
        or risk_profile.profile_id != root.settings.risk.profile_version_id
        or root.settings.runtime.mode != "PAPER"
        or account.mode != root.settings.runtime.mode
        or expected_venue is None
        or account.venue != expected_venue
    ):
        raise BotConfigResolutionError("BOT_CONFIG_SCOPE_MISMATCH")


def _evaluate(
    overlay: BotConfigOverlay,
    *,
    workspace_settings: PublishedWorkspaceSettings,
    account: TradingAccount,
    root_risk_profile: RuntimeRiskProfile,
    strategy_releases: Sequence[StrategyRelease],
    registry: ReleaseCatalog,
) -> _Evaluation:
    issues: list[OverlayIssue] = []
    root = workspace_settings.settings
    strategy = _evaluate_strategy(
        overlay,
        workspace_settings=workspace_settings,
        strategy_releases=strategy_releases,
        registry=registry,
        issues=issues,
    )
    symbols = root.market_data.symbols
    timeframes: tuple[str, ...] = root.market_data.timeframes
    if overlay.universe is not None and overlay.universe.symbols is not None:
        outside = [item for item in overlay.universe.symbols if item not in symbols]
        if outside:
            issues.append(
                OverlayIssue("universe.symbols", "Symbol phải là tập con của root market_data.")
            )
        symbols = overlay.universe.symbols
    if overlay.universe is not None and overlay.universe.timeframes is not None:
        selected = overlay.universe.timeframes
        if any(item not in timeframes for item in selected):
            issues.append(
                OverlayIssue(
                    "universe.timeframes", "Timeframe phải là tập con của root market_data."
                )
            )
        elif (
            root.strategy.entry_timeframe not in selected
            or root.strategy.confirmation_timeframe not in selected
        ):
            issues.append(
                OverlayIssue(
                    "universe.timeframes",
                    "Universe bot phải giữ entry/confirmation timeframe của root strategy.",
                )
            )
        timeframes = selected
    limits = _evaluate_risk(
        overlay,
        account,
        root_risk_profile.limits,
        issues,
        workspace_settings=workspace_settings,
    )
    return _Evaluation(tuple(issues), strategy, tuple(symbols), tuple(timeframes), limits)


def _evaluate_strategy(
    overlay: BotConfigOverlay,
    *,
    workspace_settings: PublishedWorkspaceSettings,
    strategy_releases: Sequence[StrategyRelease],
    registry: ReleaseCatalog,
    issues: list[OverlayIssue],
) -> EffectiveStrategy | None:
    root = workspace_settings.settings
    if overlay.strategy is not None:
        release_id = overlay.strategy.release_id
        raw_parameters: Mapping[str, Any] = overlay.strategy.parameters
    else:
        release_id = root.strategy.default_release
        raw_parameters = root.strategy.parameters
    parameters: tuple[tuple[str, int], ...] = ()
    parameters_valid = True
    try:
        if raw_parameters and release_id not in _PARAMETERIZED_RELEASES:
            raise ValueError("Strategy này chưa hỗ trợ tham số trong runtime.")
        parameters = tuple(sorted(raw_parameters.items()))
        StrategySelection(
            settings_version_id=str(workspace_settings.settings_version_id),
            active_release_ids=(release_id,),
            published=True,
            parameters=parameters,
        )
    except TypeError, ValueError:
        parameters_valid = False
        issues.append(
            OverlayIssue("strategy.parameters", "Tham số strategy không hợp lệ cho release này.")
        )
    try:
        registered = registry.get_release(release_id)
    except UnknownStrategyReleaseError:
        issues.append(
            OverlayIssue("strategy.release_id", "Strategy release không có trong registry.")
        )
        return None
    candidates = [row for row in strategy_releases if row.name == release_id]
    active = [
        row
        for row in candidates
        if row.workspace_id == workspace_settings.workspace_id
        and row.status == "PUBLISHED"
        and row.content_hash == registered.content_hash
    ]
    if len(active) != 1:
        if any(row.workspace_id != workspace_settings.workspace_id for row in candidates):
            message = "Strategy release thuộc workspace khác."
        elif candidates:
            message = "Strategy release không còn active hoặc hash không khớp registry."
        else:
            message = "Strategy release chưa được publish trong workspace."
        issues.append(OverlayIssue("strategy.release_id", message))
        return None
    if not parameters_valid:
        return None
    return EffectiveStrategy(
        release_id=release_id,
        release_content_hash=registered.content_hash,
        workspace_release_id=active[0].id,
        parameters=parameters,
        entry_timeframe=root.strategy.entry_timeframe,
        confirmation_timeframe=root.strategy.confirmation_timeframe,
    )


def _evaluate_risk(
    overlay: BotConfigOverlay,
    account: TradingAccount,
    root_limits: RiskLimits,
    issues: list[OverlayIssue],
    *,
    workspace_settings: PublishedWorkspaceSettings,
) -> RiskLimits | None:
    values: dict[str, Decimal | int] = {}
    for name in RISK_FIELDS:
        if name == "bot_max_open_risk_fraction":
            continue
        root_value: Decimal | int = getattr(root_limits, name)
        requested = getattr(overlay.risk, name) if overlay.risk is not None else None
        if requested is not None and requested > root_value:
            issues.append(OverlayIssue(f"risk.{name}", "Bot không được nới giới hạn root."))
        values[name] = requested if requested is not None else root_value
    account_cap = account.max_notional
    requested_notional = overlay.risk.max_notional if overlay.risk is not None else None
    if requested_notional is not None and requested_notional > account_cap:
        issues.append(OverlayIssue("risk.max_notional", "Bot không được nới cap của account."))
    # Cap account là hard cap riêng: lấy giá trị chặt nhất giữa root, account và overlay.
    values["max_notional"] = min(Decimal(values["max_notional"]), account_cap)

    ws_cap = workspace_settings.settings.risk.workspace_max_open_risk_fraction
    acc_cap = root_limits.account_max_open_risk_fraction
    if acc_cap > ws_cap:
        issues.append(
            OverlayIssue(
                "risk.account_max_open_risk_fraction", "Account không được nới giới hạn root."
            )
        )

    bot_cap = overlay.risk.bot_max_open_risk_fraction if overlay.risk is not None else None
    if bot_cap is not None:
        if bot_cap > acc_cap:
            issues.append(
                OverlayIssue(
                    "risk.bot_max_open_risk_fraction", "Bot không được nới cap của account."
                )
            )
        if bot_cap > ws_cap:
            issues.append(
                OverlayIssue("risk.bot_max_open_risk_fraction", "Bot không được nới giới hạn root.")
            )

    try:
        return RiskLimits(
            per_trade_risk_fraction=Decimal(values["per_trade_risk_fraction"]),
            max_aggregate_open_risk=Decimal(values["max_aggregate_open_risk"]),
            max_notional=Decimal(values["max_notional"]),
            max_leverage=Decimal(values["max_leverage"]),
            max_positions=int(values["max_positions"]),
            max_symbol_notional=Decimal(values["max_symbol_notional"]),
            max_concentration_fraction=Decimal(values["max_concentration_fraction"]),
            max_daily_loss=Decimal(values["max_daily_loss"]),
            max_rolling_loss=Decimal(values["max_rolling_loss"]),
            max_account_age=root_limits.max_account_age,
            max_data_age=root_limits.max_data_age,
            account_max_open_risk_fraction=acc_cap,
            bot_max_open_risk_fraction=bot_cap,
        )
    except ValueError:
        issues.append(OverlayIssue("risk", "Giới hạn risk hiệu lực không hợp lệ."))
        return None
