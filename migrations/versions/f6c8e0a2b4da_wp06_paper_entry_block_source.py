"""Đọc metadata nguồn cho thao tác chặn PAPER qua role chỉ đọc riêng."""

from collections.abc import Sequence

from alembic import op

revision: str = "f6c8e0a2b4da"
down_revision: str | None = "f6c8e0a2b4d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD = "crypidian_paper_entry_block_reader"
ADMIN = "crypidian_admin_web"
FUNCTION_NAME = "public.wp06_read_paper_entry_block_source"
FUNCTION = f"{FUNCTION_NAME}(uuid,uuid)"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
POLICY = "wp06_paper_entry_block_source_read"
WORKSPACE_GUARD = "wp06_paper_entry_block_source_workspace_guard"
COLUMNS = {
    "bot_decision": "workspace_id, bot_id, id, status, candidate_id, decision_hash",
    "learning_score_event": (
        "workspace_id, bot_id, bot_decision_id, bot_decision_hash, candidate_id, "
        "id, event_hash, mode, decision, deployment_event_id, deployment_revision, registry_id, "
        "registry_revision, active_pointer_version, scorer_artifact_id, scorer_artifact_hash, "
        "source_rights_fingerprint"
    ),
}


def upgrade() -> None:
    op.execute(f"""
        DO $$
        DECLARE guard_oid oid;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{GUARD}') THEN
                CREATE ROLE {GUARD} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                    NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
            SELECT oid INTO guard_oid FROM pg_catalog.pg_roles WHERE rolname = '{GUARD}';
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                 WHERE oid = guard_oid
                   AND (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb
                        OR rolcreaterole OR rolreplication)
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_auth_members
                 WHERE member = guard_oid OR roleid = guard_oid
            ) THEN
                RAISE EXCEPTION 'WP06_PAPER_ENTRY_BLOCK_SOURCE_ROLE_UNSAFE';
            END IF;
        END $$;
    """)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {GUARD}")
    for table, columns in COLUMNS.items():
        op.execute(f"GRANT SELECT ({columns}) ON TABLE public.{table} TO {GUARD}")
        op.execute(
            f"CREATE POLICY {POLICY} ON public.{table} FOR SELECT TO {GUARD} USING ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY {WORKSPACE_GUARD} ON public.{table} AS RESTRICTIVE "
            f"FOR SELECT TO {GUARD} USING ({WORKSPACE})"
        )
    op.execute(f"""
        CREATE FUNCTION {FUNCTION_NAME}(target_workspace uuid, target_decision uuid)
        RETURNS TABLE (
            bot_id uuid,
            bot_decision_hash text,
            candidate_id text,
            score_event_id uuid,
            score_event_hash text,
            deployment_event_id uuid,
            deployment_revision integer,
            registry_id uuid,
            registry_revision integer,
            active_pointer_version integer,
            scorer_artifact_id uuid,
            scorer_artifact_hash text,
            source_rights_fingerprint text
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        SET row_security = on
        AS $$
            SELECT score.bot_id, score.bot_decision_hash, score.candidate_id, score.id,
                   score.event_hash, score.deployment_event_id, score.deployment_revision,
                   score.registry_id, score.registry_revision, score.active_pointer_version,
                   score.scorer_artifact_id, score.scorer_artifact_hash,
                   score.source_rights_fingerprint
              FROM public.learning_score_event AS score
              JOIN public.bot_decision AS decision
                ON decision.workspace_id = score.workspace_id
               AND decision.bot_id = score.bot_id
               AND decision.id = score.bot_decision_id
               AND decision.decision_hash = score.bot_decision_hash
               AND decision.candidate_id = score.candidate_id
             WHERE session_user = '{ADMIN}'
               AND target_workspace = NULLIF(
                   pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               AND score.workspace_id = target_workspace
               AND score.bot_decision_id = target_decision
               AND score.mode = 'PAPER_GATE' AND score.decision = 'APPROVE'
               AND decision.status = 'CANDIDATE'
        $$;
    """)
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {GUARD}")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    for table, columns in COLUMNS.items():
        op.execute(f"DROP POLICY {WORKSPACE_GUARD} ON public.{table}")
        op.execute(f"DROP POLICY {POLICY} ON public.{table}")
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {GUARD}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {GUARD}")
    op.execute(f"DROP ROLE {GUARD}")
