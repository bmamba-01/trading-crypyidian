"""Lưu lineage metadata giữa quyết định bot và các nến nguồn bất biến."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_source_bar_entities import (
    BotDecisionSourceBarRecord,
)
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.application.backtest_dataset_manifest import MarketBarIdentityMetadata
from trading_bot.application.bot_dispatcher import BotDecision
from trading_bot.application.decision_snapshot_builder import (
    DecisionSnapshotBuildError,
    canonical_market_bar_metadata_event_id,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    DecisionSnapshotTimeframeLineage,
)

_SOURCE_BAR_ID_PREFIX = "market-bar-record:"

_MARKET_BAR_METADATA_COLUMNS = (
    MarketBar.id.label("id"),
    MarketBar.workspace_id.label("workspace_id"),
    MarketBar.logical_key.label("logical_key"),
    MarketBar.revision.label("revision"),
    MarketBar.supersedes_id.label("supersedes_id"),
    MarketBar.checksum.label("checksum"),
    MarketBar.raw_reference_id.label("raw_reference_id"),
    MarketBar.source.label("source"),
    MarketBar.symbol.label("symbol"),
    MarketBar.timeframe.label("timeframe"),
    MarketBar.open_time.label("open_time"),
    MarketBar.close_time.label("close_time"),
    MarketBar.available_at.label("available_at"),
    MarketBar.is_closed.label("is_closed"),
)


class BotDecisionSourceBarConflict(RuntimeError):
    """Từ chối lineage thiếu, không khớp snapshot hoặc không khớp MarketBar đã lưu."""


async def append_decision_source_bars(
    session: AsyncSession,
    decision: BotDecision,
    snapshot: DecisionSnapshot,
) -> tuple[BotDecisionSourceBarRecord, ...]:
    """Ghi từng bar identity theo đúng thứ tự timeframe trong transaction hiện hành.

    Hàm chỉ lưu ID, event ID và digest; không sao chép giá, volume hoặc payload OHLCV.
    """
    if (
        type(decision.workspace_id) is not UUID
        or type(decision.bot_id) is not UUID
        or type(decision.decision_id) is not UUID
        or not isinstance(decision.decision_snapshot_id, str)
        or decision.decision_snapshot_id != snapshot.snapshot_id
    ):
        raise BotDecisionSourceBarConflict(
            "Decision snapshot không khớp DecisionSnapshot đã đánh giá."
        )

    lineage_items = snapshot.timeframe_lineage
    series_items = snapshot.series
    timeframe_bar_counts = {item.timeframe: len(item.bars) for item in series_items}
    if (
        not lineage_items
        or not series_items
        or len(timeframe_bar_counts) != len(series_items)
        or tuple(item.timeframe for item in lineage_items)
        != tuple(item.timeframe for item in series_items)
    ):
        raise BotDecisionSourceBarConflict(
            "DecisionSnapshot thiếu timeframe lineage đầy đủ hoặc không đúng thứ tự."
        )

    if not _is_sha256(snapshot.snapshot_id):
        raise BotDecisionSourceBarConflict("DecisionSnapshot ID không phải SHA-256 hợp lệ.")

    validated: list[tuple[DecisionSnapshotTimeframeLineage, OHLCVBar, int, UUID, str]] = []
    record_ids: list[UUID] = []
    seen_record_ids: set[UUID] = set()
    for lineage, series in zip(lineage_items, series_items, strict=True):
        bars = series.bars
        if (
            lineage.workspace_id != decision.workspace_id
            or lineage.symbol != snapshot.symbol
            or series.timeframe != lineage.timeframe
            or not _is_sha256(lineage.feature_snapshot_id)
            or lineage.source_identity_pinned is not True
            or not bars
            or len(lineage.source_event_ids) != len(bars)
            or len(lineage.source_bar_ids) != len(bars)
            or _source_event_digest(
                lineage.timeframe, lineage.source_event_ids, lineage.source_bar_ids
            )
            != lineage.source_event_digest
        ):
            raise BotDecisionSourceBarConflict(
                "DecisionSnapshot thiếu lineage nguồn authoritative đầy đủ."
            )

        timeframe_ids: set[UUID] = set()
        timeframe_events: set[str] = set()
        for ordinal, (bar, source_bar_id, source_event_id) in enumerate(
            zip(
                bars,
                lineage.source_bar_ids,
                lineage.source_event_ids,
                strict=True,
            )
        ):
            identity = bar.source_identity
            record_id = _record_id(source_bar_id)
            if (
                type(identity) is not MarketBarSourceIdentity
                or identity.workspace_id != decision.workspace_id
                or identity.record_id != record_id
                or bar.symbol != snapshot.symbol
                or bar.timeframe != lineage.timeframe
                or not bar.is_closed
                or bar.close_time > snapshot.decision_time
                or bar.event_at > snapshot.decision_time
                or bar.available_at > snapshot.decision_time
                or record_id in seen_record_ids
            ):
                raise BotDecisionSourceBarConflict(
                    "Bar trong snapshot không khớp source ID hoặc mốc quyết định."
                )
            try:
                observed_event_id = canonical_market_bar_metadata_event_id(
                    MarketBarIdentityMetadata.from_bar(bar),
                    workspace_id=decision.workspace_id,
                )
            except (DecisionSnapshotBuildError, ValueError) as error:
                raise BotDecisionSourceBarConflict(
                    "Bar trong snapshot thiếu identity authoritative hợp lệ."
                ) from error
            if source_event_id != observed_event_id:
                raise BotDecisionSourceBarConflict(
                    "Source event ID không khớp identity của bar trong snapshot."
                )
            if record_id in timeframe_ids or source_event_id in timeframe_events:
                raise BotDecisionSourceBarConflict(
                    "Lineage timeframe lặp MarketBar hoặc source event ID."
                )
            timeframe_ids.add(record_id)
            timeframe_events.add(source_event_id)
            seen_record_ids.add(record_id)
            record_ids.append(record_id)
            validated.append((lineage, bar, ordinal, record_id, source_event_id))

    result = await session.execute(
        select(*_MARKET_BAR_METADATA_COLUMNS).where(
            MarketBar.workspace_id == decision.workspace_id,
            MarketBar.id.in_(record_ids),
        )
    )
    persisted_by_id: dict[UUID, Mapping[str, Any]] = {}
    for market_row in result.mappings().all():
        persisted_id = market_row.get("id")
        if type(persisted_id) is not UUID or persisted_id not in seen_record_ids:
            raise BotDecisionSourceBarConflict(
                "Cơ sở dữ liệu trả MarketBar ngoài tập lineage đã kiểm tra."
            )
        if persisted_id in persisted_by_id:
            raise BotDecisionSourceBarConflict("MarketBar metadata bị lặp trong kết quả truy vấn.")
        persisted_by_id[persisted_id] = cast(Mapping[str, Any], market_row)
    if set(persisted_by_id) != seen_record_ids:
        raise BotDecisionSourceBarConflict(
            "Thiếu MarketBar metadata authoritative trong cơ sở dữ liệu."
        )

    records: list[BotDecisionSourceBarRecord] = []
    for lineage, bar, ordinal, record_id, source_event_id in validated:
        persisted_row = persisted_by_id[record_id]
        try:
            persisted_metadata = _metadata_from_row(persisted_row)
            expected_metadata = MarketBarIdentityMetadata.from_bar(bar)
            persisted_event_id = canonical_market_bar_metadata_event_id(
                persisted_metadata,
                workspace_id=decision.workspace_id,
            )
        except (DecisionSnapshotBuildError, KeyError, TypeError, ValueError) as error:
            raise BotDecisionSourceBarConflict(
                "MarketBar metadata persisted không hợp lệ."
            ) from error
        if persisted_metadata != expected_metadata or persisted_event_id != source_event_id:
            raise BotDecisionSourceBarConflict(
                "MarketBar metadata/event đã lưu không khớp DecisionSnapshot."
            )
        records.append(
            BotDecisionSourceBarRecord(
                workspace_id=decision.workspace_id,
                bot_id=decision.bot_id,
                bot_decision_id=decision.decision_id,
                decision_snapshot_id=snapshot.snapshot_id,
                timeframe=lineage.timeframe,
                feature_snapshot_id=lineage.feature_snapshot_id,
                market_bar_id=record_id,
                source_event_id=source_event_id,
                source_event_digest=lineage.source_event_digest,
                ordinal=ordinal,
                timeframe_bar_count=timeframe_bar_counts[lineage.timeframe],
                snapshot_timeframe_count=len(snapshot.series),
            )
        )

    session.add_all(records)
    await session.flush()
    return tuple(records)


def _record_id(value: str) -> UUID:
    if not isinstance(value, str) or not value.startswith(_SOURCE_BAR_ID_PREFIX):
        raise BotDecisionSourceBarConflict(
            "Source bar ID không phải MarketBar record ID authoritative."
        )
    raw_uuid = value.removeprefix(_SOURCE_BAR_ID_PREFIX)
    try:
        parsed = UUID(raw_uuid)
    except (AttributeError, ValueError) as error:
        raise BotDecisionSourceBarConflict("Source bar ID không phải UUID hợp lệ.") from error
    if str(parsed) != raw_uuid:
        raise BotDecisionSourceBarConflict("Source bar UUID cần dạng canonical chữ thường.")
    return parsed


def _metadata_from_row(row: Mapping[str, object]) -> MarketBarIdentityMetadata:
    """Khôi phục metadata đủ để tái băm event, không đọc cột OHLCV."""
    raw_reference_id = row["raw_reference_id"]
    if raw_reference_id is not None and type(raw_reference_id) is not UUID:
        raise TypeError("raw_reference_id cần UUID hoặc None.")
    identity = MarketBarSourceIdentity(
        workspace_id=row["workspace_id"],  # type: ignore[arg-type]
        record_id=row["id"],  # type: ignore[arg-type]
        logical_key=row["logical_key"],  # type: ignore[arg-type]
        revision=row["revision"],  # type: ignore[arg-type]
        supersedes_record_id=row["supersedes_id"],  # type: ignore[arg-type]
        checksum=row["checksum"],  # type: ignore[arg-type]
    )
    return MarketBarIdentityMetadata(
        source_identity=identity,
        source=row["source"],  # type: ignore[arg-type]
        symbol=row["symbol"],  # type: ignore[arg-type]
        timeframe=row["timeframe"],  # type: ignore[arg-type]
        open_time=row["open_time"],  # type: ignore[arg-type]
        close_time=row["close_time"],  # type: ignore[arg-type]
        available_at=row["available_at"],  # type: ignore[arg-type]
        is_closed=row["is_closed"],  # type: ignore[arg-type]
        raw_reference=(None if raw_reference_id is None else str(raw_reference_id)),
    )


def _source_event_digest(
    timeframe: str,
    source_event_ids: tuple[str, ...],
    source_bar_ids: tuple[str, ...],
) -> str:
    payload = {
        "timeframe": timeframe,
        "source_event_ids": list(source_event_ids),
        "source_bar_ids": list(source_bar_ids),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
