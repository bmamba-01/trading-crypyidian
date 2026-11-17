"""Dựng candidate manifest/fold plan từ bằng chứng replay authoritative."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from trading_bot.application.candidate_universe_proof import (
    CandidateUniverseProof,
    canonical,
    digest,
)
from trading_bot.application.learning_fold_plan import (
    ManifestFoldPlan,
    build_manifest_fold_plan,
)
from trading_bot.domain.learning.dataset import (
    DatasetExclusion,
    DatasetManifest,
    DatasetSample,
    DatasetSplit,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade, WalkForwardSplit
from trading_bot.domain.learning.source_use_lineage import (
    SourceUseLineage,
    source_use_lineage_set_hash,
)
from trading_bot.domain.learning.trade_labels import (
    NET_PNL_SIGN_V1,
    SUPPORTED_LABEL_VERSIONS,
    trade_label_value,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from trading_bot.application.learning_fold_samples import NormalizedLearningDataset


SPLIT_POLICY_VERSION = "wp06-walk-forward-v1"
LABEL_VERSION = NET_PNL_SIGN_V1
SAMPLE_SCHEMA_VERSION = "wp06-entry-trade-v1"
FEATURE_VERSION = "runtime-entry-snapshot-v1"
RETENTION_POLICY_REF = "wp06-training-retention-v1"
MAX_SPLIT_FOLDS = 32


@dataclass(frozen=True, slots=True)
class LearningCandidateOutcome:
    """Một outcome row đã được repository kiểm tra canonical/hash."""

    workspace_id: UUID
    trial_record_id: UUID
    record_id: UUID
    ordinal: int
    outcome_hash: str
    trade: EvaluationTrade
    source_replay_bundle_hash: str | None = None
    source_use_lineage: tuple[SourceUseLineage, ...] = ()

    def __post_init__(self) -> None:
        value = self.source_replay_bundle_hash
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("source_replay_bundle_hash phải là SHA-256 hoặc null.")
        lineage = tuple(self.source_use_lineage)
        if any(type(item) is not SourceUseLineage for item in lineage):
            raise ValueError("source_use_lineage cần SourceUseLineage typed.")
        if lineage != tuple(sorted(lineage, key=lambda item: item.child_dataset_hash)):
            raise ValueError("source_use_lineage phải sắp xếp theo child hash.")
        if len({item.child_dataset_hash for item in lineage}) != len(lineage):
            raise ValueError("source_use_lineage không được lặp child.")
        if value is None and lineage:
            raise ValueError("Source-use lineage không được tồn tại khi thiếu bundle.")
        if value is not None and (
            not lineage or any(item.bundle_hash != value for item in lineage)
        ):
            raise ValueError("Source-use lineage phải khớp bundle và không rỗng.")
        object.__setattr__(self, "source_use_lineage", lineage)


@dataclass(frozen=True, slots=True)
class LearningCandidateTrial:
    """Một trial BASE (một job replay = một bundle/coin) trong cohort của candidate."""

    trial_record_id: UUID
    proof_record_id: UUID
    proof: CandidateUniverseProof
    outcomes: tuple[LearningCandidateOutcome, ...]


@dataclass(frozen=True, slots=True)
class LearningCandidateTrialSource:
    """Metadata trial nguồn đã ghim của candidate (không chứa outcome payload)."""

    trial_record_id: UUID
    proof_record_id: UUID
    proof_hash: str
    bundle_hash: str

    def __post_init__(self) -> None:
        if type(self.trial_record_id) is not UUID or type(self.proof_record_id) is not UUID:
            raise ValueError("Trial source cần UUID trial/proof.")
        _sha256(self.proof_hash, "proof_hash")
        _sha256(self.bundle_hash, "bundle_hash")


@dataclass(frozen=True, slots=True)
class LearningSplitCandidateConfig:
    """Holdout cutoff và các cửa sổ do operator cấu hình ngoài giao diện web."""

    holdout_start: datetime
    evaluation_cutoff: datetime
    folds: tuple[WalkForwardSplit, ...]
    # AI-5: phiên bản nhãn; mặc định giữ hợp đồng cũ (dấu net PnL).
    label_version: str = LABEL_VERSION

    def __post_init__(self) -> None:
        if self.label_version not in SUPPORTED_LABEL_VERSIONS:
            raise ValueError("label_version không được hỗ trợ.")
        _aware(self.holdout_start, "holdout_start")
        _aware(self.evaluation_cutoff, "evaluation_cutoff")
        if (
            type(self.folds) is not tuple
            or not self.folds
            or len(self.folds) > MAX_SPLIT_FOLDS
            or any(type(fold) is not WalkForwardSplit for fold in self.folds)
        ):
            raise ValueError(f"folds cần 1–{MAX_SPLIT_FOLDS} WalkForwardSplit.")


@dataclass(frozen=True, slots=True)
class LearningSplitCandidate:
    """Candidate private gồm manifest canonical, fold plan và preview metadata."""

    candidate_id: UUID
    workspace_id: UUID
    trial_record_id: UUID
    proof_record_id: UUID
    proof_hash: str
    manifest: DatasetManifest
    fold_plan: ManifestFoldPlan
    config: LearningSplitCandidateConfig
    preview_json: str
    # AI-5: mọi trial của cohort, sắp theo trial id; phần tử đầu là "neo" (= trường scalar).
    sources: tuple[LearningCandidateTrialSource, ...] = ()

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        if sources and (
            any(type(item) is not LearningCandidateTrialSource for item in sources)
            or sources != tuple(sorted(sources, key=lambda item: str(item.trial_record_id)))
            or len({item.trial_record_id for item in sources}) != len(sources)
            or sources[0].trial_record_id != self.trial_record_id
            or sources[0].proof_record_id != self.proof_record_id
            or sources[0].proof_hash != self.proof_hash
        ):
            raise ValueError("Trial sources phải sắp theo trial id, không lặp, neo khớp scalar.")
        object.__setattr__(self, "sources", sources)
        if any(
            type(value) is not UUID
            for value in (
                self.candidate_id,
                self.workspace_id,
                self.trial_record_id,
                self.proof_record_id,
            )
        ):
            raise ValueError("Candidate identifiers phải là UUID.")
        if (
            type(self.manifest) is not DatasetManifest
            or type(self.fold_plan) is not ManifestFoldPlan
        ):
            raise ValueError("Candidate cần manifest và fold plan authoritative.")
        if (
            self.manifest.workspace_id != self.workspace_id
            or self.fold_plan.manifest_hash != self.manifest.content_hash
            or self.proof_hash != _sha256(self.proof_hash, "proof_hash")
        ):
            raise ValueError("Candidate lineage/hash không khớp.")
        preview = json.loads(self.preview_json)
        if type(preview) is not dict or canonical(preview) != self.preview_json:
            raise ValueError("preview_json phải là JSON canonical object.")

    @property
    def canonical_configuration(self) -> str:
        """Cấu hình tái dựng plan; không chứa sample identity hoặc payload."""
        payload: dict[str, object] = {
            "evaluation_cutoff": self.config.evaluation_cutoff.isoformat(),
            "folds": [_split_payload(fold) for fold in self.config.folds],
            "holdout_start": self.config.holdout_start.isoformat(),
            "split_policy_version": SPLIT_POLICY_VERSION,
        }
        if self.config.label_version != LABEL_VERSION:
            payload["label_version"] = self.config.label_version
        return canonical(payload)


@dataclass(frozen=True, slots=True)
class LearningSplitCandidateStageResult:
    """Kết quả stage chỉ metadata, không chứa manifest/sample payload."""

    candidate_id: UUID
    proof_record_id: UUID
    manifest_hash: str
    fold_plan_hash: str
    preview_json: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class LearningSplitCandidateSealResult:
    """Kết quả seal đã lưu manifest và normalized folds."""

    candidate_id: UUID
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_plan_hash: str
    approval_hash: str
    duplicate: bool


def build_learning_split_candidate(
    *,
    workspace_id: UUID,
    trial_record_id: UUID,
    proof_record_id: UUID,
    proof: CandidateUniverseProof,
    outcomes: tuple[LearningCandidateOutcome, ...],
    config: LearningSplitCandidateConfig,
    as_of: datetime,
) -> LearningSplitCandidate:
    """Dựng manifest từ rows DB đã hash-verify của một trial (hash giữ nguyên hợp đồng cũ)."""
    if any(type(value) is not UUID for value in (workspace_id, trial_record_id, proof_record_id)):
        raise ValueError("Workspace/trial/proof phải là UUID.")
    return build_learning_split_cohort_candidate(
        workspace_id=workspace_id,
        trials=(
            LearningCandidateTrial(
                trial_record_id=trial_record_id,
                proof_record_id=proof_record_id,
                proof=proof,
                outcomes=tuple(outcomes),
            ),
        ),
        config=config,
        as_of=as_of,
    )


def build_learning_split_cohort_candidate(
    *,
    workspace_id: UUID,
    trials: tuple[LearningCandidateTrial, ...],
    config: LearningSplitCandidateConfig,
    as_of: datetime,
) -> LearningSplitCandidate:
    """Dựng manifest từ N trial (mỗi trial một bundle/coin) cùng release/schema (AI-5).

    Mỗi trial được kiểm với candidate-universe proof của chính nó (ordinal, membership, đếm).
    N=1 cho đúng ``query_hash``/``candidate_id`` như builder một trial trước đây.
    """
    if type(workspace_id) is not UUID:
        raise ValueError("Workspace phải là UUID.")
    if type(config) is not LearningSplitCandidateConfig:
        raise ValueError("Cần split configuration typed.")
    _aware(as_of, "as_of")
    if (
        type(trials) is not tuple
        or not trials
        or any(type(item) is not LearningCandidateTrial for item in trials)
    ):
        raise ValueError("Cần ít nhất một LearningCandidateTrial.")
    ordered_trials = tuple(sorted(trials, key=lambda item: str(item.trial_record_id)))
    if len({item.trial_record_id for item in ordered_trials}) != len(ordered_trials):
        raise ValueError("Trial bị lặp trong cohort.")
    if len({item.proof_record_id for item in ordered_trials}) != len(ordered_trials):
        raise ValueError("Proof bị lặp trong cohort.")

    accepted: list[tuple[LearningCandidateOutcome, DatasetSample]] = []
    exclusions: list[DatasetExclusion] = []
    schemas: set[str] = set()
    symbols: set[str] = set()
    query_versions: set[str] = set()
    trial_bundles: list[str] = []
    lineage_by_child: dict[str, SourceUseLineage] = {}
    pending: list[tuple[LearningCandidateOutcome, bool]] = []
    all_ids: set[UUID] = set()
    for trial in ordered_trials:
        proof = trial.proof
        if type(trial.trial_record_id) is not UUID or type(trial.proof_record_id) is not UUID:
            raise ValueError("Workspace/trial/proof phải là UUID.")
        if type(proof) is not CandidateUniverseProof:
            raise ValueError("Cần proof và split configuration typed.")
        if (
            proof.workspace_id != workspace_id
            or proof.trial_record_id != trial.trial_record_id
            or proof.training_authorized is not False
        ):
            raise ValueError("Proof không khớp workspace/trial hoặc đang cấp quyền ngoài hợp đồng.")
        query_versions.add(proof.query_version)
        ordered = tuple(sorted(trial.outcomes, key=lambda item: item.ordinal))
        if not ordered or len(ordered) != proof.candidate_count:
            raise ValueError("Outcome rows không bao phủ đúng candidate proof.")
        if any(
            type(item) is not LearningCandidateOutcome
            or item.workspace_id != workspace_id
            or item.trial_record_id != trial.trial_record_id
            or item.ordinal != expected
            for expected, item in enumerate(ordered)
        ):
            raise ValueError("Outcome rows vượt workspace/trial hoặc ordinal không liên tục.")
        row_ids = {item.record_id for item in ordered}
        if len(row_ids) != len(ordered) or row_ids & all_ids:
            raise ValueError("Outcome record ID bị lặp.")
        all_ids |= row_ids
        bundle_hashes = {item.source_replay_bundle_hash for item in ordered}
        if None in bundle_hashes or len(bundle_hashes) != 1:
            raise ValueError("Mọi outcome phải ghim đúng một replay bundle authoritative.")
        bundle_hash = next(iter(bundle_hashes))
        assert isinstance(bundle_hash, str)
        if bundle_hash in trial_bundles:
            raise ValueError("Hai trial của cohort dùng cùng một replay bundle.")
        trial_bundles.append(bundle_hash)
        lineage_values = {item.source_use_lineage for item in ordered}
        if len(lineage_values) != 1:
            raise ValueError("Mọi outcome phải ghim cùng exact source-use child lineage.")
        trial_lineage = next(iter(lineage_values))
        if not trial_lineage:
            raise ValueError("Mọi outcome cần source-use lineage không rỗng.")
        for entry in trial_lineage:
            if lineage_by_child.setdefault(entry.child_dataset_hash, entry) != entry:
                raise ValueError("Child lineage trùng hash nhưng khác nội dung giữa các trial.")

        members: list[dict[str, object]] = []
        eligible_count = 0
        exclusion_count = 0
        for item in ordered:
            trade = item.trade
            eligible = trade.has_entry_provenance
            members.append(
                {
                    "ordinal": item.ordinal,
                    "outcome_record_id": str(item.record_id),
                    "outcome_hash": item.outcome_hash,
                    "eligible": eligible,
                    "exclusion_reason": None if eligible else "entry_provenance_missing",
                }
            )
            pending.append((item, eligible))
            eligible_count += eligible
            exclusion_count += not eligible
        if digest(canonical(members)) != proof.membership_digest:
            raise ValueError("Membership digest không khớp candidate-universe proof.")
        if eligible_count != proof.eligible_count or exclusion_count != proof.exclusion_count:
            raise ValueError("Counts provenance không khớp candidate-universe proof.")
    if len(query_versions) != 1:
        raise ValueError("Mọi proof của cohort phải cùng query version.")
    source_use_lineage = tuple(lineage_by_child[key] for key in sorted(lineage_by_child))
    lineage_set_hash = source_use_lineage_set_hash(source_use_lineage)

    for item, eligible in pending:
        trade = item.trade
        if not eligible:
            exclusions.append(DatasetExclusion(str(item.record_id), ("entry_provenance_missing",)))
            continue
        assert trade.entry_decision_at is not None
        assert trade.entry_features_hash is not None
        assert trade.entry_feature_schema_hash is not None
        assert trade.entry_symbol is not None
        if trade.closed_at > as_of or trade.entry_decision_at > as_of:
            raise ValueError("Outcome chưa hoàn tất tại thời điểm dựng candidate.")
        value = trade_label_value(trade, config.label_version)
        sample_id = str(item.record_id)
        label_payload = {
            "label_version": config.label_version,
            "outcome_hash": item.outcome_hash,
            "sample_id": sample_id,
            "value": value,
        }
        split = (
            DatasetSplit.HOLDOUT
            if trade.entry_decision_at >= config.holdout_start
            else DatasetSplit.DEVELOPMENT
        )
        sample = DatasetSample(
            sample_id=sample_id,
            decision_at=trade.entry_decision_at,
            feature_available_at=trade.entry_decision_at,
            outcome_start=trade.opened_at,
            outcome_end=trade.closed_at,
            feature_snapshot_hash=trade.entry_features_hash,
            outcome_id=str(item.record_id),
            outcome_hash=item.outcome_hash,
            label_hash=digest(canonical(label_payload)),
            label_value=value,
            label_version=config.label_version,
            split=split,
            source_dependent=True,
            source_rights_hashes=(),
            source_revision_refs=(),
            source_replay_bundle_hash=item.source_replay_bundle_hash,
            source_use_lineage_set_hash=lineage_set_hash,
        )
        accepted.append((item, sample))
        schemas.add(trade.entry_feature_schema_hash)
        symbols.add(trade.entry_symbol)

    if len(schemas) != 1:
        raise ValueError("Candidate phải có một feature schema thống nhất.")
    development_count = sum(sample.split is DatasetSplit.DEVELOPMENT for _, sample in accepted)
    holdout_count = sum(sample.split is DatasetSplit.HOLDOUT for _, sample in accepted)
    if development_count == 0 or holdout_count == 0:
        raise ValueError("Candidate cần có cả development và final holdout.")

    sample_values = (
        tuple(sample for _, sample in sorted(accepted, key=lambda pair: pair[1].sample_id))
        if len(ordered_trials) > 1
        else tuple(sample for _, sample in accepted)
    )
    data_start = min(sample.decision_at for sample in sample_values)
    data_end = max(sample.outcome_end for sample in sample_values)
    if data_start >= data_end:
        raise ValueError("Candidate data range không hợp lệ.")
    query_version = next(iter(query_versions))
    if len(ordered_trials) == 1:
        query_payload: dict[str, object] = {
            "builder_version": "wp06-split-candidate-v1",
            "proof_hash": ordered_trials[0].proof.content_hash,
            "query_version": query_version,
        }
        cohort_key = str(ordered_trials[0].trial_record_id)
    else:
        query_payload = {
            "builder_version": "wp06-split-candidate-cohort-v1",
            "proof_hashes": [item.proof.content_hash for item in ordered_trials],
            "query_version": query_version,
        }
        cohort_key = "cohort:" + digest(
            canonical([str(item.trial_record_id) for item in ordered_trials])
        )
    manifest = DatasetManifest(
        workspace_id=workspace_id,
        query_hash=digest(canonical(query_payload)),
        symbols=tuple(sorted(symbols)),
        data_start=data_start,
        data_end=data_end,
        as_of=as_of,
        feature_schema_version=f"entry-feature-schema:{next(iter(schemas))}",
        feature_version=FEATURE_VERSION,
        label_version=config.label_version,
        sample_schema_version=SAMPLE_SCHEMA_VERSION,
        split_policy_version=SPLIT_POLICY_VERSION,
        samples=sample_values,
        exclusions=tuple(exclusions),
        holdout_sealed_at=as_of,
        manifest_version="dataset-manifest-v5",
        retention_policy_ref=RETENTION_POLICY_REF,
        source_use_lineage=source_use_lineage,
    )
    plan = build_manifest_fold_plan(
        manifest,
        folds=config.folds,
        expected_split_policy_version=SPLIT_POLICY_VERSION,
        evaluation_cutoff=config.evaluation_cutoff,
    )
    preview = _preview(manifest, plan)
    candidate_id = uuid5(
        NAMESPACE_URL,
        f"crypidian:wp06-split-candidate:{workspace_id}:{cohort_key}:"
        f"{manifest.content_hash}:{plan.content_hash}",
    )
    anchor = ordered_trials[0]
    return LearningSplitCandidate(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        trial_record_id=anchor.trial_record_id,
        proof_record_id=anchor.proof_record_id,
        proof_hash=anchor.proof.content_hash,
        manifest=manifest,
        fold_plan=plan,
        config=config,
        preview_json=canonical(preview),
        sources=tuple(
            LearningCandidateTrialSource(
                trial_record_id=item.trial_record_id,
                proof_record_id=item.proof_record_id,
                proof_hash=item.proof.content_hash,
                bundle_hash=bundle,
            )
            for item, bundle in zip(ordered_trials, trial_bundles, strict=True)
        ),
    )


def build_normalized_candidate_dataset(
    *,
    candidate: LearningSplitCandidate,
    outcomes: tuple[LearningCandidateOutcome, ...],
) -> NormalizedLearningDataset:
    """Chuẩn hóa đúng sample development đã gán vào fold, không gồm holdout."""
    from trading_bot.application.learning_fold_samples import (
        NormalizedFoldAssignment,
        NormalizedLearningDataset,
        NormalizedLearningSample,
        normalized_learning_dataset_hash,
    )

    by_id = {str(item.record_id): item for item in outcomes}
    if len(by_id) != len(outcomes):
        raise ValueError("Outcome rows bị lặp khi chuẩn hóa candidate.")
    samples = {item.sample_id: item for item in candidate.manifest.samples}
    used_ids = {
        sample_id
        for fold in candidate.fold_plan.folds
        for sample_id in (
            *fold.train_sample_ids,
            *fold.validation_sample_ids,
            *fold.test_sample_ids,
        )
    }
    normalized: dict[str, NormalizedLearningSample] = {}
    assignments: list[NormalizedFoldAssignment] = []
    for fold in candidate.fold_plan.folds:
        role_samples: tuple[tuple[Literal["train", "validation", "test"], tuple[str, ...]], ...] = (
            ("train", fold.train_sample_ids),
            ("validation", fold.validation_sample_ids),
            ("test", fold.test_sample_ids),
        )
        for role, sample_ids in role_samples:
            for sample_id in sample_ids:
                sample = samples.get(sample_id)
                outcome = by_id.get(sample_id)
                if sample is None or outcome is None:
                    raise ValueError("Fold membership không khớp manifest/outcome rows.")
                trade = outcome.trade
                if (
                    not trade.has_entry_provenance
                    or trade.entry_features_hash != sample.feature_snapshot_hash
                    or trade.entry_feature_schema_hash is None
                    or trade.entry_symbol not in candidate.manifest.symbols
                    or trade_label_value(trade, candidate.manifest.label_version)
                    != sample.label_value
                ):
                    raise ValueError("Feature/label entry không khớp manifest authoritative.")
                row = NormalizedLearningSample(
                    sample_id=sample_id,
                    outcome_id=sample.outcome_id,
                    outcome_hash=sample.outcome_hash,
                    feature_snapshot_hash=sample.feature_snapshot_hash,
                    feature_schema_hash=trade.entry_feature_schema_hash,
                    label_hash=sample.label_hash,
                    label_value=sample.label_value,
                    features=trade.entry_features,
                )
                previous = normalized.setdefault(sample_id, row)
                if previous != row:
                    raise ValueError("Cùng sample có payload khác nhau giữa các fold.")
                assignments.append(NormalizedFoldAssignment(fold.fold_id, sample_id, role))
    if set(normalized) != used_ids:
        raise ValueError("Normalized sample membership không đầy đủ.")
    partial = NormalizedLearningDataset(
        workspace_id=candidate.workspace_id,
        manifest_hash=candidate.manifest.content_hash,
        split_policy_version=candidate.manifest.split_policy_version,
        fold_plan_hash=candidate.fold_plan.content_hash,
        samples=tuple(normalized[key] for key in sorted(normalized)),
        assignments=tuple(sorted(assignments, key=lambda item: (item.fold_id, item.sample_id))),
        content_hash="",
    )
    return NormalizedLearningDataset(
        workspace_id=partial.workspace_id,
        manifest_hash=partial.manifest_hash,
        split_policy_version=partial.split_policy_version,
        fold_plan_hash=partial.fold_plan_hash,
        samples=partial.samples,
        assignments=partial.assignments,
        content_hash=normalized_learning_dataset_hash(partial),
    )


class LearningSplitCandidateBuilderService:
    """Stage/seal candidate bằng login builder, không chia sẻ secret với admin."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def stage(
        self,
        *,
        workspace_id: UUID,
        trial_record_id: UUID | None = None,
        trial_record_ids: tuple[UUID, ...] = (),
        config: LearningSplitCandidateConfig,
    ) -> LearningSplitCandidateStageResult:
        """Proof, quyền hiện hành và candidate private được ghi cùng transaction.

        AI-5: ``trial_record_ids`` cho cohort nhiều trial (mỗi trial một coin); một trial giữ
        đúng hợp đồng cũ.
        """
        from sqlalchemy import select, text

        from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
            CandidateUniverseProofRepository,
        )
        from trading_bot.adapters.database.repositories.learning_split_candidates import (
            LearningSplitCandidateRepository,
        )

        trial_ids = _trial_ids(trial_record_id, trial_record_ids)
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id và trial_record_id phải là UUID.")
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE")
            )
            await _bind_builder_workspace(session, workspace_id)
            proof_repository = CandidateUniverseProofRepository(session)
            trials = []
            for trial_id in trial_ids:
                trials.append(
                    await _load_candidate_trial(
                        session, proof_repository, workspace_id=workspace_id, trial_id=trial_id
                    )
                )
            database_now = await session.scalar(select(text("clock_timestamp()")))
            as_of = max(row.trade.closed_at for trial in trials for row in trial.outcomes)
            if (
                not isinstance(database_now, datetime)
                or database_now.tzinfo is None
                or as_of > database_now
            ):
                raise ValueError("Database clock không trả timestamp có timezone.")
            candidate = build_learning_split_cohort_candidate(
                workspace_id=workspace_id,
                trials=tuple(trials),
                config=config,
                as_of=as_of,
            )
            write = await LearningSplitCandidateRepository(session).append(candidate=candidate)
            for trial in trials:
                verified = await proof_repository.build_and_append(
                    workspace_id=workspace_id,
                    trial_record_id=trial.trial_record_id,
                )
                if verified.proof.content_hash != trial.proof.content_hash:
                    raise ValueError("Proof/quyền thay đổi trong khi stage candidate.")
            return LearningSplitCandidateStageResult(
                candidate_id=write.candidate.candidate_id,
                proof_record_id=candidate.proof_record_id,
                manifest_hash=write.candidate.manifest.content_hash,
                fold_plan_hash=write.candidate.fold_plan.content_hash,
                preview_json=write.candidate.preview_json,
                duplicate=write.duplicate,
            )

    async def seal(
        self, *, workspace_id: UUID, candidate_id: UUID
    ) -> LearningSplitCandidateSealResult:
        """Seal đúng candidate đã duyệt, sau recheck quyền và hash lineage."""
        from sqlalchemy import select, text

        from trading_bot.adapters.database.models import NormalizedLearningDatasetRecord
        from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
            CandidateUniverseProofRepository,
        )
        from trading_bot.adapters.database.repositories.dataset_manifest import (
            DatasetManifestRepository,
        )
        from trading_bot.adapters.database.repositories.learning_fold_samples import (
            LearningFoldSampleRepository,
        )
        from trading_bot.adapters.database.repositories.learning_split_approvals import (
            LearningSplitApprovalRepository,
        )
        from trading_bot.adapters.database.repositories.learning_split_candidates import (
            LearningSplitCandidateRepository,
        )

        if type(workspace_id) is not UUID or type(candidate_id) is not UUID:
            raise ValueError("workspace_id và candidate_id phải là UUID.")
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE")
            )
            await _bind_builder_workspace(session, workspace_id)
            candidates = LearningSplitCandidateRepository(session)
            candidate = await candidates.get_for_builder(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise LookupError("Không tìm thấy candidate trong workspace.")
            approvals = LearningSplitApprovalRepository(session)
            approval = await approvals.get_for_training_seal(
                workspace_id=workspace_id,
                manifest_hash=candidate.manifest.content_hash,
                split_policy_version=candidate.manifest.split_policy_version,
                fold_plan_hash=candidate.fold_plan.content_hash,
            )
            if approval is None:
                raise PermissionError("Candidate chưa có approval exact-match.")
            # Khóa cả manifest và bundle nguồn theo cùng thứ tự trước proof writer.
            source_ids = {
                reference.source_id
                for sample in candidate.manifest.samples
                for reference in sample.source_references
            }
            source_ids.update(
                lineage.source_id for lineage in candidate.manifest.source_use_lineage
            )
            ordered_source_ids = await session.execute(
                text(
                    "SELECT DISTINCT source_id "
                    "FROM unnest(CAST(:source_ids AS text[])) AS source_keys(source_id) "
                    "ORDER BY source_id"
                ),
                {"source_ids": list(source_ids)},
            )
            source_ids_ordered: list[str] = list(ordered_source_ids.scalars().all())
            for source_id in source_ids_ordered:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"research-source:{workspace_id}:{source_id}"},
                )
            proof_repository = CandidateUniverseProofRepository(session)
            sources = candidate.sources or (
                LearningCandidateTrialSource(
                    trial_record_id=candidate.trial_record_id,
                    proof_record_id=candidate.proof_record_id,
                    proof_hash=candidate.proof_hash,
                    bundle_hash=_single_bundle(candidate.manifest),
                ),
            )
            trials = []
            for source in sources:
                trial = await _load_candidate_trial(
                    session,
                    proof_repository,
                    workspace_id=workspace_id,
                    trial_id=source.trial_record_id,
                )
                if (
                    trial.proof_record_id != source.proof_record_id
                    or trial.proof.content_hash != source.proof_hash
                ):
                    raise PermissionError(
                        "Proof hoặc quyền TRAINING hiện hành không khớp candidate."
                    )
                trials.append(trial)
            outcomes = tuple(row for trial in trials for row in trial.outcomes)
            rebuilt = build_learning_split_cohort_candidate(
                workspace_id=workspace_id,
                trials=tuple(trials),
                config=candidate.config,
                as_of=candidate.manifest.as_of,
            )
            if (
                rebuilt.candidate_id != candidate.candidate_id
                or rebuilt.manifest.canonical_json != candidate.manifest.canonical_json
                or rebuilt.fold_plan != candidate.fold_plan
                or rebuilt.preview_json != candidate.preview_json
            ):
                raise PermissionError("Candidate không còn khớp dữ liệu replay authoritative.")

            await DatasetManifestRepository(session).append_for_training(
                workspace_id=workspace_id,
                manifest=candidate.manifest,
                folds=candidate.config.folds,
                evaluation_cutoff=candidate.config.evaluation_cutoff,
            )
            fold_samples = LearningFoldSampleRepository(session)
            await fold_samples.assert_source_training_eligible(manifest=candidate.manifest)
            normalized = build_normalized_candidate_dataset(
                candidate=candidate,
                outcomes=outcomes,
            )
            write = await fold_samples.append_normalized_dataset(
                manifest=candidate.manifest,
                fold_plan=candidate.fold_plan,
                approval=approval,
                dataset=normalized,
            )
            for source in sources:
                final_proof = await proof_repository.build_and_append(
                    workspace_id=workspace_id,
                    trial_record_id=source.trial_record_id,
                )
                if final_proof.proof.content_hash != source.proof_hash:
                    raise PermissionError(
                        "Rights TRAINING hết hạn hoặc thay đổi trước seal commit."
                    )
            stored_id = await session.scalar(
                select(NormalizedLearningDatasetRecord.id).where(
                    NormalizedLearningDatasetRecord.workspace_id == workspace_id,
                    NormalizedLearningDatasetRecord.manifest_hash
                    == candidate.manifest.content_hash,
                    NormalizedLearningDatasetRecord.fold_plan_hash
                    == candidate.fold_plan.content_hash,
                )
            )
            if stored_id is None:
                raise ValueError("Normalized dataset chưa được xác nhận trong transaction.")
            return LearningSplitCandidateSealResult(
                candidate_id=candidate.candidate_id,
                normalized_dataset_id=stored_id,
                manifest_hash=candidate.manifest.content_hash,
                fold_plan_hash=candidate.fold_plan.content_hash,
                approval_hash=approval.content_hash,
                duplicate=write.is_duplicate,
            )


