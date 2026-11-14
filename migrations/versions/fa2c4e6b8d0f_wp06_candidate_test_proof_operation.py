"""WP06: tách operation ghi TEST proof khỏi quyền evaluator đọc payload."""

from collections.abc import Sequence

from alembic import op

revision: str = "fa2c4e6b8d0f"
down_revision: str | None = "f8e0a2c4d6be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION_SIGNATURE = "public.wp06_authorize_replay_training_bundle(uuid,text,text)"
EXCEPTION = "WP06_TRAINING_REPLAY_OPERATION_DEFINITION_UNEXPECTED"

_OLD_GUARD = (
    "IF (target_operation = 'training' AND session_user <> 'crypidian_candidate_builder')\n"
    "               OR (target_operation = 'evaluation' AND session_user <> '"
    "crypidian_learning_evaluator')\n"
    "               OR target_operation NOT IN ('training', 'evaluation') THEN"
)
_NEW_GUARD = (
    "IF (target_operation IN ('training', 'candidate_test_proof')\n"
    "                    AND session_user <> 'crypidian_candidate_builder')\n"
    "               OR (target_operation = 'evaluation' AND session_user <> '"
    "crypidian_learning_evaluator')\n"
    "               OR target_operation NOT IN ('training', 'evaluation', '"
    "candidate_test_proof') THEN"
)
_OLD_OUTPUT_SCOPE = """IF target_operation = 'evaluation' THEN
                use_outputs := '[\"aggregate_report\"]'::jsonb;
                use_derived := '[\"aggregate_reporting\"]'::jsonb;
            ELSE
                use_outputs := '[\"derived_features\",\"trained_model\"]'::jsonb;
                use_derived := '[\"feature_extraction\",\"model_training\"]'::jsonb;
            END IF;"""
_NEW_OUTPUT_SCOPE = """IF target_operation IN ('evaluation', 'candidate_test_proof') THEN
                use_outputs := '[\"aggregate_report\"]'::jsonb;
                use_derived := '[\"aggregate_reporting\"]'::jsonb;
            ELSE
                use_outputs := '[\"derived_features\",\"trained_model\"]'::jsonb;
                use_derived := '[\"feature_extraction\",\"model_training\"]'::jsonb;
            END IF;"""


def _rewrite_function(
    *,
    old_guard: str,
    new_guard: str,
    old_scope: str,
    new_scope: str,
    expect_candidate_test_proof: bool,
) -> None:
    """Chỉ thay guard và scope khi hai câu lệnh cũ còn nguyên đúng một lần."""
    postimage_operator = "= 0" if expect_candidate_test_proof else "> 0"
    op.execute(
        f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            v_definition := pg_catalog.pg_get_functiondef('{FUNCTION_SIGNATURE}'::regprocedure);
            IF (pg_catalog.length(v_definition) - pg_catalog.length(
                    pg_catalog.replace(v_definition, $old_guard${old_guard}$old_guard$, '')))
                    <> pg_catalog.length($old_guard${old_guard}$old_guard$)
               OR (pg_catalog.length(v_definition) - pg_catalog.length(
                    pg_catalog.replace(v_definition, $old_scope${old_scope}$old_scope$, '')))
                    <> pg_catalog.length($old_scope${old_scope}$old_scope$) THEN
                RAISE EXCEPTION '{EXCEPTION}';
            END IF;
            v_definition := pg_catalog.replace(
                v_definition, $old_guard${old_guard}$old_guard$, $new_guard${new_guard}$new_guard$);
            v_definition := pg_catalog.replace(
                v_definition, $old_scope${old_scope}$old_scope$, $new_scope${new_scope}$new_scope$);
            EXECUTE v_definition;
            v_definition := pg_catalog.pg_get_functiondef('{FUNCTION_SIGNATURE}'::regprocedure);
            IF pg_catalog.strpos(v_definition, 'candidate_test_proof') {postimage_operator} THEN
                RAISE EXCEPTION '{EXCEPTION}_POSTIMAGE';
            END IF;
        END $migration$"""
    )


def upgrade() -> None:
    """Cho builder ghi proof metadata bằng output-scope tổng hợp đã cấp."""
    _rewrite_function(
        old_guard=_OLD_GUARD,
        new_guard=_NEW_GUARD,
        old_scope=_OLD_OUTPUT_SCOPE,
        new_scope=_NEW_OUTPUT_SCOPE,
        expect_candidate_test_proof=True,
    )


def downgrade() -> None:
    """Khôi phục guard hai operation cũ, không đổi role grants."""
    _rewrite_function(
        old_guard=_NEW_GUARD,
        new_guard=_OLD_GUARD,
        old_scope=_NEW_OUTPUT_SCOPE,
        new_scope=_OLD_OUTPUT_SCOPE,
        expect_candidate_test_proof=False,
    )
