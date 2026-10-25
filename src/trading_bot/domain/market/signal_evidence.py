"""Hợp đồng shared signal evidence trung lập, point-in-time và bất biến.

Evidence là fact/setup dùng chung trong một workspace: không gắn bot, account,
strategy decision, quantity hay lệnh, nên không thể tự trở thành quyết định
giao dịch. Mỗi bot đọc evidence rồi tự chạy strategy/config riêng.
Module thuần: không truy cập mạng, cơ sở dữ liệu hoặc đồng hồ hệ thống.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from trading_bot.domain.market.feature_snapshot import FeatureSnapshot

SIGNAL_EVIDENCE_SCHEMA_VERSION = "crypidian-shared-signal-evidence-v1"
# Namespace cố định cho UUID tất định của evidence; đổi giá trị sẽ đổi mọi evidence ID.
SIGNAL_EVIDENCE_NAMESPACE = UUID("5d0c9a52-7f3e-5b8e-9c41-3a6f0e2b7d18")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SETUP_TYPE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
# Mốc do caller/detector cấp: không thuộc định danh observation (retry ≠ revision).
_CALLER_TIMESTAMPS = frozenset({"available_at", "validity_end"})


class SignalEvidenceError(ValueError):
    """Evidence vi phạm quy tắc point-in-time hoặc provenance; `code` là mã máy đọc."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class EvidenceSourceRef:
    """Tham chiếu nguồn market/research kèm thời điểm hệ thống biết nguồn đó."""

    ref: str
    available_at: datetime

    def __post_init__(self) -> None:
        if not self.ref.strip() or self.ref != self.ref.strip():
            raise ValueError("source ref không được rỗng hoặc có khoảng trắng đầu/cuối.")
        _require_aware(self.available_at, "available_at")


@dataclass(frozen=True, slots=True)
class ModelReleaseRef:
    """Model release đã được duyệt cùng thời điểm release bắt đầu khả dụng."""

    release_id: UUID
    available_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, UUID):
            raise ValueError("release_id phải là UUID.")
        _require_aware(self.available_at, "available_at")


