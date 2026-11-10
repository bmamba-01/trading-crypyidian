"""Gate quyền TRAINING hiện hành cho mọi child của replay bundle persisted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
    BacktestDatasetReplayBundleRepository,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.candidate_universe_proof import canonical, digest
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_registry import source_rights_hash

SOURCE_ID = "BINANCE_USDM_REST"


@dataclass(frozen=True, slots=True)
class ReplayTrainingRightsReceipt:
    """Snapshot quyền đúng child/purpose, không dùng lại làm quyền hiện hành."""

    workspace_id: UUID
    bundle_hash: str
    child_dataset_hash: str
    interval: str
    symbol: str
    bars_content_hash: str
    policy_id: str
    policy_fingerprint: str
    terms_url: str
    verified_at: datetime
    checked_at: datetime
    valid_until: datetime
    purpose: SourceUsePurpose = SourceUsePurpose.TRAINING
    provider: str = SOURCE_ID

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
            "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint,
            "terms_url": self.terms_url,
            "verified_at": self.verified_at.isoformat(),
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
        self, *, workspace_id: UUID, bundle_hash: str
    ) -> tuple[ReplayTrainingRightsReceipt, ...]:
        """Caller phải giữ source advisory xact lock tới lúc proof commit."""
        reference = await BacktestDatasetReplayBundleRepository(self._session).get_by_hash(
            workspace_id=workspace_id, bundle_hash=bundle_hash
        )
        if (
            reference is None
            or reference.workspace_id != workspace_id
            or reference.bundle_hash != bundle_hash
            or reference.rights_provider != SOURCE_ID
        ):
            raise PermissionError("Thiếu replay bundle authoritative đúng provider/workspace/hash.")
        registry = SourceRegistryRepository(self._session)
        observed = await registry.get_observed_entry(workspace_id=workspace_id, source_id=SOURCE_ID)
        now = await registry.database_clock(workspace_id=workspace_id)
        if observed is None or now.tzinfo is None or now.utcoffset() is None:
            raise PermissionError("Thiếu policy observer hoặc DB clock có múi giờ.")
        now = now.astimezone(UTC)
        current = observed.history.current
        policy = current.policy
        known_at = observed.known_at.get(policy.policy_id)
        terms = urlsplit(policy.evidence_ref or "")
        if (
            observed.history.source_id != SOURCE_ID
            or policy.source_id != SOURCE_ID
            or known_at is None
            or known_at >= now
            or not observed.history.permits(SourceUsePurpose.TRAINING, at=now)
            or terms.scheme != "https"
            or not terms.netloc
            or policy.reviewed_at is None
        ):
            raise PermissionError(
                "Policy hiện hành không cấp TRAINING với observer/evidence hợp lệ."
            )
        valid_until = min(
            now + timedelta(minutes=5), policy.valid_until or now + timedelta(minutes=5)
        )
        receipts = []
        for component in reference.components:
            child = await BacktestDatasetManifestRepository(self._session).get_by_hash(
                workspace_id=workspace_id, dataset_hash=component.dataset_hash
            )
            if child is None:
                raise PermissionError(
                    "Thiếu child manifest authoritative; không cấp quyền từng phần."
                )
            child.require_ready()
            if (
                child.rights_workspace_id != workspace_id
                or child.rights_provider != SOURCE_ID
                or child.dataset_hash != component.dataset_hash
                or child.interval != component.interval
                or child.bars_content_hash != component.bars_content_hash
                or child.symbol != reference.symbol
                or child.evaluation_start != reference.evaluation_start
                or child.range_end != reference.range_end
                or child.policy_fingerprint != reference.policy_fingerprint
                or child.rights_purpose != reference.rights_purpose
            ):
                raise PermissionError("Child manifest không khớp toàn bộ scope bundle persisted.")
            receipts.append(
                ReplayTrainingRightsReceipt(
                    workspace_id=workspace_id,
                    bundle_hash=bundle_hash,
                    child_dataset_hash=child.dataset_hash,
                    interval=child.interval,
                    symbol=child.symbol,
                    bars_content_hash=child.bars_content_hash,
                    policy_id=policy.policy_id,
                    policy_fingerprint=source_rights_hash(current),
                    terms_url=policy.evidence_ref or "",
                    verified_at=policy.reviewed_at,
                    checked_at=now,
                    valid_until=valid_until,
                )
            )
        return tuple(receipts)


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
