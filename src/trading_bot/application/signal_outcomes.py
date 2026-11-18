"""Chấm định kỳ kết quả tín hiệu (CRY-430) và ghi ``signal_outcome`` append-only.

Chạy trong tiến trình ``market-signal-worker`` (role ``crypidian_market_signal_worker``): role này
đã đọc ``bot_decision``/``feature_snapshot``/``market_bar`` theo RLS và chỉ được thêm quyền
SELECT/INSERT bảng kết quả. Không đọc ledger PAPER, không gọi sàn, không đặt lệnh.

Idempotent: khóa duy nhất (workspace, decision, method_version, horizon) + ``ON CONFLICT DO
NOTHING``; horizon đã có hàng thì không chấm lại. Point-in-time: chỉ dùng nến đã đóng
(``close_time``/``available_at`` ≤ thời điểm chấm) và chỉ ghi khi đủ nến liền mạch tới hết horizon.
Nến bị hở quá ``gap_grace`` sau hạn horizon → hàng ``DATA_GAP`` để không quét lại mãi.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    BotDecisionRecord,
    BotInstance,
    FeatureSnapshotRecord,
    SignalOutcomeRecord,
)
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.application.bot_dispatcher import BotDecision
from trading_bot.application.market_signal_composition import (
    SIGNAL_BAR_SOURCES,
    SIGNAL_LIVE_BAR_SOURCE,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.strategy_market_requirements import (
    TIMEFRAME_INTERVALS,
    entry_timeframe_for_release,
)
from trading_bot.domain.strategy.signal_outcome import (
    HORIZON_MULTIPLES,
    LABEL_VERSION,
    METHOD_VERSION,
    ForwardBar,
    OutcomeStatus,
    SignalLevels,
    SignalOutcome,
    SignalOutcomeError,
    contiguous_window,
    levels_from_candidate,
    reference_matches,
    score_signal,
    signal_bar,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignalOutcomeTickResult:
    bots: int = 0
    decisions_examined: int = 0
    scored: int = 0
    data_gap: int = 0
    invalid: int = 0
    pending: int = 0


@dataclass(frozen=True, slots=True)
class _Signal:
    decision_id: UUID
    bot_id: UUID
    symbol: str
    timeframe: str
    direction: str
    strategy_id: str
    strategy_release_id: str
    signal_at: datetime
    candidate: dict[str, Any]


class SignalOutcomeScorer:
    """Một lượt: với mỗi bot, chấm các tín hiệu trong ``lookback`` còn thiếu horizon."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        lookback: timedelta = timedelta(days=14),
        batch_limit: int = 1000,
        gap_grace: timedelta = timedelta(hours=6),
        horizons: Sequence[int] = HORIZON_MULTIPLES,
    ) -> None:
        if lookback <= timedelta(0) or not 1 <= batch_limit <= 2000:
            raise ValueError("lookback phải dương và batch_limit trong 1–2000.")
        if not horizons or any(h < 1 for h in horizons):
            raise ValueError("horizons phải là các số nguyên ≥ 1.")
        self._sessions = sessions
        self._lookback = lookback
        self._batch_limit = batch_limit
        self._gap_grace = gap_grace
        self._horizons = tuple(sorted(set(horizons)))

    async def score_once(self, *, workspace_id: UUID, now: datetime) -> SignalOutcomeTickResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        now = now.astimezone(UTC)
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            bot_ids = list(
                (
                    await uow.session.scalars(
                        select(BotInstance.id)
                        .where(BotInstance.workspace_id == workspace_id)
                        .order_by(BotInstance.id)
                    )
                ).all()
            )
        totals = {"examined": 0, "scored": 0, "gap": 0, "invalid": 0, "pending": 0}
        for bot_id in bot_ids:
            async with UnitOfWork(self._sessions, workspace_id=workspace_id, bot_id=bot_id) as uow:
                await self._score_bot(uow.session, workspace_id, bot_id, now, totals)
        return SignalOutcomeTickResult(
            bots=len(bot_ids),
            decisions_examined=totals["examined"],
            scored=totals["scored"],
            data_gap=totals["gap"],
            invalid=totals["invalid"],
            pending=totals["pending"],
        )

    async def _score_bot(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        bot_id: UUID,
        now: datetime,
        totals: dict[str, int],
    ) -> None:
        done_count = (
            select(func.count())
            .where(
                SignalOutcomeRecord.workspace_id == BotDecisionRecord.workspace_id,
                SignalOutcomeRecord.decision_id == BotDecisionRecord.id,
                SignalOutcomeRecord.method_version == METHOD_VERSION,
            )
            .correlate(BotDecisionRecord)
            .scalar_subquery()
        )
        rows = (
            await session.execute(
                select(
                    BotDecisionRecord.id,
                    BotDecisionRecord.workspace_id,
                    BotDecisionRecord.bot_id,
                    BotDecisionRecord.status,
                    BotDecisionRecord.reason_code,
                    BotDecisionRecord.decision_hash,
                    BotDecisionRecord.payload,
                )
                .where(
                    BotDecisionRecord.workspace_id == workspace_id,
                    BotDecisionRecord.bot_id == bot_id,
                    BotDecisionRecord.status == "CANDIDATE",
                    BotDecisionRecord.created_at >= now - self._lookback,
                    done_count < len(self._horizons),
                )
                .order_by(BotDecisionRecord.created_at, BotDecisionRecord.id)
                .limit(self._batch_limit)
            )
        ).all()
        existing: dict[UUID, set[int]] = defaultdict(set)
        if rows:
            done = await session.execute(
                select(SignalOutcomeRecord.decision_id, SignalOutcomeRecord.horizon_bars).where(
                    SignalOutcomeRecord.workspace_id == workspace_id,
                    SignalOutcomeRecord.decision_id.in_([row.id for row in rows]),
                    SignalOutcomeRecord.method_version == METHOD_VERSION,
                )
            )
            for decision_id, horizon in done.all():
                existing[decision_id].add(int(horizon))
        for row in rows:
            totals["examined"] += 1
            signal = await self._signal(session, row)
            if signal is None:
                continue
            missing = [h for h in self._horizons if h not in existing[signal.decision_id]]
            interval = TIMEFRAME_INTERVALS[signal.timeframe]
            if not any(signal.signal_at + interval * h <= now for h in missing):
                # Chưa horizon nào tới hạn: không truy vấn nến (tránh tốn DB mỗi lượt).
                totals["pending"] += len(missing)
                continue
            await self._score_signal(session, workspace_id, signal, missing, now, totals)

    async def _signal(self, session: AsyncSession, row: Any) -> _Signal | None:
        """Quyết định đã xác minh hash; dữ liệu lệch thì bỏ qua (không chấm thứ không tin được)."""
        try:
            decision = BotDecision.from_payload(row.payload, row.decision_hash)
            if (
                decision.decision_id != row.id
                or decision.bot_id != row.bot_id
                or decision.workspace_id != row.workspace_id
                or decision.candidate_json is None
            ):
                raise ValueError("Cột vật lý không khớp payload quyết định.")
            candidate = json.loads(decision.candidate_json)
            # Release dựng sẵn có khung cố định; release lạ (Strategy Card) đọc khung của feature
            # snapshot mà tín hiệu dùng — cùng nguồn với thẻ Telegram.
            timeframe = entry_timeframe_for_release(
                str(candidate["strategy_release_id"])
            ) or await session.scalar(
                select(FeatureSnapshotRecord.timeframe).where(
                    FeatureSnapshotRecord.workspace_id == decision.workspace_id,
                    FeatureSnapshotRecord.snapshot_id == decision.feature_snapshot_id,
                )
            )
            if timeframe not in TIMEFRAME_INTERVALS:
                raise ValueError("Khung tín hiệu không được hỗ trợ.")
            return _Signal(
                decision_id=decision.decision_id,
                bot_id=decision.bot_id,
                symbol=str(candidate["symbol"]),
                timeframe=str(timeframe),
                direction=str(candidate["direction"]),
                strategy_id=str(candidate["strategy_id"]),
                strategy_release_id=str(candidate["strategy_release_id"]),
                signal_at=datetime.fromisoformat(
                    str(candidate["signal_at"]).replace("Z", "+00:00")
                ).astimezone(UTC),
                candidate=candidate,
            )
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(
                "Bỏ qua tín hiệu không xác minh được; decision=%s code=%s",
                row.id,
                type(error).__name__,
            )
            return None

    async def _score_signal(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        signal: _Signal,
        missing: Sequence[int],
        now: datetime,
        totals: dict[str, int],
    ) -> None:
        if not missing:
            return
        try:
            levels: SignalLevels | None = levels_from_candidate(signal.candidate)
        except SignalOutcomeError:
            levels = None
        if levels is None or signal.direction not in ("long", "short"):
            for horizon in missing:
                await _insert(session, workspace_id, signal, horizon, now, None, None,
                              OutcomeStatus.INVALID_SIGNAL, "SIGNAL_LEVELS_INVALID")  # fmt: skip
                totals["invalid"] += 1
            return
        interval = TIMEFRAME_INTERVALS[signal.timeframe]
        # Nạp cả nến tín hiệu (mở trong khung ngay trước ``signal_at``) để kiểm giá tham chiếu.
        bars = await load_forward_bars(
            session,
            workspace_id=workspace_id,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            after=signal.signal_at - interval,
            until=signal.signal_at + interval * max(missing),
            as_of=now,
        )
        origin = signal_bar(bars, signal_at=signal.signal_at, interval=interval)
        if origin is not None and not reference_matches(levels.entry, origin.close):
            for horizon in missing:
                await _insert(session, workspace_id, signal, horizon, now, levels, None,
                              OutcomeStatus.INVALID_SIGNAL, "REFERENCE_PRICE_MISMATCH")  # fmt: skip
                totals["invalid"] += 1
            return
        for horizon in missing:
            window = contiguous_window(
                bars, signal_at=signal.signal_at, interval=interval, horizon_bars=horizon
            )
            if window is None:
                if now >= signal.signal_at + interval * horizon + self._gap_grace:
                    await _insert(session, workspace_id, signal, horizon, now, levels, None,
                                  OutcomeStatus.DATA_GAP, "FORWARD_BARS_INCOMPLETE")  # fmt: skip
                    totals["gap"] += 1
                else:
                    totals["pending"] += 1
                continue
            outcome = score_signal(levels, signal_at=signal.signal_at, window=window)
            await _insert(session, workspace_id, signal, horizon, now, levels, outcome,
                          OutcomeStatus.SCORED, None)  # fmt: skip
            totals["scored"] += 1


