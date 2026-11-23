"""Dịch vụ thu thập metric freshness/coverage/lag/latency và cảnh báo WP-07E từ DB (CRY-376)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.entities import StrategyRelease, TelegramBinding
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.models.research_entities import SourcePolicyRevisionRecord
from trading_bot.adapters.database.models.telegram_entities import (
    TelegramSignalDelivery,
    TelegramSignalSubscription,
)
from trading_bot.adapters.database.models.worker_heartbeat_entities import WorkerHeartbeat
from trading_bot.observability.workbench_metrics import (
    BackfillProgressMetric,
    BarFreshnessMetric,
    DatasetRightsInput,
    StrategyPublishInput,
    TelegramBindingHealthInput,
    TelegramSubscriptionHealthInput,
    WorkbenchMetricsReport,
    evaluate_bar_freshness,
    evaluate_workbench_metrics_and_alerts,
)

_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}


def _bar_staleness_threshold_seconds(timeframe: str) -> int:
    """Ngưỡng coi nến bị cũ: 2 chu kỳ nến (tối thiểu 180 giây)."""
    tf_sec = _TIMEFRAME_SECONDS.get(timeframe, 3600)
    return max(180, tf_sec * 2)


async def collect_workbench_metrics_alerts(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    now: datetime | None = None,
) -> WorkbenchMetricsReport:
    """Thu thập số liệu freshness, coverage, lag, delivery latency và cảnh báo WP-07E."""
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    window_start = current - timedelta(hours=24)

    # 1. Freshness & coverage nến trong 24h gần nhất theo (symbol, timeframe)
    bar_rows = (
        await session.execute(
            select(
                MarketBar.symbol,
                MarketBar.timeframe,
                func.max(MarketBar.close_time).label("latest_close"),
                func.count(MarketBar.id).label("actual_bars"),
            )
            .where(MarketBar.workspace_id == workspace_id)
            .where(MarketBar.close_time >= window_start)
            .group_by(MarketBar.symbol, MarketBar.timeframe)
            .order_by(MarketBar.symbol, MarketBar.timeframe)
        )
    ).all()

    bar_metrics: list[BarFreshnessMetric] = []
    for bar_row in bar_rows:
        tf_sec = _TIMEFRAME_SECONDS.get(str(bar_row.timeframe), 3600)
        expected_bars = max(1, int(86400 // tf_sec))
        actual_bars = int(bar_row.actual_bars or 0)
        gap_count = max(0, expected_bars - actual_bars)
        bar_metrics.append(
            evaluate_bar_freshness(
                symbol=str(bar_row.symbol),
                timeframe=str(bar_row.timeframe),
                latest_bar_close_at=bar_row.latest_close,
                expected_bars=expected_bars,
                actual_bars=actual_bars,
                gap_count=gap_count,
                max_age_seconds=_bar_staleness_threshold_seconds(str(bar_row.timeframe)),
                now=current,
            )
        )

    # 2. Scanner lag từ WorkerHeartbeat
    heartbeat_rows = (
        await session.scalars(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.workspace_id == workspace_id)
            .order_by(WorkerHeartbeat.heartbeat_at.desc())
        )
    ).all()
    scanner_hb = next(
        (hb for hb in heartbeat_rows if "scanner" in hb.component or "signal" in hb.component),
        heartbeat_rows[0] if heartbeat_rows else None,
    )
    last_scan_at = scanner_hb.last_tick_at if scanner_hb else None
    scanner_trace_id = str(scanner_hb.instance_id) if scanner_hb else None

    # 3. Candidate -> Telegram delivery latency & retry/dead-letter trong 24h
    delivery_rows = (
        await session.execute(
            select(
                TelegramSignalDelivery.id,
                TelegramSignalDelivery.status,
                TelegramSignalDelivery.attempts,
                TelegramSignalDelivery.created_at,
                TelegramSignalDelivery.sent_at,
            )
            .where(TelegramSignalDelivery.workspace_id == workspace_id)
            .where(TelegramSignalDelivery.created_at >= window_start)
            .order_by(TelegramSignalDelivery.created_at.desc())
            .limit(500)
        )
    ).all()
    latencies: list[float] = []
    retry_count = 0
    dead_letter_count = 0
    dead_outbox_id: str | None = None
    for row in delivery_rows:
        attempts = int(row.attempts or 0)
        if attempts > 1:
            retry_count += attempts - 1
        if str(row.status) == "DEAD":
            dead_letter_count += 1
            if dead_outbox_id is None:
                dead_outbox_id = str(row.id)
        if str(row.status) == "SENT" and row.sent_at is not None and row.created_at is not None:
            latencies.append(max(0.0, (row.sent_at - row.created_at).total_seconds()))

    # 4. Strategy release chưa publish
    bots = (
        await session.scalars(select(BotInstance).where(BotInstance.workspace_id == workspace_id))
    ).all()
    releases = (
        await session.scalars(
            select(StrategyRelease).where(StrategyRelease.workspace_id == workspace_id)
        )
    ).all()
    release_by_id = {rel.id: rel for rel in releases}
    strategy_inputs: list[StrategyPublishInput] = []
    for bot in bots:
        rel_id = getattr(bot, "active_release_id", None)
        if rel_id is not None:
            rel = release_by_id.get(rel_id)
            if rel is None:
                strategy_inputs.append(
                    StrategyPublishInput(
                        bot_name=bot.name,
                        release_id=str(rel_id),
                        status="MISSING",
                        trace_ref=str(bot.id),
                    )
                )
            elif rel.status != "PUBLISHED":
                strategy_inputs.append(
                    StrategyPublishInput(
                        bot_name=bot.name,
                        release_id=rel.name,
                        status=rel.status,
                        trace_ref=str(rel.id),
                    )
                )

    # 5. Telegram bindings & subscriptions
    bindings = (
        await session.scalars(
            select(TelegramBinding).where(TelegramBinding.workspace_id == workspace_id)
        )
    ).all()
    active_binding_ids = {b.id for b in bindings if b.revoked_at is None}
    binding_inputs = [
        TelegramBindingHealthInput(
            binding_id=str(b.id),
            revoked_at=b.revoked_at,
        )
        for b in bindings
        if b.revoked_at is not None
    ]

    bot_name_by_id = {b.id: b.name for b in bots}
    subs = (
        await session.scalars(
            select(TelegramSignalSubscription).where(
                TelegramSignalSubscription.workspace_id == workspace_id
            )
        )
    ).all()
    sub_inputs = [
        TelegramSubscriptionHealthInput(
            subscription_id=str(sub.id),
            bot_name=bot_name_by_id.get(sub.bot_id, str(sub.bot_id)),
            enabled=bool(sub.enabled),
            binding_active=sub.telegram_binding_id in active_binding_ids,
        )
        for sub in subs
    ]

    # 6. Quyền dataset / source policy revision hết hạn hoặc bị thu hồi
    policies = (
        await session.scalars(
            select(SourcePolicyRevisionRecord).where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id
            )
        )
    ).all()
    rights_inputs = [
        DatasetRightsInput(
            grant_id=str(p.id),
            source_code=str(p.source_id),
            status=str(p.state),
            expires_at=p.valid_until,
            manifest_id=str(p.policy_id),
        )
        for p in policies
    ]

    # 7. Backfill progress (đọc từ counters của worker heartbeat backfill nếu có)
    backfill_metrics: list[BackfillProgressMetric] = []
    for hb in heartbeat_rows:
        if "backfill" in hb.component:
            counters = hb.counters or {}
            processed = int(counters.get("processed_bars", 0) or 0)
            total = int(counters.get("total_bars", 0) or 0)
            ratio = (processed / total) if total > 0 else 0.0
            backfill_metrics.append(
                BackfillProgressMetric(
                    job_id=str(hb.instance_id),
                    manifest_id=(
                        str(counters["manifest_id"]) if counters.get("manifest_id") else None
                    ),
                    processed_bars=processed,
                    total_bars=total,
                    progress_ratio=min(1.0, max(0.0, ratio)),
                    status=str(hb.state).upper(),
                    archive_checksum=(
                        str(counters["archive_checksum"])
                        if counters.get("archive_checksum")
                        else None
                    ),
                )
            )

    return evaluate_workbench_metrics_and_alerts(
        now=current,
        bar_freshness=bar_metrics,
        last_scan_at=last_scan_at,
        scanner_trace_run_id=scanner_trace_id,
        delivery_latencies_seconds=latencies,
        delivery_retry_count=retry_count,
        delivery_dead_letter_count=dead_letter_count,
        delivery_trace_outbox_id=dead_outbox_id,
        backfill_progress=backfill_metrics,
        strategy_statuses=strategy_inputs,
        telegram_bindings=binding_inputs,
        telegram_subscriptions=sub_inputs,
        dataset_rights=rights_inputs,
    )


__all__ = ["collect_workbench_metrics_alerts"]