@dataclass(frozen=True, slots=True)
class SharedSignalEvidence:
    """Evidence chung: chỉ scope workspace, có provenance hash và cửa sổ hiệu lực."""

    workspace_id: UUID
    evidence_id: UUID
    symbol: str
    setup_type: str
    observed_at: datetime
    available_at: datetime
    validity_end: datetime
    feature_snapshot_id: str
    source_refs: tuple[str, ...]
    model_release_id: UUID | None
    evidence_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.evidence_id, UUID):
            raise ValueError("workspace_id và evidence_id phải là UUID.")
        if self.model_release_id is not None and not isinstance(self.model_release_id, UUID):
            raise ValueError("model_release_id phải là UUID hoặc None.")
        if not self.symbol.strip() or self.symbol != self.symbol.strip() or len(self.symbol) > 64:
            raise ValueError("symbol không được rỗng, có khoảng trắng thừa hoặc dài hơn 64.")
        if not _SETUP_TYPE.fullmatch(self.setup_type):
            raise ValueError("setup_type phải là mã chữ thường [a-z0-9_.:-], tối đa 80 ký tự.")
        for name in ("observed_at", "available_at", "validity_end"):
            value = getattr(self, name)
            _require_aware(value, name)
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.available_at < self.observed_at:
            raise ValueError("available_at không được trước observed_at (nhân quả).")
        if self.validity_end <= self.available_at:
            raise ValueError("validity_end phải sau available_at.")
        if not _SHA256_HEX.fullmatch(self.feature_snapshot_id):
            raise ValueError("feature_snapshot_id phải là SHA-256 hex chữ thường.")
        refs = tuple(self.source_refs)
        if (
            not refs
            or len(set(refs)) != len(refs)
            or any(not ref.strip() or ref != ref.strip() for ref in refs)
        ):
            raise ValueError("source_refs phải không rỗng, không trùng và không có ref trống.")
        object.__setattr__(self, "source_refs", tuple(sorted(refs)))
        expected_hash = _hash(self.canonical_payload())
        if self.evidence_hash != expected_hash:
            raise ValueError("evidence_hash không khớp nội dung canonical.")
        if self.evidence_id != signal_evidence_id(self.workspace_id, self.content_hash):
            raise ValueError("evidence_id không khớp workspace và content_hash của observation.")

    @property
    def content_hash(self) -> str:
        """Hash định danh observation, bỏ `available_at`/`validity_end` do caller cấp.

        Hai lần chạy detector cho cùng feature/source/model/observed_at là retry, nên phải
        cùng ID dù đồng hồ wall-clock khác; revision thật luôn đổi nội dung nên đổi hash.
        `evidence_hash` vẫn phủ cả mốc thời gian để xác minh toàn vẹn bản ghi đã lưu.
        """
        return _hash(_content_payload(self.canonical_payload()))

    @classmethod
    def create(
        cls,
        *,
        workspace_id: UUID,
        symbol: str,
        setup_type: str,
        observed_at: datetime,
        available_at: datetime,
        validity_end: datetime,
        feature_snapshot_id: str,
        source_refs: Sequence[str],
        model_release_id: UUID | None,
    ) -> SharedSignalEvidence:
        """Dựng evidence; hash toàn vẹn từ payload, ID tất định từ content hash observation."""
        for name, value in (
            ("observed_at", observed_at),
            ("available_at", available_at),
            ("validity_end", validity_end),
        ):
            _require_aware(value, name)
        payload = _canonical_payload(
            workspace_id=workspace_id,
            symbol=symbol,
            setup_type=setup_type,
            observed_at=observed_at,
            available_at=available_at,
            validity_end=validity_end,
            feature_snapshot_id=feature_snapshot_id,
            source_refs=tuple(source_refs),
            model_release_id=model_release_id,
        )
        evidence_hash = _hash(payload)
        return cls(
            workspace_id=workspace_id,
            evidence_id=signal_evidence_id(workspace_id, _hash(_content_payload(payload))),
            symbol=symbol,
            setup_type=setup_type,
            observed_at=observed_at,
            available_at=available_at,
            validity_end=validity_end,
            feature_snapshot_id=feature_snapshot_id,
            source_refs=tuple(source_refs),
            model_release_id=model_release_id,
            evidence_hash=evidence_hash,
        )

    @classmethod
    def from_canonical_json(cls, canonical_json: str) -> SharedSignalEvidence:
        """Khôi phục từ canonical JSON đã lưu; hash/ID được tính lại để caller đối chiếu."""
        payload = json.loads(canonical_json)
        if payload.get("schema_version") != SIGNAL_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("Schema version của evidence không được hỗ trợ.")
        model_release = payload["model_release_id"]
        return cls.create(
            workspace_id=UUID(payload["workspace_id"]),
            symbol=payload["symbol"],
            setup_type=payload["setup_type"],
            observed_at=_parse_utc(payload["observed_at"]),
            available_at=_parse_utc(payload["available_at"]),
            validity_end=_parse_utc(payload["validity_end"]),
            feature_snapshot_id=payload["feature_snapshot_id"],
            source_refs=tuple(payload["source_refs"]),
            model_release_id=None if model_release is None else UUID(model_release),
        )

    def canonical_payload(self) -> dict[str, Any]:
        """Payload canonical dùng cho evidence_hash; không có evidence_id vì ID suy ra từ hash."""
        return _canonical_payload(
            workspace_id=self.workspace_id,
            symbol=self.symbol,
            setup_type=self.setup_type,
            observed_at=self.observed_at,
            available_at=self.available_at,
            validity_end=self.validity_end,
            feature_snapshot_id=self.feature_snapshot_id,
            source_refs=self.source_refs,
            model_release_id=self.model_release_id,
        )

    def canonical_json(self) -> str:
        """Serialize theo khóa/thứ tự ổn định để lưu, hash và replay."""
        return _canonical_json(self.canonical_payload())

    def unavailable_reason(self, evaluated_at: datetime) -> str | None:
        """Trả mã lý do nếu evidence không được dùng tại `evaluated_at`, ngược lại None."""
        _require_aware(evaluated_at, "evaluated_at")
        if evaluated_at < self.available_at:
            return "EVIDENCE_NOT_YET_AVAILABLE"
        if evaluated_at >= self.validity_end:
            return "EVIDENCE_EXPIRED"
        return None

    def require_usable_at(self, evaluated_at: datetime) -> None:
        """Fail-closed khi evidence chưa khả dụng hoặc đã hết hiệu lực."""
        reason = self.unavailable_reason(evaluated_at)
        if reason is not None:
            raise SignalEvidenceError(
                reason, "Evidence chỉ dùng được khi available_at <= evaluated_at < validity_end."
            )


