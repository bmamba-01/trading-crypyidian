"""Gate quyền TRAINING hiện hành cho mọi child của replay bundle persisted."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.application.binance_rest_backfill import BACKTEST_RIGHTS_PROVIDERS
from trading_bot.application.candidate_universe_proof import canonical, digest
from trading_bot.domain.research.source_policy import SourceUsePurpose

SOURCE_ID = "BINANCE_USDM_REST"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ReplayTrainingOperation(StrEnum):
    """Operation TRAINING cần được kiểm quyền riêng theo đầu ra được phép."""

    TRAINING = "training"
    EVALUATION = "evaluation"


@dataclass(frozen=True, slots=True)
class ReplayTrainingRightsReceipt:
    """Snapshot quyền đúng child/purpose, không dùng lại làm quyền hiện hành."""

    workspace_id: UUID
    bundle_hash: str
    child_dataset_hash: str
    interval: str
    symbol: str
    bars_content_hash: str
    product: str
    venue: str
    contract: str
    data_start: datetime
    data_end: datetime
    policy_id: str
    policy_fingerprint: str
    bundle_policy_fingerprint: str
    current_policy_fingerprint: str
    terms_url: str
    verified_at: datetime
    checked_at: datetime
    valid_until: datetime
    license_evidence_id: UUID
    evidence_digest: str
    license_scope_hash: str
    scope_hash: str
    operation: ReplayTrainingOperation
    purpose: SourceUsePurpose = SourceUsePurpose.TRAINING
    provider: str = SOURCE_ID

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID or type(self.license_evidence_id) is not UUID:
            raise ValueError("Receipt cần workspace/evidence UUID authoritative.")
        if self.provider not in BACKTEST_RIGHTS_PROVIDERS or self.policy_fingerprint != (
            self.current_policy_fingerprint
        ):
            raise ValueError("Receipt provider nằm ngoài allowlist scorer backtest.")
        if self.purpose is not SourceUsePurpose.TRAINING or not isinstance(
            self.operation, ReplayTrainingOperation
        ):
            raise ValueError("Replay receipt cần purpose TRAINING và operation cụ thể.")
        if any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in (
                self.bundle_hash,
                self.child_dataset_hash,
                self.bars_content_hash,
                self.policy_fingerprint,
                self.bundle_policy_fingerprint,
                self.current_policy_fingerprint,
                self.evidence_digest,
                self.license_scope_hash,
                self.scope_hash,
            )
        ):
            raise ValueError("Receipt replay chứa hash sai định dạng.")
        if any(
            not isinstance(getattr(self, name), str) or not getattr(self, name).strip()
            for name in ("product", "venue", "contract", "policy_id", "symbol", "interval")
        ):
            raise ValueError("Receipt product/venue/contract/scope không được để trống.")
        terms = urlsplit(self.terms_url)
        if terms.scheme != "https" or not terms.netloc:
            raise ValueError("terms_url receipt cần HTTPS đầy đủ.")
        for name in ("data_start", "data_end", "verified_at", "checked_at", "valid_until"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} receipt cần múi giờ.")
        if not (
            self.data_start < self.data_end
            and self.verified_at <= self.checked_at < self.valid_until
        ):
            raise ValueError("Lease receipt phải thỏa verified <= checked < valid_until.")

    @property
    def scope_payload(self) -> dict[str, object]:
        """Scope ổn định cho retry; thời điểm lease được lưu riêng trong receipt."""
        return {
            "workspace_id": str(self.workspace_id),
            "bundle_hash": self.bundle_hash,
            "child_dataset_hash": self.child_dataset_hash,
            "interval": self.interval,
            "symbol": self.symbol,
            "bars_content_hash": self.bars_content_hash,
            "product": self.product,
            "venue": self.venue,
            "contract": self.contract,
            "data_start": self.data_start.isoformat(),
            "data_end": self.data_end.isoformat(),
            "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint,
            "bundle_policy_fingerprint": self.bundle_policy_fingerprint,
            "current_policy_fingerprint": self.current_policy_fingerprint,
            "terms_url": self.terms_url,
            "verified_at": self.verified_at.isoformat(),
            "license_evidence_id": str(self.license_evidence_id),
            "evidence_digest": self.evidence_digest,
            "license_scope_hash": self.license_scope_hash,
            "scope_hash": self.scope_hash,
            "operation": self.operation.value,
            "purpose": self.purpose.value,
            "provider": self.provider,
        }

    @property
    def canonical_json(self) -> str:
        """Payload evidence đầy đủ, bao gồm DB clock và hạn lease."""
        return canonical(
            {
                **self.scope_payload,
                "checked_at": self.checked_at.isoformat(),
                "valid_until": self.valid_until.isoformat(),
            }
        )


class DatabaseReplayTrainingRightsGate:
    """Chỉ đọc metadata/registry; không nạp bars, outcome hoặc label."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authorize(
        self,
        *,
        workspace_id: UUID,
        bundle_hash: str,
        operation: ReplayTrainingOperation,
    ) -> tuple[ReplayTrainingRightsReceipt, ...]:
        """Lấy receipt metadata từ verifier SECURITY DEFINER; không đọc evidence ledger."""
        if type(workspace_id) is not UUID or _SHA256.fullmatch(bundle_hash) is None:
            raise ValueError("Workspace/bundle hash không hợp lệ.")
        if not isinstance(operation, ReplayTrainingOperation):
            raise ValueError("Replay rights gate cần operation TRAINING tường minh.")
        result = await self._session.execute(
            text(
                "SELECT * FROM public.wp06_authorize_replay_training_bundle("
                "CAST(:workspace_id AS uuid), CAST(:bundle_hash AS text), "
                "CAST(:operation AS text))"
            ),
            {
                "workspace_id": str(workspace_id),
                "bundle_hash": bundle_hash,
                "operation": operation.value,
            },
        )
        rows = tuple(result.mappings().all())
        if not rows:
            raise PermissionError("Verifier không cấp current TRAINING scope cho bundle.")
        receipts = tuple(_receipt_from_authorized_row(row, operation=operation) for row in rows)
        children = tuple(item.child_dataset_hash for item in receipts)
        if len(set(children)) != len(children) or receipts != tuple(
            sorted(receipts, key=lambda item: item.child_dataset_hash)
        ):
            raise PermissionError("Verifier trả child receipt trùng hoặc không canonical.")
        if any(
            receipt.workspace_id != workspace_id
            or receipt.bundle_hash != bundle_hash
            or receipt.operation is not operation
            for receipt in receipts
        ):
            raise PermissionError("Verifier trả receipt vượt bundle/workspace/operation.")
        return receipts


