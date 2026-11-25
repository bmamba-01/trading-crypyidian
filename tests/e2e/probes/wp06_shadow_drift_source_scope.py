"""Probe QA-only denials cho exact source scope Shadow/Drift."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, insert, select, text, update
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.models.learning_source_bar_entities import (
    BotDecisionSourceBarRecord,
)
from trading_bot.adapters.database.repositories.learning_drift import LearningDriftRepository
from trading_bot.application.learning_drift import (
    LearningDriftApplication,
    LearningDriftMembershipConflict,
)
from trading_bot.domain.learning.drift import LearningDriftAssessment
from trading_bot.domain.learning.shadow import ShadowModelReference

sys.path.insert(0, "/e2e-tests")
from e2e.probes.wp06_paper_performance_source_scope import _seed_qa_decision_source  # noqa: E402


def _url(user: str, password: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


async def _check_drift_application(
    observer_url: URL,
    workspace_id: UUID,
    decision_id: UUID,
    challenger_registry_id: UUID,
    observation_id: UUID,
    missing_lineage_observation_id: UUID,
) -> tuple[dict[str, bool], LearningDriftAssessment]:
    """Chạy Drift writer trên cohort QA/thiếu lineage, chỉ kiểm tra denial."""
    from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
        LearningDriftAssessmentMemberRecord,
        LearningDriftAssessmentRecord,
        LearningRetrainProposalRecord,
        LearningShadowObservationRecord,
    )

    engine = create_async_engine(observer_url)
    sessions = async_sessionmaker(engine)
    try:
        async with sessions.begin() as session:
            repository = LearningDriftRepository(session)
            scope = await repository.authorize_current_source_scope(
                workspace_id=workspace_id, decision_id=decision_id
            )
            assert scope is None

        application = LearningDriftApplication(sessions)
        run = await application.assess_cohort(
            workspace_id=workspace_id,
            challenger_registry_id=challenger_registry_id,
            challenger_revision=1,
        )
        assert run is not None
        assert run.assessment.member_count == 2
        assert run.assessment.eligible_count == 0
        assert run.assessment.data_quality_status.value == "inconclusive"
        assert run.assessment.performance_status.value == "inconclusive"
        assert run.assessment.snapshot_coverage_rate is None
        assert run.assessment.quality_rate is None
        assert run.assessment.abstention_rate is None
        assert "rights_unverified" in {reason.value for reason in run.assessment.reason_codes}
        assert run.proposal is None

        repeated = await application.assess_cohort(
            workspace_id=workspace_id,
            challenger_registry_id=challenger_registry_id,
            challenger_revision=1,
        )
        assert repeated is not None
        assert repeated.is_duplicate
        assert repeated.assessment.assessment_id == run.assessment.assessment_id

        member_is_ineligible_without_fingerprint = False
        mixed_lineage_cohort_keeps_all_members = False
        async with sessions.begin() as session:
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            observation = await session.get(LearningShadowObservationRecord, observation_id)
            assert observation is not None
            member = await session.scalar(
                select(LearningDriftAssessmentMemberRecord).where(
                    LearningDriftAssessmentMemberRecord.workspace_id == workspace_id,
                    LearningDriftAssessmentMemberRecord.assessment_id
                    == run.assessment.assessment_id,
                    LearningDriftAssessmentMemberRecord.observation_id == observation_id,
                )
            )
            missing_lineage_member = await session.scalar(
                select(LearningDriftAssessmentMemberRecord).where(
                    LearningDriftAssessmentMemberRecord.workspace_id == workspace_id,
                    LearningDriftAssessmentMemberRecord.assessment_id
                    == run.assessment.assessment_id,
                    LearningDriftAssessmentMemberRecord.observation_id
                    == missing_lineage_observation_id,
                )
            )
            assert member is not None
            assert missing_lineage_member is not None
            assert observation.rights_eligible is False
            assert observation.rights_fingerprint is None
            assert observation.source_scope_fingerprint is None
            assert member.eligible is False
            assert member.current_eligible is False
            assert member.current_rights_eligible is False
            assert member.current_rights_fingerprint is None
            assert member.current_source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"]
            member_is_ineligible_without_fingerprint = (
                member.current_rights_eligible is False
                and member.current_rights_fingerprint is None
                and observation.source_scope_fingerprint is None
                and member.current_source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"]
            )
            missing_lineage_observation = await session.get(
                LearningShadowObservationRecord, missing_lineage_observation_id
            )
            assert missing_lineage_observation is not None
            assert missing_lineage_observation.rights_eligible is False
            assert missing_lineage_observation.rights_fingerprint is None
            assert missing_lineage_observation.source_scope_fingerprint is None
            assert missing_lineage_member.eligible is False
            assert missing_lineage_member.current_eligible is False
            assert missing_lineage_member.current_rights_eligible is False
            assert missing_lineage_member.current_rights_fingerprint is None
            assert missing_lineage_member.current_source_ids == []
            assessment_count = await session.scalar(
                select(func.count())
                .select_from(LearningDriftAssessmentRecord)
                .where(
                    LearningDriftAssessmentRecord.workspace_id == workspace_id,
                    LearningDriftAssessmentRecord.cohort_registry_id == challenger_registry_id,
                    LearningDriftAssessmentRecord.cohort_revision == 1,
                    LearningDriftAssessmentRecord.membership_digest
                    == run.assessment.membership_digest,
                    LearningDriftAssessmentRecord.source_rights_digest
                    == run.assessment.source_rights_digest,
                )
            )
            member_count = await session.scalar(
                select(func.count())
                .select_from(LearningDriftAssessmentMemberRecord)
                .where(
                    LearningDriftAssessmentMemberRecord.workspace_id == workspace_id,
                    LearningDriftAssessmentMemberRecord.assessment_id
                    == run.assessment.assessment_id,
                )
            )
            proposal_count = await session.scalar(
                select(func.count())
                .select_from(LearningRetrainProposalRecord)
                .where(
                    LearningRetrainProposalRecord.workspace_id == workspace_id,
                    LearningRetrainProposalRecord.assessment_id == run.assessment.assessment_id,
                )
            )
            assert assessment_count == 1
            assert member_count == 2
            assert proposal_count == 0
            mixed_lineage_cohort_keeps_all_members = (
                missing_lineage_member.current_rights_eligible is False
                and missing_lineage_member.current_rights_fingerprint is None
                and missing_lineage_member.current_source_ids == []
                and member.current_rights_eligible is False
                and member.current_rights_fingerprint is None
                and member.current_source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"]
                and member_count == 2
                and proposal_count == 0
            )

        return {
            "nonempty_qa_assessment_has_no_performance_result": (
                run.assessment.performance_status.value == "inconclusive"
            ),
            "nonempty_qa_assessment_retry_is_idempotent": repeated.is_duplicate,
            "nonempty_qa_member_is_ineligible_without_fingerprint": (
                member_is_ineligible_without_fingerprint
            ),
            "mixed_lineage_cohort_keeps_all_members_inconclusive": (
                mixed_lineage_cohort_keeps_all_members
                and run.assessment.member_count == 2
                and run.assessment.eligible_count == 0
                and run.assessment.data_quality_status.value == "inconclusive"
                and run.assessment.performance_status.value == "inconclusive"
                and run.assessment.snapshot_coverage_rate is None
                and run.assessment.quality_rate is None
                and run.assessment.abstention_rate is None
                and run.proposal is None
                and repeated.is_duplicate
            ),
        }, run.assessment
    finally:
        await engine.dispose()


def _seed_qa_shadow_observation(
    connection: Connection,
    workspace_id: UUID,
    decision_id: UUID,
    *,
    challenger_model: ShadowModelReference | None = None,
) -> tuple[ShadowModelReference, UUID]:
    """Seed observation synthetic chỉ mang lineage QA, không có receipt quyền."""
    from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
    from trading_bot.adapters.database.models.learning_model_registry_entities import (
        LearningModelRegistryRecord,
    )
    from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
        LearningShadowObservationRecord,
    )
    from trading_bot.adapters.database.repositories.learning_shadow import (
        _record_from_observation,
    )
    from trading_bot.domain.learning.model_registry import LearningModelRegistryState
    from trading_bot.domain.learning.shadow import (
        IncumbentDecisionReference,
        IncumbentDecisionStatus,
        ShadowAbstainReason,
        ShadowObservation,
        ShadowSnapshotReference,
    )

    decision = (
        connection.execute(
            text(
                "SELECT decision_hash, evaluated_at, strategy_release_id "
                "FROM public.bot_decision WHERE workspace_id = :workspace AND id = :decision"
            ),
            {"workspace": workspace_id, "decision": decision_id},
        )
        .mappings()
        .one()
    )
    observed_at = connection.execute(text("SELECT pg_catalog.clock_timestamp()")).scalar_one()
    source_lineage = (
        connection.execute(
            text(
                "SELECT feature_snapshot_id, market_bar_id, source_event_id "
                "FROM public.bot_decision_source_bar "
                "WHERE workspace_id = :workspace AND bot_decision_id = :decision"
            ),
            {"workspace": workspace_id, "decision": decision_id},
        )
        .mappings()
        .one_or_none()
    )
    snapshot_id = (
        source_lineage["feature_snapshot_id"] if source_lineage is not None else uuid4().hex * 2
    )
    snapshot_hash = snapshot_id
    snapshot_as_of = decision["evaluated_at"] - timedelta(seconds=1)
    snapshot = ShadowSnapshotReference(
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        schema_hash="b" * 64,
        as_of=snapshot_as_of,
    )
    observation_id = uuid4()
    model = challenger_model or ShadowModelReference(
        workspace_id=workspace_id,
        registry_id=uuid4(),
        artifact_id=uuid4(),
        artifact_hash="c" * 64,
        manifest_hash="d" * 64,
        fold_plan_hash="e" * 64,
        training_approval_hash="f" * 64,
        registry_approval_event_id=uuid4(),
        registry_approval_event_hash="1" * 64,
        registry_approval_event_sequence=1,
        revision=1,
        state=LearningModelRegistryState.EVALUATED,
    )
    incumbent = IncumbentDecisionReference(
        decision_id=decision_id,
        workspace_id=workspace_id,
        decision_hash=decision["decision_hash"],
        strategy_release_id=decision["strategy_release_id"],
        strategy_release_hash="2" * 64,
        status=IncumbentDecisionStatus.CANDIDATE,
        snapshot=snapshot,
        evaluated_at=decision["evaluated_at"],
    )
    observation = ShadowObservation(
        observation_id=observation_id,
        workspace_id=workspace_id,
        snapshot=snapshot,
        challenger_model=model,
        incumbent_decision=incumbent,
        active_registry_id=uuid4(),
        active_registry_revision=1,
        active_artifact_hash="3" * 64,
        active_pointer_version=1,
        snapshot_coverage=1.0,
        snapshot_quality_ok=True,
        schema_compatible=True,
        rights_eligible=False,
        rights_fingerprint=None,
        rights_source_id=None,
        rights_policy_id=None,
        rights_policy_sequence=None,
        rights_known_at=None,
        challenger_reason=ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE,
        observed_at=observed_at,
    )
    record = _record_from_observation(observation)
    values = {
        column.key: getattr(record, column.key)
        for column in LearningShadowObservationRecord.__table__.columns
        if getattr(record, column.key) is not None and column.key != "created_at"
    }
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    connection.execute(
        insert(FeatureSnapshotRecord).values(
            id=uuid4(),
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            content_hash=snapshot_id,
            symbol="BTCUSDT",
            timeframe="1h",
            as_of=snapshot_as_of,
            computed_at=decision["evaluated_at"],
            schema_hash=snapshot.schema_hash,
            formula_hash="4" * 64,
            code_hash="5" * 64,
            input_bar_ids=(
                [f"market-bar-record:{source_lineage['market_bar_id']}"]
                if source_lineage is not None
                else []
            ),
            input_event_ids=(
                [source_lineage["source_event_id"]] if source_lineage is not None else []
            ),
            features={},
            warmup={},
            quality_flags=[],
            data_freshness="FRESH",
            data_coverage=1.0,
            regime={
                "label": "UNKNOWN",
                "confidence": 0.0,
                "coverage": 1.0,
                "reason_code": "QA_ONLY_FIXTURE",
                "reason_vi": "Dòng kiểm thử chỉ mô phỏng metadata.",
            },
            is_usable=True,
            canonical_json="{}",
        )
    )
    if challenger_model is None:
        connection.execute(
            insert(LearningModelRegistryRecord).values(
                id=model.registry_id,
                workspace_id=workspace_id,
                artifact_id=model.artifact_id,
                artifact_hash=model.artifact_hash,
                manifest_hash=model.manifest_hash,
                fold_plan_hash=model.fold_plan_hash,
                approval_hash=model.training_approval_hash,
                evaluation_report_id=None,
                evaluation_report_hash=None,
                state="evaluated",
                revision=1,
                created_by_membership_id=uuid4(),
            )
        )
    connection.execute(insert(LearningShadowObservationRecord).values(values))
    connection.execute(text("SET LOCAL session_replication_role = origin"))
    return model, observation_id


def _remove_decision_source_lineage_for_qa_fixture(
    connection: Connection, workspace_id: UUID, decision_id: UUID
) -> None:
    """Tạo trạng thái decision QA legacy không có liên kết bar trong DB test dùng một lần."""
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    result = connection.execute(
        text(
            "DELETE FROM public.bot_decision_source_bar "
            "WHERE workspace_id = :workspace AND bot_decision_id = :decision"
        ),
        {"workspace": workspace_id, "decision": decision_id},
    )
    assert result.rowcount == 1
    connection.execute(text("SET LOCAL session_replication_role = origin"))


def _seed_qa_shadow_pending_candidate(
    connection: Connection, workspace_id: UUID
) -> tuple[UUID, UUID, UUID]:
    """Seed decision QA cùng active/challenger target để chạy đúng Shadow application writer."""
    from trading_bot.adapters.database.models.feature_entities import FeatureSnapshotRecord
    from trading_bot.adapters.database.models.learning_model_registry_entities import (
        LearningModelActivePointerRecord,
        LearningModelRegistryEventRecord,
        LearningModelRegistryRecord,
    )
    from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
        LearningScoringDeploymentPointerRecord,
    )
    from trading_bot.adapters.database.models.learning_training_artifact_entities import (
        LearningTrainingArtifactRecord,
    )
    from trading_bot.adapters.database.models.signal_entities import (
        BotDecisionRecord,
        SharedSignalEvidenceRecord,
    )

    now = datetime.now(UTC)
    connection.execute(
        insert(Workspace).values(
            id=workspace_id,
            name="WP06 QA Shadow writer",
            slug=f"wp06-shadow-qa-{workspace_id.hex}",
            mode="PAPER",
            status="ACTIVE",
        )
    )
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    decision_id, _ = _seed_qa_decision_source(
        connection, workspace_id, uuid4(), include_trade=False
    )
    decision = (
        connection.execute(
            text(
                "SELECT evidence_id,evidence_hash,evaluated_at,strategy_release_id "
                "FROM public.bot_decision WHERE workspace_id=:workspace AND id=:decision"
            ),
            {"workspace": workspace_id, "decision": decision_id},
        )
        .mappings()
        .one()
    )
    lineage = (
        connection.execute(
            text(
                "SELECT feature_snapshot_id,market_bar_id,source_event_id "
                "FROM public.bot_decision_source_bar "
                "WHERE workspace_id=:workspace AND bot_decision_id=:decision"
            ),
            {"workspace": workspace_id, "decision": decision_id},
        )
        .mappings()
        .one()
    )
    snapshot_id = lineage["feature_snapshot_id"]
    evaluated_at = decision["evaluated_at"]
    snapshot_as_of = evaluated_at - timedelta(seconds=1)
    source_event_id = lineage["source_event_id"]
    connection.execute(
        update(BotDecisionRecord)
        .where(
            BotDecisionRecord.workspace_id == workspace_id,
            BotDecisionRecord.id == decision_id,
        )
        .values(strategy_release_hash="2" * 64)
    )
    connection.execute(
        insert(FeatureSnapshotRecord).values(
            id=uuid4(),
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            content_hash=snapshot_id,
            symbol="BTCUSDT",
            timeframe="1h",
            as_of=snapshot_as_of,
            computed_at=evaluated_at,
            schema_hash="b" * 64,
            formula_hash="4" * 64,
            code_hash="5" * 64,
            input_bar_ids=[f"market-bar-record:{lineage['market_bar_id']}"],
            input_event_ids=[source_event_id],
            features={},
            warmup={},
            quality_flags=[],
            data_freshness="FRESH",
            data_coverage=1.0,
            regime={
                "label": "UNKNOWN",
                "confidence": 0.0,
                "coverage": 1.0,
                "reason_code": "QA_ONLY_FIXTURE",
                "reason_vi": "Fixture metadata QA, không có quyền dữ liệu bên ngoài.",
            },
            is_usable=True,
            canonical_json="{}",
        )
    )
    connection.execute(
        insert(SharedSignalEvidenceRecord).values(
            id=decision["evidence_id"],
            workspace_id=workspace_id,
            symbol="BTCUSDT",
            setup_type="QA_ONLY_FIXTURE",
            observed_at=snapshot_as_of,
            available_at=snapshot_as_of,
            validity_end=evaluated_at + timedelta(hours=1),
            feature_snapshot_id=snapshot_id,
            source_refs=["CRYPIDIAN_FIRST_PARTY_QA"],
            model_release_id=None,
            content_hash="8" * 64,
            evidence_hash=decision["evidence_hash"],
            schema_version="qa-only-v1",
            canonical_json="{}",
        )
    )

    def seed_artifact(marker: str) -> tuple[UUID, str]:
        artifact_id = uuid4()
        artifact_hash = marker * 64
        artifact = {"scorer_state": "abstain", "calibrated": False}
        connection.execute(
            insert(LearningTrainingArtifactRecord).values(
                id=artifact_id,
                workspace_id=workspace_id,
                attempt_id=uuid4(),
                normalized_dataset_id=uuid4(),
                artifact_hash=artifact_hash,
                artifact_schema_version="qa-only-v1",
                trainer_version="qa-only-fixture",
                manifest_hash="a" * 64,
                fold_plan_hash="c" * 64,
                fold_id="d" * 64,
                approval_hash="e" * 64,
                batch_hash="f" * 64,
                feature_schema_hash="b" * 64,
                attempt_content_hash="1" * 64,
                canonical_artifact=json.dumps(artifact, sort_keys=True, separators=(",", ":")),
                artifact=artifact,
                elapsed_milliseconds=1,
                cpu_time_milliseconds=1,
                peak_memory_bytes=1024,
                actual_operation_units=1,
                process_id=1,
            )
        )
        return artifact_id, artifact_hash

    active_artifact_id, active_artifact_hash = seed_artifact("6")
    challenger_artifact_id, challenger_artifact_hash = seed_artifact("7")
    active_registry_id = uuid4()
    challenger_registry_id = uuid4()
    actor_membership_id = uuid4()
    for registry_id, artifact_id, artifact_hash, state in (
        (active_registry_id, active_artifact_id, active_artifact_hash, "paper_active"),
        (challenger_registry_id, challenger_artifact_id, challenger_artifact_hash, "evaluated"),
    ):
        connection.execute(
            insert(LearningModelRegistryRecord).values(
                id=registry_id,
                workspace_id=workspace_id,
                artifact_id=artifact_id,
                artifact_hash=artifact_hash,
                manifest_hash="a" * 64,
                fold_plan_hash="c" * 64,
                approval_hash="e" * 64,
                evaluation_report_id=None,
                evaluation_report_hash=None,
                state=state,
                revision=1,
                created_by_membership_id=actor_membership_id,
            )
        )
    approval_event_id = uuid4()
    connection.execute(
        insert(LearningModelRegistryEventRecord).values(
            id=approval_event_id,
            workspace_id=workspace_id,
            registry_id=challenger_registry_id,
            sequence=1,
            action="approve",
            from_state="draft",
            to_state="evaluated",
            actor_membership_id=actor_membership_id,
            policy_version="qa-only-shadow-fixture-v1",
            reason_code="QA_FIXTURE_APPROVAL",
            reason="Target chỉ dùng để kiểm thử writer QA không có license.",
            idempotency_key=uuid4(),
            mfa_verified_at=now,
            expected_revision=1,
            expected_pointer_version=1,
            evaluation_report_id=None,
            evaluation_report_hash=None,
        )
    )
    connection.execute(
        insert(LearningModelActivePointerRecord).values(
            workspace_id=workspace_id,
            registry_id=active_registry_id,
            version=1,
            updated_by_membership_id=actor_membership_id,
        )
    )
    # Không tạo scorer deployment pointer: runtime scorer mặc định DISABLED.
    assert (
        connection.execute(
            select(func.count())
            .select_from(LearningScoringDeploymentPointerRecord)
            .where(
                LearningScoringDeploymentPointerRecord.workspace_id == workspace_id,
                LearningScoringDeploymentPointerRecord.mode.in_(("SHADOW", "PAPER_GATE")),
            )
        ).scalar_one()
        == 0
    )
    connection.execute(text("SET LOCAL session_replication_role = origin"))
    return decision_id, active_registry_id, challenger_registry_id


async def _check_shadow_application_qa_writer(observer_url: URL) -> dict[str, bool]:
    """Chạy observer writer thật cho decision QA; không tạo quyền hoặc scorer mode."""
    from trading_bot.adapters.database.models.learning_model_registry_entities import (
        LearningModelActivePointerRecord,
    )
    from trading_bot.adapters.database.models.learning_score_event_entities import (
        LearningScoreEventRecord,
    )
    from trading_bot.adapters.database.models.learning_scorer_artifact_entities import (
        LearningScorerArtifactRecord,
    )
    from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
        LearningScoringDeploymentEventRecord,
        LearningScoringDeploymentPointerRecord,
    )
    from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
        LearningShadowObservationRecord,
    )
    from trading_bot.adapters.database.models.paper_entities import (
        ExchangeOrderRecord,
        FillRecord,
        OrderIntentRecord,
        PositionEventRecord,
        TradeRecord,
    )
    from trading_bot.adapters.database.models.signal_entities import BotDecisionRecord
    from trading_bot.adapters.database.repositories.learning_shadow import (
        LearningShadowRepository,
    )
    from trading_bot.application.learning_shadow import LearningShadowApplication
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.learning.shadow import ShadowAbstainReason

    super_engine = create_engine(
        _url(
            os.environ["TRADING_BOT_DATABASE_USER"],
            Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        )
    )
    workspace_id = uuid4()
    with super_engine.begin() as connection:
        decision_id, active_registry_id, challenger_registry_id = _seed_qa_shadow_pending_candidate(
            connection, workspace_id
        )
        before = tuple(
            connection.execute(
                select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
            ).scalar_one()
            for model in (
                BotDecisionRecord,
                OrderIntentRecord,
                ExchangeOrderRecord,
                FillRecord,
                PositionEventRecord,
                TradeRecord,
                LearningScoreEventRecord,
                LearningScorerArtifactRecord,
                LearningScoringDeploymentEventRecord,
                LearningScoringDeploymentPointerRecord,
                LearningShadowObservationRecord,
            )
        )
        assert before[5] == 0
        scorer_active_count = connection.execute(
            select(func.count())
            .select_from(LearningScoringDeploymentPointerRecord)
            .where(
                LearningScoringDeploymentPointerRecord.workspace_id == workspace_id,
                LearningScoringDeploymentPointerRecord.mode.in_(("SHADOW", "PAPER_GATE")),
            )
        ).scalar_one()
        scorer_mode = connection.execute(
            select(LearningScoringDeploymentPointerRecord.mode).where(
                LearningScoringDeploymentPointerRecord.workspace_id == workspace_id
            )
        ).scalar_one_or_none()
        assert scorer_mode in (None, "DISABLED")
        active_registry_version = connection.execute(
            select(LearningModelActivePointerRecord.version).where(
                LearningModelActivePointerRecord.workspace_id == workspace_id,
                LearningModelActivePointerRecord.registry_id == active_registry_id,
            )
        ).scalar_one()

    engine = create_async_engine(observer_url)
    sessions = async_sessionmaker(engine)
    try:

        async def inspect_source_scope() -> tuple[tuple[str, ...], str | None]:
            async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
                repository = LearningShadowRepository(uow.session)
                source_ids = await repository.decision_source_ids(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                )
                checked_at = await repository.authorization_clock(workspace_id=workspace_id)
                fingerprint = await repository.authorize_inference_source_scope(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                    checked_at=checked_at,
                )
            return source_ids, fingerprint

        source_ids_before, scope_fingerprint_before = await inspect_source_scope()
        assert source_ids_before == ("CRYPIDIAN_FIRST_PARTY_QA",)
        assert scope_fingerprint_before is None

        application = LearningShadowApplication(sessions)
        first = await application.observe_pending(workspace_id=workspace_id)
        assert first.scanned_count == 1
        assert first.created_count == 1
        assert len(first.observations) == 1
        observation = first.observations[0]
        assert observation.incumbent_decision.decision_id == decision_id
        assert observation.active_registry_id == active_registry_id
        assert observation.active_pointer_version == active_registry_version == 1
        assert observation.challenger_model.registry_id == challenger_registry_id
        assert observation.challenger_reason is ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE
        assert observation.rights_eligible is False
        assert observation.rights_fingerprint is None
        assert observation.source_scope_fingerprint is None
        assert observation.rights_source_id is None
        assert observation.rights_policy_id is None
        assert observation.rights_policy_sequence is None
        assert observation.rights_known_at is None
        projection = observation.canonical_payload()
        assert set(projection) == {
            "active_artifact_hash",
            "active_pointer_version",
            "active_registry_id",
            "active_registry_revision",
            "challenger_model",
            "challenger_reason",
            "challenger_snapshot_state",
            "incumbent_decision",
            "observation_id",
            "observed_at",
            "snapshot",
            "source_rights",
            "workspace_id",
        }
        assert set(projection["challenger_model"]) == {
            "artifact_hash",
            "artifact_id",
            "fold_plan_hash",
            "manifest_hash",
            "registry_approval_event_hash",
            "registry_approval_event_id",
            "registry_approval_event_sequence",
            "registry_id",
            "revision",
            "state",
            "training_approval_hash",
            "workspace_id",
        }
        assert set(projection["challenger_snapshot_state"]) == {
            "coverage",
            "quality_ok",
            "schema_compatible",
        }
        assert set(projection["incumbent_decision"]) == {
            "decision_hash",
            "decision_id",
            "evaluated_at",
            "status",
            "strategy_release_hash",
            "strategy_release_id",
        }
        assert set(projection["snapshot"]) == {
            "as_of",
            "schema_hash",
            "snapshot_hash",
            "snapshot_id",
        }
        assert set(projection["source_rights"]) == {
            "eligible",
            "fingerprint",
            "known_at",
            "policy_id",
            "policy_sequence",
            "source_id",
        }
        assert set(LearningShadowObservationRecord.__table__.columns.keys()) == {
            "active_artifact_hash",
            "active_pointer_version",
            "active_registry_id",
            "active_registry_revision",
            "challenger_artifact_hash",
            "challenger_artifact_id",
            "challenger_decision",
            "challenger_fold_plan_hash",
            "challenger_manifest_hash",
            "challenger_reason",
            "challenger_registry_id",
            "challenger_revision",
            "challenger_state",
            "content_hash",
            "created_at",
            "decision_evaluated_at",
            "decision_hash",
            "id",
            "idempotency_key",
            "incumbent_decision_id",
            "incumbent_status",
            "observed_at",
            "registry_approval_event_hash",
            "registry_approval_event_id",
            "registry_approval_event_sequence",
            "rights_eligible",
            "rights_fingerprint",
            "rights_known_at",
            "rights_policy_id",
            "rights_policy_sequence",
            "rights_source_id",
            "schema_compatible",
            "snapshot_as_of",
            "snapshot_coverage",
            "snapshot_hash",
            "snapshot_id",
            "snapshot_schema_hash",
            "snapshot_quality_ok",
            "source_scope_fingerprint",
            "strategy_release_hash",
            "strategy_release_id",
            "training_approval_hash",
            "workspace_id",
        }

        source_ids_after, scope_fingerprint_after = await inspect_source_scope()
        assert source_ids_after == ("CRYPIDIAN_FIRST_PARTY_QA",)
        assert scope_fingerprint_after is None

        retry = await application.observe_pending(workspace_id=workspace_id)
        assert retry.scanned_count == 0
        assert retry.created_count == 0
        assert retry.observations == ()

        with super_engine.begin() as connection:
            after = tuple(
                connection.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                ).scalar_one()
                for model in (
                    BotDecisionRecord,
                    OrderIntentRecord,
                    ExchangeOrderRecord,
                    FillRecord,
                    PositionEventRecord,
                    TradeRecord,
                    LearningScoreEventRecord,
                    LearningScorerArtifactRecord,
                    LearningScoringDeploymentEventRecord,
                    LearningScoringDeploymentPointerRecord,
                    LearningShadowObservationRecord,
                )
            )
            stored = connection.execute(
                select(
                    LearningShadowObservationRecord.challenger_reason,
                    LearningShadowObservationRecord.rights_eligible,
                    LearningShadowObservationRecord.rights_fingerprint,
                    LearningShadowObservationRecord.source_scope_fingerprint,
                    LearningShadowObservationRecord.challenger_registry_id,
                    LearningShadowObservationRecord.active_registry_id,
                    LearningShadowObservationRecord.active_pointer_version,
                ).where(
                    LearningShadowObservationRecord.workspace_id == workspace_id,
                    LearningShadowObservationRecord.incumbent_decision_id == decision_id,
                )
            ).one()
            final_scorer_active_count = connection.execute(
                select(func.count())
                .select_from(LearningScoringDeploymentPointerRecord)
                .where(
                    LearningScoringDeploymentPointerRecord.workspace_id == workspace_id,
                    LearningScoringDeploymentPointerRecord.mode.in_(("SHADOW", "PAPER_GATE")),
                )
            ).scalar_one()
            final_scorer_mode = connection.execute(
                select(LearningScoringDeploymentPointerRecord.mode).where(
                    LearningScoringDeploymentPointerRecord.workspace_id == workspace_id
                )
            ).scalar_one_or_none()

        assert after == (*before[:10], 1)
        assert after[5] == 0
        assert stored.challenger_reason == "SOURCE_RIGHTS_INELIGIBLE"
        assert stored.rights_eligible is False
        assert stored.rights_fingerprint is None
        assert stored.source_scope_fingerprint is None
        assert stored.challenger_registry_id == challenger_registry_id
        assert stored.active_registry_id == active_registry_id
        assert stored.active_pointer_version == 1
        assert scorer_active_count == final_scorer_active_count == 0
        assert scorer_mode == final_scorer_mode
        assert final_scorer_mode in (None, "DISABLED")
        return {
            "shadow_application_persists_qa_abstain_metadata": True,
            "shadow_application_retry_is_idempotent": True,
            "shadow_application_exact_qa_source_is_denied": (
                source_ids_before == source_ids_after == ("CRYPIDIAN_FIRST_PARTY_QA",)
                and scope_fingerprint_before is None
                and scope_fingerprint_after is None
            ),
            "shadow_application_leaves_decision_order_and_score_ledgers_unchanged": (
                before[:10] == after[:10]
            ),
        }
    finally:
        await engine.dispose()
        super_engine.dispose()


async def _check_membership_race(super_engine: Engine, observer_url: URL) -> dict[str, bool]:
    """Kiểm rollback khi tập observation QA đổi trong lúc Drift chờ khóa nguồn."""
    from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
        LearningDriftAssessmentMemberRecord,
        LearningDriftAssessmentRecord,
        LearningRetrainProposalRecord,
    )

    workspace_id = uuid4()
    with super_engine.begin() as connection:
        connection.execute(
            insert(Workspace).values(
                id=workspace_id,
                name="WP06 QA membership race",
                slug=f"wp06-qa-{workspace_id.hex}",
                mode="PAPER",
                status="ACTIVE",
            )
        )
        first_decision_id, _ = _seed_qa_decision_source(connection, workspace_id, uuid4())
        second_decision_id, _ = _seed_qa_decision_source(connection, workspace_id, uuid4())
        # Helper seed dùng cùng ID snapshot mặc định cho cả hai quyết định giả lập.
        connection.execute(
            update(BotDecisionSourceBarRecord)
            .where(
                BotDecisionSourceBarRecord.workspace_id == workspace_id,
                BotDecisionSourceBarRecord.bot_decision_id == second_decision_id,
            )
            .values(feature_snapshot_id=uuid4().hex + uuid4().hex)
        )
        model, first_observation_id = _seed_qa_shadow_observation(
            connection, workspace_id, first_decision_id
        )

    source_id = "CRYPIDIAN_FIRST_PARTY_QA"
    source_key = f"research-source:{workspace_id}:{source_id}"
    member_key = f"wp06-learning-observation-member-set:{workspace_id}"
    application_name = f"wp06-qa-membership-{workspace_id.hex}"
    engine = create_async_engine(observer_url, connect_args={"application_name": application_name})
    application = LearningDriftApplication(async_sessionmaker(engine))
    scope = {
        "workspace_id": workspace_id,
        "challenger_registry_id": model.registry_id,
        "challenger_revision": model.revision,
    }
    task = None
    try:
        with super_engine.begin() as fixture:
            # Cho assessor lấy khóa registry và đọc tập ban đầu trước khi chờ nguồn.
            for lock_key in (source_key, member_key):
                fixture.execute(
                    text(
                        "SELECT pg_catalog.pg_advisory_xact_lock("
                        "pg_catalog.hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": lock_key},
                )
            _, second_observation_id = _seed_qa_shadow_observation(
                fixture, workspace_id, second_decision_id, challenger_model=model
            )
            assert fixture.execute(text("SHOW session_replication_role")).scalar_one() == "origin"
            blocker_pid = fixture.execute(text("SELECT pg_catalog.pg_backend_pid()")).scalar_one()
            task = asyncio.create_task(application.assess_cohort(**scope))
            # Dùng phiên autocommit để đọc trạng thái lock mới trong mỗi vòng.
            with super_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as monitor:
                async with asyncio.timeout(10):
                    while not monitor.execute(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_catalog.pg_stat_activity AS observer
                                JOIN pg_catalog.pg_locks AS wanted ON wanted.pid = observer.pid
                                WHERE observer.application_name = :name
                                  AND observer.usename = 'crypidian_learning_shadow_observer'
                                  AND wanted.locktype = 'advisory' AND NOT wanted.granted
                                  AND wanted.objsubid = 1
                                  AND ((wanted.classid::bigint << 32) | wanted.objid::bigint)
                                      = pg_catalog.hashtextextended(:source_key, 0)
                                  AND :blocker = ANY(pg_catalog.pg_blocking_pids(observer.pid))
                            )
                            """
                        ),
                        {
                            "blocker": blocker_pid,
                            "name": application_name,
                            "source_key": source_key,
                        },
                    ).scalar_one():
                        if task.done():
                            await task
                            raise AssertionError("Drift đã kết thúc trước khi chờ khóa nguồn QA.")
                        await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(task, timeout=10)
        except LearningDriftMembershipConflict:
            pass
        else:
            raise AssertionError("Drift chưa từ chối tập observation mới sau khi lấy khóa.")

        with super_engine.connect() as connection:
            for record in (
                LearningDriftAssessmentRecord,
                LearningDriftAssessmentMemberRecord,
                LearningRetrainProposalRecord,
            ):
                assert (
                    connection.scalar(
                        select(func.count())
                        .select_from(record)
                        .where(record.workspace_id == workspace_id)
                    )
                    == 0
                ), record.__tablename__

        run = await asyncio.wait_for(application.assess_cohort(**scope), timeout=10)
        assert run is not None
        assert run.is_duplicate is False
        assessment = run.assessment
        expected_members = {first_observation_id, second_observation_id}
        assert set(assessment.member_ids) == expected_members
        assert assessment.member_count == 2
        assert assessment.eligible_count == 0
        assert assessment.data_quality_status.value == "inconclusive"
        assert assessment.performance_status.value == "inconclusive"
        assert "rights_unverified" in {reason.value for reason in assessment.reason_codes}
        assert assessment.snapshot_coverage_rate is None
        assert assessment.quality_rate is None
        assert assessment.abstention_rate is None
        assert run.proposal is None

        duplicate = await asyncio.wait_for(application.assess_cohort(**scope), timeout=10)
        assert duplicate is not None
        assert duplicate.is_duplicate
        assert duplicate.assessment.assessment_id == assessment.assessment_id
        assert duplicate.proposal is None

        with super_engine.connect() as connection:
            assert connection.scalars(
                select(LearningDriftAssessmentRecord.id).where(
                    LearningDriftAssessmentRecord.workspace_id == workspace_id
                )
            ).all() == [assessment.assessment_id]
            members = LearningDriftAssessmentMemberRecord.__table__
            persisted_members = connection.execute(
                select(members).where(members.c.workspace_id == workspace_id)
            ).all()
            assert len(persisted_members) == 2
            assert {row.observation_id for row in persisted_members} == expected_members
            for row in persisted_members:
                assert row.assessment_id == assessment.assessment_id
                assert row.eligible is False
                assert row.current_eligible is False
                assert row.current_rights_eligible is False
                assert row.current_rights_fingerprint is None
                assert row.current_source_ids == [source_id]
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(LearningRetrainProposalRecord)
                    .where(LearningRetrainProposalRecord.workspace_id == workspace_id)
                )
                == 0
            )

        return {
            "membership_race_rolls_back_drift_ledger": True,
            "membership_race_retry_denies_both_qa_members": True,
            "membership_race_retry_is_idempotent": True,
        }
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