async def load_forward_bars(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    symbol: str,
    timeframe: str,
    after: datetime,
    until: datetime,
    as_of: datetime,
) -> tuple[ForwardBar, ...]:
    """Nến đã đóng mở trong ``(after, until]`` và khả dụng tại ``as_of``; live ưu tiên backfill."""
    filters = (
        MarketBar.workspace_id == workspace_id,
        MarketBar.symbol == symbol,
        MarketBar.timeframe == timeframe,
        MarketBar.source.in_(SIGNAL_BAR_SOURCES),
        MarketBar.is_closed.is_(True),
        MarketBar.open_time > after,
        MarketBar.open_time <= until,
        MarketBar.close_time <= as_of,
        MarketBar.available_at <= as_of,
    )
    rows = await session.scalars(
        select(MarketBar)
        .where(*filters)
        .ext(distinct_on(MarketBar.open_time))
        .order_by(
            MarketBar.open_time,
            case((MarketBar.source == SIGNAL_LIVE_BAR_SOURCE, 0), else_=1).asc(),
            MarketBar.available_at.desc(),
            MarketBar.revision.desc(),
            MarketBar.id.desc(),
        )
    )
    return tuple(
        ForwardBar(
            open_time=row.open_time,
            close_time=row.close_time,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            ref=f"{row.id}:{row.checksum}",
            source=row.source,
        )
        for row in rows.all()
    )


