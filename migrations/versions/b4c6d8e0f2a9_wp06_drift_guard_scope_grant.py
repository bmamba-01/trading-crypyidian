"""Cho phép trigger Drift dùng cổng scope lấy thời gian từ PostgreSQL."""

from collections.abc import Sequence

from alembic import op

revision: str = "b4c6d8e0f2a9"
down_revision: str | None = "a6c8e0b2d4f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_ROLE = "crypidian_learning_shadow_integrity_guard"
_CURRENT_GATE = "public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)"
_RIGHTS_DIGEST = "public.wp06_learning_drift_current_rights_digest(uuid,uuid,timestamptz)"
_TRIGGER_FUNCTION = "public.wp06_require_learning_drift_member_lineage()"


def _replace_member_scope_gate(*, restore: bool = False) -> None:
    old = "public.wp06_authorize_learning_drift_source_scope(\n                    "
    old += "NEW.workspace_id, observation_row.incumbent_decision_id,\n                    "
    old += "pg_catalog.clock_timestamp())"
    new = "public.wp06_authorize_learning_drift_source_scope_current(\n                    "
    new += "NEW.workspace_id, observation_row.incumbent_decision_id)"
    if restore:
        old, new = new, old
    op.execute(f"""
        DO $migration$
        DECLARE
            function_definition text := pg_catalog.pg_get_functiondef(
                '{_TRIGGER_FUNCTION}'::regprocedure);
            old_fragment text := $old${old}$old$;
        BEGIN
            IF pg_catalog.length(function_definition) - pg_catalog.length(
                pg_catalog.replace(function_definition, old_fragment, '')
            ) IS DISTINCT FROM pg_catalog.length(old_fragment) THEN
                RAISE EXCEPTION 'WP06_DRIFT_GUARD_SCOPE_PREIMAGE_UNEXPECTED';
            END IF;
            EXECUTE pg_catalog.replace(function_definition, old_fragment, $new${new}$new$);
        END
        $migration$
    """)


def upgrade() -> None:
    _replace_member_scope_gate()
    op.execute(f"GRANT EXECUTE ON FUNCTION {_CURRENT_GATE} TO {_GUARD_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_RIGHTS_DIGEST} TO {_GUARD_ROLE}")


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
                RAISE EXCEPTION 'WP06_DRIFT_GUARD_SCOPE_GRANT_DOWNGRADE_POPULATED_DENIED';
            END IF;
            PERFORM pg_catalog.set_config('row_security', previous_row_security, true);
        END $$
    """)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_RIGHTS_DIGEST} FROM {_GUARD_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_CURRENT_GATE} FROM {_GUARD_ROLE}")
    _replace_member_scope_gate(restore=True)
