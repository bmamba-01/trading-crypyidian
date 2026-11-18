"""Dựng dataset học AI-5 riêng từ kết quả tín hiệu (CRY-438, nhãn ``signal-gross-r-band-v1``).

Tách biệt hoàn toàn với dataset lệnh thật (``net-r-band-v1`` / ``net-pnl-sign-v1``):
- Chỉ nhận mẫu ``signal_outcome`` ở trạng thái ``SCORED`` tại một horizon cố định (mặc định 4×).
- Chỉ dùng làm nhãn khi **mọi** nến trong horizon có quyền ``TRAINING`` trong Source Registry
  (trong workspace ``crypidian-cards-v1``, chỉ ``BINANCE_USDM_REST`` có quyền ``training``;
  nến live ``BINANCE_USDM`` chỉ có ``paper_demo`` nên bị loại kèm lý do).
- Chia ``DEVELOPMENT`` / ``HOLDOUT`` theo thời gian dựa trên ``signal_at`` và ``horizon_end_at``
  để cửa sổ nhãn không rò rỉ qua ranh giới holdout.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
from trading_bot.adapters.database.models.research_entities import SourcePolicyRevisionRecord
from trading_bot.adapters.database.models.signal_entities import BotDecisionRecord
from trading_bot.adapters.database.models.signal_outcome_entities import SignalOutcomeRecord
from trading_bot.domain.learning.dataset import (
    DatasetExclusion,
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.learning.trade_labels import SUPPORTED_LABEL_VERSIONS
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.strategy.signal_outcome import (
    LABEL_VERSION as SIGNAL_LABEL_VERSION,
)

DEFAULT_SIGNAL_LABEL_HORIZON_BARS = 4
SIGNAL_SAMPLE_SCHEMA_VERSION = "ai5-signal-outcome-v1"
SIGNAL_FEATURE_SCHEMA_VERSION = "runtime-entry-snapshot-v1"
SIGNAL_FEATURE_VERSION = "runtime-entry-snapshot-v1"
SIGNAL_SPLIT_POLICY_VERSION = "ai5-signal-temporal-v1"
SIGNAL_RETENTION_POLICY_REF = "wp06-training-retention-v1"


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SignalOutcomeSampleInput:
    """Một mẫu tín hiệu đã chấm kèm snapshot đặc trưng và nguồn nến đã dùng."""

    outcome_id: UUID
    decision_id: UUID
    symbol: str
    timeframe: str
    signal_at: datetime
    feature_available_at: datetime
    feature_snapshot_hash: str
    horizon_bars: int
    horizon_end_at: datetime | None
    status: str
    label: int | None
    label_version: str
    outcome_hash: str | None
    bar_sources: tuple[str, ...]


def build_signal_outcome_dataset_manifest(
    *,
    workspace_id: UUID,
    rows: Sequence[SignalOutcomeSampleInput],
    training_licensed_sources: Mapping[str, DatasetSourceReference],
    holdout_start: datetime,
    evaluation_cutoff: datetime,
    as_of: datetime,
    horizon_bars: int = DEFAULT_SIGNAL_LABEL_HORIZON_BARS,
    label_version: str = SIGNAL_LABEL_VERSION,
    retention_policy_ref: str = SIGNAL_RETENTION_POLICY_REF,
) -> DatasetManifest:
    """Dựng ``DatasetManifest`` riêng cho nhãn kết quả tín hiệu AI-5 (không trộn nhãn lệnh)."""
    if label_version in SUPPORTED_LABEL_VERSIONS or label_version != SIGNAL_LABEL_VERSION:
        raise ValueError(
            "Dataset tín hiệu AI-5 phải dùng nhãn riêng signal-gross-r-band-v1, "
            "không trộn với nhãn lệnh thật."
        )
    if horizon_bars < 1:
        raise ValueError("horizon_bars phải >= 1.")
    if holdout_start >= evaluation_cutoff:
        raise ValueError("holdout_start phải trước evaluation_cutoff.")

    eligible_rows = [row for row in rows if row.horizon_bars == horizon_bars]
    if not eligible_rows:
        raise ValueError("Không có mẫu signal_outcome nào ở horizon được chọn.")

    samples: list[DatasetSample] = []
    exclusions: list[DatasetExclusion] = []
    symbols = sorted({row.symbol for row in eligible_rows})
    data_start = min(row.signal_at for row in eligible_rows)
    data_end = max(row.horizon_end_at or row.signal_at for row in eligible_rows)
    if data_start >= data_end:
        data_end = evaluation_cutoff

    for row in eligible_rows:
        sample_id = f"signal:{row.decision_id}:h{row.horizon_bars}"
        reasons: list[str] = []

        if row.status != "SCORED" or row.label is None or row.outcome_hash is None:
            reasons.append(f"SIGNAL_OUTCOME_{row.status}")
        if row.label_version != label_version:
            reasons.append("SIGNAL_LABEL_VERSION_MISMATCH")
        if row.horizon_end_at is None:
            reasons.append("SIGNAL_HORIZON_END_MISSING")
        if row.feature_available_at > row.signal_at:
            reasons.append("FEATURE_LOOKAHEAD_VIOLATION")

        source_refs: list[DatasetSourceReference] = []
        if not row.bar_sources:
            reasons.append("SIGNAL_BAR_SOURCE_UNKNOWN")
        else:
            missing_rights = [
                src for src in row.bar_sources if src not in training_licensed_sources
            ]
            if missing_rights:
                reasons.append("SIGNAL_BAR_SOURCE_NOT_TRAINING_LICENSED")
            else:
                source_refs = sorted(
                    {training_licensed_sources[src] for src in row.bar_sources},
                    key=lambda item: (item.source_id, item.revision_id, item.policy_id),
                )

        split: DatasetSplit | None = None
        if row.horizon_end_at is not None:
            if row.horizon_end_at <= holdout_start:
                split = DatasetSplit.DEVELOPMENT
            elif row.signal_at >= holdout_start and row.horizon_end_at <= evaluation_cutoff:
                split = DatasetSplit.HOLDOUT
            elif row.signal_at < holdout_start < row.horizon_end_at:
                reasons.append("LABEL_WINDOW_CROSSES_HOLDOUT_BOUNDARY")
            else:
                reasons.append("OUTSIDE_EVALUATION_CUTOFF")

        if reasons or split is None or row.horizon_end_at is None or row.label is None:
            exclusions.append(
                DatasetExclusion(
                    sample_id=sample_id,
                    reasons=tuple(reasons or ("SIGNAL_SAMPLE_INELIGIBLE",)),
                )
            )
            continue

        assert row.outcome_hash is not None
        label_hash = _sha256_json(
            {
                "decision_id": str(row.decision_id),
                "horizon_bars": row.horizon_bars,
                "label": int(row.label),
                "label_version": label_version,
                "outcome_hash": row.outcome_hash,
            }
        )
        samples.append(
            DatasetSample(
                sample_id=sample_id,
                decision_at=row.signal_at,
                feature_available_at=row.feature_available_at,
                outcome_start=row.signal_at,
                outcome_end=row.horizon_end_at,
                feature_snapshot_hash=row.feature_snapshot_hash,
                outcome_id=str(row.outcome_id),
                outcome_hash=row.outcome_hash,
                label_hash=label_hash,
                label_value=int(row.label),
                label_version=label_version,
                split=split,
                source_dependent=True,
                source_rights_hashes=tuple(sorted({ref.rights_hash for ref in source_refs})),
                source_revision_refs=tuple(
                    sorted({f"{ref.source_id}:{ref.revision_id}" for ref in source_refs})
                ),
                source_references=(),
            )
        )

    query_hash = _sha256_json(
        {
            "workspace_id": str(workspace_id),
            "horizon_bars": horizon_bars,
            "label_version": label_version,
            "holdout_start": holdout_start.isoformat(),
            "evaluation_cutoff": evaluation_cutoff.isoformat(),
            "symbols": symbols,
        }
    )
    return DatasetManifest(
        workspace_id=workspace_id,
        query_hash=query_hash,
        symbols=tuple(symbols),
        data_start=data_start,
        data_end=data_end,
        as_of=as_of,
        feature_schema_version=SIGNAL_FEATURE_SCHEMA_VERSION,
        feature_version=SIGNAL_FEATURE_VERSION,
        label_version=label_version,
        sample_schema_version=SIGNAL_SAMPLE_SCHEMA_VERSION,
        split_policy_version=SIGNAL_SPLIT_POLICY_VERSION,
        samples=tuple(samples),
        exclusions=tuple(exclusions),
        holdout_sealed_at=(
            holdout_start if any(s.split is DatasetSplit.HOLDOUT for s in samples) else None
        ),
        manifest_version="dataset-manifest-v2",
        retention_policy_ref=retention_policy_ref,
    )


async def load_and_build_signal_outcome_dataset(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    holdout_start: datetime,
    evaluation_cutoff: datetime,
    as_of: datetime,
    horizon_bars: int = DEFAULT_SIGNAL_LABEL_HORIZON_BARS,
    symbols: Iterable[str] | None = None,
) -> DatasetManifest:
    """Nạp ``signal_outcome`` + ``feature_snapshot`` + quyền ``TRAINING`` và dựng manifest."""
    policy_rows = (
        await session.scalars(
            select(SourcePolicyRevisionRecord)
            .where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id,
                SourcePolicyRevisionRecord.state == "verified",
            )
            .order_by(
                SourcePolicyRevisionRecord.source_id,
                SourcePolicyRevisionRecord.sequence.desc(),
            )
        )
    ).all()
    training_sources: dict[str, DatasetSourceReference] = {}
    for policy in policy_rows:
        if policy.source_id in training_sources:
            continue
        purposes = policy.allowed_purposes or []
        if SourceUsePurpose.TRAINING.value in purposes:
            rights_hash = policy.license_scope_hash or _sha256_json(
                {
                    "source_id": policy.source_id,
                    "policy_id": policy.policy_id,
                    "sequence": int(policy.sequence),
                }
            )
            training_sources[policy.source_id] = DatasetSourceReference(
                source_id=policy.source_id,
                item_id=policy.source_id,
                revision_id=str(policy.sequence),
                policy_id=policy.policy_id,
                policy_sequence=int(policy.sequence),
                purpose=SourceUsePurpose.TRAINING,
                rights_hash=rights_hash,
            )

    stmt = (
        select(
            SignalOutcomeRecord,
            FeatureSnapshotRecord.content_hash,
            FeatureSnapshotRecord.computed_at,
        )
        .join(
            BotDecisionRecord,
            (BotDecisionRecord.workspace_id == SignalOutcomeRecord.workspace_id)
            & (BotDecisionRecord.bot_id == SignalOutcomeRecord.bot_id)
            & (BotDecisionRecord.id == SignalOutcomeRecord.decision_id),
        )
        .join(
            FeatureSnapshotRecord,
            (FeatureSnapshotRecord.workspace_id == BotDecisionRecord.workspace_id)
            & (
                FeatureSnapshotRecord.snapshot_id
                == BotDecisionRecord.payload["feature_snapshot_id"].astext
            ),
        )
        .where(
            SignalOutcomeRecord.workspace_id == workspace_id,
            SignalOutcomeRecord.horizon_bars == horizon_bars,
            SignalOutcomeRecord.signal_at <= evaluation_cutoff,
        )
        .order_by(SignalOutcomeRecord.signal_at, SignalOutcomeRecord.decision_id)
    )
    symbol_list = sorted({s for s in (symbols or ()) if s})
    if symbol_list:
        stmt = stmt.where(SignalOutcomeRecord.symbol.in_(symbol_list))
    db_rows = (await session.execute(stmt)).all()

    sample_inputs = [
        SignalOutcomeSampleInput(
            outcome_id=outcome.id,
            decision_id=outcome.decision_id,
            symbol=outcome.symbol,
            timeframe=outcome.timeframe,
            signal_at=outcome.signal_at,
            feature_available_at=feature_available_at,
            feature_snapshot_hash=snapshot_hash,
            horizon_bars=int(outcome.horizon_bars),
            horizon_end_at=outcome.horizon_end_at,
            status=outcome.status,
            label=outcome.label,
            label_version=outcome.label_version,
            outcome_hash=outcome.input_hash,
            bar_sources=tuple(outcome.bar_sources or ()),
        )
        for outcome, snapshot_hash, feature_available_at in db_rows
    ]
    return build_signal_outcome_dataset_manifest(
        workspace_id=workspace_id,
        rows=sample_inputs,
        training_licensed_sources=training_sources,
        holdout_start=holdout_start,
        evaluation_cutoff=evaluation_cutoff,
        as_of=as_of,
        horizon_bars=horizon_bars,
    )


__all__ = [
    "DEFAULT_SIGNAL_LABEL_HORIZON_BARS",
    "SIGNAL_FEATURE_SCHEMA_VERSION",
    "SIGNAL_FEATURE_VERSION",
    "SIGNAL_LABEL_VERSION",
    "SIGNAL_RETENTION_POLICY_REF",
    "SIGNAL_SAMPLE_SCHEMA_VERSION",
    "SIGNAL_SPLIT_POLICY_VERSION",
    "SignalOutcomeSampleInput",
    "build_signal_outcome_dataset_manifest",
    "load_and_build_signal_outcome_dataset",
]
