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
        CheckConstraint(
            "(entry_decision_snapshot_id IS NULL AND entry_decision_at IS NULL AND "
            "entry_feature_schema_hash IS NULL AND entry_features IS NULL AND "
            "entry_features_hash IS NULL AND entry_symbol IS NULL) OR "
            "(entry_decision_snapshot_id IS NOT NULL AND "
            "length(btrim(entry_decision_snapshot_id)) > 0 AND entry_decision_at IS NOT NULL "
            "AND entry_feature_schema_hash IS NOT NULL AND entry_features IS NOT NULL AND "
            "entry_features_hash IS NOT NULL AND entry_symbol IS NOT NULL AND entry_symbol ~ "
            "'^[A-Z0-9_]{2,32}$' AND entry_feature_schema_hash ~ '^[0-9a-f]{64}$' AND "
            "entry_features_hash ~ '^[0-9a-f]{64}$' AND "
            "public.wp06_entry_features_valid(entry_features))",
            name="entry_provenance_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id", "entry_opportunity_event_id"],
            [
                "backtest_decision_opportunity.workspace_id",
                "backtest_decision_opportunity.trial_record_id",
                "backtest_decision_opportunity.event_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "entry_candidate_id",
            name="uq_backtest_outcome_entry_candidate",
        ),
        CheckConstraint(
            (
                "(entry_opportunity_event_id IS NULL AND entry_candidate_id IS NULL AND"
                " entry_vector_hash IS NULL) OR (entry_opportunity_event_id IS NOT NULL"
                " AND length(btrim(entry_opportunity_event_id)) > 0 AND entry_candidate"
                "_id IS NOT NULL AND entry_vector_hash IS NOT NULL AND entry_decision_s"
                "napshot_id IS NOT NULL AND entry_candidate_id ~ '^[0-9a-f]{64}$' AND e"
                "ntry_vector_hash ~ '^[0-9a-f]{64}$')"
            ),
            name="outcome_opportunity_link_valid",
        ),
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
    entry_decision_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    entry_feature_schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_features: Mapped[list[list[Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    entry_features_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_opportunity_event_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    entry_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_vector_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
