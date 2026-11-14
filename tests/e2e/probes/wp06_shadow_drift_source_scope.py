"""Probe QA-only denials cho exact source scope Shadow/Drift."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

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
                    "'EXECUTE')"
                )
            ).scalar_one()
        with observer_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
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
                    "'EXECUTE')"
                )
            ).scalar_one()
        outcome = {
            "app_role_cannot_execute_scope_gates": role_isolated,
            "drift_qa_source_denied": drift_scope is None,
            "missing_assessment_recheck_false": missing_drift is False,
            "missing_decision_denied": missing_shadow is None,
            "missing_observation_recheck_false": missing_observation is False,
            "observer_can_execute_shadow_source_gate": observer_can_execute,
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
