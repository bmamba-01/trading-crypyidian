"""Bản ghi scenario và kết quả trade của replay, bất biến theo workspace."""

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

if TYPE_CHECKING:
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput


class BacktestScenarioRecord(UuidPrimaryKey, CreatedAt, Base):
    """Scenario và override đã dùng để tạo đúng trial."""

    __tablename__ = "backtest_scenario"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_scenario_workspace_id"),
        UniqueConstraint("workspace_id", "trial_record_id", name="uq_backtest_scenario_trial"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_scenario_manifest_sha256"),
        CheckConstraint("length(scenario_hash) = 64", name="backtest_scenario_sha256"),
        CheckConstraint("kind IN ('BASE', 'COST', 'PARAMETER')", name="backtest_scenario_kind"),
        CheckConstraint("jsonb_typeof(scenario) = 'object'", name="backtest_scenario_object"),
        Index("ix_backtest_scenario_workspace_run", "workspace_id", "run_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scenario_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_scenario: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestTradeOutcomeRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một giao dịch đóng trong replay, lưu theo thứ tự canonical."""

    __tablename__ = "backtest_trade_outcome"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "trial_record_id", "ordinal", name="uq_backtest_outcome_ordinal"
        ),
        UniqueConstraint(
            "workspace_id", "trial_record_id", "trade_id", name="uq_backtest_outcome_trade"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_outcome_manifest_sha256"),
        CheckConstraint("length(outcome_hash) = 64", name="backtest_outcome_sha256"),
        CheckConstraint("ordinal >= 0", name="backtest_outcome_ordinal_nonnegative"),
        CheckConstraint("jsonb_typeof(outcome) = 'object'", name="backtest_outcome_object"),
        Index(
            "ix_backtest_outcome_workspace_trial",
            "workspace_id",
            "trial_record_id",
            "ordinal",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_id: Mapped[str] = mapped_column(String(240), nullable=False)
    outcome_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestRunDatasetRightsRecord(UuidPrimaryKey, CreatedAt, Base):
    """Bằng chứng quyền tại lần chạy; không cấp quyền replay hiện hành."""

    __tablename__ = "backtest_run_dataset_rights"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "run_id",
            "child_dataset_hash",
            name="uq_backtest_run_dataset_rights_child",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "bundle_hash ~ '^[0-9a-f]{64}$' AND child_dataset_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_fingerprint ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="backtest_run_dataset_rights_hashes_valid",
        ),
        CheckConstraint(
            "length(interval) > 0 AND length(provider) > 0 AND length(purpose) > 0 "
            "AND length(terms_url) > 0",
            name="backtest_run_dataset_rights_scope_valid",
        ),
        CheckConstraint(
            "verified_at <= checked_at AND checked_at < valid_until",
            name="backtest_run_dataset_rights_lease_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object'", name="backtest_run_dataset_rights_json_object"
        ),
        Index("ix_backtest_run_dataset_rights_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    child_dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    terms_url: Mapped[str] = mapped_column(Text, nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_evidence: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @classmethod
    def from_bundle_component(
        cls,
        *,
        workspace_id: UUID,
        run_id: UUID,
        bundle_hash: str,
        component: BacktestDatasetReplayInput,
    ) -> BacktestRunDatasetRightsRecord:
        """Gắn bằng chứng hiện hành đã kiểm bằng đồng hồ DB với child và run."""
        from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
        from trading_bot.application.binance_rest_backfill import BackfillRightsEvidence

        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if type(run_id) is not UUID:
            raise ValueError("run_id phải là UUID.")
        if not isinstance(bundle_hash, str) or re.fullmatch(r"[0-9a-f]{64}", bundle_hash) is None:
            raise ValueError("bundle_hash phải là SHA-256 dạng hex thường.")
        if type(component) is not BacktestDatasetReplayInput:
            raise ValueError("Component quyền phải là replay input đã xác thực.")
        manifest = component.manifest
        manifest.require_ready()
        rights = component.current_rights
        if type(rights) is not BackfillRightsEvidence or (
            workspace_id != manifest.rights_workspace_id
            or rights.workspace_id != workspace_id
            or rights.provider != manifest.rights_provider
            or rights.purpose.value != manifest.rights_purpose
            or rights.policy_fingerprint != manifest.policy_fingerprint
        ):
            raise ValueError("Bằng chứng quyền không khớp workspace hoặc policy manifest.")
        if rights.checked_at is None:
            raise ValueError("checked_at từ đồng hồ database là bắt buộc cho biên nhận quyền.")
        payload = {
            "schema_version": 1,
            "workspace_id": str(workspace_id),
            "run_id": str(run_id),
            "bundle_hash": bundle_hash,
            "child_dataset_hash": manifest.dataset_hash,
            "interval": manifest.interval,
            "provider": rights.provider,
            "purpose": rights.purpose.value,
            "terms_url": rights.terms_url,
            "policy_fingerprint": rights.policy_fingerprint,
            "verified_at": rights.verified_at.isoformat(),
            "checked_at": rights.checked_at.isoformat(),
            "valid_until": rights.valid_until.isoformat(),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            workspace_id=workspace_id,
            run_id=run_id,
            bundle_hash=bundle_hash,
            child_dataset_hash=manifest.dataset_hash,
            interval=manifest.interval,
            provider=rights.provider,
            purpose=rights.purpose.value,
            terms_url=rights.terms_url,
            policy_fingerprint=rights.policy_fingerprint,
            verified_at=rights.verified_at,
            checked_at=rights.checked_at,
            valid_until=rights.valid_until,
            evidence_json=payload,
            canonical_evidence=canonical,
            evidence_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
