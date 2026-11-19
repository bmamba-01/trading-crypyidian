"""Cho phép observer kiểm quyền tổng hợp Drift theo lineage đã lưu."""

from collections.abc import Sequence

from alembic import op

revision: str = "a6c8e0b2d4f7"
down_revision: str | None = "f4b6d8e0a2c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GATE = "public.wp06_authorize_learning_drift_source_scope(uuid,uuid,timestamptz)"
_PAPER_SCOPE = (
    "public.wp06_authorize_paper_performance_decision_source_scope(uuid,uuid,timestamptz)"
)
_PAPER_SOURCE_IDS = "public.wp06_paper_performance_decision_source_ids(uuid,uuid)"
_CURRENT_GATE = "public.wp06_authorize_learning_drift_source_scope_current"
_CURRENT_GATE_SIGNATURE = f"{_CURRENT_GATE}(uuid,uuid)"
_OBSERVER = "crypidian_learning_shadow_observer"
_RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"


def _replace_login_guard(*, restore: bool = False) -> None:
    # Hai hàm PAPER vẫn chỉ truy cập được qua quyền của owner khi observer gọi cổng Drift.
    old = "IF session_user <> 'crypidian_admin_web'"
    new = f"IF session_user NOT IN ('crypidian_admin_web', '{_OBSERVER}')"
    if restore:
        old, new = new, old
    for function in (_PAPER_SOURCE_IDS, _PAPER_SCOPE, _GATE):
        op.execute(f"""
            DO $migration$
            DECLARE
                definition text := pg_catalog.pg_get_functiondef('{function}'::regprocedure);
                old_fragment text := $old${old}$old$;
            BEGIN
                IF pg_catalog.length(definition) - pg_catalog.length(
                    pg_catalog.replace(definition, old_fragment, '')
                ) IS DISTINCT FROM pg_catalog.length(old_fragment) THEN
                    RAISE EXCEPTION 'WP06_DRIFT_OBSERVER_SCOPE_PREIMAGE_UNEXPECTED';
                END IF;
                EXECUTE pg_catalog.replace(definition, old_fragment, $new${new}$new$);
            END
            $migration$
        """)


def upgrade() -> None:
    _replace_login_guard()
    op.execute(f"""
        CREATE FUNCTION {_CURRENT_GATE}(
            target_workspace uuid, target_decision uuid
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source_ids text[];
            source_id text;
            checked_at timestamptz;
        BEGIN
            IF session_user <> '{_OBSERVER}'
               OR target_workspace IS NULL OR target_decision IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_SCOPE_DENIED';
            END IF;
            source_ids := public.wp06_paper_performance_decision_source_ids(
                target_workspace, target_decision);
            IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0
               OR pg_catalog.array_position(source_ids, NULL) IS NOT NULL THEN
                RETURN NULL;
            END IF;
            FOR source_id IN
                SELECT candidate.source_id
                  FROM pg_catalog.unnest(source_ids) AS candidate(source_id)
                 ORDER BY candidate.source_id
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            checked_at := pg_catalog.clock_timestamp();
            RETURN public.wp06_authorize_learning_drift_source_scope(
                target_workspace, target_decision, checked_at);
        EXCEPTION WHEN raise_exception THEN
            RETURN NULL;
        END; $$
    """)
    op.execute(f"ALTER FUNCTION {_CURRENT_GATE_SIGNATURE} OWNER TO {_RIGHTS_ROLE}")
    op.execute(
        f"REVOKE ALL ON FUNCTION {_CURRENT_GATE_SIGNATURE} "
        "FROM PUBLIC, crypidian_admin_web, "
        "crypidian_app, crypidian_trading_runtime"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_CURRENT_GATE_SIGNATURE} TO {_OBSERVER}")


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.learning_shadow_observation, public.learning_drift_assessment, "
        "public.learning_drift_assessment_member IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$
        DECLARE
            previous_row_security text := pg_catalog.current_setting('row_security');
        BEGIN
            SET LOCAL row_security = off;
            IF EXISTS (SELECT 1 FROM public.learning_shadow_observation)
               OR EXISTS (SELECT 1 FROM public.learning_drift_assessment)
               OR EXISTS (SELECT 1 FROM public.learning_drift_assessment_member) THEN
                RAISE EXCEPTION 'WP06_DRIFT_OBSERVER_SCOPE_DOWNGRADE_POPULATED_DENIED';
            END IF;
            PERFORM pg_catalog.set_config('row_security', previous_row_security, true);
        END $$
    """)
    op.execute(f"DROP FUNCTION {_CURRENT_GATE_SIGNATURE}")
    _replace_login_guard(restore=True)
