"""Composition DB cho producer signal-only; không cung cấp cổng thực thi."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.repositories.bot_decisions import PostgresSignalDecisionStore
from trading_bot.adapters.database.repositories.bot_runtime_controls import read_runtime_control
from trading_bot.adapters.database.repositories.feature_snapshots import FeatureSnapshotRepository
from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
)
from trading_bot.application.bot_decision_contexts import (
    EffectiveBotDecisionContextProvider,
    RuntimeBotRef,
    _count_open_incidents,
)
from trading_bot.application.bot_dispatcher import (
    BotDecisionContext,
    BotEvidenceDispatcher,
    SignalEvidenceFanOutConsumer,
)
from trading_bot.application.decision_snapshot_builder import (
    build_decision_snapshot_from_bars,
    quote_volumes_for_bars,
)
from trading_bot.application.market_signal_scanner import (
    MarketSignalObservation,
    MarketSignalScanner,
    MarketSignalScanResult,
    QualifiedMarketObservationSource,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.scorer_runtime_policy import ScorerRuntimePolicyProvider
from trading_bot.application.strategy_market_requirements import (
    TIMEFRAME_INTERVALS,
    feature_requirements_for_release,
    feature_settings_for,
)
from trading_bot.domain.execution.runtime_control import BotEntryState, WorkspaceKillState
from trading_bot.domain.market.feature_snapshot import FeatureCalculator
from trading_bot.domain.market.models import MarketBarSourceIdentity, MarketSnapshot, OHLCVBar
from trading_bot.domain.strategy.contracts import DecisionSnapshot
from trading_bot.domain.strategy.registry import StrategyRegistry

_MAX_BARS_PER_STREAM = 256
_LIVE_BAR_SOURCE = "BINANCE_USDM"
_BACKFILL_BAR_SOURCE = "BINANCE_USDM_REST_BACKFILL"


class PostgresClosedMarketObservationSource(QualifiedMarketObservationSource):
    """Đọc revision nến đóng theo as-of, tính/persist feature snapshot có lineage."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        max_bars_per_stream: int = _MAX_BARS_PER_STREAM,
    ) -> None:
        if not 200 <= max_bars_per_stream <= 2000:
            raise ValueError("max_bars_per_stream phải chứa warm-up 200 nến và không quá 2000.")
        self._sessions = sessions
        self._max_bars_per_stream = max_bars_per_stream

    async def qualified_closed_observations(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
        limit: int,
        symbols: Sequence[str],
        timeframes: Sequence[str],
    ) -> tuple[MarketSignalObservation, ...]:
        _validate_as_of(as_of)
        symbols = tuple(sorted(set(symbols)))
        timeframes = tuple(sorted(set(timeframes)))
        if not symbols or not timeframes:
            return ()
        unknown = set(timeframes) - TIMEFRAME_INTERVALS.keys()
        if unknown:
            raise ValueError("Bot yêu cầu timeframe chưa được hỗ trợ bởi producer.")
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            rows = await _latest_bar_rows(
                uow.session,
                workspace_id=workspace_id,
                as_of=as_of,
                symbols=symbols,
                timeframes=timeframes,
                max_bars_per_stream=self._max_bars_per_stream,
            )
            grouped = _group_bars(rows)
            observations: list[MarketSignalObservation] = []
            for (symbol, timeframe), stream in sorted(grouped.items()):
                bars = tuple(_to_bar(row) for row in stream)
                if not bars:
                    continue
                snapshot_as_of = max(bar.available_at for bar in bars)
                snapshot = FeatureCalculator(feature_settings_for(timeframe)).calculate(
                    MarketSnapshot(as_of=snapshot_as_of, bars=bars),
                    workspace_id=workspace_id,
                    input_event_ids=tuple(
                        _persisted_market_bar_event_id(bar, workspace_id=workspace_id)
                        for bar in bars
                    ),
                    symbol=symbol,
                    timeframe=timeframe,
                    computed_at=snapshot_as_of,
                )
                if snapshot.is_usable and snapshot.warmup_complete:
                    await FeatureSnapshotRepository(uow.session).append(
                        workspace_id=workspace_id,
                        snapshot=snapshot,
                    )
                observations.append(
                    MarketSignalObservation(
                        workspace_id=workspace_id,
                        bars=bars,
                        feature_snapshot=snapshot,
                        setup_type=f"market.bar.closed:{timeframe}",
                    )
                )
            observations.sort(
                key=lambda item: (
                    -item.bars[-1].open_time.timestamp(),
                    item.bars[-1].symbol,
                    item.bars[-1].timeframe,
                )
            )
            return tuple(observations[:limit])


