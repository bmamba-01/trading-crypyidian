"""Ca sử dụng trang "Khẩu vị rủi ro" (CRY-428): xem và chỉnh giới hạn rủi ro của workspace.

Đọc:
- Risk profile đang được workspace settings đã publish tham chiếu (hard caps + tỷ lệ rủi ro).
- Ngưỡng ngắt mạch: trần lỗ ngày/trượt 24 giờ (risk profile) và drawdown từ đỉnh
  (``risk.max_drawdown_fraction`` của workspace settings, mặc định 10%). Cả hai đã gác cổng
  NEW_ENTRY thật trong RiskEngine của runtime PAPER (FX-2).
- Giá trị hiệu lực theo từng bot = workspace ∩ overlay của bot (resolver MB), kèm trạng thái
  lỗ hôm nay và drawdown hiện tại đo bằng cùng hàm runtime dùng.
- Bảng chính sách thoát chỉ đọc theo release + ví dụ quy đổi % theo ATR hiện tại của coin.

Ghi (``save``): cần quyền ``RISK_PROFILE_WRITE`` + MFA gần đây + lý do. Nếu hard caps đổi thì
thêm một phiên bản risk profile mới (append-only, có audit); sau đó tạo bản nháp workspace
settings → kiểm định → publish qua ``SettingsAdminService`` (audit + outbox + đánh giá capacity
như API JSON). Từ chối nếu siết workspace xuống dưới overlay đã publish của một bot, vì resolver
sẽ từ chối bot đó (fail-closed) — phải chỉnh overlay bot trước.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import RiskProfileVersion
from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionRepository,
)
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_instances import BotInstanceRepository
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.admin.settings_service import SettingsAdminError, SettingsAdminService
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    load_effective_bot_config,
)
from trading_bot.application.exit_policy_catalog import (
    ExitPolicyView,
    build_exit_policy_catalog,
    percent_example,
)
from trading_bot.application.market_signal_composition import load_closed_market_bars
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_market_context import (
    PostgresPaperDecisionContextProvider,
    _calculate_features,
)
from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    risk_limits_from_profile,
)
from trading_bot.application.workspace_risk import mark_policy_from
from trading_bot.auth.action_challenge import (
    ACTION_RISK_APPETITE_SAVE,
    ActionChallengeError,
    ActionChallengeService,
    StepUpProof,
    risk_appetite_save_payload,
    verify_service_step_up,
)
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.config.bot_settings import BotConfigOverlay
from trading_bot.config.workspace_settings import WorkspaceSettings, decimal_text
from trading_bot.domain.risk.drawdown_breaker import (
    PAPER_DRAWDOWN_BREAKER_POLICY,
    drawdown_policy_for,
)
from trading_bot.domain.risk.planner import RiskLimits
from trading_bot.domain.risk.risk_windows import loss_window_policy_for

logger = logging.getLogger(__name__)

# Trường của risk profile chỉnh trên trang: (tên form, khóa hard_caps hoặc cột, kiểu).
PROFILE_DECIMAL_CAPS = (
    "max_leverage",
    "max_notional",
    "max_symbol_notional",
    "max_aggregate_open_risk",
    "max_concentration_fraction",
    "max_daily_loss",
    "max_rolling_loss",
)
# Trường so sánh với overlay bot (cùng tên trong RiskLimits và BotRiskOverlay).
OVERLAY_COMPARED_FIELDS = (
    "per_trade_risk_fraction",
    "max_concentration_fraction",
    "max_aggregate_open_risk",
    "max_notional",
    "max_leverage",
    "max_symbol_notional",
    "max_daily_loss",
    "max_rolling_loss",
    "max_positions",
)
BOT_EFFECTIVE_FIELDS = (
    "per_trade_risk_fraction",
    "max_leverage",
    "max_positions",
    "max_notional",
    "max_daily_loss",
    "max_rolling_loss",
)


@dataclass(frozen=True, slots=True)
class RiskAppetiteUpdate:
    """Giá trị mới từ form; mọi Decimal đã chuẩn hóa, chưa kiểm ràng buộc chéo."""

    per_trade_risk_fraction: Decimal
    account_max_open_risk_fraction: Decimal
    max_positions: int
    caps: dict[str, Decimal]
    workspace_max_open_risk_fraction: Decimal
    max_drawdown_fraction: Decimal
    reason: str


class RiskAppetiteFormError(ValueError):
    """Form không hợp lệ; ``field`` là tên ô để hiển thị lỗi."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(message)


