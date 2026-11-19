"""Probe QA-only denials cho exact source scope Shadow/Drift."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.repositories.learning_drift import LearningDriftRepository
from trading_bot.application.learning_drift import LearningDriftApplication
from trading_bot.domain.learning.drift import LearningDriftAssessment

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
) -> tuple[dict[str, bool], LearningDriftAssessment]:
    """Chạy Drift writer trên cohort QA không rỗng, chỉ để kiểm tra denial."""
    from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
        LearningDriftAssessmentMemberRecord,
        LearningDriftAssessmentRecord,
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
        assert run.assessment.member_count == 1
        assert run.assessment.eligible_count == 0
        assert run.assessment.data_quality_status.value == "inconclusive"
        assert run.assessment.performance_status.value == "inconclusive"
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
            assert member is not None
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
            assert assessment_count == 1
            assert member_count == 1

        return {
            "nonempty_qa_assessment_has_no_performance_result": (
                run.assessment.performance_status.value == "inconclusive"
            ),
            "nonempty_qa_assessment_retry_is_idempotent": repeated.is_duplicate,
            "nonempty_qa_member_is_ineligible_without_fingerprint": (
                member_is_ineligible_without_fingerprint
            ),
        }, run.assessment
    finally:
        await engine.dispose()


def _seed_qa_shadow_observation(
    connection, workspace_id: UUID, decision_id: UUID
) -> tuple[UUID, UUID]:
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
        ShadowModelReference,
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
        .one()
    )
    snapshot_id = source_lineage["feature_snapshot_id"]
    snapshot_hash = snapshot_id
    snapshot_as_of = decision["evaluated_at"] - timedelta(seconds=1)
    snapshot = ShadowSnapshotReference(
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        schema_hash="b" * 64,
        as_of=snapshot_as_of,
    )
    challenger_registry_id = uuid4()
    observation_id = uuid4()
    model = ShadowModelReference(
        workspace_id=workspace_id,
        registry_id=challenger_registry_id,
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
            input_bar_ids=[f"market-bar-record:{source_lineage['market_bar_id']}"],
            input_event_ids=[source_lineage["source_event_id"]],
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
    connection.execute(
        insert(LearningModelRegistryRecord).values(
            id=challenger_registry_id,
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
    return challenger_registry_id, observation_id


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
            challenger_registry_id, observation_id = _seed_qa_shadow_observation(
                connection, workspace_id, decision_id
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
                challenger_registry_id,
                observation_id,
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
        outcome = {
            "app_role_cannot_execute_scope_gates": role_isolated,
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
