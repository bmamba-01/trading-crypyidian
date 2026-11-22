"""Context provider PAPER thật từ dữ liệu đã persist (RT-1 lát 2).

`PostgresPaperDecisionContextProvider` thay `SyntheticContextProvider` của probe E2E: mọi số liệu
đến từ PostgreSQL trong UoW tenant-scoped của command, không gọi API sàn nào.

- Snapshot: dựng bằng `build_market_decision_snapshot` (cùng hàm với producer signal-only), cùng
  cutoff `observed_at` của evidence và mốc freshness `evaluated_at`, nên khi account phẳng ID
  snapshot trùng `decision_snapshot_id` đã lưu. `position_is_flat` đọc từ ledger (point-in-time).
  Macro để unknown (None): H06 sẽ abstain, đúng hành vi signal-only hiện tại.
- PlannerSettings: ExchangeRules từ `market_instrument` mới nhất đã khả dụng; đòn bẩy (leverage)
  là trần `max_leverage` của cấu hình hiệu lực (bot overlay đã siết hoặc risk profile) — không có
  trường leverage riêng trong settings, thiếu/không hợp lệ thì fail-closed.
- AccountSnapshot/RiskState: vốn từ `AccountEquityRepository.load_paper_equity_snapshot` (unknown
  → `PAPER_EQUITY_UNKNOWN`), lỗ ngày/trượt từ `calculate_loss_windows` (chính sách UTC đã ghim),
  exposure mở + reservation đang giữ như runtime thật.
- Ngắt mạch drawdown (FX-2, v2): đỉnh vốn đã chốt kể từ lần "Tiếp tục" bot gần nhất trong
  `bot_runtime_control` (resume cần MFA + lý do) so với vốn mark-to-market hiện tại; ≥ ngưỡng
  `risk.max_drawdown_fraction` của workspace settings đã publish (mặc định 10%, CRY-428) thì
  `RiskState.drawdown_breaker_tripped` và RiskEngine từ chối NEW_ENTRY (thoát lệnh không bị chặn).
- CostModel không nằm ở đây: application dùng `ResolvedPaperExecutionProfile.cost_model`.
- Monitor evidence: ATR (`atr_14`) của entry timeframe tính bằng `FeatureCalculator` từ nến đã
  đóng trước khi nến monitor mở; mức kênh/feature thoát cho recipe v2. `opposing_signal` luôn False
  (giới hạn đã ghi: chưa đánh giá lại release tại từng nến; chỉ H01 dùng thoát theo tín hiệu ngược).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.bot_runtime_entities import BotRuntimeControlRecord
from trading_bot.adapters.database.models.entities import TradingAccount
from trading_bot.adapters.database.models.market_entities import MarketInstrument
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord, TradeRecord
from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
from trading_bot.adapters.database.models.signal_entities import (
    BotDecisionRecord,
    SharedSignalEvidenceRecord,
)
from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
from trading_bot.adapters.database.repositories.risk_reservations import (
    RiskReservationRepository,
)
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    load_effective_bot_config,
)
from trading_bot.application.decision_snapshot_builder import build_decision_snapshot_from_bars
from trading_bot.application.market_signal_composition import (
    build_market_decision_snapshot,
    load_closed_market_bars,
)
from trading_bot.application.operational_telemetry import evaluate_and_persist_breaker
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_runtime_application import (
    PaperDecisionContext,
    PaperMonitorEvidence,
    PaperRuntimeApplicationError,
)
from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    load_risk_profile,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
    feature_settings_for,
)
from trading_bot.application.workspace_risk import mark_policy_from
from trading_bot.domain.market.feature_snapshot import FeatureCalculator
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
from trading_bot.domain.risk.circuit_breaker import CircuitBreakerPolicy
from trading_bot.domain.risk.drawdown_breaker import (
    PAPER_DRAWDOWN_BREAKER_POLICY,
    ClosedTradePnl,
    DrawdownBreakerPolicy,
    DrawdownBreakerState,
    drawdown_policy_for,
    evaluate_drawdown_breaker,
)
from trading_bot.domain.risk.engine import RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    ExchangeRules,
    OpenExposure,
    PlannerSettings,
)
from trading_bot.domain.risk.risk_windows import (
    RISK_LOSS_WINDOW_POLICY,
    RealizedTradePnlEvent,
    calculate_loss_windows,
)
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.rules_v3 import exit_evidence_spec

# v2: ngắt mạch drawdown FX-2; v3 (CRY-428): ngưỡng drawdown lấy từ workspace settings.
CONTEXT_PROVIDER_VERSION = "rt1-postgres-paper-context-v3"
INSTRUMENT_SOURCE = "BINANCE_USDM"
# Trạng thái intent còn có thể khớp hoặc chưa rõ: account không được coi là phẳng.
LIVE_INTENT_STATUSES = ("ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN")
# Độ dài nến danh định (gồm khung execution 1m/5m không có trong hợp đồng feature).
BAR_INTERVALS: Mapping[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_LOSS_LOOKBACK = timedelta(days=2)


def strategy_bar_closed(*, opened_at: datetime, execution: str, strategy: str) -> bool:
    """Nến execution mở lúc `opened_at` có khép một nến của timeframe chiến lược không.

    Dùng độ dài danh định (`open + interval`) thay vì `close_time` (Binance đóng ở `−1ms`).
    """
    execution_interval = BAR_INTERVALS[execution]
    strategy_interval = BAR_INTERVALS[strategy]
    end = opened_at.astimezone(UTC) + execution_interval
    return (end - _EPOCH) % strategy_interval == timedelta(0)


def exchange_rules_from_instrument(
    instrument: Any,
    *,
    max_notional: Decimal,
    reference_price: Decimal,
) -> ExchangeRules:
    """ExchangeRules từ instrument đã lưu; trần notional/khối lượng lấy từ giới hạn rủi ro."""
    if str(getattr(instrument, "status", "")) != "TRADING":
        raise PaperRuntimeApplicationError("PAPER_INSTRUMENT_NOT_TRADING")
    tick = _positive(getattr(instrument, "tick_size", None))
    step = _positive(getattr(instrument, "quantity_step", None))
    min_notional = getattr(instrument, "min_notional", None)
    if (
        tick is None
        or step is None
        or not isinstance(min_notional, Decimal)
        or not min_notional.is_finite()
        or min_notional < 0
    ):
        raise PaperRuntimeApplicationError("PAPER_INSTRUMENT_RULES_INVALID")
    cap = _positive(max_notional)
    price = _positive(reference_price)
    if cap is None or price is None:
        raise PaperRuntimeApplicationError("PAPER_EXCHANGE_LIMITS_UNAVAILABLE")
    max_quantity = ((cap / price) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if max_quantity <= 0 or cap < min_notional:
        raise PaperRuntimeApplicationError("PAPER_EXCHANGE_LIMITS_UNAVAILABLE")
    return ExchangeRules(
        tick_size=tick,
        quantity_step=step,
        contract_multiplier=Decimal("1"),
        min_notional=min_notional,
        max_notional=cap,
        max_quantity=max_quantity,
    )


def effective_leverage(limits: Any) -> Decimal:
    """Đòn bẩy runtime = trần `max_leverage` hiệu lực; thiếu/không dương thì fail-closed."""
    value = _positive(getattr(limits, "max_leverage", None))
    if value is None:
        raise PaperRuntimeApplicationError("PAPER_LEVERAGE_UNAVAILABLE")
    return value


async def position_is_flat_at(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    account_id: UUID,
    symbol: str,
    as_of: datetime,
) -> bool:
    """Account phẳng trên symbol tại `as_of` khi không có hoạt động nào chồng lên mốc đó.

    Không phẳng nếu có trade còn mở hoặc đóng sau `as_of`, intent còn sống/UNKNOWN hoặc tạo sau
    `as_of`. Như vậy một candidate cũ không thể vào lệnh chồng thời gian với vị thế đã có.
    """
    trade = await session.scalar(
        select(TradeRecord.id)
        .where(
            TradeRecord.workspace_id == workspace_id,
            TradeRecord.account_id == account_id,
            TradeRecord.symbol == symbol,
            TradeRecord.mode == "PAPER",
            or_(TradeRecord.status == "OPEN", TradeRecord.closed_at > as_of),
        )
        .limit(1)
    )
    if trade is not None:
        return False
    intent = await session.scalar(
        select(OrderIntentRecord.id)
        .where(
            OrderIntentRecord.workspace_id == workspace_id,
            OrderIntentRecord.account_id == account_id,
            OrderIntentRecord.symbol == symbol,
            OrderIntentRecord.mode == "PAPER",
            or_(
                OrderIntentRecord.status.in_(LIVE_INTENT_STATUSES),
                OrderIntentRecord.created_at > as_of,
            ),
        )
        .limit(1)
    )
    return intent is None


class PostgresPaperDecisionContextProvider:
    """Nạp `PaperDecisionContext`/`PaperMonitorEvidence` chỉ từ dữ liệu PostgreSQL đã lưu."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        *,
        workspace_id: UUID,
        feature_cache_size: int = 64,
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._feature_cache: dict[tuple[str, str, datetime], tuple[Any, datetime | None]] = {}
        self._feature_cache_size = feature_cache_size

    # --- DecisionSnapshotProvider (dispatcher runtime) ------------------------------------
    async def snapshot_for(
        self, *, evidence: Any, context: Any, evaluated_at: datetime
    ) -> DecisionSnapshot:
        """Snapshot cho job bot; như producer nhưng `position_is_flat` theo ledger account."""
        if self._sessions is None:
            raise PaperRuntimeApplicationError("DATABASE_UNAVAILABLE")
        if evidence.workspace_id != context.workspace_id:
            raise ValueError("Evidence và config bot phải cùng workspace.")
        cutoff = getattr(evidence, "observed_at", None)
        if not isinstance(cutoff, datetime):
            raise ValueError("Evidence cần có observed_at làm cutoff bất biến.")
        if cutoff > evaluated_at:
            raise ValueError("Evidence cutoff không được sau evaluated_at.")
        async with UnitOfWork(
            self._sessions, workspace_id=context.workspace_id, bot_id=context.bot_id
        ) as uow:
            flat = await position_is_flat_at(
                uow.session,
                workspace_id=context.workspace_id,
                account_id=context.account_id,
                symbol=evidence.symbol,
                as_of=evaluated_at,
            )
            return await build_market_decision_snapshot(
                uow.session,
                workspace_id=context.workspace_id,
                symbol=evidence.symbol,
                strategy_release_id=context.strategy_release_id,
                decision_time=cutoff,
                series_quality_as_of=evaluated_at,
                position_is_flat=flat,
            )

    # --- PaperDecisionContextProvider ---------------------------------------------------
    async def load_context(
        self,
        *,
        uow: Any,
        command: Any,
        published_settings: Any,
        execution_profile: Any,
        account: Any,
    ) -> PaperDecisionContext:
        session: AsyncSession = uow.session
        published = published_settings
        workspace_id = published.workspace_id
        if workspace_id != self._workspace_id or uow.workspace_id != workspace_id:
            raise PaperRuntimeApplicationError("PAPER_WORKSPACE_SCOPE_MISMATCH")
        evaluated_at = command.evaluated_at.astimezone(UTC)
        decision, evidence = await self._load_decision_evidence(session, command, account)
        release_id = decision.strategy_release_id
        try:
            release_hash = strategy_registry().get_release(release_id).content_hash
        except KeyError:
            raise PaperRuntimeApplicationError("PAPER_STRATEGY_RELEASE_NOT_PUBLISHED") from None

        flat = await position_is_flat_at(
            session,
            workspace_id=workspace_id,
            account_id=account.account_id,
            symbol=evidence.symbol,
            as_of=evaluated_at,
        )
        try:
            snapshot = await build_market_decision_snapshot(
                session,
                workspace_id=workspace_id,
                symbol=evidence.symbol,
                strategy_release_id=release_id,
                decision_time=evidence.observed_at,
                series_quality_as_of=evaluated_at,
                position_is_flat=flat,
            )
        except ValueError:
            raise PaperRuntimeApplicationError("PAPER_DECISION_SNAPSHOT_UNAVAILABLE") from None
        bars = tuple(bar for series in snapshot.series for bar in series.bars)
        if not bars:
            raise PaperRuntimeApplicationError("PAPER_MARKET_DATA_UNAVAILABLE")
        market_data_as_of = max(bar.available_at for bar in bars)
        entry_timeframe = entry_timeframe_for_release(release_id)
        entry_bars = next(
            (series.bars for series in snapshot.series if series.timeframe == entry_timeframe),
            (),
        )
        if not entry_bars:
            raise PaperRuntimeApplicationError("PAPER_MARKET_DATA_UNAVAILABLE")
        reference_price = entry_bars[-1].close

        try:
            profile = await load_risk_profile(
                session,
                workspace_id=workspace_id,
                profile_id=published.settings.risk.profile_version_id,
            )
            bot_config = await load_effective_bot_config(
                session,
                workspace_id=workspace_id,
                account_id=account.account_id,
                as_of=evaluated_at,
                published=published,
            )
        except RiskProfileRuntimeError, BotConfigResolutionError:
            raise PaperRuntimeApplicationError("PAPER_RISK_CONFIG_UNAVAILABLE") from None
        limits = bot_config.risk_limits if bot_config is not None else profile.limits
        leverage = effective_leverage(limits)

        instrument = await session.scalar(
            select(MarketInstrument)
            .where(
                MarketInstrument.workspace_id == workspace_id,
                MarketInstrument.symbol == evidence.symbol,
                MarketInstrument.source == INSTRUMENT_SOURCE,
                MarketInstrument.available_at <= evaluated_at,
            )
            .order_by(MarketInstrument.available_at.desc(), MarketInstrument.revision.desc())
            .limit(1)
        )
        if instrument is None:
            raise PaperRuntimeApplicationError("PAPER_INSTRUMENT_UNAVAILABLE")
        account_cap = await session.scalar(
            select(TradingAccount.max_notional).where(
                TradingAccount.workspace_id == workspace_id,
                TradingAccount.id == account.account_id,
            )
        )
        caps = [limits.max_notional, limits.max_symbol_notional]
        if isinstance(account_cap, Decimal) and account_cap > 0:
            caps.append(account_cap)
        exchange = exchange_rules_from_instrument(
            instrument, max_notional=min(caps), reference_price=reference_price
        )

        risk = published.settings.risk
        equity = await AccountEquityRepository(session).load_paper_equity_snapshot(
            workspace_id=workspace_id,
            bot_id=account.bot_id,
            account_id=account.account_id,
            as_of=evaluated_at,
            max_equity_age=timedelta(seconds=risk.max_equity_age_seconds),
            max_mark_age=timedelta(seconds=risk.max_mark_age_seconds),
            mark_policy=mark_policy_from(published.settings),
        )
        if not equity.known or equity.equity_amount <= 0:
            raise PaperRuntimeApplicationError("PAPER_EQUITY_UNKNOWN")

        exposures, open_margin = await self._open_exposures(session, account)
        totals = await RiskReservationRepository(session)._active_totals(
            workspace_id=workspace_id, account_id=account.account_id, symbol=evidence.symbol
        )
        losses = await self._loss_windows(session, account, as_of=evaluated_at)
        breaker = await self._drawdown_breaker(
            session,
            account,
            as_of=evaluated_at,
            equity=equity.equity_amount,
            seed=equity.seed_capital,
            policy=drawdown_policy_for(risk.max_drawdown_fraction),
        )
        _telemetry, _loss_windows, ops_breaker = await evaluate_and_persist_breaker(
            session,
            workspace_id=workspace_id,
            bot_id=account.bot_id,
            as_of=evaluated_at,
            policy=CircuitBreakerPolicy(
                max_drawdown_pct=breaker.max_drawdown_fraction * Decimal("100"),
            ),
            drawdown_pct_override=(breaker.drawdown_fraction * Decimal("100")).quantize(
                Decimal("0.01")
            ),
            outbox=uow,
        )
        available_margin = max(Decimal("0"), equity.equity_amount - open_margin)
        workspace = str(workspace_id)
        version = str(published.settings_version_id)
        account_snapshot = AccountSnapshot(
            workspace,
            str(account.account_id),
            version,
            equity.captured_at,
            equity.equity_amount,
            available_margin,
            True,
        )
        planner = PlannerSettings(
            workspace_id=workspace,
            settings_version_id=version,
            risk_profile_version_id=str(risk.profile_version_id),
            published=True,
            active_release_ids=(release_id,),
            leverage=leverage,
            exchange=exchange,
            limits=limits,
            release_content_hashes=((release_id, release_hash),),
            settings_hash=published.settings_hash,
        )
        state = RiskState(
            workspace_id=workspace,
            account_id=str(account.account_id),
            settings_version_id=version,
            risk_profile_version_id=str(risk.profile_version_id),
            evaluated_at=command.evaluated_at,
            account_captured_at=equity.captured_at,
            market_data_as_of=market_data_as_of,
            equity=equity.equity_amount,
            available_margin=available_margin,
            open_exposures=exposures,
            realized_loss_today=losses.realized_loss_today,
            realized_loss_rolling=losses.realized_loss_rolling,
            open_position_count=len(exposures),
            is_known=True,
            pending_reservation_initial_margin=totals.initial_margin,
            pending_reservation_risk=totals.risk,
            pending_reservation_count=totals.count,
            pending_reservation_notional=totals.notional,
            pending_symbol_reservation_notional=totals.symbol_notional,
            drawdown_breaker_tripped=breaker.tripped or not ops_breaker.entry_allowed,
        )
        return PaperDecisionContext(
            snapshot=snapshot,
            planner_settings=planner,
            account=account_snapshot,
            risk_state=state,
            account_quote_asset=equity.asset,
            open_exposures=exposures,
            evidence={
                "context_provider": CONTEXT_PROVIDER_VERSION,
                "decision_snapshot_id": snapshot.snapshot_id,
                "signal_evidence_id": str(evidence.id),
                "equity_source_hash": equity.source_hash,
                "instrument_record_id": str(instrument.id),
                "instrument_revision": int(instrument.revision),
                "leverage_source": "effective_max_leverage",
                "loss_window_timezone": losses.timezone_name,
                "market_data_as_of": market_data_as_of.isoformat(),
                "circuit_breaker_state": ops_breaker.state.value,
                "circuit_breaker_revision": ops_breaker.revision,
                **breaker.evidence(),
            },
        )

    async def load_monitor_evidence(
        self,
        *,
        uow: Any,
        command: Any,
        published_settings: Any,
        execution_profile: Any,
        account: Any,
        position: Any,
        pinned_strategy_release_name: str | None,
        pinned_strategy_release_hash: str | None,
        pinned_strategy_settings_version_id: str | None,
        pinned_strategy_parameters: tuple[tuple[str, int], ...],
    ) -> PaperMonitorEvidence:
        session: AsyncSession = uow.session
        bar = command.bar
        workspace_id = published_settings.workspace_id
        if workspace_id != self._workspace_id:
            raise PaperRuntimeApplicationError("PAPER_WORKSPACE_SCOPE_MISMATCH")
        release = pinned_strategy_release_name or ""
        entry_timeframe = entry_timeframe_for_release(release) if release else None
        execution = _execution_timeframe(bar)
        atr: Decimal | None = None
        known_at: list[datetime] = []
        if entry_timeframe is not None:
            atr, atr_available = await self._feature_at(
                session,
                symbol=bar.symbol,
                timeframe=entry_timeframe,
                cutoff=bar.opened_at,
                name="atr_14",
            )
            if atr_available is not None:
                known_at.append(atr_available)
        channel_level: Decimal | None = None
        feature_value: Decimal | None = None
        closes_strategy_bar = True
        recipe = getattr(position, "recipe", None)
        spec = exit_evidence_spec(recipe.recipe_id) if recipe is not None else None
        if spec is not None and execution is not None:
            closes_strategy_bar = strategy_bar_closed(
                opened_at=bar.opened_at, execution=execution, strategy=spec.timeframe
            )
            if spec.channel_period is not None:
                strategy_bars = await load_closed_market_bars(
                    session,
                    workspace_id=workspace_id,
                    symbol=bar.symbol,
                    timeframe=spec.timeframe,
                    as_of=bar.opened_at,
                    max_bars=spec.channel_period,
                )
                if len(strategy_bars) >= spec.channel_period:
                    window = strategy_bars[-spec.channel_period :]
                    channel_level = (
                        min(item.low for item in window)
                        if position.direction == "long"
                        else max(item.high for item in window)
                    )
                    known_at.append(max(item.available_at for item in window))
            if spec.feature_name is not None and closes_strategy_bar:
                # Như replay: feature thoát của nến chiến lược khép tại nến monitor này, biết ở
                # lúc nến đóng (`available_at` của bar); không tính vào provenance lúc mở nến.
                feature_value, _ = await self._feature_at(
                    session,
                    symbol=bar.symbol,
                    timeframe=spec.timeframe,
                    cutoff=bar.available_at,
                    name=spec.feature_name,
                    positive_only=False,
                )
        elif entry_timeframe is not None and execution is not None:
            # Recipe v1: đếm `max_hold_bars` theo nến của timeframe chiến lược, không theo 1m.
            closes_strategy_bar = strategy_bar_closed(
                opened_at=bar.opened_at, execution=execution, strategy=entry_timeframe
            )
        opposing_signal = False
        if (
            recipe is not None
            and getattr(recipe, "opposite_signal_exit", False)
            and not getattr(position, "is_closed", False)
            and getattr(position, "quantity", Decimal("0")) > Decimal("0")
            and closes_strategy_bar
        ):
            opposing_signal = await self._evaluate_opposing_signal(
                session,
                workspace_id=workspace_id,
                symbol=bar.symbol,
                release=release,
                pinned_strategy_release_hash=pinned_strategy_release_hash,
                pinned_strategy_settings_version_id=pinned_strategy_settings_version_id,
                pinned_strategy_parameters=pinned_strategy_parameters,
                position_direction=position.direction,
                as_of=bar.opened_at,
            )
        # ATR/kênh chỉ dùng nến đóng trước lúc nến monitor mở nên mốc này không sau `opened_at`.
        available_at = max(known_at) if known_at else bar.opened_at - timedelta(microseconds=1)
        return PaperMonitorEvidence(
            workspace_id=workspace_id,
            account_id=account.account_id,
            feature_snapshot_id=command.feature_snapshot_id,
            symbol=bar.symbol,
            available_at=available_at,
            strategy_release_name=release,
            strategy_release_hash=pinned_strategy_release_hash or "",
            # RT-1/opposing-h01: tính tín hiệu đối ứng point-in-time từ nến chiến lược đã đóng.
            opposing_signal=opposing_signal,
            atr=atr,
            strategy_settings_version_id=pinned_strategy_settings_version_id,
            strategy_parameters=pinned_strategy_parameters,
            exit_channel_level=channel_level,
            exit_feature_value=feature_value,
            strategy_bar_closed=closes_strategy_bar,
        )

    # --- helpers --------------------------------------------------------------------------
    async def _evaluate_opposing_signal(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        symbol: str,
        release: str,
        pinned_strategy_release_hash: str | None,
        pinned_strategy_settings_version_id: str | None,
        pinned_strategy_parameters: tuple[tuple[str, int], ...],
        position_direction: str,
        as_of: datetime,
    ) -> bool:
        """Đánh giá tín hiệu đối ứng point-in-time từ nến đã đóng (RT-1/opposing-h01)."""
        if not release:
            return False
        try:
            requirements = feature_requirements_for_release(release)
        except Exception:
            return False
        bars_by_timeframe: dict[str, tuple[OHLCVBar, ...]] = {}
        for tf in requirements:
            bars = await load_closed_market_bars(
                session,
                workspace_id=workspace_id,
                symbol=symbol,
                timeframe=tf,
                as_of=as_of,
            )
            if not bars:
                return False
            bars_by_timeframe[tf] = bars

        try:
            snapshot = build_decision_snapshot_from_bars(
                workspace_id=workspace_id,
                symbol=symbol,
                strategy_release_id=release,
                decision_time=as_of,
                series_quality_as_of=as_of,
                bars_by_timeframe=bars_by_timeframe,
                position_is_flat=True,
                source_identity_required=False,
                legacy_identity_namespace="paper-monitor-opposing",
            )
        except Exception:
            return False

        try:
            selection = StrategySelection(
                settings_version_id=pinned_strategy_settings_version_id or "",
                active_release_ids=(release,),
                published=True,
                parameters=pinned_strategy_parameters,
            )
            strategy = strategy_registry().resolve(release, selection)
            candidate = strategy.evaluate(snapshot, selection)
            if (
                isinstance(candidate, StrategyCandidate)
                and candidate.direction in ("long", "short")
            ):
                return candidate.direction != position_direction
        except Exception:
            return False
        return False

    # --- helpers --------------------------------------------------------------------------
    async def _load_decision_evidence(
        self, session: AsyncSession, command: Any, account: Any
    ) -> tuple[BotDecisionRecord, SharedSignalEvidenceRecord]:
        decision_id = getattr(command, "bot_decision_id", None)
        if decision_id is None:
            raise PaperRuntimeApplicationError("PAPER_CONTEXT_BOT_DECISION_REQUIRED")
        decision = await session.scalar(
            select(BotDecisionRecord).where(
                BotDecisionRecord.workspace_id == self._workspace_id,
                BotDecisionRecord.account_id == account.account_id,
                BotDecisionRecord.bot_id == account.bot_id,
                BotDecisionRecord.id == decision_id,
            )
        )
        if decision is None:
            raise PaperRuntimeApplicationError("BOT_DECISION_NOT_FOUND")
        evidence = await session.scalar(
            select(SharedSignalEvidenceRecord).where(
                SharedSignalEvidenceRecord.workspace_id == self._workspace_id,
                SharedSignalEvidenceRecord.id == decision.evidence_id,
            )
        )
        if (
            evidence is None
            or evidence.feature_snapshot_id != command.feature_snapshot_id
            or decision.evaluated_at != command.evaluated_at
            or evidence.observed_at > command.evaluated_at
        ):
            raise PaperRuntimeApplicationError("PAPER_DECISION_CONTEXT_MISMATCH")
        return decision, evidence

    async def _open_exposures(
        self, session: AsyncSession, account: Any
    ) -> tuple[tuple[OpenExposure, ...], Decimal]:
        rows = (
            await session.execute(
                select(
                    TradeRecord.symbol,
                    RiskReservationRecord.reserved_notional,
                    RiskReservationRecord.reserved_risk,
                    RiskReservationRecord.reserved_initial_margin,
                )
                .join(
                    RiskReservationRecord,
                    (RiskReservationRecord.workspace_id == TradeRecord.workspace_id)
                    & (RiskReservationRecord.id == TradeRecord.risk_reservation_id),
                )
                .where(
                    TradeRecord.workspace_id == self._workspace_id,
                    TradeRecord.account_id == account.account_id,
                    TradeRecord.status == "OPEN",
                )
                .order_by(TradeRecord.opened_at, TradeRecord.id)
            )
        ).all()
        workspace = str(self._workspace_id)
        exposures = tuple(
            OpenExposure(
                workspace, str(account.account_id), symbol, Decimal(notional), Decimal(risk)
            )
            for symbol, notional, risk, _margin in rows
        )
        margin = sum((Decimal(row[3]) for row in rows), Decimal("0"))
        return exposures, margin

    async def _loss_windows(self, session: AsyncSession, account: Any, *, as_of: datetime) -> Any:
        rows = (
            await session.execute(
                select(TradeRecord.id, TradeRecord.closed_at, TradeRecord.net_pnl).where(
                    TradeRecord.workspace_id == self._workspace_id,
                    TradeRecord.account_id == account.account_id,
                    TradeRecord.status == "CLOSED",
                    TradeRecord.closed_at <= as_of,
                    TradeRecord.closed_at > as_of - _LOSS_LOOKBACK,
                )
            )
        ).all()
        events = [
            RealizedTradePnlEvent(str(trade_id), closed_at, Decimal(net_pnl))
            for trade_id, closed_at, net_pnl in rows
            if closed_at is not None
        ]
        return calculate_loss_windows(events, as_of=as_of, policy=RISK_LOSS_WINDOW_POLICY)

    async def _drawdown_breaker(
        self,
        session: AsyncSession,
        account: Any,
        *,
        as_of: datetime,
        equity: Decimal,
        seed: Decimal | None,
        policy: DrawdownBreakerPolicy = PAPER_DRAWDOWN_BREAKER_POLICY,
    ) -> DrawdownBreakerState:
        """Đo drawdown từ ledger; thiếu seed thì fail-closed như vốn unknown."""
        if seed is None or seed <= 0:
            raise PaperRuntimeApplicationError("PAPER_EQUITY_UNKNOWN")
        baseline_at = await session.scalar(
            select(BotRuntimeControlRecord.changed_at)
            .where(
                BotRuntimeControlRecord.workspace_id == self._workspace_id,
                BotRuntimeControlRecord.bot_id == account.bot_id,
                BotRuntimeControlRecord.entry_state == "ENABLED",
                BotRuntimeControlRecord.changed_at <= as_of,
            )
            .order_by(BotRuntimeControlRecord.changed_at.desc())
            .limit(1)
        )
        rows = (
            await session.execute(
                select(TradeRecord.closed_at, TradeRecord.net_pnl).where(
                    TradeRecord.workspace_id == self._workspace_id,
                    TradeRecord.account_id == account.account_id,
                    TradeRecord.status == "CLOSED",
                    TradeRecord.closed_at <= as_of,
                )
            )
        ).all()
        trades = [
            ClosedTradePnl(closed_at, Decimal(net_pnl))
            for closed_at, net_pnl in rows
            if closed_at is not None and net_pnl is not None
        ]
        return evaluate_drawdown_breaker(
            seed_equity=seed,
            closed_trades=trades,
            current_equity=equity,
            baseline_at=baseline_at,
            policy=policy,
        )

    async def _feature_at(
        self,
        session: AsyncSession,
        *,
        symbol: str,
        timeframe: str,
        cutoff: datetime,
        name: str,
        positive_only: bool = True,
    ) -> tuple[Decimal | None, datetime | None]:
        """Giá trị feature tính từ nến đóng đã khả dụng tại `cutoff` (không nhìn trước)."""
        bars = await load_closed_market_bars(
            session,
            workspace_id=self._workspace_id,
            symbol=symbol,
            timeframe=timeframe,
            as_of=cutoff,
        )
        if not bars:
            return None, None
        last = bars[-1]
        key = (symbol, timeframe, last.open_time)
        cached = self._feature_cache.get(key)
        if cached is None:
            cached = (_calculate_features(self._workspace_id, bars, timeframe), last.available_at)
            if len(self._feature_cache) >= self._feature_cache_size:
                self._feature_cache.pop(next(iter(self._feature_cache)))
            self._feature_cache[key] = cached
        snapshot, available = cached
        if snapshot is None or not snapshot.is_usable or not snapshot.warmup.get(name, False):
            return None, available
        raw = snapshot.features.get(name)
        if raw is None:
            return None, available
        value = Decimal(str(raw))
        if not value.is_finite() or (positive_only and value <= 0):
            return None, available
        return value, available


def _calculate_features(workspace_id: UUID, bars: tuple[OHLCVBar, ...], timeframe: str) -> Any:
    as_of = max(bar.available_at for bar in bars)
    try:
        return FeatureCalculator(feature_settings_for(timeframe)).calculate(
            MarketSnapshot(as_of=as_of, bars=bars),
            workspace_id=workspace_id,
            input_event_ids=tuple(
                str(bar.source_identity.record_id)
                if bar.source_identity is not None
                else f"{bar.symbol}:{bar.timeframe}:{bar.open_time.isoformat()}"
                for bar in bars
            ),
            symbol=bars[-1].symbol,
            timeframe=timeframe,
            computed_at=as_of,
        )
    except ValueError:
        return None


def _execution_timeframe(bar: Any) -> str | None:
    """Timeframe execution suy từ bar key `source:symbol:timeframe:open_time` của event loop."""
    parts = str(getattr(bar, "bar_key", "")).split(":")
    if len(parts) >= 3 and parts[2] in BAR_INTERVALS:
        return parts[2]
    return None


def _positive(value: object) -> Decimal | None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        return None
    return value