def _parse_decimal(values: dict[str, str], name: str) -> Decimal:
    raw = values.get(name, "").strip().replace(",", ".")
    if not raw:
        raise RiskAppetiteFormError(name, "Không được để trống.")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as error:
        raise RiskAppetiteFormError(name, "Phải là số thập phân.") from error
    if not parsed.is_finite() or parsed <= 0:
        raise RiskAppetiteFormError(name, "Phải là số dương.")
    return parsed


def _fraction(values: dict[str, str], name: str, *, upper_open: bool = False) -> Decimal:
    parsed = _parse_decimal(values, name)
    if parsed > 1 or (upper_open and parsed >= 1):
        raise RiskAppetiteFormError(name, "Tỷ lệ phải nằm trong (0, 1).")
    return parsed


def parse_risk_appetite_form(values: dict[str, str]) -> RiskAppetiteUpdate:
    """Đọc form HTML; dấu phẩy thập phân được chấp nhận. Không nhận float để giữ độ chính xác."""
    raw_positions = values.get("max_positions", "").strip()
    if not raw_positions.isdigit() or int(raw_positions) < 1:
        raise RiskAppetiteFormError("max_positions", "Phải là số nguyên dương.")
    reason = values.get("reason", "").strip()
    if not 1 <= len(reason) <= 500:
        raise RiskAppetiteFormError("reason", "Lý do cần từ 1 đến 500 ký tự.")
    caps = {
        name: (
            _fraction(values, name)
            if name == "max_concentration_fraction"
            else _parse_decimal(values, name)
        )
        for name in PROFILE_DECIMAL_CAPS
    }
    return RiskAppetiteUpdate(
        per_trade_risk_fraction=_fraction(values, "per_trade_risk_fraction"),
        account_max_open_risk_fraction=_fraction(values, "account_max_open_risk_fraction"),
        max_positions=int(raw_positions),
        caps=caps,
        workspace_max_open_risk_fraction=_fraction(values, "workspace_max_open_risk_fraction"),
        max_drawdown_fraction=_fraction(values, "max_drawdown_fraction", upper_open=True),
        reason=reason,
    )


def overlay_conflicts(
    overlay: BotConfigOverlay | None, new_limits: dict[str, Decimal | int]
) -> list[str]:
    """Trường overlay của bot lớn hơn giới hạn workspace mới (resolver sẽ từ chối bot)."""
    if overlay is None or overlay.risk is None:
        return []
    conflicts: list[str] = []
    for name in OVERLAY_COMPARED_FIELDS:
        requested = getattr(overlay.risk, name)
        if requested is not None and name in new_limits and requested > new_limits[name]:
            conflicts.append(name)
    return conflicts


