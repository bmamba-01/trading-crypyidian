"""Đồng bộ budget learning với số lượt feature của CPU trainer."""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d5e7f9a1b2"
down_revision: str | None = "b2c4e6f8a0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = "public.wp06_validate_learning_training_attempt()"
_OLD_FORMULA = "v_ops := v_samples * v_features::bigint * v_steps::bigint * 3;"
_NEW_FORMULA = (
    "v_ops := 3 * v_features::bigint * "
    "(2 * v_train::bigint * v_steps::bigint + v_validation::bigint);"
)


def _replace_formula(*, old: str, new: str) -> None:
    op.execute(
        f"""DO $migration$
        DECLARE
            v_source text;
        BEGIN
            v_source := pg_catalog.pg_get_functiondef('{_FUNCTION}'::regprocedure);
            IF pg_catalog.strpos(v_source, '{old}') = 0
                OR (pg_catalog.length(v_source) - pg_catalog.length(
                    pg_catalog.replace(v_source, '{old}', '')
                )) <> pg_catalog.length('{old}') THEN
                RAISE EXCEPTION 'WP06_ATTEMPT_RESOURCE_FORMULA_UNEXPECTED';
            END IF;
            v_source := pg_catalog.replace(v_source, '{old}', '{new}');
            EXECUTE v_source;
        END
        $migration$"""
    )


def upgrade() -> None:
    _replace_formula(old=_OLD_FORMULA, new=_NEW_FORMULA)


def downgrade() -> None:
    _replace_formula(old=_NEW_FORMULA, new=_OLD_FORMULA)
