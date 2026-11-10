"""Bản ghi replay/backtest append-only, giới hạn theo workspace."""

import json
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
    from trading_bot.application.backtest_dataset_manifest import BacktestDatasetManifest
    from trading_bot.application.backtest_dataset_replay_bundle import (
        BacktestDatasetReplayBundleReference,
    )


class BacktestRunRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một manifest replay bất biến, định danh bằng hash nội dung."""

    __tablename__ = "backtest_run"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_run_workspace_id"),
        UniqueConstraint("workspace_id", "manifest_hash", name="uq_backtest_run_manifest"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_run_manifest_sha256"),
        CheckConstraint("jsonb_typeof(manifest) = 'object'", name="backtest_run_manifest_object"),
        Index("ix_backtest_run_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_manifest: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestTrialRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lượt thử có trạng thái và số quan sát, kể cả replay bị từ chối."""

    __tablename__ = "backtest_trial"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_trial_workspace_id"),
        UniqueConstraint("workspace_id", "trial_id", name="uq_backtest_trial_trial_id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(trial_id) = 64", name="backtest_trial_id_sha256"),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_trial_manifest_sha256"),
        CheckConstraint(
            "length(parameter_family_hash) = 64", name="backtest_trial_parameter_sha256"
        ),
        CheckConstraint("length(content_hash) = 64", name="backtest_trial_content_sha256"),
        CheckConstraint(
            "trial_count >= 1 AND trial_index >= 0 AND trial_index < trial_count",
            name="backtest_trial_index_valid",
        ),
        CheckConstraint(
            "observation_count >= 0 AND completed_observation_count >= 0 "
            "AND completed_observation_count <= observation_count AND trade_count >= 0",
            name="backtest_trial_counts_valid",
        ),
        CheckConstraint(
            "status IN ('COMPLETED', 'REJECTED', 'FAILED')", name="backtest_trial_status_valid"
        ),
        Index("ix_backtest_trial_workspace_run", "workspace_id", "run_id", "trial_index"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_family_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trial_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_trial: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestReportRecord(UuidPrimaryKey, CreatedAt, Base):
    """Kết quả đánh giá bất biến được nối với đúng run và trial."""

    __tablename__ = "backtest_report"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_report_workspace_id"),
        UniqueConstraint("workspace_id", "report_hash", name="uq_backtest_report_hash"),
        UniqueConstraint("workspace_id", "trial_record_id", name="uq_backtest_report_trial"),
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
        CheckConstraint("length(manifest_hash) = 64", name="backtest_report_manifest_sha256"),
        CheckConstraint("length(report_hash) = 64", name="backtest_report_sha256"),
        CheckConstraint("jsonb_typeof(report) = 'object'", name="backtest_report_object"),
        CheckConstraint(
            "observation_count >= 0 AND usable_observation_count >= 0 "
            "AND usable_observation_count <= observation_count AND trade_count >= 0",
            name="backtest_report_counts_valid",
        ),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="backtest_report_coverage_valid"),
        Index("ix_backtest_report_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    usable_observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[float] = mapped_column(nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestDatasetManifestRecord(UuidPrimaryKey, CreatedAt, Base):
    """Manifest lịch sử bất biến, có checksum và scope workspace."""

    __tablename__ = "backtest_dataset_manifest"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "dataset_hash", name="uq_backtest_dataset_manifest_hash"),
        UniqueConstraint("workspace_id", "id", name="uq_backtest_dataset_manifest_workspace_id"),
        CheckConstraint(
            "dataset_hash ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="backtest_dataset_manifest_hashes_valid",
        ),
        CheckConstraint(
            "status IN ('READY', 'INCOMPLETE')", name="backtest_dataset_manifest_status_valid"
        ),
        CheckConstraint(
            "length(symbol) > 0 AND length(interval) > 0",
            name="backtest_dataset_manifest_identity_valid",
        ),
        CheckConstraint(
            "warmup_start <= evaluation_start AND evaluation_start < range_end",
            name="backtest_dataset_manifest_range_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(manifest_json) = 'object'",
            name="backtest_dataset_manifest_json_object",
        ),
        Index("ix_backtest_dataset_manifest_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_backtest_dataset_manifest_workspace_symbol_range",
            "workspace_id",
            "symbol",
            "range_end",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    warmup_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_manifest: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_manifest(
        cls, *, workspace_id: UUID, manifest: object
    ) -> BacktestDatasetManifestRecord:
        """Lưu canonical payload của application manifest cùng chỉ mục tìm kiếm."""
        from trading_bot.application.backtest_dataset_manifest import BacktestDatasetManifest

        if type(workspace_id) is not UUID or type(manifest) is not BacktestDatasetManifest:
            raise ValueError("workspace_id và manifest phải có kiểu tường minh.")
        if manifest.rights_workspace_id != workspace_id:
            raise ValueError("workspace lưu manifest phải khớp workspace rights evidence.")
        canonical = manifest.canonical_json
        return cls(
            workspace_id=workspace_id,
            dataset_hash=manifest.dataset_hash,
            status=manifest.status,
            symbol=manifest.symbol,
            interval=manifest.interval,
            warmup_start=manifest.warmup_start,
            evaluation_start=manifest.evaluation_start,
            range_end=manifest.range_end,
            prepared_at=manifest.prepared_at,
            policy_fingerprint=manifest.policy_fingerprint,
            manifest_json=json.loads(canonical),
            canonical_manifest=canonical,
        )

    def to_domain(self) -> BacktestDatasetManifest:
        """Khôi phục và kiểm hash canonical cùng các cột chỉ mục đã lưu."""
        from trading_bot.application.backtest_dataset_manifest import (
            BacktestDatasetManifest,
        )

        manifest = BacktestDatasetManifest.from_canonical_json(
            self.canonical_manifest,
            expected_dataset_hash=self.dataset_hash,
        )
        if self.manifest_json != json.loads(self.canonical_manifest):
            raise ValueError("JSONB manifest không khớp canonical bytes.")
        if self.workspace_id != manifest.rights_workspace_id:
            raise ValueError("Workspace row không khớp workspace rights trong manifest.")
        denormalized = (
            self.status == manifest.status,
            self.symbol == manifest.symbol,
            self.interval == manifest.interval,
            self.warmup_start == manifest.warmup_start,
            self.evaluation_start == manifest.evaluation_start,
            self.range_end == manifest.range_end,
            self.prepared_at == manifest.prepared_at,
            self.policy_fingerprint == manifest.policy_fingerprint,
        )
        if not all(denormalized):
            raise ValueError("Cột chỉ mục backtest manifest không khớp canonical bytes.")
        return manifest


class BacktestDatasetReplayBundleRecord(UuidPrimaryKey, CreatedAt, Base):
    """Tham chiếu bundle bất biến, kiểm lại canonical và scope trước khi dùng."""

    __tablename__ = "backtest_dataset_replay_bundle"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "bundle_hash", name="uq_backtest_replay_bundle_hash"),
        CheckConstraint(
            "bundle_hash ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="backtest_replay_bundle_hashes_valid",
        ),
        CheckConstraint("schema_version = 1", name="backtest_replay_bundle_schema_valid"),
        CheckConstraint(
            "length(symbol) > 0 AND length(rights_provider) > 0 AND length(rights_purpose) > 0",
            name="backtest_replay_bundle_scope_valid",
        ),
        CheckConstraint("evaluation_start < range_end", name="backtest_replay_bundle_range_valid"),
        CheckConstraint(
            "jsonb_typeof(bundle_json) = 'object'", name="backtest_replay_bundle_json_object"
        ),
        Index("ix_backtest_replay_bundle_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_backtest_replay_bundle_workspace_symbol_range",
            "workspace_id",
            "symbol",
            "range_end",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rights_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    rights_purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_bundle: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_reference(
        cls, *, workspace_id: UUID, reference: BacktestDatasetReplayBundleReference
    ) -> BacktestDatasetReplayBundleRecord:
        """Lưu tham chiếu canonical cùng chỉ mục, từ chối workspace khác."""
        from trading_bot.application.backtest_dataset_replay_bundle import (
            BacktestDatasetReplayBundleReference,
        )

        if (
            type(workspace_id) is not UUID
            or type(reference) is not BacktestDatasetReplayBundleReference
        ):
            raise ValueError("workspace_id và reference phải có kiểu tường minh.")
        if reference.workspace_id != workspace_id:
            raise ValueError("workspace lưu bundle không khớp reference.")
        canonical = reference.canonical_json
        return cls(
            workspace_id=workspace_id,
            bundle_hash=reference.bundle_hash,
            schema_version=reference.schema_version,
            symbol=reference.symbol,
            evaluation_start=reference.evaluation_start,
            range_end=reference.range_end,
            rights_provider=reference.rights_provider,
            rights_purpose=reference.rights_purpose,
            policy_fingerprint=reference.policy_fingerprint,
            bundle_json=json.loads(canonical),
            canonical_bundle=canonical,
        )

    def to_reference(self) -> BacktestDatasetReplayBundleReference:
        """Khôi phục tham chiếu sau khi kiểm hash, JSONB và tất cả cột scope."""
        from trading_bot.application.backtest_dataset_replay_bundle import (
            BacktestDatasetReplayBundleReference,
        )

        reference = BacktestDatasetReplayBundleReference.from_canonical_json(
            self.canonical_bundle, expected_bundle_hash=self.bundle_hash
        )
        if (
            json.dumps(self.bundle_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            != self.canonical_bundle
        ):
            raise ValueError("JSONB bundle không khớp canonical bytes.")
        if not all(
            (
                self.workspace_id == reference.workspace_id,
                self.schema_version == reference.schema_version,
                self.symbol == reference.symbol,
                self.evaluation_start == reference.evaluation_start,
                self.range_end == reference.range_end,
                self.rights_provider == reference.rights_provider,
                self.rights_purpose == reference.rights_purpose,
                self.policy_fingerprint == reference.policy_fingerprint,
            )
        ):
            raise ValueError("Cột chỉ mục bundle không khớp canonical bytes.")
        return reference