def _trial_ids(single: UUID | None, many: tuple[UUID, ...]) -> tuple[UUID, ...]:
    """Trial của cohort sắp theo id (thứ tự khóa cố định, tránh deadlock)."""
    values = tuple(many) + ((single,) if single is not None else ())
    if not values or any(type(value) is not UUID for value in values):
        raise ValueError("workspace_id và trial_record_id phải là UUID.")
    if len(set(values)) != len(values):
        raise ValueError("Trial bị lặp trong cohort.")
    return tuple(sorted(values, key=str))


def _single_bundle(manifest: DatasetManifest) -> str:
    bundles = {item.source_replay_bundle_hash for item in manifest.samples}
    if len(bundles) != 1 or None in bundles:
        raise ValueError("Candidate cũ phải ghim đúng một replay bundle.")
    value = next(iter(bundles))
    assert isinstance(value, str)
    return value


async def _load_candidate_trial(
    session: AsyncSession,
    proof_repository: object,
    *,
    workspace_id: UUID,
    trial_id: UUID,
) -> LearningCandidateTrial:
    """Proof universe + outcome + lineage bundle của một trial, đúng như builder một trial."""
    from trading_bot.adapters.database.repositories.backtest_outcomes import (
        BacktestOutcomeRepository,
    )

    proof_result = await proof_repository.build_and_append(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        trial_record_id=trial_id,
    )
    outcome_repository = BacktestOutcomeRepository(session)
    persisted = await outcome_repository.load_trade_outcome_rows(
        workspace_id=workspace_id,
        trial_record_id=trial_id,
    )
    training_lineage = await outcome_repository.load_training_bundle_lineage(
        workspace_id=workspace_id,
        trial_record_id=trial_id,
        outcomes=persisted.rows,
    )
    return LearningCandidateTrial(
        trial_record_id=trial_id,
        proof_record_id=proof_result.record_id,
        proof=proof_result.proof,
        outcomes=tuple(
            LearningCandidateOutcome(
                workspace_id=row.workspace_id,
                trial_record_id=row.trial_record_id,
                record_id=row.record_id,
                ordinal=row.ordinal,
                outcome_hash=row.outcome_hash,
                trade=row.trade,
                source_replay_bundle_hash=training_lineage.bundle_hash,
                source_use_lineage=training_lineage.source_use_lineage,
            )
            for row in persisted.rows
        ),
    )


