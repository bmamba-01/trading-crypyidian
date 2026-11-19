"""Probe QA-only denials cho exact source scope Shadow/Drift."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.repositories.learning_drift import LearningDriftRepository
from trading_bot.application.learning_drift import LearningDriftApplication

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
    observer_url: URL, workspace_id: UUID, decision_id: UUID
) -> None:
    """Gọi repository của writer trên decision QA và ứng dụng thật với cohort trống."""
    engine = create_async_engine(observer_url)
    sessions = async_sessionmaker(engine)
    try:
        async with sessions.begin() as session:
            repository = LearningDriftRepository(session)
            scope = await repository.authorize_current_source_scope(
                workspace_id=workspace_id, decision_id=decision_id
            )
            assert scope is None
        run = await LearningDriftApplication(sessions).assess_cohort(
            workspace_id=workspace_id, challenger_registry_id=uuid4(), challenger_revision=1
        )
        assert run is None
    finally:
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
            decision_id, _ = _seed_qa_decision_source(connection, workspace_id, uuid4())
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
        asyncio.run(_check_drift_application(observer_engine.url, workspace_id, decision_id))
        with super_engine.begin() as connection:
            for table in ("learning_drift_assessment", "learning_drift_assessment_member"):
                count = connection.execute(
                    text(f"SELECT count(*) FROM public.{table} WHERE workspace_id = :workspace"),
                    {"workspace": workspace_id},
                ).scalar_one()
                assert count == 0
        outcome = {
            "app_role_cannot_execute_scope_gates": role_isolated,
            "drift_qa_source_denied": drift_scope is None,
            "missing_assessment_recheck_false": missing_drift is False,
            "missing_decision_denied": missing_shadow is None,
            "missing_observation_recheck_false": missing_observation is False,
            "observer_can_execute_shadow_source_gate": observer_can_execute,
            "observer_drift_qa_source_denied": observer_drift_scope is None,
            "observer_qa_source_is_explicit": observer_source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"],
            "observer_shadow_qa_source_denied": observer_shadow_scope is None,
            "qa_source_is_explicit": source_ids == ["CRYPIDIAN_FIRST_PARTY_QA"],
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
