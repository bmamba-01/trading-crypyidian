"""Điều phối một lượt đánh giá từ strategy đã chọn tới PAPER intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.application.runtime_risk_profile import load_risk_profile
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperOrderIntent
from trading_bot.domain.risk.engine import RiskDecision, RiskEngine, RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    OpenExposure,
    PlannerSettings,
    PlanRejected,
    TradePlan,
    TradePlanner,
)
from trading_bot.domain.risk.workspace_budget import WorkspaceRiskDecision
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    NoTrade,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.registry import StrategyRegistry

if TYPE_CHECKING:
    from trading_bot.application.bot_config_resolver import EffectiveBotConfig

type StrategyResult = StrategyCandidate | NoTrade
type PlanResult = TradePlan | PlanRejected


class DecisionCycleOutcome(StrEnum):
    NO_TRADE = "NO_TRADE"
    PLAN_REJECTED = "PLAN_REJECTED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    PAPER_INTENT_CREATED = "PAPER_INTENT_CREATED"


def strategy_selection_from_published_settings(
    settings: WorkspaceSettings,
    *,
    settings_version_id: str,
    is_published: bool,
) -> StrategySelection:
    """Chuyển release và tham số root settings sang hợp đồng strategy runtime."""
    if not is_published:
        raise ValueError("Bản nháp WorkspaceSettings không thể tạo strategy selection runtime.")
    return strategy_selection_for_release(
        release_id=settings.strategy.default_release,
        parameters=settings.strategy.parameters,
        settings_version_id=settings_version_id,
    )


def strategy_selection_for_release(
    *,
    release_id: str,
    parameters: Mapping[str, int],
    settings_version_id: str,
) -> StrategySelection:
    """Dựng selection đã publish cho một release; dùng chung cho root settings và bot config."""
    if parameters and release_id != "h03-v1":
        raise ValueError(f"Strategy '{release_id}' chưa hỗ trợ tham số trong runtime.")
    if not isinstance(parameters, Mapping):
        raise ValueError("strategy.parameters phải là một object có khóa và giá trị hợp lệ.")
    return StrategySelection(
        settings_version_id=settings_version_id,
        active_release_ids=(release_id,),
        published=True,
        parameters=tuple(sorted(parameters.items())),
    )


@dataclass(frozen=True, slots=True)
class DecisionCycleResult:
    """Kết quả bất biến kèm đầy đủ lineage của một lượt quyết định."""

    outcome: DecisionCycleOutcome
    strategy_result: StrategyResult
    plan: PlanResult | None = None
    risk_decision: RiskDecision | None = None
    order: PaperOrderIntent | None = None
    workspace_risk_decision: WorkspaceRiskDecision | None = None


class PaperDecisionCycle:
    """Chạy candidate, planner, hard risk gate rồi mới gửi vào PAPER gateway."""

    def __init__(
        self,
        strategies: StrategyRegistry,
        planner: TradePlanner,
        risk_engine: RiskEngine,
        paper_gateway: PaperExecutionGateway,
    ) -> None:
        self._strategies = strategies
        self._planner = planner
        self._risk_engine = risk_engine
        self._paper_gateway = paper_gateway

    @property
    def paper_gateway(self) -> PaperExecutionGateway:
        """Gateway gắn với chu kỳ để composition xác minh phạm vi PAPER."""
        return self._paper_gateway

    def evaluate_published(
        self,
        *,
        published_settings: PublishedWorkspaceSettings,
        snapshot: DecisionSnapshot,
        evaluated_at: datetime,
        planner_settings: PlannerSettings,
        account: AccountSnapshot,
        risk_state: RiskState,
        open_exposures: tuple[OpenExposure, ...],
        cost_model: CostModel,
        idempotency_key: str,
    ) -> DecisionCycleResult:
        """Chạy theo release duy nhất từ phiên bản root settings đã xác minh."""
        result = self.prepare_published(
            published_settings=published_settings,
            snapshot=snapshot,
            evaluated_at=evaluated_at,
            planner_settings=planner_settings,
            account=account,
            risk_state=risk_state,
            open_exposures=open_exposures,
            cost_model=cost_model,
        )
        if result.outcome is not DecisionCycleOutcome.RISK_APPROVED:
            return result
        return self.submit_approved(
            result, evaluated_at=evaluated_at, idempotency_key=idempotency_key
        )

    def prepare_published(
        self,
        *,
        published_settings: PublishedWorkspaceSettings,
        snapshot: DecisionSnapshot,
        evaluated_at: datetime,
        planner_settings: PlannerSettings,
        account: AccountSnapshot,
        risk_state: RiskState,
        open_exposures: tuple[OpenExposure, ...],
        cost_model: CostModel,
        strategy_parameters: tuple[tuple[str, int], ...] = (),
        selection_override: StrategySelection | None = None,
        allowed_symbols: tuple[str, ...] | None = None,
        allowed_timeframes: tuple[str, ...] | None = None,
    ) -> DecisionCycleResult:
        """Chuẩn bị quyết định từ root settings mà chưa gửi PAPER intent.

        ``selection_override``/``allowed_*`` đến từ cấu hình hiệu lực của bot (tập con root).
        """
        settings_version_id = str(published_settings.settings_version_id)
        if (
            published_settings.effective_at is not None
            and evaluated_at < published_settings.effective_at
        ):
            no_trade = NoTrade(
                strategy_id=published_settings.settings.strategy.default_release,
                code="SETTINGS_NOT_EFFECTIVE",
                detail_vi=(
                    "Phiên bản root settings được ghim chưa có hiệu lực tại thời điểm đánh giá."
                ),
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
        if (
            str(published_settings.workspace_id) != planner_settings.workspace_id
            or settings_version_id != planner_settings.settings_version_id
            or published_settings.settings_hash != planner_settings.settings_hash
            or published_settings.settings.runtime.mode != "PAPER"
        ):
            no_trade = NoTrade(
                strategy_id=published_settings.settings.strategy.default_release,
                code="PUBLISHED_SETTINGS_SCOPE_MISMATCH",
                detail_vi="Settings đã publish không cùng workspace/version hoặc không ở PAPER.",
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
        configured_symbols = set(published_settings.settings.market_data.symbols)
        configured_timeframes = set(published_settings.settings.market_data.timeframes)
        observed_timeframes = {series.timeframe for series in snapshot.series}
        if (
            snapshot.symbol not in configured_symbols
            or not observed_timeframes
            or not observed_timeframes.issubset(configured_timeframes)
            or published_settings.settings.strategy.entry_timeframe not in observed_timeframes
        ):
            no_trade = NoTrade(
                strategy_id=published_settings.settings.strategy.default_release,
                code="MARKET_SCOPE_MISMATCH",
                detail_vi=(
                    "Symbol hoặc timeframe của snapshot không khớp WorkspaceSettings đã publish."
                ),
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
        if (allowed_symbols is not None and snapshot.symbol not in allowed_symbols) or (
            allowed_timeframes is not None and not observed_timeframes.issubset(allowed_timeframes)
        ):
            no_trade = NoTrade(
                strategy_id=published_settings.settings.strategy.default_release,
                code="BOT_MARKET_SCOPE_MISMATCH",
                detail_vi="Symbol hoặc timeframe nằm ngoài universe của cấu hình bot hiệu lực.",
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
        selection = strategy_selection_from_published_settings(
            published_settings.settings,
            settings_version_id=settings_version_id,
            is_published=True,
        )
        if selection_override is not None:
            if (
                selection_override.settings_version_id != settings_version_id
                or len(selection_override.active_release_ids) != 1
                or not selection_override.published
            ):
                no_trade = NoTrade(
                    strategy_id=published_settings.settings.strategy.default_release,
                    code="BOT_CONFIG_SCOPE_MISMATCH",
                    detail_vi="Strategy của bot không cùng root version/release đang chạy.",
                )
                return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
            selection = selection_override
        elif strategy_parameters:
            selection = StrategySelection(
                settings_version_id=selection.settings_version_id,
                active_release_ids=selection.active_release_ids,
                published=selection.published,
                parameters=strategy_parameters,
            )
        release_id = selection.active_release_ids[0]
        release_hashes = dict(planner_settings.release_content_hashes)
        try:
            registered_hash = self._strategies.get_release(release_id).content_hash
        except KeyError:
            registered_hash = None
        if registered_hash is None or release_hashes.get(release_id) != registered_hash:
            no_trade = NoTrade(
                strategy_id=release_id,
                code="STRATEGY_RELEASE_NOT_IN_PUBLISHED_SCOPE",
                detail_vi="Strategy release không thuộc danh sách đã publish của workspace.",
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
        effective_planner = replace(planner_settings, active_release_ids=(release_id,))
        return self.prepare(
            snapshot=snapshot,
            evaluated_at=evaluated_at,
            release_id=release_id,
            selection=selection,
            planner_settings=effective_planner,
            account=account,
            risk_state=risk_state,
            open_exposures=open_exposures,
            cost_model=cost_model,
        )

    async def prepare_published_with_risk_profile(
        self,
        *,
        session: AsyncSession,
        published_settings: PublishedWorkspaceSettings,
        snapshot: DecisionSnapshot,
        evaluated_at: datetime,
        planner_settings: PlannerSettings,
        account: AccountSnapshot,
        risk_state: RiskState,
        open_exposures: tuple[OpenExposure, ...],
        cost_model: CostModel,
        bot_config: EffectiveBotConfig | None = None,
    ) -> DecisionCycleResult:
        """Nạp hard caps root; có cấu hình bot thì thay bằng giới hạn đã siết của bot."""
        profile = await load_risk_profile(
            session,
            workspace_id=published_settings.workspace_id,
            profile_id=published_settings.settings.risk.profile_version_id,
        )
        effective_planner = profile.apply_to(planner_settings)
        if bot_config is None:
            return self.prepare_published(
                published_settings=published_settings,
                snapshot=snapshot,
                evaluated_at=evaluated_at,
                planner_settings=effective_planner,
                account=account,
                risk_state=risk_state,
                open_exposures=open_exposures,
                cost_model=cost_model,
            )
        if (
            bot_config.workspace_id != published_settings.workspace_id
            or bot_config.settings_version_id != published_settings.settings_version_id
            or bot_config.settings_hash != published_settings.settings_hash
            or bot_config.risk_profile_version_id != profile.profile_id
        ):
            no_trade = NoTrade(
                strategy_id=published_settings.settings.strategy.default_release,
                code="BOT_CONFIG_SCOPE_MISMATCH",
                detail_vi="Cấu hình bot không được resolve trên root/risk profile đang chạy.",
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)
        # Resolver đã chứng minh mọi giới hạn bot <= root, nên thay limits không nới cap.
        return self.prepare_published(
            published_settings=published_settings,
            snapshot=snapshot,
            evaluated_at=evaluated_at,
            planner_settings=replace(effective_planner, limits=bot_config.risk_limits),
            account=account,
            risk_state=risk_state,
            open_exposures=open_exposures,
            cost_model=cost_model,
            selection_override=bot_config.strategy_selection(),
            allowed_symbols=bot_config.symbols,
            allowed_timeframes=bot_config.timeframes,
        )

    def prepare(
        self,
        *,
        snapshot: DecisionSnapshot,
        evaluated_at: datetime,
        release_id: str,
        selection: StrategySelection,
        planner_settings: PlannerSettings,
        account: AccountSnapshot,
        risk_state: RiskState,
        open_exposures: tuple[OpenExposure, ...],
        cost_model: CostModel,
    ) -> DecisionCycleResult:
        """Đánh giá strategy, plan và risk mà chưa tạo PAPER intent."""
        if (
            not selection.published
            or not planner_settings.published
            or selection.settings_version_id != planner_settings.settings_version_id
            or selection.active_release_ids != planner_settings.active_release_ids
        ):
            no_trade = NoTrade(
                strategy_id=release_id,
                code="PUBLISHED_SETTINGS_MISMATCH",
                detail_vi="Cấu hình strategy và planner không cùng phiên bản đã công bố.",
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)

        strategy = self._strategies.resolve(release_id, selection)
        strategy_result = strategy.evaluate(snapshot, selection)
        if isinstance(strategy_result, NoTrade):
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, strategy_result)
        if strategy_result.settings_version_id != planner_settings.settings_version_id:
            no_trade = NoTrade(
                strategy_id=strategy_result.strategy_id,
                code="STALE_SETTINGS_VERSION",
                detail_vi="Candidate dùng phiên bản cấu hình khác planner.",
            )
            return DecisionCycleResult(DecisionCycleOutcome.NO_TRADE, no_trade)

        plan = self._planner.plan(
            strategy_result,
            planner_settings,
            account,
            open_exposures,
            cost_model,
            now=evaluated_at,
        )
        if isinstance(plan, PlanRejected):
            return DecisionCycleResult(
                DecisionCycleOutcome.PLAN_REJECTED,
                strategy_result,
                plan=plan,
            )

        decision = self._risk_engine.evaluate(plan, risk_state, planner_settings)
        if not decision.approved:
            return DecisionCycleResult(
                DecisionCycleOutcome.RISK_REJECTED,
                strategy_result,
                plan=plan,
                risk_decision=decision,
            )

        return DecisionCycleResult(
            DecisionCycleOutcome.RISK_APPROVED,
            strategy_result,
            plan=plan,
            risk_decision=decision,
        )

    def submit_approved(
        self,
        result: DecisionCycleResult,
        *,
        evaluated_at: datetime,
        idempotency_key: str,
    ) -> DecisionCycleResult:
        """Tạo intent sau khi caller đã lưu risk approval và reservation."""
        if (
            result.outcome is not DecisionCycleOutcome.RISK_APPROVED
            or not isinstance(result.strategy_result, StrategyCandidate)
            or not isinstance(result.plan, TradePlan)
            or result.risk_decision is None
            or not result.risk_decision.approved
        ):
            raise ValueError("Chỉ kết quả đã vượt risk gate mới được gửi vào PAPER gateway.")
        order = self._paper_gateway.submit(
            result.plan,
            idempotency_key,
            submitted_at=evaluated_at,
            eligible_after=result.strategy_result.eligible_after,
        )
        return DecisionCycleResult(
            DecisionCycleOutcome.PAPER_INTENT_CREATED,
            result.strategy_result,
            plan=result.plan,
            risk_decision=result.risk_decision,
            order=order,
        )

    def evaluate(
        self,
        *,
        snapshot: DecisionSnapshot,
        evaluated_at: datetime,
        release_id: str,
        selection: StrategySelection,
        planner_settings: PlannerSettings,
        account: AccountSnapshot,
        risk_state: RiskState,
        open_exposures: tuple[OpenExposure, ...],
        cost_model: CostModel,
        idempotency_key: str,
    ) -> DecisionCycleResult:
        """Chạy thuần cho PAPER không lưu DB; dịch vụ lưu trữ nên dùng prepare trước."""
        result = self.prepare(
            snapshot=snapshot,
            evaluated_at=evaluated_at,
            release_id=release_id,
            selection=selection,
            planner_settings=planner_settings,
            account=account,
            risk_state=risk_state,
            open_exposures=open_exposures,
            cost_model=cost_model,
        )
        if result.outcome is not DecisionCycleOutcome.RISK_APPROVED:
            return result
        return self.submit_approved(
            result, evaluated_at=evaluated_at, idempotency_key=idempotency_key
        )
