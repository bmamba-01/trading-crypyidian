"""Đưa nến đã đóng qua shared-evidence và BotDecision, không có đường thực thi lệnh."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from trading_bot.application.bot_dispatcher import BotDecision, BotDecisionContext
from trading_bot.application.strategy_market_requirements import entry_timeframe_for_release
from trading_bot.domain.market.feature_snapshot import DataFreshness, FeatureSnapshot
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.market.signal_evidence import (
    EvidenceSourceRef,
    SharedSignalEvidence,
    SignalEvidenceError,
    build_shared_signal_evidence,
)


class MarketSignalScanError(ValueError):
    """Input scanner sai scope, point-in-time hoặc quality contract."""


@dataclass(frozen=True, slots=True)
class MarketSignalObservation:
    """Một chuỗi nến đã đóng cùng feature snapshot được tính từ đúng chuỗi đó."""

    workspace_id: UUID
    bars: tuple[OHLCVBar, ...]
    feature_snapshot: FeatureSnapshot
    setup_type: str = "market.bar.closed"

    def __post_init__(self) -> None:
        bars = tuple(self.bars)
        if not isinstance(self.workspace_id, UUID) or not bars:
            raise MarketSignalScanError("Quan sát cần workspace UUID và ít nhất một nến.")
        if any(not bar.is_closed for bar in bars):
            raise MarketSignalScanError("Scanner chỉ nhận nến đã đóng.")
        if any(
            (left.symbol, left.timeframe) != (right.symbol, right.timeframe)
            for left, right in zip(bars, bars[1:], strict=False)
        ):
            raise MarketSignalScanError("Một quan sát chỉ được chứa cùng symbol/timeframe.")
        if any(
            left.open_time >= right.open_time for left, right in zip(bars, bars[1:], strict=False)
        ):
            raise MarketSignalScanError("Nến đầu vào phải tăng nghiêm ngặt theo thời gian.")
        feature = self.feature_snapshot
        latest = bars[-1]
        if (feature.workspace_id, feature.symbol, feature.timeframe) != (
            self.workspace_id,
            latest.symbol,
            latest.timeframe,
        ):
            raise MarketSignalScanError("Feature snapshot không cùng workspace/symbol/timeframe.")
        if len(feature.input_bar_ids) != len(bars) or len(feature.input_event_ids) != len(bars):
            raise MarketSignalScanError("Feature snapshot không khớp số lượng nến nguồn.")
        if any(not event_id.strip() for event_id in feature.input_event_ids) or len(
            set(feature.input_event_ids)
        ) != len(feature.input_event_ids):
            raise MarketSignalScanError(
                "Feature snapshot cần event ID nguồn không rỗng và duy nhất."
            )
        if feature.input_bar_ids != tuple(_bar_id(bar) for bar in bars):
            raise MarketSignalScanError("Feature snapshot không được tính từ đúng nến đầu vào.")
        if feature.as_of < max(bar.available_at for bar in bars):
            raise MarketSignalScanError("Feature snapshot as_of sớm hơn dữ liệu nến đã biết.")
        if not self.setup_type.strip():
            raise MarketSignalScanError("setup_type không được để trống.")
        object.__setattr__(self, "bars", bars)

    @property
    def observed_at(self) -> datetime:
        """Thời điểm dữ liệu đủ khả dụng để hình thành quan sát, theo feature snapshot."""
        return self.feature_snapshot.as_of

    @property
    def available_at(self) -> datetime:
        """Thời điểm muộn nhất mà nến nguồn và feature snapshot đã khả dụng."""
        return max(
            self.feature_snapshot.computed_at,
            *(bar.available_at for bar in self.bars),
        )

    def source_refs(self) -> tuple[EvidenceSourceRef, ...]:
        """Tạo provenance ổn định từ event ID đã được feature snapshot ghim."""
        return tuple(
            EvidenceSourceRef(
                ref=f"market-bar-event:{event_id}",
                available_at=bar.available_at,
            )
            for event_id, bar in zip(self.feature_snapshot.input_event_ids, self.bars, strict=True)
        )


class QualifiedMarketObservationSource(Protocol):
    """Trả các quan sát đã đóng; implementation chịu trách nhiệm dựng bars/features."""

    async def qualified_closed_observations(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
        limit: int,
        symbols: Sequence[str],
        timeframes: Sequence[str],
    ) -> Sequence[MarketSignalObservation]: ...


class SignalBotContextProvider(Protocol):
    """Lấy cấu hình chiến lược đã publish và còn hiệu lực tại mốc đánh giá."""

    async def contexts_for(
        self, *, workspace_id: UUID, as_of: datetime
    ) -> Sequence[BotDecisionContext]: ...


class SignalEvidenceDispatcher(Protocol):
    """Ghi shared evidence và đánh giá bot; interface không có planner hay order port."""

    async def dispatch_evidence(
        self,
        evidence: SharedSignalEvidence,
        bots: Sequence[BotDecisionContext],
        as_of: datetime,
    ) -> Sequence[BotDecision]: ...


@dataclass(frozen=True, slots=True)
class MarketSignalScanResult:
    """Tổng kết một tick để readiness/metrics có thể báo phần bị bỏ qua."""

    dispatched_evidence_ids: tuple[UUID, ...]
    decision_count: int
    stale_count: int
    unusable_count: int
    expired_count: int
    no_context_count: int
    duplicate_count: int

    @property
    def dispatched_count(self) -> int:
        return len(self.dispatched_evidence_ids)


class MarketSignalScanner:
    """Tạo shared evidence từ market observation rồi gọi dispatcher thuần decision.

    Scanner không tải dữ liệu, mở session DB, tạo risk plan hoặc gửi order. Composition layer
    phải cung cấp source, bot context và dispatcher có quyền tối thiểu phù hợp.
    """

    def __init__(
        self,
        source: QualifiedMarketObservationSource,
        contexts: SignalBotContextProvider,
        dispatcher: SignalEvidenceDispatcher,
        *,
        max_observation_age: timedelta,
        signal_validity: timedelta,
        batch_limit: int = 100,
    ) -> None:
        if max_observation_age <= timedelta(0) or signal_validity <= timedelta(0):
            raise ValueError("Ngưỡng freshness và signal validity phải dương.")
        if not 1 <= batch_limit <= 1000:
            raise ValueError("batch_limit phải nằm trong khoảng 1–1000.")
        self._source = source
        self._contexts = contexts
        self._dispatcher = dispatcher
        self._max_observation_age = max_observation_age
        self._signal_validity = signal_validity
        self._batch_limit = batch_limit

    async def scan_once(
        self,
        *,
        workspace_id: UUID,
        now: datetime,
        limit: int | None = None,
    ) -> MarketSignalScanResult:
        """Đánh giá batch point-in-time; dữ liệu tương lai luôn làm tick fail-closed."""
        _require_aware(now, "now")
        now = now.astimezone(UTC)
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        batch_limit = self._batch_limit if limit is None else limit
        if (
            not isinstance(batch_limit, int)
            or isinstance(batch_limit, bool)
            or not (1 <= batch_limit <= self._batch_limit)
        ):
            raise ValueError(f"limit phải nằm trong khoảng 1–{self._batch_limit}.")
        contexts = tuple(await self._contexts.contexts_for(workspace_id=workspace_id, as_of=now))
        if any(context.workspace_id != workspace_id for context in contexts):
            raise MarketSignalScanError("Bot context ngoài workspace được yêu cầu.")

        # Chỉ rà symbol và entry timeframe mà strategy release hỗ trợ; nến xác nhận HTF
        # được tải riêng bởi DecisionSnapshotProvider, không sinh evidence lặp cho bot.
        contexts_by_timeframe: dict[str, tuple[BotDecisionContext, ...]] = {}
        for timeframe in sorted(
            {
                timeframe
                for context in contexts
                if (timeframe := _entry_timeframe(context.strategy_release_id)) is not None
            }
        ):
            contexts_by_timeframe[timeframe] = tuple(
                context
                for context in contexts
                if _entry_timeframe(context.strategy_release_id) == timeframe
            )
        if not contexts_by_timeframe:
            return MarketSignalScanResult((), 0, 0, 0, 0, 1, 0)
        symbols = tuple(sorted({symbol for context in contexts for symbol in context.symbols}))
        timeframes = tuple(sorted(contexts_by_timeframe))
        observations = await self._source.qualified_closed_observations(
            workspace_id=workspace_id,
            as_of=now,
            limit=batch_limit,
            symbols=symbols,
            timeframes=timeframes,
        )

        dispatched: list[UUID] = []
        seen: set[UUID] = set()
        decision_count = 0
        stale_count = 0
        unusable_count = 0
        expired_count = 0
        no_context_count = 0
        duplicate_count = 0

        for observation in observations:
            if observation.workspace_id != workspace_id:
                raise MarketSignalScanError("Source trả observation ngoài workspace được yêu cầu.")
            feature = observation.feature_snapshot
            if observation.available_at > now or observation.observed_at > now:
                raise MarketSignalScanError("Observation chứa dữ liệu khả dụng trong tương lai.")
            if feature.computed_at > now:
                raise MarketSignalScanError("Feature snapshot được tính trong tương lai.")
            if (
                not feature.is_usable
                or not feature.warmup_complete
                or feature.data_freshness is not DataFreshness.FRESH
                or feature.data_coverage < 1.0
            ):
                unusable_count += 1
                continue
            if now - observation.observed_at > self._max_observation_age:
                stale_count += 1
                continue

            available_at = observation.available_at
            evidence = build_shared_signal_evidence(
                feature_snapshot=feature,
                setup_type=observation.setup_type,
                observed_at=observation.observed_at,
                available_at=available_at,
                validity_end=available_at + self._signal_validity,
                sources=observation.source_refs(),
            )
            try:
                evidence.require_usable_at(now)
            except SignalEvidenceError as error:
                if error.code != "EVIDENCE_EXPIRED":
                    raise
                expired_count += 1
                continue
            if evidence.evidence_id in seen:
                duplicate_count += 1
                continue
            seen.add(evidence.evidence_id)

            matching_contexts = tuple(
                context
                for context in contexts_by_timeframe.get(feature.timeframe, ())
                if observation.bars[-1].symbol in context.symbols
            )
            if not matching_contexts:
                no_context_count += 1
                continue
            decisions = await self._dispatcher.dispatch_evidence(evidence, matching_contexts, now)
            dispatched.append(evidence.evidence_id)
            decision_count += len(decisions)

        return MarketSignalScanResult(
            dispatched_evidence_ids=tuple(dispatched),
            decision_count=decision_count,
            stale_count=stale_count,
            unusable_count=unusable_count,
            expired_count=expired_count,
            no_context_count=no_context_count,
            duplicate_count=duplicate_count,
        )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _bar_id(bar: OHLCVBar) -> str:
    """Tạo định danh nến cùng contract với FeatureCalculator."""
    payload = {
        "open_time": bar.open_time.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_timeframe(strategy_release_id: str) -> str | None:
    """Entry timeframe của built-in release; release lạ không được tự động quét."""
    return entry_timeframe_for_release(strategy_release_id)
