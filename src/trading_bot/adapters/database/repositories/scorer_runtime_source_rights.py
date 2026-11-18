"""Kiểm quyền PAPER chính xác trên lineage của job scorer đang khóa."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.repositories.feature_snapshots import (
    FeatureSnapshotRepository,
    FeatureSnapshotRepositoryConflict,
)
from trading_bot.application.backtest_dataset_manifest import MarketBarIdentityMetadata
from trading_bot.application.bot_dispatcher import BotDecisionJob
from trading_bot.application.decision_snapshot_builder import (
    DecisionSnapshotBuildError,
    canonical_market_bar_metadata_event_id,
)
from trading_bot.application.strategy_market_requirements import feature_requirements_for_release
from trading_bot.domain.market.feature_snapshot import FeatureSnapshot
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy.contracts import DecisionSnapshot

_SOURCE_BAR_ID_PREFIX = "market-bar-record:"
_SHA256 = frozenset("0123456789abcdef")
_RIGHTS_PROVIDER_BY_MARKET_SOURCE = {
    "BINANCE_USDM": "BINANCE_USDM_REST",
    "BINANCE_USDM_REST_BACKFILL": "BINANCE_USDM_REST",
    "CRYPIDIAN_FIRST_PARTY_QA_FIXTURE": "CRYPIDIAN_FIRST_PARTY_QA",
}
_METADATA_COLUMNS = (
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


class ScorerDecisionSourceLineageUnavailable(RuntimeError):
    """DecisionSnapshot không có lineage MarketBar persistable và đầy đủ."""

    reason_code = "SCORER_SOURCE_LINEAGE_MISSING"


class ScorerDecisionSourceRightsDenied(RuntimeError):
    """Current PAPER rights gateway từ chối source-use scope của candidate."""

    def __init__(self, reason_code: str = "WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED") -> None:
        self.reason_code = reason_code
        super().__init__("Current PAPER source-use rights bị từ chối.")


async def stage_job_source_bars(
    session: AsyncSession,
    *,
    job: BotDecisionJob,
    snapshot: DecisionSnapshot,
) -> tuple[dict[str, object], ...]:
    """Ghi lineage tạm trong UoW từ snapshot do provider DB-backed vừa tạo.

    Bản ghi được ràng buộc bởi job/evidence; trigger PostgreSQL xác minh lại từng MarketBar.
    Sau khi BotDecision được thêm, trigger dọn lineage tạm trong chính transaction đó.
    """
    if (
        type(job) is not BotDecisionJob
        or type(snapshot) is not DecisionSnapshot
        or not _valid_hash(snapshot.snapshot_id)
        or not snapshot.timeframe_lineage
        or not snapshot.series
        or snapshot.workspace_id != job.workspace_id
        or len(snapshot.feature_snapshots) != len(snapshot.timeframe_lineage)
        or len(snapshot.timeframe_lineage) != len(snapshot.series)
        or any(item.workspace_id != job.workspace_id for item in snapshot.timeframe_lineage)
        or any(item.workspace_id != job.workspace_id for item in snapshot.feature_snapshots)
        or tuple(item.timeframe for item in snapshot.timeframe_lineage)
        != tuple(item.timeframe for item in snapshot.series)
    ):
        raise ScorerDecisionSourceLineageUnavailable(
            "Candidate không có DecisionSnapshot lineage đầy đủ."
        )
    if {item.workspace_id for item in snapshot.timeframe_lineage} != {job.workspace_id}:
        raise ScorerDecisionSourceLineageUnavailable(
            "DecisionSnapshot lineage không thuộc workspace của job đã khóa."
        )
    try:
        expected_timeframes = tuple(
            feature_requirements_for_release(job.context.strategy_release_id)
        )
    except ValueError as error:
        raise ScorerDecisionSourceLineageUnavailable(
            "Strategy release không có hợp đồng timeframe authoritative."
        ) from error
    if tuple(item.timeframe for item in snapshot.timeframe_lineage) != expected_timeframes:
        raise ScorerDecisionSourceLineageUnavailable(
            "DecisionSnapshot timeframe không khớp strategy release đã ghim trong job."
        )

    prepared: list[dict[str, object]] = []
    record_ids: list[UUID] = []
    seen_ids: set[UUID] = set()
    for lineage, series, feature_snapshot in zip(
        snapshot.timeframe_lineage,
        snapshot.series,
        snapshot.feature_snapshots,
        strict=True,
    ):
        bars = series.bars
        if (
            lineage.workspace_id != job.workspace_id
            or lineage.symbol != snapshot.symbol
            or series.timeframe != lineage.timeframe
            or type(feature_snapshot) is not FeatureSnapshot
            or feature_snapshot.snapshot_id != lineage.feature_snapshot_id
            or feature_snapshot.workspace_id != job.workspace_id
            or feature_snapshot.symbol != snapshot.symbol
            or feature_snapshot.timeframe != lineage.timeframe
            or feature_snapshot.as_of != snapshot.decision_time
            or feature_snapshot.computed_at != snapshot.decision_time
            or feature_snapshot.input_event_ids != lineage.source_event_ids
            or not _valid_hash(lineage.feature_snapshot_id)
            or lineage.source_identity_pinned is not True
            or lineage.as_of != snapshot.decision_time
            or not bars
            or len(lineage.source_event_ids) != len(bars)
            or len(lineage.source_bar_ids) != len(bars)
            or _source_event_digest(
                lineage.timeframe, lineage.source_event_ids, lineage.source_bar_ids
            )
            != lineage.source_event_digest
        ):
            raise ScorerDecisionSourceLineageUnavailable(
                "Candidate thiếu source identity đã ghim hoặc timeframe completeness."
            )
        for ordinal, (bar, bar_id, event_id) in enumerate(
            zip(bars, lineage.source_bar_ids, lineage.source_event_ids, strict=True)
        ):
            record_id = _market_bar_record_id(bar_id)
            identity = bar.source_identity
            if (
                type(identity) is not MarketBarSourceIdentity
                or identity.workspace_id != job.workspace_id
                or identity.record_id != record_id
                or record_id in seen_ids
                or bar.symbol != snapshot.symbol
                or bar.timeframe != lineage.timeframe
                or bar.source not in _RIGHTS_PROVIDER_BY_MARKET_SOURCE
                or not bar.is_closed
                or bar.close_time > snapshot.decision_time
                or bar.event_at > snapshot.decision_time
                or bar.available_at > snapshot.decision_time
            ):
                raise ScorerDecisionSourceLineageUnavailable(
                    "Candidate source bar ID hoặc mốc causal không hợp lệ."
                )
            try:
                canonical_event_id = canonical_market_bar_metadata_event_id(
                    MarketBarIdentityMetadata.from_bar(bar),
                    workspace_id=job.workspace_id,
                )
            except (DecisionSnapshotBuildError, ValueError) as error:
                raise ScorerDecisionSourceLineageUnavailable(
                    "Candidate MarketBar identity không canonical."
                ) from error
            if canonical_event_id != event_id:
                raise ScorerDecisionSourceLineageUnavailable(
                    "Candidate source event ID không khớp MarketBar identity."
                )
            seen_ids.add(record_id)
            record_ids.append(record_id)
            prepared.append(
                {
                    "id": uuid4(),
                    "workspace_id": job.workspace_id,
                    "bot_id": job.bot_id,
                    "job_id": job.job_id,
                    "evidence_id": job.evidence_id,
                    "evidence_hash": job.evidence_hash,
                    "decision_snapshot_id": snapshot.snapshot_id,
                    "decision_time": snapshot.decision_time,
                    "timeframe": lineage.timeframe,
                    "feature_snapshot_id": lineage.feature_snapshot_id,
                    "market_bar_id": record_id,
                    "source_event_id": event_id,
                    "source_event_digest": lineage.source_event_digest,
                    "ordinal": ordinal,
                    "timeframe_bar_count": len(bars),
                    "snapshot_timeframe_count": len(snapshot.series),
                }
            )

    if not record_ids:
        raise ScorerDecisionSourceLineageUnavailable(
            "Candidate không có source MarketBar để kiểm quyền."
        )

    result = await session.execute(
        select(*_METADATA_COLUMNS).where(
            MarketBar.workspace_id == job.workspace_id,
            MarketBar.id.in_(record_ids),
        )
    )
    persisted_by_id: dict[UUID, Mapping[str, Any]] = {}
    for raw_row in result.mappings().all():
        row = cast(Mapping[str, Any], raw_row)
        row_id = row.get("id")
        if type(row_id) is not UUID or row_id not in seen_ids or row_id in persisted_by_id:
            raise ScorerDecisionSourceLineageUnavailable(
                "Cơ sở dữ liệu trả MarketBar metadata ngoài lineage candidate."
            )
        persisted_by_id[row_id] = row
    if set(persisted_by_id) != seen_ids:
        raise ScorerDecisionSourceLineageUnavailable(
            "Không tìm thấy đủ MarketBar metadata trong workspace candidate."
        )

    for line, bar in zip(prepared, _snapshot_bars(snapshot), strict=True):
        persisted = persisted_by_id[cast(UUID, line["market_bar_id"])]
        try:
            event_id = canonical_market_bar_metadata_event_id(
                _metadata_from_row(persisted),
                workspace_id=job.workspace_id,
            )
        except (DecisionSnapshotBuildError, KeyError, TypeError, ValueError) as error:
            raise ScorerDecisionSourceLineageUnavailable(
                "MarketBar metadata đã lưu không canonical."
            ) from error
        if (
            _metadata_from_row(persisted) != MarketBarIdentityMetadata.from_bar(bar)
            or event_id != line["source_event_id"]
        ):
            raise ScorerDecisionSourceLineageUnavailable(
                "MarketBar metadata đã lưu khác snapshot candidate."
            )

    try:
        await session.execute(
            text(
                "INSERT INTO public.bot_decision_job_source_bar ("
                "id, workspace_id, bot_id, job_id, evidence_id, evidence_hash, "
                "decision_snapshot_id, decision_time, timeframe, feature_snapshot_id, "
                "market_bar_id, "
                "source_event_id, source_event_digest, ordinal, timeframe_bar_count, "
                "snapshot_timeframe_count) VALUES ("
                ":id, :workspace_id, :bot_id, :job_id, :evidence_id, :evidence_hash, "
                ":decision_snapshot_id, :decision_time, :timeframe, :feature_snapshot_id, "
                ":market_bar_id, "
                ":source_event_id, :source_event_digest, :ordinal, :timeframe_bar_count, "
                ":snapshot_timeframe_count)"
            ),
            prepared,
        )
        await session.flush()
    except DBAPIError as error:
        if _database_reason_code(error) in {
            "WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING",
            "WP06_SCORER_JOB_SOURCE_UNKNOWN_PROVIDER",
        }:
            raise ScorerDecisionSourceLineageUnavailable(
                "PostgreSQL từ chối lineage nến của snapshot scorer."
            ) from error
        raise
    return tuple(prepared)


async def authorize_job_source_scope(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    job_id: UUID,
) -> str:
    """Gateway dựng exact PAPER use scopes internamente; caller chỉ truyền job identity."""
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )
    try:
        result = await session.execute(
            text(
                "SELECT public.wp06_authorize_scorer_job_source_scope("
                "CAST(:workspace_id AS uuid), CAST(:job_id AS uuid))"
            ),
            {"workspace_id": str(workspace_id), "job_id": str(job_id)},
        )
        fingerprint = result.scalar_one_or_none()
    except DBAPIError as error:
        _raise_source_gate_error(error)
    if not _valid_hash(fingerprint):
        raise ScorerDecisionSourceRightsDenied("WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED")
    return cast(str, fingerprint)


async def preflight_job_source_rights(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    job_id: UUID,
) -> str:
    """Kiểm rights hiện hành trước khi lưu snapshot feature trong savepoint job."""
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )
    try:
        result = await session.execute(
            text(
                "SELECT public.wp06_preflight_scorer_job_source_rights("
                "CAST(:workspace_id AS uuid), CAST(:job_id AS uuid))"
            ),
            {"workspace_id": str(workspace_id), "job_id": str(job_id)},
        )
        fingerprint = result.scalar_one_or_none()
    except DBAPIError as error:
        _raise_source_gate_error(error)
        raise
    if not _valid_hash(fingerprint):
        raise ScorerDecisionSourceRightsDenied("WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED")
    return cast(str, fingerprint)


async def persist_decision_feature_snapshots(
    session: AsyncSession,
    *,
    job: BotDecisionJob,
    snapshot: DecisionSnapshot,
) -> None:
    """Append snapshot canonical trong savepoint sau preflight rights, trước inference."""
    if (
        type(job) is not BotDecisionJob
        or type(snapshot) is not DecisionSnapshot
        or len(snapshot.feature_snapshots) != len(snapshot.timeframe_lineage)
        or {item.workspace_id for item in snapshot.timeframe_lineage} != {job.workspace_id}
        or tuple(item.timeframe for item in snapshot.feature_snapshots)
        != tuple(item.timeframe for item in snapshot.timeframe_lineage)
    ):
        raise ScorerDecisionSourceLineageUnavailable(
            "FeatureSnapshot không khớp workspace/timeframe lineage của job."
        )
    feature_rows = tuple(
        zip(
            snapshot.feature_snapshots,
            snapshot.timeframe_lineage,
            strict=True,
        )
    )
    for feature_snapshot, lineage in feature_rows:
        try:
            canonical_json = feature_snapshot.canonical_json()
        except (TypeError, ValueError) as error:
            raise ScorerDecisionSourceLineageUnavailable(
                "FeatureSnapshot không thể mã hóa canonical."
            ) from error
        if (
            feature_snapshot.snapshot_id != lineage.feature_snapshot_id
            or feature_snapshot.workspace_id != job.workspace_id
            or feature_snapshot.snapshot_id != feature_snapshot.content_hash
            or hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
            != feature_snapshot.snapshot_id
            or feature_snapshot.input_event_ids != lineage.source_event_ids
            or feature_snapshot.symbol != snapshot.symbol
            or feature_snapshot.timeframe != lineage.timeframe
            or feature_snapshot.as_of != snapshot.decision_time
            or feature_snapshot.computed_at != snapshot.decision_time
        ):
            raise ScorerDecisionSourceLineageUnavailable(
                "FeatureSnapshot canonical input không khớp MarketBar lineage."
            )
    repository = FeatureSnapshotRepository(session)
    for feature_snapshot, _lineage in feature_rows:
        try:
            await repository.append(workspace_id=job.workspace_id, snapshot=feature_snapshot)
        except FeatureSnapshotRepositoryConflict as error:
            raise ScorerDecisionSourceLineageUnavailable(
                "FeatureSnapshot canonical khác bản bất biến đã lưu."
            ) from error


def combine_source_rights_fingerprints(
    *, deployment_fingerprint: str, decision_fingerprint: str
) -> str:
    """Gộp hai fingerprint metadata-only theo JSON tất định."""
    if not _valid_hash(deployment_fingerprint) or not _valid_hash(decision_fingerprint):
        raise ValueError("Fingerprint quyền scorer phải là SHA-256 hex chữ thường.")
    canonical = json.dumps(
        {
            "decision_source_rights": decision_fingerprint,
            "deployment_source_rights": deployment_fingerprint,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_bars(snapshot: DecisionSnapshot) -> tuple[OHLCVBar, ...]:
    return tuple(bar for series in snapshot.series for bar in series.bars)


def _market_bar_record_id(value: str) -> UUID:
    if not isinstance(value, str) or not value.startswith(_SOURCE_BAR_ID_PREFIX):
        raise ScorerDecisionSourceLineageUnavailable(
            "Source bar ID không phải MarketBar record UUID."
        )
    raw_uuid = value.removeprefix(_SOURCE_BAR_ID_PREFIX)
    try:
        parsed = UUID(raw_uuid)
    except ValueError as error:
        raise ScorerDecisionSourceLineageUnavailable(
            "Source bar ID không phải UUID hợp lệ."
        ) from error
    if str(parsed) != raw_uuid:
        raise ScorerDecisionSourceLineageUnavailable("Source bar UUID không ở dạng canonical.")
    return parsed


def _source_event_digest(
    timeframe: str,
    source_event_ids: tuple[str, ...],
    source_bar_ids: tuple[str, ...],
) -> str:
    canonical = json.dumps(
        {
            "timeframe": timeframe,
            "source_event_ids": list(source_event_ids),
            "source_bar_ids": list(source_bar_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _metadata_from_row(row: Mapping[str, Any]) -> MarketBarIdentityMetadata:
    raw_reference_id = row["raw_reference_id"]
    if raw_reference_id is not None and type(raw_reference_id) is not UUID:
        raise TypeError("raw_reference_id phải là UUID hoặc None.")
    identity = MarketBarSourceIdentity(
        workspace_id=row["workspace_id"],
        record_id=row["id"],
        logical_key=row["logical_key"],
        revision=row["revision"],
        supersedes_record_id=row["supersedes_id"],
        checksum=row["checksum"],
    )
    return MarketBarIdentityMetadata(
        source_identity=identity,
        source=row["source"],
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        open_time=row["open_time"],
        close_time=row["close_time"],
        available_at=row["available_at"],
        is_closed=row["is_closed"],
        raw_reference=None if raw_reference_id is None else str(raw_reference_id),
    )


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256 for character in value)
    )


def _database_reason_code(error: DBAPIError) -> str | None:
    original = error.orig
    diagnostic = getattr(original, "diag", None)
    candidate = getattr(diagnostic, "message_primary", None)
    if not isinstance(candidate, str):
        candidate = str(original).splitlines()[0]
    return candidate if candidate.startswith("WP06_SCORER_JOB_SOURCE_") else None


def _raise_source_gate_error(error: DBAPIError) -> None:
    """Chuyển mã SQL gateway thành denial hoặc lineage fail-closed có kiểu."""
    reason_code = _database_reason_code(error)
    if reason_code in {
        "WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING",
        "WP06_SCORER_JOB_SOURCE_UNKNOWN_PROVIDER",
    }:
        raise ScorerDecisionSourceLineageUnavailable(
            "PostgreSQL không có source lineage đủ cho job scorer."
        ) from error
    if reason_code is not None:
        raise ScorerDecisionSourceRightsDenied(reason_code) from error
    raise error


__all__ = [
    "ScorerDecisionSourceLineageUnavailable",
    "ScorerDecisionSourceRightsDenied",
    "authorize_job_source_scope",
    "combine_source_rights_fingerprints",
    "persist_decision_feature_snapshots",
    "preflight_job_source_rights",
    "stage_job_source_bars",
]
