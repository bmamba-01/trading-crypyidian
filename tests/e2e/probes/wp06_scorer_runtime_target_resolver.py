"""Kiểm role thật và hợp đồng fail-closed của gateway scorer WP06 C4."""

from __future__ import annotations

import json
import os
from datetime import UTC, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
    LearningScoringDeploymentEventRecord,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
)


def main() -> None:
    runtime_password = Path(os.environ["WP06_RUNTIME_PASSWORD_FILE"]).read_text().strip()
    admin_web_password = Path(os.environ["WP06_ADMIN_WEB_PASSWORD_FILE"]).read_text().strip()
    admin_web = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_admin_web",
            password=admin_web_password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    workspace_uuid = uuid4()
    workspace_id = str(workspace_uuid)
    actor_membership_id = uuid4()
    deployment_event_id = uuid4()
    with admin_web.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO public.workspace(id,name,slug,mode,status) "
                "VALUES (CAST(:id AS uuid),:name,:slug,'PAPER','ACTIVE')"
            ),
            {
                "id": workspace_id,
                "name": "WP06 runtime target probe",
                "slug": f"wp06-runtime-{workspace_id[:8]}",
            },
        )
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": workspace_id},
        )
        user_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO public.user_account(id,email_normalized,password_hash,status) "
                "VALUES (:id,:email,'probe-password-hash','ACTIVE')"
            ),
            {"id": user_id, "email": f"wp06-runtime-{workspace_id[:8]}@example.test"},
        )
        connection.execute(
            text(
                "INSERT INTO public.user_membership(id,workspace_id,user_id,status) "
                "VALUES (:id,:workspace_id,:user_id,'ACTIVE')"
            ),
            {
                "id": actor_membership_id,
                "workspace_id": workspace_uuid,
                "user_id": user_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.role_binding(id,workspace_id,membership_id,role) "
                "VALUES (:id,:workspace_id,:membership_id,'owner')"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_uuid,
                "membership_id": actor_membership_id,
            },
        )
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": workspace_id},
        )
        now = connection.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        event = ScorerDeploymentEvent(
            event_id=deployment_event_id,
            workspace_id=workspace_uuid,
            revision=1,
            mode=ScorerDeploymentMode.DISABLED,
            target=None,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=now - timedelta(minutes=1),
            mfa_method="TOTP",
            expected_pointer_version=0,
            expected_deployment_revision=0,
            reason="Seed event để kiểm workspace RLS scorer.",
            idempotency_key=uuid4(),
            previous_event_id=None,
            previous_event_hash=None,
            created_at=now.astimezone(UTC),
        )
        record = LearningScoringDeploymentEventRecord.from_domain(event)
        record_values = {
            column.name: getattr(record, column.name) for column in record.__table__.columns
        }
        connection.execute(record.__table__.insert().values(**record_values))
    admin_web.dispose()

    admin_password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    admin = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=admin_password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_trading_runtime",
            password=runtime_password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    with engine.begin() as connection:
        current_user = connection.scalar(text("SELECT session_user"))
        assert current_user == "crypidian_trading_runtime", current_user
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": workspace_id},
        )
        active_targets = connection.execute(
            text(
                "SELECT * FROM public.wp06_resolve_scorer_runtime_target("
                "target_workspace => CAST(:workspace_id AS uuid), "
                "target_mode => NULL::text, target => NULL::jsonb, "
                "target_deployment_event_id => NULL::uuid, "
                "target_deployment_revision => NULL::integer, target_is_pending => false)"
            ),
            {"workspace_id": workspace_id},
        ).all()
        disabled_workspace_is_inert = active_targets == []
        invalid_target_rejected = False
        try:
            connection.execute(
                text(
                    "SELECT * FROM public.wp06_resolve_scorer_runtime_target("
                    "target_workspace => CAST(:workspace_id AS uuid), "
                    "target_mode => 'SHADOW', target => CAST('{}' AS jsonb), "
                    "target_deployment_event_id => CAST(:event_id AS uuid), "
                    "target_deployment_revision => 1, target_is_pending => false)"
                ),
                {"workspace_id": workspace_id, "event_id": str(uuid4())},
            )
        except DBAPIError as error:
            invalid_target_rejected = "WP06_SCORER_RUNTIME_TARGET_EVENT_STALE" in str(error.orig)
            if not invalid_target_rejected:
                raise AssertionError(str(error.orig)) from error
    engine.dispose()

    with admin.begin() as connection:
        runtime_executes = connection.scalar(
            text(
                "SELECT has_function_privilege('crypidian_trading_runtime', "
                "'public.wp06_resolve_scorer_runtime_target("
                "uuid,text,jsonb,uuid,integer,boolean)', "
                "'EXECUTE')"
            )
        )
        runtime_cannot_read = not connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_trading_runtime', "
                "'public.learning_scorer_artifact', 'canonical_artifact', 'SELECT')"
            )
        )
        runtime_cannot_read_deployment_event = not connection.scalar(
            text(
                "SELECT has_table_privilege('crypidian_trading_runtime', "
                "'public.learning_scoring_deployment_event', 'SELECT')"
            )
        ) and not connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_trading_runtime', "
                "'public.learning_scoring_deployment_event', 'event_payload', 'SELECT')"
            )
        )
        runtime_cannot_read_deployment_event = runtime_cannot_read_deployment_event and all(
            not connection.scalar(
                text(
                    "SELECT has_column_privilege('crypidian_trading_runtime', "
                    "'public.learning_scoring_deployment_event', :column_name, 'SELECT')"
                ),
                {"column_name": name},
            )
            for name in (
                "workspace_id",
                "id",
                "mode",
                "revision",
                "scorer_artifact_id",
                "evaluation_report_id",
                "event_payload",
            )
        )
        runtime_cannot_read_deployment_pointer = not connection.scalar(
            text(
                "SELECT has_table_privilege('crypidian_trading_runtime', "
                "'public.learning_scoring_deployment_pointer', 'SELECT')"
            )
        ) and not connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_trading_runtime', "
                "'public.learning_scoring_deployment_pointer', 'event_id', 'SELECT')"
            )
        )
        runtime_cannot_read_deployment_pointer = runtime_cannot_read_deployment_pointer and all(
            not connection.scalar(
                text(
                    "SELECT has_column_privilege('crypidian_trading_runtime', "
                    "'public.learning_scoring_deployment_pointer', :column_name, 'SELECT')"
                ),
                {"column_name": name},
            )
            for name in (
                "workspace_id",
                "event_id",
                "revision",
                "active_pointer_version",
                "mode",
            )
        )
        worker_cannot_read_deployment_event = not connection.scalar(
            text(
                "SELECT has_table_privilege('crypidian_market_signal_worker', "
                "'public.learning_scoring_deployment_event', 'SELECT')"
            )
        ) and all(
            not connection.scalar(
                text(
                    "SELECT has_column_privilege('crypidian_market_signal_worker', "
                    "'public.learning_scoring_deployment_event', :column_name, 'SELECT')"
                ),
                {"column_name": name},
            )
            for name in (
                "workspace_id",
                "id",
                "mode",
                "revision",
                "scorer_artifact_id",
                "evaluation_report_id",
            )
        )
        worker_cannot_read_deployment_pointer = not connection.scalar(
            text(
                "SELECT has_table_privilege('crypidian_market_signal_worker', "
                "'public.learning_scoring_deployment_pointer', 'SELECT')"
            )
        ) and all(
            not connection.scalar(
                text(
                    "SELECT has_column_privilege('crypidian_market_signal_worker', "
                    "'public.learning_scoring_deployment_pointer', :column_name, 'SELECT')"
                ),
                {"column_name": name},
            )
            for name in (
                "workspace_id",
                "event_id",
                "revision",
                "active_pointer_version",
                "mode",
            )
        )
        rights_metadata = connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_training_artifact', 'canonical_artifact', 'SELECT') "
                "AND has_column_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_training_artifact', 'artifact', 'SELECT') "
                "AND NOT has_table_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_training_artifact', 'SELECT')"
            )
        )
        deployment_pointer_metadata = connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_scoring_deployment_pointer', 'event_id', 'SELECT') "
                "AND NOT has_table_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_scoring_deployment_pointer', 'SELECT')"
            )
        )
        deployment_event_payload_metadata = connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_scoring_deployment_event', 'event_payload', 'SELECT') "
                "AND NOT has_table_privilege('crypidian_scorer_deployment_rights_reader', "
                "'public.learning_scoring_deployment_event', 'SELECT')"
            )
        )
        trigger_definition = connection.scalar(
            text(
                "SELECT pg_get_functiondef('public.wp06_scoring_deployment_guard()'::regprocedure)"
            )
        )
        apply_definition = connection.scalar(
            text(
                "SELECT pg_get_functiondef('public.wp06_scoring_deployment_apply()'::regprocedure)"
            )
        )
        resolver_definition = connection.scalar(
            text(
                "SELECT pg_get_functiondef('public.wp06_resolve_scorer_runtime_target("
                "uuid,text,jsonb,uuid,integer,boolean)'::regprocedure)"
            )
        )
        guard_trigger = connection.scalar(
            text(
                "SELECT pg_get_triggerdef(trigger_row.oid) "
                "FROM pg_catalog.pg_trigger AS trigger_row "
                "WHERE trigger_row.tgrelid = "
                "'public.learning_scoring_deployment_event'::pg_catalog.regclass "
                "AND trigger_row.tgname = 'learning_scoring_deployment_event_guard' "
                "AND NOT trigger_row.tgisinternal"
            )
        )
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": workspace_id},
        )
        connection.execute(text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader"))
        visible_event = connection.execute(
            text(
                "SELECT id,event_payload->>'mode' AS mode "
                "FROM public.learning_scoring_deployment_event "
                "WHERE workspace_id=CAST(:workspace_id AS uuid) "
                "AND id=CAST(:event_id AS uuid)"
            ),
            {"workspace_id": workspace_id, "event_id": str(deployment_event_id)},
        ).one_or_none()
        rights_role_can_read_workspace_event = (
            visible_event is not None
            and visible_event.id == deployment_event_id
            and visible_event.mode == ScorerDeploymentMode.DISABLED.value
        )
    admin.dispose()
    assert runtime_executes is True
    assert runtime_cannot_read is True
    assert runtime_cannot_read_deployment_event is True
    assert runtime_cannot_read_deployment_pointer is True
    assert worker_cannot_read_deployment_event is True
    assert worker_cannot_read_deployment_pointer is True
    assert disabled_workspace_is_inert is True
    assert rights_metadata is True
    assert deployment_pointer_metadata is True
    assert deployment_event_payload_metadata is True
    assert rights_role_can_read_workspace_event is True
    assert "wp06_resolve_scorer_runtime_target" in trigger_definition
    assert "WP06_SCORING_DEPLOYMENT_RIGHTS_GATE_UNAVAILABLE" not in trigger_definition
    assert "new.previous_event_id" in trigger_definition.lower()
    assert "new.expected_deployment_revision" in trigger_definition.lower()
    assert "new.mode" in trigger_definition.lower()
    assert "true" in trigger_definition.lower()
    assert guard_trigger is not None
    assert "BEFORE INSERT" in guard_trigger
    assert "new.id" in apply_definition.lower()
    assert "new.revision" in apply_definition.lower()
    assert "event.event_payload->'target'" in resolver_definition.lower()
    assert "wp06_resolve_scorer_runtime_target" in resolver_definition

    print(
        json.dumps(
            {
                "runtime_executes_resolver": runtime_executes,
                "runtime_cannot_read_scorer_rows": runtime_cannot_read,
                "runtime_cannot_read_deployment_event": runtime_cannot_read_deployment_event,
                "runtime_cannot_read_deployment_pointer": runtime_cannot_read_deployment_pointer,
                "market_signal_worker_cannot_read_deployment_event": (
                    worker_cannot_read_deployment_event
                ),
                "market_signal_worker_cannot_read_deployment_pointer": (
                    worker_cannot_read_deployment_pointer
                ),
                "rights_role_has_only_target_metadata": rights_metadata,
                "rights_role_has_deployment_pointer_metadata": deployment_pointer_metadata,
                "rights_role_has_event_payload_metadata": deployment_event_payload_metadata,
                "rights_role_can_read_workspace_event": rights_role_can_read_workspace_event,
                "trigger_uses_resolver": "wp06_resolve_scorer_runtime_target" in trigger_definition,
                "trigger_binds_pending_predecessor": (
                    "new.previous_event_id" in trigger_definition.lower()
                    and "new.expected_deployment_revision" in trigger_definition.lower()
                    and "true" in trigger_definition.lower()
                ),
                "trigger_is_before_insert": (
                    guard_trigger is not None and "BEFORE INSERT" in guard_trigger
                ),
                "after_insert_applies_new_pointer": (
                    "new.id" in apply_definition.lower()
                    and "new.revision" in apply_definition.lower()
                ),
                "invalid_target_rejected": invalid_target_rejected,
                "disabled_workspace_is_inert": disabled_workspace_is_inert,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