class PostgresMarketDecisionSnapshotProvider:
    """Dựng DecisionSnapshot từ nến as-of; thiếu quote volume/macro thì để strategy abstain."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        max_bars_per_stream: int = _MAX_BARS_PER_STREAM,
    ) -> None:
        self._source = PostgresClosedMarketObservationSource(
            sessions,
            max_bars_per_stream=max_bars_per_stream,
        )

    async def snapshot_for(
        self,
        *,
        evidence: Any,
        context: BotDecisionContext,
        evaluated_at: datetime,
    ) -> DecisionSnapshot:
        _validate_as_of(evaluated_at)
        if evidence.workspace_id != context.workspace_id:
            raise ValueError("Evidence và config bot phải cùng workspace.")
        evidence_cutoff = getattr(evidence, "observed_at", None)
        if not isinstance(evidence_cutoff, datetime):
            raise ValueError("Evidence cần có observed_at làm cutoff bất biến.")
        _validate_as_of(evidence_cutoff)
        if evidence_cutoff > evaluated_at:
            raise ValueError("Evidence cutoff không được sau evaluated_at.")
        try:
            requirements = feature_requirements_for_release(context.strategy_release_id)
        except ValueError as error:
            raise ValueError(
                "Strategy release chưa được hỗ trợ bởi market snapshot provider."
            ) from error
        async with UnitOfWork(self._source._sessions, workspace_id=context.workspace_id) as uow:
            rows = await _latest_bar_rows(
                uow.session,
                workspace_id=context.workspace_id,
                as_of=evidence_cutoff,
                symbols=(evidence.symbol,),
                timeframes=tuple(requirements),
                max_bars_per_stream=self._source._max_bars_per_stream,
            )
        grouped = _group_bars(rows)
        return build_decision_snapshot_from_bars(
            workspace_id=context.workspace_id,
            symbol=evidence.symbol,
            strategy_release_id=context.strategy_release_id,
            decision_time=evidence_cutoff,
            series_quality_as_of=evaluated_at,
            bars_by_timeframe={
                timeframe: tuple(
                    _to_bar(row) for row in grouped.get((evidence.symbol, timeframe), ())
                )
                for timeframe in requirements
            },
            # Signal-only chỉ đưa strategy qua bộ lọc; không đọc PAPER position hay tạo lệnh.
            position_is_flat=True,
            source_identity_required=True,
        )


class SignalRuntimeGate:
    """Áp dụng kill/pause/incident; không lệ thuộc readiness hoặc trạng thái PAPER."""

    async def __call__(self, uow: Any, bot: RuntimeBotRef) -> str | None:
        control = await read_runtime_control(
            uow.session, workspace_id=uow.workspace_id, bot_id=bot.bot_id
        )
        if control.workspace_kill_state is WorkspaceKillState.KILLED:
            return "WORKSPACE_KILLED"
        if control.bot_entry_state is BotEntryState.PAUSED:
            return "BOT_PAUSED"
        if await _count_open_incidents(
            uow.session, workspace_id=uow.workspace_id, bot_id=bot.bot_id
        ):
            return "EXTERNAL_ACTIVITY_UNRECONCILED"
        return None


class SignalExclusionRecorder:
    """Không ghi runtime state: producer không sở hữu lease bot/account PAPER."""

    async def record(self, _eligibility: Any) -> None:
        return None


def build_market_signal_scanner(
    sessions: async_sessionmaker[AsyncSession],
    *,
    max_observation_age: timedelta,
    signal_validity: timedelta,
    scorer_policy_provider: ScorerRuntimePolicyProvider | None = None,
) -> _MarketSignalPipeline:
    """Lắp source/context/dispatcher signal-only, không phụ thuộc PAPER supervisor."""
    contexts = EffectiveBotDecisionContextProvider(
        sessions,
        runtime_gate=SignalRuntimeGate(),
        exclusion_recorder=SignalExclusionRecorder(),
    )
    store = PostgresSignalDecisionStore(
        sessions,
        score_candidate=PostgresScorerRuntimeCommitter(scorer_policy_provider),
    )
    dispatcher = BotEvidenceDispatcher(
        store,
        strategies=StrategyRegistry(),
        snapshots=PostgresMarketDecisionSnapshotProvider(sessions),
    )
    scanner = MarketSignalScanner(
        PostgresClosedMarketObservationSource(sessions),
        contexts,
        dispatcher,
        max_observation_age=max_observation_age,
        signal_validity=signal_validity,
    )
    fanout_consumer = SignalEvidenceFanOutConsumer(
        store,
        dispatcher,
        contexts,
        worker_id=f"market-signal-producer-{uuid4().hex}",
    )
    return _MarketSignalPipeline(scanner, fanout_consumer)


class _MarketSignalPipeline:
    """Tạo candidate rồi drain/fan-out outbox đã ghi trong cùng worker độc lập."""

    def __init__(
        self,
        scanner: MarketSignalScanner,
        fanout_consumer: SignalEvidenceFanOutConsumer,
    ) -> None:
        self._scanner = scanner
        self._fanout_consumer = fanout_consumer

    async def scan_once(self, *, workspace_id: UUID, now: datetime) -> MarketSignalScanResult:
        result = await self._scanner.scan_once(workspace_id=workspace_id, now=now)
        await self._fanout_consumer.consume_once(
            workspace_id=workspace_id,
            # available_at của outbox dùng server_default=now(), nên claim có thể
            # tới sau mốc đánh giá vừa lấy ở đầu tick vài mili giây.
            now=max(now.astimezone(UTC), datetime.now(UTC)),
            limit=100,
        )
        return result


async def _latest_bar_rows(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    as_of: datetime,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    max_bars_per_stream: int,
) -> tuple[MarketBar, ...]:
    result_rows: list[MarketBar] = []
    for symbol in sorted(set(symbols)):
        for timeframe in sorted(set(timeframes)):
            filters = (
                MarketBar.workspace_id == workspace_id,
                MarketBar.symbol == symbol,
                MarketBar.timeframe == timeframe,
                MarketBar.source.in_((_LIVE_BAR_SOURCE, _BACKFILL_BAR_SOURCE)),
                MarketBar.is_closed.is_(True),
                MarketBar.close_time <= as_of,
                MarketBar.available_at <= as_of,
            )
            open_times_result = await session.scalars(
                select(MarketBar.open_time)
                .where(*filters)
                .distinct()
                .order_by(MarketBar.open_time.desc())
                .limit(max_bars_per_stream)
            )
            open_times = tuple(open_times_result.all())
            if not open_times:
                continue
            rows_result = await session.scalars(
                select(MarketBar)
                .where(*filters, MarketBar.open_time.in_(open_times))
                .ext(distinct_on(MarketBar.open_time))
                .order_by(
                    MarketBar.open_time,
                    case(
                        (MarketBar.source == _LIVE_BAR_SOURCE, 0),
                        (MarketBar.source == _BACKFILL_BAR_SOURCE, 1),
                        else_=2,
                    ).asc(),
                    MarketBar.available_at.desc(),
                    MarketBar.revision.desc(),
                    MarketBar.id.desc(),
                )
            )
            result_rows.extend(rows_result.all())
    return tuple(result_rows)


def _group_bars(rows: Sequence[MarketBar]) -> dict[tuple[str, str], tuple[MarketBar, ...]]:
    groups: dict[tuple[str, str], list[MarketBar]] = defaultdict(list)
    for row in rows:
        groups[(row.symbol, row.timeframe)].append(row)
    return {
        key: tuple(sorted(value, key=lambda row: row.open_time)) for key, value in groups.items()
    }


def _to_bar(row: MarketBar) -> OHLCVBar:
    return OHLCVBar(
        symbol=row.symbol,
        timeframe=row.timeframe,
        open_time=row.open_time,
        close_time=row.close_time,
        event_at=row.event_at,
        received_at=row.received_at,
        available_at=row.available_at,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        quote_volume=row.quote_volume,
        is_closed=row.is_closed,
        source=row.source,
        source_cursor=row.source_cursor,
        checksum=row.checksum,
        raw_reference=None if row.raw_reference_id is None else str(row.raw_reference_id),
        source_identity=MarketBarSourceIdentity(
            workspace_id=row.workspace_id,
            record_id=row.id,
            logical_key=row.logical_key,
            revision=row.revision,
            supersedes_record_id=row.supersedes_id,
            checksum=row.checksum,
        ),
    )


def _persisted_market_bar_event_id(bar: OHLCVBar, *, workspace_id: UUID) -> str:
    """Trả ID bản ghi nguồn để PostgreSQL xác minh rights lineage trực tiếp."""
    identity = bar.source_identity
    if (
        type(identity) is not MarketBarSourceIdentity
        or identity.workspace_id != workspace_id
        or identity.checksum != bar.checksum
    ):
        raise ValueError("Signal market bar thiếu identity DB chính xác trong workspace.")
    return str(identity.record_id)


def _quote_volumes(bars: tuple[OHLCVBar, ...]) -> tuple[float, ...] | None:
    """Giữ lại helper tương thích; phép tính dùng chung nằm trong snapshot builder."""
    return quote_volumes_for_bars(bars)


def _validate_as_of(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of phải có múi giờ.")