def build_shared_signal_evidence(
    *,
    feature_snapshot: FeatureSnapshot,
    setup_type: str,
    observed_at: datetime,
    available_at: datetime,
    validity_end: datetime,
    sources: Sequence[EvidenceSourceRef],
    model_release: ModelReleaseRef | None = None,
) -> SharedSignalEvidence:
    """Dựng evidence từ feature snapshot sau khi kiểm mọi input đã khả dụng.

    `available_at` của evidence phải không sớm hơn thời điểm khả dụng của feature,
    nguồn market/research và model release; nhờ vậy điều kiện
    `evidence.available_at <= evaluated_at` bảo đảm cả input cũng không đến từ tương lai.
    """
    _require_aware(observed_at, "observed_at")
    _require_aware(available_at, "available_at")
    if not feature_snapshot.is_usable:
        raise SignalEvidenceError(
            "FEATURE_SNAPSHOT_UNUSABLE", "Feature snapshot chưa đạt chất lượng để tạo evidence."
        )
    if feature_snapshot.as_of > observed_at:
        raise SignalEvidenceError(
            "FEATURE_AFTER_OBSERVATION", "Feature snapshot chứa dữ liệu sau observed_at."
        )
    if feature_snapshot.computed_at > available_at:
        raise SignalEvidenceError(
            "FEATURE_NOT_AVAILABLE_AT_EVIDENCE",
            "Feature snapshot được tính sau thời điểm evidence khả dụng.",
        )
    if not sources:
        raise SignalEvidenceError(
            "SOURCE_REFS_REQUIRED", "Evidence cần ít nhất một nguồn market/research."
        )
    if any(source.available_at > available_at for source in sources):
        raise SignalEvidenceError(
            "SOURCE_NOT_AVAILABLE_AT_EVIDENCE",
            "Có nguồn (hoặc bản sửa đổi) chỉ khả dụng sau evidence.",
        )
    if model_release is not None and model_release.available_at > available_at:
        raise SignalEvidenceError(
            "MODEL_RELEASE_NOT_AVAILABLE_AT_EVIDENCE",
            "Model release chưa khả dụng tại thời điểm evidence.",
        )
    return SharedSignalEvidence.create(
        workspace_id=feature_snapshot.workspace_id,
        symbol=feature_snapshot.symbol,
        setup_type=setup_type,
        observed_at=observed_at,
        available_at=available_at,
        validity_end=validity_end,
        feature_snapshot_id=feature_snapshot.snapshot_id,
        source_refs=tuple(source.ref for source in sources),
        model_release_id=None if model_release is None else model_release.release_id,
    )


def signal_evidence_id(workspace_id: UUID, content_hash: str) -> UUID:
    """UUID tất định theo workspace + content hash nên cùng observation chỉ có một ID."""
    return uuid5(SIGNAL_EVIDENCE_NAMESPACE, f"{workspace_id}:{content_hash}")


def _content_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Phần định danh observation: bỏ mốc khả dụng/hết hạn mà caller có thể lấy từ wall-clock."""
    return {key: value for key, value in payload.items() if key not in _CALLER_TIMESTAMPS}


def _canonical_payload(
    *,
    workspace_id: UUID,
    symbol: str,
    setup_type: str,
    observed_at: datetime,
    available_at: datetime,
    validity_end: datetime,
    feature_snapshot_id: str,
    source_refs: tuple[str, ...],
    model_release_id: UUID | None,
) -> dict[str, Any]:
    return {
        "available_at": _utc_iso(available_at),
        "feature_snapshot_id": feature_snapshot_id,
        "model_release_id": None if model_release_id is None else str(model_release_id),
        "observed_at": _utc_iso(observed_at),
        "schema_version": SIGNAL_EVIDENCE_SCHEMA_VERSION,
        "setup_type": setup_type,
        "source_refs": sorted(source_refs),
        "symbol": symbol,
        "validity_end": _utc_iso(validity_end),
        "workspace_id": str(workspace_id),
    }


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