def _text(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    return decimal_text(value) if isinstance(value, Decimal) else str(value)


def _limits_view(limits: RiskLimits) -> dict[str, str | None]:
    return {
        "per_trade_risk_fraction": _text(limits.per_trade_risk_fraction),
        "account_max_open_risk_fraction": _text(limits.account_max_open_risk_fraction),
        "max_positions": _text(limits.max_positions),
        **{name: _text(getattr(limits, name)) for name in PROFILE_DECIMAL_CAPS},
    }


def _require_write(principal: Principal) -> None:
    if not authorize(principal, Permission.RISK_PROFILE_WRITE):
        raise SettingsAdminError(403, "Cần quyền thay đổi hồ sơ rủi ro.")
    now = datetime.now(UTC)
    if principal.mfa_verified_at > now or now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise SettingsAdminError(403, "Cần xác thực MFA gần đây.")


class RiskAppetiteService:
    """Đọc tổng hợp và lưu khẩu vị rủi ro; mọi ghi đi qua audit/outbox của workspace."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings_service: SettingsAdminService | None = None,
        *,
        action_challenge_service: ActionChallengeService | None = None,
        require_action_step_up: bool = False,
    ) -> None:
        self._sessions = sessions
        self._action_challenge_service = action_challenge_service
        self._require_action_step_up = require_action_step_up
        self._settings = settings_service or SettingsAdminService(
            sessions,
            action_challenge_service=action_challenge_service,
            require_action_step_up=require_action_step_up,
        )

    def _uow(self, principal: Principal) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        )

    # --- Đọc ---------------------------------------------------------------------------------

    async def overview(self, principal: Principal, *, symbol: str | None = None) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with self._uow(principal) as unit:
            session = unit.session
            published = await WorkspaceSettingsRepository(session).get_effective(
                workspace_id=principal.workspace_id
            )
            if published is None:
                return {"configured": False, "exit_policies": self._exit_rows(None)}
            settings = WorkspaceSettings.model_validate(published.settings_json)
            profile_record = await session.scalar(
                select(RiskProfileVersion).where(
                    RiskProfileVersion.workspace_id == principal.workspace_id,
                    RiskProfileVersion.id == settings.risk.profile_version_id,
                )
            )
            profile_view: dict[str, Any] | None = None
            root_limits: RiskLimits | None = None
            if profile_record is not None:
                try:
                    runtime_profile = risk_limits_from_profile(profile_record)
                    root_limits = runtime_profile.limits
                    profile_view = {
                        "id": str(profile_record.id),
                        "name": profile_record.name,
                        "version": profile_record.version,
                        "limits": _limits_view(root_limits),
                    }
                except RiskProfileRuntimeError:
                    profile_view = None
            history = (
                await session.execute(
                    select(
                        RiskProfileVersion.name,
                        RiskProfileVersion.version,
                        RiskProfileVersion.created_at,
                        RiskProfileVersion.id,
                    )
                    .where(RiskProfileVersion.workspace_id == principal.workspace_id)
                    .order_by(RiskProfileVersion.created_at.desc())
                    .limit(10)
                )
            ).all()
            drawdown = settings.risk.max_drawdown_fraction
            bots = await self._bot_rows(unit, principal, settings, published, root_limits, now)
            chosen = symbol if symbol in settings.market_data.symbols else None
            chosen = chosen or settings.market_data.symbols[0]
            atr_by_timeframe = await self._atr_examples(
                session, principal.workspace_id, chosen, now
            )
        return {
            "configured": True,
            "settings": {
                "id": str(published.id),
                "version": published.version,
                "revision": published.revision,
                "effective_at": published.effective_at.isoformat()
                if published.effective_at
                else None,
                "workspace_max_open_risk_fraction": _text(
                    settings.risk.workspace_max_open_risk_fraction
                ),
                "max_drawdown_fraction": _text(
                    drawdown or PAPER_DRAWDOWN_BREAKER_POLICY.max_drawdown_fraction
                ),
                "max_drawdown_is_default": drawdown is None,
                "default_release": settings.strategy.default_release,
                "symbols": list(settings.market_data.symbols),
            },
            "profile": profile_view,
            "profile_history": [
                {
                    "name": name,
                    "version": version,
                    "created_at": created_at.isoformat() if created_at else None,
                    "active": str(profile_id) == str(settings.risk.profile_version_id),
                }
                for name, version, created_at, profile_id in history
            ],
            "bots": bots,
            "symbol": chosen,
            "atr": {
                timeframe: {"atr": _text(atr), "price": _text(price)}
                for timeframe, (atr, price) in atr_by_timeframe.items()
            },
            "exit_policies": self._exit_rows(atr_by_timeframe),
        }

    @staticmethod
    def _exit_rows(
        atr_by_timeframe: dict[str, tuple[Decimal, Decimal]] | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for policy in build_exit_policy_catalog():
            example = None
            sample = (atr_by_timeframe or {}).get(policy.timeframe or "")
            # Strategy Card không có bội số cố định: hiện ghi chú thay vì ví dụ %.
            if sample is not None and policy.source != "strategy_card":
                atr, price = sample
                computed = percent_example(policy, atr=atr, price=price)
                example = {
                    "stop_pct": _pct(computed.stop_pct),
                    "trailing_pct": _pct(computed.trailing_pct),
                    "take_profit_pct": _pct(computed.take_profit_pct),
                    "ladder_pcts": [_pct(value) for value in computed.ladder_pcts],
                }
            rows.append(_policy_row(policy) | {"example": example})
        return rows

    async def _bot_rows(
        self,
        unit: UnitOfWork,
        principal: Principal,
        settings: WorkspaceSettings,
        published: Any,
        root_limits: RiskLimits | None,
        now: datetime,
    ) -> list[dict[str, Any]]:
        session = unit.session
        grants = BotGrantRepository(session)
        rows: list[dict[str, Any]] = []
        policy = drawdown_policy_for(settings.risk.max_drawdown_fraction)
        provider = PostgresPaperDecisionContextProvider(
            self._sessions, workspace_id=principal.workspace_id
        )
        for bot, account in await BotInstanceRepository(session).list_with_accounts(
            workspace_id=principal.workspace_id
        ):
            try:
                await grants.require(principal, bot_id=bot.id, permission=BotPermission.READ)
            except PermissionError:
                continue
            row: dict[str, Any] = {
                "id": str(bot.id),
                "name": bot.name,
                "status": bot.status,
                "release_id": settings.strategy.default_release,
                "limits": None,
                "overridden": [],
                "error": None,
                "loss_today": None,
                "loss_rolling": None,
                "drawdown": None,
                "exit_override": None,
            }
            limits = root_limits
            try:
                effective = await load_effective_bot_config(
                    session,
                    workspace_id=principal.workspace_id,
                    account_id=account.id,
                    as_of=now,
                )
                if effective is not None:
                    limits = effective.risk_limits
                    row["release_id"] = effective.strategy.release_id
                    if effective.exit_override is not None:
                        row["exit_override"] = effective.exit_override.payload()
            except BotConfigResolutionError as error:
                row["error"] = str(error) or "BOT_CONFIG_UNRESOLVED"
                limits = None
            if limits is not None:
                row["limits"] = {
                    name: _text(getattr(limits, name)) for name in BOT_EFFECTIVE_FIELDS
                }
                if root_limits is not None:
                    row["overridden"] = [
                        name
                        for name in BOT_EFFECTIVE_FIELDS
                        if getattr(limits, name) != getattr(root_limits, name)
                    ]
            await self._breaker_status(session, provider, row, bot, account, settings, policy, now)
            rows.append(row)
        return rows

    @staticmethod
    async def _breaker_status(
        session: AsyncSession,
        provider: PostgresPaperDecisionContextProvider,
        row: dict[str, Any],
        bot: Any,
        account: Any,
        settings: WorkspaceSettings,
        policy: Any,
        now: datetime,
    ) -> None:
        """Lỗ hôm nay/24 giờ và drawdown bằng đúng hàm runtime; lỗi đọc chỉ làm ô "không rõ"."""
        ref = _AccountRef(account_id=account.id, bot_id=bot.id)
        try:
            async with session.begin_nested():
                losses = await provider._loss_windows(
                    session,
                    ref,
                    as_of=now,
                    policy=loss_window_policy_for(settings.risk.loss_window_timezone),
                )
                row["loss_today"] = _text(losses.realized_loss_today)
                row["loss_rolling"] = _text(losses.realized_loss_rolling)
                equity = await AccountEquityRepository(session).load_paper_equity_snapshot(
                    workspace_id=bot.workspace_id,
                    bot_id=bot.id,
                    account_id=account.id,
                    as_of=now,
                    max_equity_age=timedelta(seconds=settings.risk.max_equity_age_seconds),
                    max_mark_age=timedelta(seconds=settings.risk.max_mark_age_seconds),
                    mark_policy=mark_policy_from(settings),
                )
                if equity.known and equity.seed_capital:
                    state = await provider._drawdown_breaker(
                        session,
                        ref,
                        as_of=now,
                        equity=equity.equity_amount,
                        seed=equity.seed_capital,
                        policy=policy,
                    )
                    row["drawdown"] = {
                        "fraction": _text(state.drawdown_fraction),
                        "peak": _text(state.peak_equity),
                        "current": _text(state.current_equity),
                        "tripped": state.tripped,
                    }
        except Exception:  # noqa: BLE001 — trang chỉ đọc, không được hỏng vì một bot lỗi
            logger.warning("Không đọc được trạng thái ngắt mạch của bot %s", bot.id, exc_info=True)

    @staticmethod
    async def _atr_examples(
        session: AsyncSession, workspace_id: UUID, symbol: str | None, now: datetime
    ) -> dict[str, tuple[Decimal, Decimal]]:
        """ATR(14) và giá đóng nến cuối theo từng khung vào lệnh của các release."""
        if symbol is None:
            return {}
        timeframes = sorted(
            {policy.timeframe for policy in build_exit_policy_catalog() if policy.timeframe}
        )
        result: dict[str, tuple[Decimal, Decimal]] = {}
        for timeframe in timeframes:
            try:
                async with session.begin_nested():
                    bars = await load_closed_market_bars(
                        session,
                        workspace_id=workspace_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        as_of=now,
                    )
            except Exception:  # noqa: BLE001 — thiếu dữ liệu chỉ làm mất ví dụ %
                logger.warning("Không nạp được nến %s %s", symbol, timeframe, exc_info=True)
                continue
            if not bars:
                continue
            snapshot = _calculate_features(workspace_id, bars, timeframe)
            if snapshot is None or not snapshot.is_usable:
                continue
            raw = snapshot.features.get("atr_14")
            if raw is None or not snapshot.warmup.get("atr_14", False):
                continue
            atr = Decimal(str(raw))
            price = Decimal(str(bars[-1].close))
            if atr.is_finite() and atr > 0 and price > 0:
                result[timeframe] = (atr, price)
        return result

    # --- Ghi ---------------------------------------------------------------------------------

    async def save(
        self,
        principal: Principal,
        update: RiskAppetiteUpdate,
        *,
        step_up: StepUpProof | None = None,
    ) -> dict[str, Any]:
        """Lưu khẩu vị rủi ro mới; trả version settings đã publish (hoặc ``unchanged``)."""
        if not authorize(principal, Permission.RISK_PROFILE_WRITE):
            raise SettingsAdminError(403, "Cần quyền thay đổi hồ sơ rủi ro.")
        try:
            await verify_service_step_up(
                principal,
                action_challenge_service=self._action_challenge_service,
                require_action_step_up=self._require_action_step_up,
                action=ACTION_RISK_APPETITE_SAVE,
                payload=risk_appetite_save_payload(
                    per_trade_risk_fraction=update.per_trade_risk_fraction,
                    account_max_open_risk_fraction=update.account_max_open_risk_fraction,
                    max_positions=update.max_positions,
                    caps=update.caps,
                    workspace_max_open_risk_fraction=update.workspace_max_open_risk_fraction,
                    max_drawdown_fraction=update.max_drawdown_fraction,
                    reason=update.reason,
                ),
                step_up=step_up,
                fallback_recent_mfa=lambda: _require_write(principal),
            )
        except ActionChallengeError as error:
            raise SettingsAdminError(error.status_code, error.message, code=error.code) from error
        async with self._uow(principal) as unit:
            session = unit.session
            current = await WorkspaceSettingsRepository(session).get_effective(
                workspace_id=principal.workspace_id
            )
            if current is None:
                raise SettingsAdminError(409, "Workspace chưa có cấu hình đã publish.")
            settings = WorkspaceSettings.model_validate(current.settings_json)
            profile = await session.scalar(
                select(RiskProfileVersion).where(
                    RiskProfileVersion.workspace_id == principal.workspace_id,
                    RiskProfileVersion.id == settings.risk.profile_version_id,
                )
            )
            if profile is None:
                raise SettingsAdminError(422, "Risk profile hiện hành không tồn tại.")
            hard_caps = dict(profile.settings.get("hard_caps", {}))
            new_caps = hard_caps | {
                name: decimal_text(value) for name, value in update.caps.items()
            }
            new_caps["max_positions"] = update.max_positions
            new_settings_json = dict(profile.settings) | {"hard_caps": new_caps}
            profile_changed = (
                new_caps != hard_caps
                or profile.max_risk_fraction != update.per_trade_risk_fraction
                or profile.max_open_risk_fraction != update.account_max_open_risk_fraction
            )
            new_limits: dict[str, Decimal | int] = {
                "per_trade_risk_fraction": update.per_trade_risk_fraction,
                "max_positions": update.max_positions,
                **update.caps,
            }
            await self._reject_overlay_conflicts(session, principal, new_limits)
            profile_id = profile.id
            if profile_changed:
                latest = await session.scalar(
                    select(func.max(RiskProfileVersion.version)).where(
                        RiskProfileVersion.workspace_id == principal.workspace_id,
                        RiskProfileVersion.name == profile.name,
                    )
                )
                created = RiskProfileVersion(
                    workspace_id=principal.workspace_id,
                    name=profile.name,
                    version=(latest or 0) + 1,
                    max_risk_fraction=update.per_trade_risk_fraction,
                    max_open_risk_fraction=update.account_max_open_risk_fraction,
                    settings=new_settings_json,
                    is_active=False,
                )
                try:
                    # Kiểm định trước khi ghi: runtime phải diễn giải được profile mới.
                    probe = _ProfileProbe(created, principal.workspace_id)
                    risk_limits_from_profile(probe)
                except RiskProfileRuntimeError as error:
                    raise SettingsAdminError(422, str(error)) from error
                session.add(created)
                await session.flush()
                profile_id = created.id
                unit.record_audit(
                    action="risk_profile.version_created",
                    target_type="risk_profile_version",
                    target_id=created.id,
                    reason=update.reason,
                    before_state={
                        "id": str(profile.id),
                        "version": profile.version,
                        "max_risk_fraction": decimal_text(profile.max_risk_fraction),
                        "max_open_risk_fraction": decimal_text(profile.max_open_risk_fraction),
                        "settings": profile.settings,
                    },
                    after_state={
                        "id": str(created.id),
                        "version": created.version,
                        "max_risk_fraction": decimal_text(update.per_trade_risk_fraction),
                        "max_open_risk_fraction": decimal_text(
                            update.account_max_open_risk_fraction
                        ),
                        "settings": new_settings_json,
                    },
                )
            next_risk = settings.risk.model_copy(
                update={
                    "profile_version_id": profile_id,
                    "workspace_max_open_risk_fraction": update.workspace_max_open_risk_fraction,
                    "max_drawdown_fraction": update.max_drawdown_fraction,
                }
            )
            next_settings = WorkspaceSettings.model_validate(
                settings.model_dump(mode="json") | {"risk": next_risk.model_dump(mode="json")}
            )
            parent_id = current.id
        if next_settings == settings:
            return {"status": "unchanged", "version": current.version}
        draft = await self._settings.create_draft(
            principal,
            settings=next_settings,
            expected_parent_version_id=parent_id,
            _verified_step_up=True,
        )
        validation = await self._settings.validate_draft(principal, draft_id=UUID(draft["id"]))
        if not validation.get("valid"):
            messages = "; ".join(item["message"] for item in validation.get("errors", []))
            raise SettingsAdminError(422, f"Cấu hình mới không hợp lệ: {messages}")
        published = await self._settings.publish_draft(
            principal,
            draft_id=UUID(draft["id"]),
            expected_revision=int(draft["revision"]),
            _verified_step_up=True,
        )
        async with self._uow(principal) as unit:
            unit.record_audit(
                action="risk_appetite.published",
                target_type="workspace_settings_version",
                target_id=UUID(published["id"]),
                reason=update.reason,
                before_state={
                    "version": current.version,
                    "risk": settings.risk.model_dump(mode="json"),
                },
                after_state={
                    "version": published["version"],
                    "risk": next_settings.risk.model_dump(mode="json"),
                    "risk_profile_changed": profile_changed,
                },
            )
        return {
            "status": "published",
            "version": published["version"],
            "risk_profile_changed": profile_changed,
            "capacity": published.get("capacity"),
        }

    @staticmethod
    async def _reject_overlay_conflicts(
        session: AsyncSession, principal: Principal, new_limits: dict[str, Decimal | int]
    ) -> None:
        versions = BotConfigVersionRepository(session)
        blocked: list[str] = []
        for bot, _account in await BotInstanceRepository(session).list_with_accounts(
            workspace_id=principal.workspace_id
        ):
            head = await versions.get_head(workspace_id=principal.workspace_id, bot_id=bot.id)
            if head is None:
                continue
            try:
                overlay = BotConfigOverlay.model_validate(head.overlay_json)
            except ValueError:
                continue
            fields = overlay_conflicts(overlay, new_limits)
            if fields:
                blocked.append(f"{bot.name} ({', '.join(fields)})")
        if blocked:
            raise SettingsAdminError(
                409,
                "Giới hạn mới thấp hơn overlay đã publish của bot: "
                + "; ".join(blocked)
                + ". Hạ overlay của bot trước để bot không bị từ chối cấu hình.",
            )


@dataclass(frozen=True, slots=True)
class _AccountRef:
    account_id: UUID
    bot_id: UUID


class _ProfileProbe:
    """Bản xem của profile chưa flush (chưa có id/created_at) để chạy kiểm định runtime."""

    def __init__(self, record: RiskProfileVersion, workspace_id: UUID) -> None:
        self.id = UUID(int=0)
        self.workspace_id = workspace_id
        self.name = record.name
        self.version = record.version
        self.max_risk_fraction = record.max_risk_fraction
        self.max_open_risk_fraction = record.max_open_risk_fraction
        self.settings = record.settings


def _pct(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01")), "f")


def _policy_row(policy: ExitPolicyView) -> dict[str, Any]:
    return {
        "release_id": policy.release_id,
        "strategy_id": policy.strategy_id,
        "name_vi": policy.name_vi,
        "timeframe": policy.timeframe,
        "stop_label_vi": policy.stop_label_vi,
        "stop_atr_multiple": _text(policy.stop_atr_multiple),
        "trailing_atr_multiple": _text(policy.trailing_atr_multiple),
        "take_profit_label_vi": policy.take_profit_label_vi,
        "other_exits_vi": list(policy.other_exits_vi),
        "max_hold_label_vi": policy.max_hold_label_vi,
        "notes_vi": list(policy.notes_vi),
        "source": policy.source,
    }


__all__ = [
    "RiskAppetiteFormError",
    "RiskAppetiteService",
    "RiskAppetiteUpdate",
    "overlay_conflicts",
    "parse_risk_appetite_form",
]
