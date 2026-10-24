"""Kho snapshot feature bất biến, giới hạn workspace và hỗ trợ replay as-of."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureSnapshot,
    MarketRegime,
    RegimeAssessment,
)


class FeatureSnapshotRepositoryConflict(RuntimeError):
    """Từ chối khóa hash trùng có payload khác hoặc input không xác thực."""


@dataclass(frozen=True, slots=True)
class FeatureSnapshotAppendResult:
    """Kết quả lưu snapshot mới hoặc nhận diện lần gửi lặp."""

    record_id: UUID
    snapshot_id: str
    content_hash: str
    duplicate: bool


class FeatureSnapshotRepository:
    """Mọi truy vấn nhận workspace tường minh; transaction do caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self, *, workspace_id: UUID, snapshot: FeatureSnapshot
    ) -> FeatureSnapshotAppendResult:
        """Append bất biến; retry cùng hash/payload trả lại định danh hiện có."""
        if workspace_id != snapshot.workspace_id:
            raise FeatureSnapshotRepositoryConflict("Snapshot không thuộc workspace được yêu cầu.")
        canonical_json = snapshot.canonical_json()
        content_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if snapshot.snapshot_id != snapshot.content_hash or snapshot.snapshot_id != content_hash:
            raise FeatureSnapshotRepositoryConflict(
                "Snapshot ID/hash không khớp payload canonical."
            )

        existing = await self._find_by_snapshot_id(
            workspace_id=workspace_id, snapshot_id=snapshot.snapshot_id
        )
        if existing is not None:
            return self._existing_result(existing, snapshot, canonical_json)

        payload = snapshot.canonical_payload()
        regime = payload["regime"]
        row = FeatureSnapshotRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            snapshot_id=snapshot.snapshot_id,
            content_hash=content_hash,
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            as_of=snapshot.as_of,
            computed_at=snapshot.computed_at,
            schema_hash=snapshot.schema_hash,
            formula_hash=snapshot.formula_hash,
            code_hash=snapshot.code_hash,
            input_bar_ids=list(snapshot.input_bar_ids),
            input_event_ids=list(snapshot.input_event_ids),
            features=dict(snapshot.features),
            warmup=dict(snapshot.warmup),
            quality_flags=list(snapshot.quality_flags),
            data_freshness=snapshot.data_freshness.value,
            data_coverage=snapshot.data_coverage,
            regime=regime,
            is_usable=snapshot.is_usable,
            canonical_json=canonical_json,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._find_by_snapshot_id(
                workspace_id=workspace_id, snapshot_id=snapshot.snapshot_id
            )
            if existing is None:
                raise FeatureSnapshotRepositoryConflict(
                    "Không thể append snapshot do xung đột unique khác."
                ) from error
            return self._existing_result(existing, snapshot, canonical_json)
        return FeatureSnapshotAppendResult(
            record_id=row.id,
            snapshot_id=snapshot.snapshot_id,
            content_hash=content_hash,
            duplicate=False,
        )

    async def get_as_of(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        schema_hash: str | None = None,
        formula_hash: str | None = None,
        code_hash: str | None = None,
    ) -> FeatureSnapshot | None:
        """Lấy snapshot gần nhất đã tính và có dữ liệu không muộn hơn mốc yêu cầu."""
        _require_aware(as_of, "as_of")
        if not symbol.strip() or not timeframe.strip():
            raise ValueError("Symbol và timeframe không được để trống.")
        conditions = [
            FeatureSnapshotRecord.workspace_id == workspace_id,
            FeatureSnapshotRecord.symbol == symbol,
            FeatureSnapshotRecord.timeframe == timeframe,
            FeatureSnapshotRecord.as_of <= as_of,
            FeatureSnapshotRecord.computed_at <= as_of,
        ]
        if schema_hash is not None:
            conditions.append(FeatureSnapshotRecord.schema_hash == schema_hash)
        if formula_hash is not None:
            conditions.append(FeatureSnapshotRecord.formula_hash == formula_hash)
        if code_hash is not None:
            conditions.append(FeatureSnapshotRecord.code_hash == code_hash)
        row = await self._session.scalar(
            select(FeatureSnapshotRecord)
            .where(*conditions)
            .order_by(
                FeatureSnapshotRecord.as_of.desc(),
                FeatureSnapshotRecord.computed_at.desc(),
                FeatureSnapshotRecord.snapshot_id.desc(),
            )
            .limit(1)
        )
        return None if row is None else _restore_snapshot(row)

    async def _find_by_snapshot_id(
        self, *, workspace_id: UUID, snapshot_id: str
    ) -> FeatureSnapshotRecord | None:
        return await self._session.scalar(
            select(FeatureSnapshotRecord).where(
                FeatureSnapshotRecord.workspace_id == workspace_id,
                FeatureSnapshotRecord.snapshot_id == snapshot_id,
            )
        )

    @staticmethod
    def _existing_result(
        existing: FeatureSnapshotRecord,
        snapshot: FeatureSnapshot,
        canonical_json: str,
    ) -> FeatureSnapshotAppendResult:
        if (
            existing.content_hash != snapshot.content_hash
            or existing.canonical_json != canonical_json
        ):
            raise FeatureSnapshotRepositoryConflict("Snapshot ID/hash đã tồn tại với payload khác.")
        return FeatureSnapshotAppendResult(
            record_id=existing.id,
            snapshot_id=snapshot.snapshot_id,
            content_hash=snapshot.content_hash,
            duplicate=True,
        )


def _restore_snapshot(row: FeatureSnapshotRecord) -> FeatureSnapshot:
    content_hash = hashlib.sha256(row.canonical_json.encode("utf-8")).hexdigest()
    if row.snapshot_id != content_hash or row.content_hash != content_hash:
        raise FeatureSnapshotRepositoryConflict("Hash của snapshot đã lưu không còn khớp payload.")
    payload = json.loads(row.canonical_json)
    if payload["workspace_id"] != str(row.workspace_id):
        raise FeatureSnapshotRepositoryConflict("Payload snapshot không khớp workspace của hàng.")
    regime_data = payload["regime"]
    regime = RegimeAssessment(
        label=MarketRegime(regime_data["label"]),
        confidence=float(regime_data["confidence"]),
        coverage=float(regime_data["coverage"]),
        reason_code=regime_data["reason_code"],
        reason_vi=regime_data["reason_vi"],
    )
    return FeatureSnapshot(
        snapshot_id=row.snapshot_id,
        content_hash=row.content_hash,
        workspace_id=UUID(payload["workspace_id"]),
        symbol=payload["symbol"],
        timeframe=payload["timeframe"],
        as_of=datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00")),
        computed_at=row.computed_at,
        schema_hash=payload["schema_hash"],
        formula_hash=payload["formula_hash"],
        code_hash=payload["code_hash"],
        input_bar_ids=tuple(payload["input_bar_ids"]),
        input_event_ids=tuple(payload["input_event_ids"]),
        features=payload["features"],
        warmup=payload["warmup"],
        quality_flags=tuple(payload["quality_flags"]),
        data_freshness=DataFreshness(payload["data_freshness"]),
        data_coverage=float(payload["data_coverage"]),
        regime=regime,
        is_usable=payload["is_usable"],
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