def main() -> None:
    workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
    super_engine = create_engine(
        _url(
            os.environ["TRADING_BOT_DATABASE_USER"],
            Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        )
    )
    admin_engine = create_engine(
        _url(
            "crypidian_admin_web",
            Path("/run/secrets/database_admin_web_password").read_text().strip(),
        )
    )
    observer_engine = create_engine(
        _url(
            "crypidian_learning_shadow_observer",
            Path("/run/secrets/database_learning_shadow_observer_password").read_text().strip(),
        )
    )
    try:
        with super_engine.begin() as connection:
            connection.execute(
                insert(Workspace).values(
                    id=workspace_id,
                    name="WP06 QA-only source scope test",
                    slug=f"wp06-qa-{uuid4().hex}",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            decision_id, _ = _seed_qa_decision_source(connection, workspace_id, uuid4())
            challenger_model, observation_id = _seed_qa_shadow_observation(
                connection, workspace_id, decision_id
            )
            missing_lineage_decision_id, _ = _seed_qa_decision_source(
                connection, workspace_id, uuid4(), include_trade=False
            )
            _remove_decision_source_lineage_for_qa_fixture(
                connection, workspace_id, missing_lineage_decision_id
            )
            _, missing_lineage_observation_id = _seed_qa_shadow_observation(
                connection,
                workspace_id,
                missing_lineage_decision_id,
                challenger_model=challenger_model,
            )
        with admin_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            for table in (
                "learning_shadow_observation",
                "learning_drift_assessment",
                "learning_drift_assessment_member",
            ):
                for privilege in ("SELECT", "INSERT"):
                    allowed = connection.execute(
                        text(
                            "SELECT pg_catalog.has_any_column_privilege("
                            "current_user, :table, :privilege)"
                        ),
                        {"table": f"public.{table}", "privilege": privilege},
                    ).scalar_one()
                    assert allowed is False, (table, privilege)
            source_ids = connection.execute(
                text("SELECT public.wp06_shadow_drift_decision_source_ids(:workspace,:decision)"),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            shadow_scope = connection.execute(
                text(
                    "SELECT public.wp06_authorize_shadow_inference_source_scope("
                    ":workspace,:decision,pg_catalog.clock_timestamp())"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            past_clock_shadow_scope = connection.execute(
                text(
                    "SELECT public.wp06_authorize_shadow_inference_source_scope("
                    ":workspace,:decision,TIMESTAMPTZ '0001-01-01 00:00:00+00')"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            future_clock_shadow_scope = connection.execute(
                text(
                    "SELECT public.wp06_authorize_shadow_inference_source_scope("
                    ":workspace,:decision,TIMESTAMPTZ '9999-12-31 23:59:59+00')"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            drift_scope = connection.execute(
                text(
                    "SELECT public.wp06_authorize_learning_drift_source_scope("
                    ":workspace,:decision,pg_catalog.clock_timestamp())"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            missing_shadow = connection.execute(
                text(
                    "SELECT public.wp06_authorize_shadow_inference_source_scope("
                    ":workspace,:decision,pg_catalog.clock_timestamp())"
                ),
                {"workspace": workspace_id, "decision": uuid4()},
            ).scalar_one()
            missing_drift = connection.execute(
                text(
                    "SELECT public.wp06_recheck_learning_drift_assessment_source_rights("
                    ":workspace,:assessment)"
                ),
                {"workspace": workspace_id, "assessment": uuid4()},
            ).scalar_one()
            missing_observation = connection.execute(
                text(
                    "SELECT public.wp06_recheck_learning_shadow_observation_source_rights("
                    ":workspace,:observation)"
                ),
                {"workspace": workspace_id, "observation": uuid4()},
            ).scalar_one()
            role_isolated = connection.execute(
                text(
                    "SELECT NOT pg_catalog.has_function_privilege("
                    "'crypidian_app',"
                    "'public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,timestamptz)',"
                    "'EXECUTE') AND NOT pg_catalog.has_function_privilege("
                    "'crypidian_app',"
                    "'public.wp06_recheck_learning_drift_assessment_source_rights(uuid,uuid)',"
                    "'EXECUTE') AND NOT pg_catalog.has_function_privilege("
                    "'crypidian_app',"
                    "'public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)',"
                    "'EXECUTE') AND NOT pg_catalog.has_function_privilege("
                    "'crypidian_trading_runtime',"
                    "'public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)',"
                    "'EXECUTE')"
                )
            ).scalar_one()
            internal_gate_private = connection.execute(
                text(
                    "SELECT NOT pg_catalog.has_function_privilege("
                    "'crypidian_admin_web',"
                    "'public.wp06_authorize_shadow_inference_source_scope_with_caller_clock"
                    "(uuid,uuid,timestamptz)','EXECUTE') AND NOT "
                    "pg_catalog.has_function_privilege("
                    "'crypidian_learning_shadow_observer',"
                    "'public.wp06_authorize_shadow_inference_source_scope_with_caller_clock"
                    "(uuid,uuid,timestamptz)','EXECUTE')"
                )
            ).scalar_one()
            shadow_guard_can_execute_current_gate = connection.execute(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'crypidian_learning_shadow_integrity_guard',"
                    "'public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)',"
                    "'EXECUTE') AND pg_catalog.has_function_privilege("
                    "'crypidian_learning_shadow_integrity_guard',"
                    "'public.wp06_learning_drift_current_rights_digest(uuid,uuid,timestamptz)',"
                    "'EXECUTE')"
                )
            ).scalar_one()
        with observer_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            observer = "crypidian_learning_shadow_observer"
            assert tuple(connection.execute(text("SELECT session_user, current_user")).one()) == (
                observer,
                observer,
            )
            parameters = {"workspace": workspace_id, "decision": decision_id}
            drift_gate = text(
                "SELECT public.wp06_authorize_learning_drift_source_scope_current("
                ":workspace,:decision)"
            )
            # Phải tới khóa đúng nguồn trước khi từ chối QA ở sâu trong hai hàm PAPER.
            with super_engine.begin() as blocker:
                blocker.execute(text("SET LOCAL lock_timeout = '3s'"))
                blocker.execute(
                    text(
                        "SELECT pg_catalog.pg_advisory_xact_lock("
                        "pg_catalog.hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": f"research-source:{workspace_id}:CRYPIDIAN_FIRST_PARTY_QA"},
                )
                connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
                try:
                    with connection.begin_nested():
                        connection.execute(drift_gate, parameters)
                except DBAPIError as error:
                    if error.orig.sqlstate != "55P03":
                        raise
                else:
                    raise AssertionError("Drift chưa tới bước khóa nguồn QA.")
            observer_drift_scope = connection.execute(drift_gate, parameters).scalar_one()
            try:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "SELECT public.wp06_authorize_learning_drift_source_scope("
                            ":workspace,:decision,TIMESTAMPTZ '2000-01-01 00:00:00+00')"
                        ),
                        parameters,
                    )
            except DBAPIError as error:
                if error.orig.sqlstate != "42501":
                    raise
            else:
                raise AssertionError("Observer có thể truyền thời điểm vào cổng Drift cũ.")
            for helper in (
                "wp06_paper_performance_decision_source_ids(:workspace,:decision)",
                "wp06_authorize_paper_performance_decision_source_scope("
                ":workspace,:decision,pg_catalog.clock_timestamp())",
            ):
                try:
                    with connection.begin_nested():
                        connection.execute(text(f"SELECT public.{helper}"), parameters)
                except DBAPIError as error:
                    if error.orig.sqlstate != "42501":
                        raise
                else:
                    raise AssertionError(f"Observer gọi trực tiếp được hàm nội bộ {helper}")
            observer_source_ids = connection.execute(
                text("SELECT public.wp06_shadow_drift_decision_source_ids(:workspace,:decision)"),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            observer_shadow_scope = connection.execute(
                text(
                    "SELECT public.wp06_authorize_shadow_inference_source_scope("
                    ":workspace,:decision,pg_catalog.clock_timestamp())"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            observer_can_execute = connection.execute(
                text(
                    "SELECT pg_catalog.has_function_privilege(current_user, "
                    "'public.wp06_shadow_drift_decision_source_ids(uuid,uuid)', 'EXECUTE') "
                    "AND pg_catalog.has_function_privilege(current_user, "
                    "'public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,timestamptz)', "
                    "'EXECUTE') "
                    "AND pg_catalog.has_function_privilege(current_user, "
                    "'public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)', "
                    "'EXECUTE') "
                    "AND NOT pg_catalog.has_function_privilege(current_user, "
                    "'public.wp06_authorize_learning_drift_source_scope(uuid,uuid,timestamptz)', "
                    "'EXECUTE')"
                )
            ).scalar_one()
        drift_writer, assessment = asyncio.run(
            _check_drift_application(
                observer_engine.url,
                workspace_id,
                decision_id,
                challenger_model.registry_id,
                observation_id,
                missing_lineage_observation_id,
            )
        )
        # So hash thuần trên composite tạm của assessment QA đã bị từ chối quyền.
        sample = replace(assessment, snapshot_coverage_rate=1e-7)
        assert sample.source_rights_digest is not None
        assert '"snapshot_coverage_rate":1e-07' in sample.canonical_json
        row = json.loads(sample.canonical_json)
        row["id"] = row.pop("assessment_id")
        members = row.pop("member_ids")
        with super_engine.begin() as connection:
            sql_hash = connection.execute(
                text(
                    "SELECT public.wp06_drift_assessment_content_hash("
                    "pg_catalog.json_populate_record(NULL::public.learning_drift_assessment, "
                    "CAST(:row AS json)), CAST(:members AS json))"
                ),
                {"row": json.dumps(row), "members": json.dumps(members)},
            ).scalar_one()
            assert sql_hash == sample.content_hash
        membership_race = asyncio.run(_check_membership_race(super_engine, observer_engine.url))
        shadow_writer = asyncio.run(_check_shadow_application_qa_writer(observer_engine.url))
        outcome = {
            **membership_race,
            **shadow_writer,
            "app_role_cannot_execute_scope_gates": role_isolated,
            "caller_clock_internal_gate_is_private": internal_gate_private,
            "drift_qa_source_denied": drift_scope is None,
            "missing_assessment_recheck_false": missing_drift is False,
            "missing_decision_denied": missing_shadow is None,
            "missing_observation_recheck_false": missing_observation is False,
            "observer_can_execute_shadow_source_gate": observer_can_execute,
            "observer_drift_qa_source_denied": observer_drift_scope is None,
            **drift_writer,
            "observer_qa_source_is_explicit": observer_source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"],
            "observer_shadow_qa_source_denied": observer_shadow_scope is None,
            "qa_source_is_explicit": source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"],
            "shadow_guard_can_execute_current_scope_and_digest": (
                shadow_guard_can_execute_current_gate
            ),
            "shadow_qa_source_denied": shadow_scope is None,
            "past_caller_clock_cannot_bypass_qa_denial": past_clock_shadow_scope is None,
            "future_caller_clock_cannot_bypass_qa_denial": future_clock_shadow_scope is None,
        }
        if not all(outcome.values()):
            raise AssertionError(f"QA-only source-use denial probe failed: {outcome!r}")
        print(json.dumps(outcome, sort_keys=True))
    finally:
        observer_engine.dispose()
        admin_engine.dispose()
        super_engine.dispose()


if __name__ == "__main__":
    main()
