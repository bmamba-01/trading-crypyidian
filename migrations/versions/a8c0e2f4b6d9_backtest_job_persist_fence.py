"""WP-07C: khóa quyền persist và hoàn tất job trong transaction ghi outcome."""

from collections.abc import Sequence

from alembic import op

revision: str = "a8c0e2f4b6d9"
down_revision: str | None = "f7a9c1e3d5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER = "crypidian_backtest_persist_owner"
TRAINER = "crypidian_trainer_worker"
TABLE = "public.backtest_replay_job"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
SELECT_COLUMNS = (
    "id, workspace_id, lease_owner, lease_generation, lease_until, status, cancel_requested, stage"
)
UPDATE_COLUMNS = "status, stage, progress, result_run_ids, lease_owner, lease_until, error_code"
BEGIN_SIGNATURE = "public.begin_backtest_job_persist(uuid, uuid, text, integer)"
COMPLETE_SIGNATURE = "public.complete_backtest_job_persist(uuid, uuid, text, integer, uuid[])"
MARKET_TABLES = (
    "public.market_instrument, public.market_bar, "
    "public.market_funding, public.market_open_interest"
)


def upgrade() -> None:
    # Grant legacy từ WP-02 không còn phù hợp: trainer chỉ persist, worker đọc dữ liệu riêng.
    op.execute(f"REVOKE SELECT ON TABLE {MARKET_TABLES} FROM {TRAINER}")
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{OWNER}') THEN
                CREATE ROLE {OWNER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                    NOCREATEDB NOCREATEROLE NOREPLICATION;
            ELSIF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = '{OWNER}'
                AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolinherit
                    OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members m
                        WHERE m.roleid = r.oid OR m.member = r.oid
                    ))
            ) THEN RAISE EXCEPTION 'WP07C_PERSIST_OWNER_ROLE_UNSAFE'; END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {OWNER}")
    op.execute(f"GRANT SELECT ({SELECT_COLUMNS}) ON TABLE {TABLE} TO {OWNER}")
    op.execute(f"GRANT UPDATE ({UPDATE_COLUMNS}) ON TABLE {TABLE} TO {OWNER}")
    op.execute(
        f"CREATE POLICY backtest_persist_owner_select ON {TABLE} "
        f"FOR SELECT TO {OWNER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY backtest_persist_owner_update ON {TABLE} "
        f"FOR UPDATE TO {OWNER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        """CREATE FUNCTION public.begin_backtest_job_persist(
            p_workspace_id uuid, p_job_id uuid, p_worker_id text, p_generation integer
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE job record;
        BEGIN
            IF p_workspace_id IS NULL OR p_workspace_id IS DISTINCT FROM
               NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP07C_PERSIST_WORKSPACE_INVALID';
            END IF;
            SELECT id, workspace_id, lease_owner, lease_generation, lease_until,
                   status, cancel_requested, stage INTO job
            FROM public.backtest_replay_job
            WHERE workspace_id = p_workspace_id AND id = p_job_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP07C_PERSIST_JOB_MISSING'; END IF;
            IF job.status <> 'RUNNING' THEN
                RAISE EXCEPTION 'WP07C_PERSIST_STATUS_INVALID';
            END IF;
            IF job.lease_owner IS DISTINCT FROM p_worker_id OR
               job.lease_generation IS DISTINCT FROM p_generation THEN
                RAISE EXCEPTION 'WP07C_PERSIST_OWNER_INVALID';
            END IF;
            IF job.cancel_requested THEN RAISE EXCEPTION 'WP07C_PERSIST_CANCELLED'; END IF;
            IF job.lease_until IS NULL OR job.lease_until <= pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'WP07C_PERSIST_LEASE_EXPIRED';
            END IF;
            UPDATE public.backtest_replay_job SET stage = 'persist'
            WHERE workspace_id = p_workspace_id AND id = p_job_id;
            -- Marker chỉ ràng buộc protocol của application tin cậy trong transaction hiện tại.
            PERFORM pg_catalog.set_config('app.backtest_job_persist',
                pg_catalog.jsonb_build_array(
                    p_workspace_id, p_job_id, p_worker_id, p_generation,
                    pg_catalog.pg_current_xact_id()::text
                )::text, true);
        END; $$"""
    )
    op.execute(
        """CREATE FUNCTION public.complete_backtest_job_persist(
            p_workspace_id uuid, p_job_id uuid, p_worker_id text, p_generation integer,
            p_result_run_ids uuid[]
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE job record;
        BEGIN
            IF p_workspace_id IS NULL OR p_workspace_id IS DISTINCT FROM
               NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP07C_PERSIST_WORKSPACE_INVALID';
            END IF;
            IF pg_catalog.current_setting('app.backtest_job_persist', true)
               IS DISTINCT FROM pg_catalog.jsonb_build_array(
                    p_workspace_id, p_job_id, p_worker_id, p_generation,
                    pg_catalog.pg_current_xact_id()::text
               )::text THEN
                RAISE EXCEPTION 'WP07C_PERSIST_BEGIN_REQUIRED';
            END IF;
            IF p_result_run_ids IS NULL OR pg_catalog.cardinality(p_result_run_ids) = 0 OR
               pg_catalog.array_position(p_result_run_ids, NULL::uuid) IS NOT NULL THEN
                RAISE EXCEPTION 'WP07C_PERSIST_RESULTS_INVALID';
            END IF;
            SELECT id, workspace_id, lease_owner, lease_generation, status, cancel_requested,
                   stage INTO job FROM public.backtest_replay_job
            WHERE workspace_id = p_workspace_id AND id = p_job_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP07C_PERSIST_JOB_MISSING'; END IF;
            IF job.status <> 'RUNNING' OR job.stage <> 'persist' THEN
                RAISE EXCEPTION 'WP07C_PERSIST_STATUS_INVALID';
            END IF;
            IF job.lease_owner IS DISTINCT FROM p_worker_id OR
               job.lease_generation IS DISTINCT FROM p_generation THEN
                RAISE EXCEPTION 'WP07C_PERSIST_OWNER_INVALID';
            END IF;
            IF job.cancel_requested THEN RAISE EXCEPTION 'WP07C_PERSIST_CANCELLED'; END IF;
            -- Begin đã giữ row lock; lease hết sau đó không làm mất quyền commit transaction này.
            UPDATE public.backtest_replay_job
            SET status = 'SUCCEEDED', stage = 'completed', progress = 100,
                result_run_ids = pg_catalog.to_jsonb(p_result_run_ids),
                lease_owner = NULL, lease_until = NULL, error_code = NULL
            WHERE workspace_id = p_workspace_id AND id = p_job_id;
            PERFORM pg_catalog.set_config('app.backtest_job_persist', '', true);
        END; $$"""
    )
    for signature in (BEGIN_SIGNATURE, COMPLETE_SIGNATURE):
        op.execute(f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {OWNER}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {TRAINER}")


def downgrade() -> None:
    # Thu hồi điểm vào trước, rồi bỏ function/policy/grant; không đụng dữ liệu job/outcome.
    for signature in (COMPLETE_SIGNATURE, BEGIN_SIGNATURE):
        op.execute(f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM {TRAINER}, PUBLIC")
        op.execute(f"DROP FUNCTION {signature}")
    op.execute(f"DROP POLICY backtest_persist_owner_update ON {TABLE}")
    op.execute(f"DROP POLICY backtest_persist_owner_select ON {TABLE}")
    op.execute(f"REVOKE UPDATE ({UPDATE_COLUMNS}) ON TABLE {TABLE} FROM {OWNER}")
    op.execute(f"REVOKE SELECT ({SELECT_COLUMNS}) ON TABLE {TABLE} FROM {OWNER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {OWNER}")
    # Khôi phục chính xác bốn grant SELECT legacy khi quay về parent revision.
    op.execute(f"GRANT SELECT ON TABLE {MARKET_TABLES} TO {TRAINER}")
    # Role toàn cluster giữ NOLOGIN, không grant; tránh xóa dependency ở database khác.