async def _insert(
    session: AsyncSession,
    workspace_id: UUID,
    signal: _Signal,
    horizon: int,
    now: datetime,
    levels: SignalLevels | None,
    outcome: SignalOutcome | None,
    status: OutcomeStatus,
    detail_code: str | None,
) -> None:
    values: dict[str, Any] = {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "bot_id": signal.bot_id,
        "decision_id": signal.decision_id,
        "method_version": METHOD_VERSION,
        "label_version": LABEL_VERSION,
        "horizon_bars": horizon,
        "status": status.value,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "direction": signal.direction if signal.direction in ("long", "short") else "long",
        "strategy_id": signal.strategy_id[:80],
        "strategy_release_id": signal.strategy_release_id[:80],
        "signal_at": signal.signal_at,
        "detail_code": detail_code,
        "scored_at": now,
    }
    if levels is not None:
        values |= {
            "entry_price": levels.entry,
            "stop_price": levels.stop,
            "tp1_price": levels.tp1,
            "tp2_price": levels.tp2,
        }
    if outcome is not None:
        values |= {
            "horizon_end_at": outcome.horizon_end_at,
            "first_touch": outcome.first_touch.value,
            "same_bar_conflict": outcome.same_bar_conflict,
            "tp1_hit": outcome.tp1_hit,
            "tp2_hit": outcome.tp2_hit,
            "invalidated": outcome.invalidated,
            "bars_to_first_touch": outcome.bars_to_first_touch,
            "seconds_to_first_touch": outcome.seconds_to_first_touch,
            "mfe_r": outcome.mfe_r,
            "mae_r": outcome.mae_r,
            "close_r": outcome.close_r,
            "outcome_r": outcome.outcome_r,
            "label": outcome.label,
            "input_hash": outcome.input_hash,
            "bar_sources": list(outcome.bar_sources),
        }
    await session.execute(
        insert(SignalOutcomeRecord)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_signal_outcome_decision_horizon")
    )


__all__ = ["SignalOutcomeScorer", "SignalOutcomeTickResult", "load_forward_bars"]
