"""Macro calendar evidence point-in-time và raw response reference trong replay (WP-07C / CRY-366).

Cho phép chiến lược H06 backtest được với trạng thái macro blackout đúng thời điểm (as-of,
chống lookahead tuyệt đối) và gắn tham chiếu bất biến tới payload gốc (`MarketRawReference`)
kèm chính sách retention 90 ngày (không nhồi raw payload vào bảng SQL chính).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from trading_bot.domain.research.macro_calendar import (
    BLACKOUT_AFTER,
    BLACKOUT_BEFORE,
    MACRO_POLICY_VERSION,
)

DEFAULT_RAW_RETENTION_DAYS = 90


class ReplayMacroStatus(StrEnum):
    """Trạng thái macro tại thời điểm quyết định trong replay cho H06."""

    CLEAR = "CLEAR"
    BLACKOUT = "BLACKOUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RawPayloadRetentionPolicy:
    """Chính sách lưu trữ payload gốc: lưu ở object store, DB chỉ giữ tham chiếu + SHA-256."""

    retention_days: int = DEFAULT_RAW_RETENTION_DAYS
    store_in_object_store_only: bool = True

    def __post_init__(self) -> None:
        if self.retention_days <= 0:
            raise ValueError("retention_days phải lớn hơn 0.")


@dataclass(frozen=True, slots=True)
class ReplayRawReference:
    """Tham chiếu bất biến tới raw response/archive (khớp schema `MarketRawReference`)."""

    raw_reference_id: UUID
    source: str
    object_key: str
    checksum: str
    byte_length: int
    content_type: str
    captured_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if len(self.checksum) != 64:
            raise ValueError("checksum phải là SHA-256 64 ký tự hex.")
        if self.byte_length < 0:
            raise ValueError("byte_length không được âm.")

    def is_expired(self, as_of: datetime) -> bool:
        return as_of.astimezone(UTC) >= self.expires_at.astimezone(UTC)


def build_replay_raw_reference(
    *,
    raw_reference_id: UUID,
    source: str,
    object_key: str,
    raw_bytes: bytes,
    content_type: str,
    captured_at: datetime,
    retention: RawPayloadRetentionPolicy | None = None,
) -> ReplayRawReference:
    """Tạo tham chiếu raw response kèm SHA-256 checksum và hạn retention."""
    pol = retention or RawPayloadRetentionPolicy()
    cap_utc = captured_at.astimezone(UTC)
    return ReplayRawReference(
        raw_reference_id=raw_reference_id,
        source=source,
        object_key=object_key,
        checksum=hashlib.sha256(raw_bytes).hexdigest(),
        byte_length=len(raw_bytes),
        content_type=content_type,
        captured_at=cap_utc,
        expires_at=cap_utc + timedelta(days=pol.retention_days),
    )


@dataclass(frozen=True, slots=True)
class ReplayMacroEvidencePoint:
    """Một sự kiện lịch vĩ mô có `available_at` để kiểm tra as-of không nhìn trước."""

    source_id: str
    event_name: str
    release_at: datetime
    available_at: datetime
    is_high_impact: bool
    raw_reference_id: UUID | None = None
    raw_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayMacroBlackoutState:
    """Trạng thái macro point-in-time cung cấp cho context replay (H06)."""

    decision_time: datetime
    status: ReplayMacroStatus
    policy_version: str
    matched_event_name: str | None = None
    matched_source_id: str | None = None
    raw_reference_id: UUID | None = None
    detail_vi: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_time": self.decision_time.astimezone(UTC).isoformat(),
            "status": self.status.value,
            "policy_version": self.policy_version,
            "matched_event_name": self.matched_event_name,
            "matched_source_id": self.matched_source_id,
            "raw_reference_id": str(self.raw_reference_id) if self.raw_reference_id else None,
            "detail_vi": self.detail_vi,
        }


def evaluate_replay_macro_as_of(
    events: Sequence[ReplayMacroEvidencePoint],
    *,
    decision_time: datetime,
    required_sources: frozenset[str],
    blackout_before: timedelta = BLACKOUT_BEFORE,
    blackout_after: timedelta = BLACKOUT_AFTER,
) -> ReplayMacroBlackoutState:
    """Đánh giá trạng thái macro blackout đúng thời điểm `decision_time` (chống lookahead).

    - Chỉ dùng sự kiện có `available_at <= decision_time`.
    - Thiếu bất kỳ nguồn bắt buộc nào tại `decision_time` -> `UNKNOWN` (fail-closed cho H06).
    - Nằm trong `[release_at - blackout_before, release_at + blackout_after]` -> `BLACKOUT`.
    - Ngoài cửa sổ blackout và đủ nguồn -> `CLEAR`.
    """
    dec_utc = decision_time.astimezone(UTC)
    known_events = [ev for ev in events if ev.available_at.astimezone(UTC) <= dec_utc]
    observed_sources = {ev.source_id for ev in known_events}
    missing_sources = required_sources - observed_sources

    if missing_sources:
        return ReplayMacroBlackoutState(
            decision_time=dec_utc,
            status=ReplayMacroStatus.UNKNOWN,
            policy_version=MACRO_POLICY_VERSION,
            detail_vi=(
                f"Thiếu bằng chứng lịch vĩ mô tại {dec_utc.isoformat()}: "
                f"{', '.join(sorted(missing_sources))}."
            ),
        )

    for ev in known_events:
        if not ev.is_high_impact:
            continue
        rel_utc = ev.release_at.astimezone(UTC)
        if (rel_utc - blackout_before) <= dec_utc <= (rel_utc + blackout_after):
            return ReplayMacroBlackoutState(
                decision_time=dec_utc,
                status=ReplayMacroStatus.BLACKOUT,
                policy_version=MACRO_POLICY_VERSION,
                matched_event_name=ev.event_name,
                matched_source_id=ev.source_id,
                raw_reference_id=ev.raw_reference_id,
                detail_vi=f"Đang trong cửa sổ cấm vĩ mô cho sự kiện {ev.event_name}.",
            )

    return ReplayMacroBlackoutState(
        decision_time=dec_utc,
        status=ReplayMacroStatus.CLEAR,
        policy_version=MACRO_POLICY_VERSION,
        detail_vi="Đủ bằng chứng lịch vĩ mô as-of và ngoài cửa sổ blackout.",
    )


@dataclass(frozen=True, slots=True)
class ReplayEvidenceEnvelope:
    """Vỏ bọc bất biến gắn macro evidence + raw references vào bundle replay."""

    bundle_hash: str
    macro_policy_version: str
    macro_event_count: int
    raw_reference_ids: tuple[UUID, ...]
    envelope_hash: str


def attach_macro_and_raw_evidence_to_bundle(
    *,
    bundle_hash: str,
    macro_points: Sequence[ReplayMacroEvidencePoint],
    raw_references: Sequence[ReplayRawReference],
) -> ReplayEvidenceEnvelope:
    """Tạo envelope SHA-256 bất biến nối bundle replay với macro evidence và raw references."""
    payload = {
        "bundle_hash": bundle_hash,
        "macro_policy_version": MACRO_POLICY_VERSION,
        "macro_points": [
            {
                "source_id": m.source_id,
                "event_name": m.event_name,
                "release_at": m.release_at.astimezone(UTC).isoformat(),
                "available_at": m.available_at.astimezone(UTC).isoformat(),
                "is_high_impact": m.is_high_impact,
                "raw_checksum": m.raw_checksum,
            }
            for m in sorted(macro_points, key=lambda x: (x.source_id, x.release_at.isoformat()))
        ],
        "raw_references": [
            {
                "id": str(r.raw_reference_id),
                "source": r.source,
                "object_key": r.object_key,
                "checksum": r.checksum,
                "byte_length": r.byte_length,
            }
            for r in sorted(raw_references, key=lambda x: str(x.raw_reference_id))
        ],
    }
    envelope_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReplayEvidenceEnvelope(
        bundle_hash=bundle_hash,
        macro_policy_version=MACRO_POLICY_VERSION,
        macro_event_count=len(macro_points),
        raw_reference_ids=tuple(r.raw_reference_id for r in raw_references),
        envelope_hash=envelope_hash,
    )