def _receipt_from_authorized_row(
    row: Any, *, operation: ReplayTrainingOperation
) -> ReplayTrainingRightsReceipt:
    """Khôi phục metadata đã cấp bởi SQL verifier, từ chối kiểu hoặc hash lệch."""
    try:
        return ReplayTrainingRightsReceipt(
            workspace_id=_required_row_value(row, "workspace_id", UUID),
            bundle_hash=_required_row_value(row, "bundle_hash", str),
            child_dataset_hash=_required_row_value(row, "child_dataset_hash", str),
            interval=_required_row_value(row, "interval", str),
            symbol=_required_row_value(row, "symbol", str),
            bars_content_hash=_required_row_value(row, "bars_content_hash", str),
            product=_required_row_value(row, "product", str),
            venue=_required_row_value(row, "venue", str),
            contract=_required_row_value(row, "contract", str),
            data_start=_required_row_value(row, "data_start", datetime),
            data_end=_required_row_value(row, "data_end", datetime),
            policy_id=_required_row_value(row, "policy_id", str),
            policy_fingerprint=_required_row_value(row, "policy_fingerprint", str),
            bundle_policy_fingerprint=_required_row_value(row, "bundle_policy_fingerprint", str),
            current_policy_fingerprint=_required_row_value(row, "current_policy_fingerprint", str),
            terms_url=_required_row_value(row, "terms_url", str),
            verified_at=_required_row_value(row, "verified_at", datetime),
            checked_at=_required_row_value(row, "checked_at", datetime),
            valid_until=_required_row_value(row, "valid_until", datetime),
            license_evidence_id=_required_row_value(row, "license_evidence_id", UUID),
            evidence_digest=_required_row_value(row, "evidence_digest", str),
            license_scope_hash=_required_row_value(row, "license_scope_hash", str),
            scope_hash=_required_row_value(row, "scope_hash", str),
            operation=operation,
            purpose=SourceUsePurpose(_required_row_value(row, "purpose", str)),
            provider=_required_row_value(row, "provider", str),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PermissionError("Verifier trả receipt thiếu field hoặc sai schema.") from error


def _required_row_value[T](row: Any, key: str, expected_type: type[T]) -> T:
    """Đọc cột metadata với kiểu dữ liệu DB bắt buộc."""
    value = row[key]
    if not isinstance(value, expected_type):
        raise ValueError(f"Verifier trả cột {key} sai kiểu dữ liệu.")
    return value


def training_scope_digest(receipts: tuple[ReplayTrainingRightsReceipt, ...]) -> str:
    """Gắn mọi child/policy/purpose vào proof mà không lặp lại payload quyền."""
    return digest(
        canonical(
            [
                receipt.scope_payload
                for receipt in sorted(receipts, key=lambda item: item.child_dataset_hash)
            ]
        )
    )