async def _bind_builder_workspace(session: AsyncSession, workspace_id: UUID) -> None:
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )
    session.info["workspace_id"] = workspace_id


def _preview(manifest: DatasetManifest, plan: ManifestFoldPlan) -> dict[str, object]:
    by_id = {item.sample_id: item for item in manifest.samples}
    folds = []
    for fold in plan.folds:
        folds.append(
            {
                "fold_id": fold.fold_id,
                "train": {
                    "start": fold.split.train.start.isoformat(),
                    "end": fold.split.train.end.isoformat(),
                    "count": len(fold.train_sample_ids),
                },
                "validation": {
                    "start": fold.split.validation.start.isoformat(),
                    "end": fold.split.validation.end.isoformat(),
                    "count": len(fold.validation_sample_ids),
                },
                "test": {
                    "start": fold.split.test.start.isoformat(),
                    "end": fold.split.test.end.isoformat(),
                    "count": len(fold.test_sample_ids),
                },
            }
        )
    return {
        "data_start": manifest.data_start.isoformat(),
        "data_end": manifest.data_end.isoformat(),
        "symbols": list(manifest.symbols),
        "sample_count": len(manifest.samples),
        "exclusion_count": len(manifest.exclusions),
        "development_count": sum(
            item.split is DatasetSplit.DEVELOPMENT for item in manifest.samples
        ),
        "holdout_count": sum(item.split is DatasetSplit.HOLDOUT for item in manifest.samples),
        "folds": folds,
        "fold_sample_count": len(by_id),
    }


def _split_payload(split: WalkForwardSplit) -> dict[str, list[str]]:
    return {
        role: [getattr(split, role).start.isoformat(), getattr(split, role).end.isoformat()]
        for role in ("train", "validation", "test")
    }


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có timezone.")


def _sha256(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} phải là SHA-256 lowercase.")
    return value


__all__ = [
    "FEATURE_VERSION",
    "LABEL_VERSION",
    "LearningCandidateOutcome",
    "LearningCandidateTrial",
    "LearningCandidateTrialSource",
    "LearningSplitCandidate",
    "LearningSplitCandidateConfig",
    "LearningSplitCandidateSealResult",
    "LearningSplitCandidateStageResult",
    "LearningSplitCandidateBuilderService",
    "RETENTION_POLICY_REF",
    "SAMPLE_SCHEMA_VERSION",
    "SPLIT_POLICY_VERSION",
    "build_learning_split_candidate",
    "build_learning_split_cohort_candidate",
    "build_normalized_candidate_dataset",
]
