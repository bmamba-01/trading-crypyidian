"""Bằng chứng coverage bất biến, không lưu feature/label payload."""

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class CandidateUniverseProofRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một proof duy nhất theo trial/query version trong workspace."""

    __tablename__ = "candidate_universe_proof"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "query_version",
            name="uq_candidate_universe_proof_trial_query",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_candidate_universe_proof_workspace"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "report_id"],
            ["backtest_report.workspace_id", "backtest_report.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["backtest_scenario.workspace_id", "backtest_scenario.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "candidate_count >= 0 AND eligible_count >= 0 AND exclusion_count >= 0 AND "
            "candidate_count = eligible_count + exclusion_count",
            name="candidate_universe_proof_counts_valid",
        ),
        CheckConstraint(
            "proof_hash ~ '^[0-9a-f]{64}$' AND membership_digest ~ '^[0-9a-f]{64}$' AND "
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="candidate_universe_proof_hashes_valid",
        ),
        CheckConstraint(
            (
                "query_version IN ('base-closed-trade-universe-v1','base-decision-oppor"
                "tunity-universe-v1')"
            ),
            name="candidate_universe_proof_query_valid",
        ),
        CheckConstraint(
            (
                "(query_version = 'base-closed-trade-universe-v1' AND opportunity_count"
                " IS NULL AND no_candidate_count IS NULL AND not_run_count IS NULL AND "
                "input_excluded_count IS NULL AND unlabeled_candidate_count IS NULL AND"
                " outcome_link_count IS NULL AND source_scope_hash IS NULL AND complete"
                "ness_verified IS NULL) OR (query_version = 'base-decision-opportunity-"
                "universe-v1' AND opportunity_count IS NOT NULL AND no_candidate_count "
                "IS NOT NULL AND not_run_count IS NOT NULL AND input_excluded_count IS "
                "NOT NULL AND unlabeled_candidate_count IS NOT NULL AND outcome_link_co"
                "unt IS NOT NULL AND source_scope_hash IS NOT NULL AND completeness_ver"
                "ified IS NOT NULL AND opportunity_count >= 0 AND no_candidate_count >="
                " 0 AND not_run_count >= 0 AND input_excluded_count >= 0 AND unlabeled_"
                "candidate_count >= 0 AND outcome_link_count >= 0 AND opportunity_count"
                " = candidate_count + no_candidate_count + not_run_count AND input_excl"
                "uded_count <= not_run_count AND unlabeled_candidate_count <= exclusion"
                "_count AND outcome_link_count <= candidate_count AND source_scope_hash"
                " ~ '^[0-9a-f]{64}$' AND (NOT completeness_verified OR input_excluded_c"
                "ount = 0))"
            ),
            name="candidate_universe_proof_v2_fields_valid",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    report_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    scenario_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    query_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exclusion_count: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_proof: Mapped[str] = mapped_column(Text, nullable=False)
    opportunity_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    no_candidate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    not_run_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_excluded_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unlabeled_candidate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_link_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_scope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completeness_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class CandidateTrainingRightsRecord(UuidPrimaryKey, CreatedAt, Base):
    """Biên nhận TRAINING của từng child tại lần proof đầu tiên, bất biến."""

    __tablename__ = "candidate_training_rights"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "proof_id",
            "child_dataset_hash",
            name="uq_candidate_training_rights_child",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            ["candidate_universe_proof.workspace_id", "candidate_universe_proof.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("purpose = 'training'", name="candidate_training_rights_purpose_valid"),
        CheckConstraint(
            "child_dataset_hash ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ "
            "'^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="candidate_training_rights_hashes_valid",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    proof_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    child_dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_evidence: Mapped[str] = mapped_column(Text, nullable=False)
