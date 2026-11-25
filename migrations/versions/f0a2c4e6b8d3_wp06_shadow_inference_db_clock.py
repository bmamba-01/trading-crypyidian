"""Dùng giờ PostgreSQL sau source lock cho quyền Shadow inference."""

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d3"
down_revision: str | None = "f0a2c4e6b8d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
OBSERVER = "crypidian_learning_shadow_observer"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
GATE = "public.wp06_authorize_shadow_inference_source_scope"
INTERNAL_GATE = "public.wp06_authorize_shadow_inference_source_scope_with_caller_clock"


def upgrade() -> None:
    """Giữ tên API nhưng lấy mốc quyền trực tiếp từ PostgreSQL sau khi khóa nguồn."""
    op.execute(
        f"ALTER FUNCTION {GATE}(uuid,uuid,timestamptz) "
        "RENAME TO wp06_authorize_shadow_inference_source_scope_with_caller_clock"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {INTERNAL_GATE}(uuid,uuid,timestamptz) "
        f"FROM PUBLIC, {ADMIN}, {OBSERVER}, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(f"""
        CREATE FUNCTION {GATE}(
            target_workspace uuid,
            target_decision uuid,
            caller_checked_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source_ids text[];
            source_id text;
            database_checked_at timestamptz;
        BEGIN
            IF session_user NOT IN ('{ADMIN}', '{OBSERVER}')
               OR target_workspace IS NULL OR target_decision IS NULL
               OR caller_checked_at IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_SOURCE_SCOPE_DENIED';
            END IF;

            source_ids := public.wp06_shadow_drift_decision_source_ids(
                target_workspace, target_decision);
            IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0 THEN
                RETURN NULL;
            END IF;
            FOREACH source_id IN ARRAY source_ids LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;

            database_checked_at := pg_catalog.clock_timestamp();
            RETURN {INTERNAL_GATE}(
                target_workspace, target_decision, database_checked_at);
        END; $$
    """)
    op.execute(f"ALTER FUNCTION {GATE}(uuid,uuid,timestamptz) OWNER TO {RIGHTS_ROLE}")
    op.execute(
        f"REVOKE ALL ON FUNCTION {GATE}(uuid,uuid,timestamptz) "
        "FROM PUBLIC, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {GATE}(uuid,uuid,timestamptz) TO {ADMIN}, {OBSERVER}")


def downgrade() -> None:
    """Khôi phục chữ ký cũ nhưng không mở quyền cho các vai trò rộng hơn trước."""
    op.execute(f"DROP FUNCTION {GATE}(uuid,uuid,timestamptz)")
    op.execute(
        f"ALTER FUNCTION {INTERNAL_GATE}(uuid,uuid,timestamptz) "
        "RENAME TO wp06_authorize_shadow_inference_source_scope"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {GATE}(uuid,uuid,timestamptz) TO {ADMIN}, {OBSERVER}")
