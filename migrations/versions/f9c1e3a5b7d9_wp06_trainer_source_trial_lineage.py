"""WP06: nối manifest learning với trial nguồn đã duyệt trong gateway trainer."""

from collections.abc import Sequence

from alembic import op

revision: str = "f9c1e3a5b7d9"
down_revision: str | None = "e9a1c3e5f7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER = "crypidian_learning_batch_reader"
TABLE = "learning_split_candidate"
COLUMNS = "workspace_id, manifest_hash, fold_plan_hash, trial_record_id"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
LEGACY_LINK = "AND outcome.manifest_hash = sample.manifest_hash"
SOURCE_TRIAL_LINK = """AND EXISTS (
                    SELECT 1 FROM public.learning_split_candidate AS source_candidate
                     WHERE source_candidate.workspace_id = sample.workspace_id
                       AND source_candidate.manifest_hash = sample.manifest_hash
                       AND source_candidate.fold_plan_hash = sample.fold_plan_hash
                       AND source_candidate.trial_record_id = outcome.trial_record_id
                )"""

RETURN_CASTS = (
    (
        "opportunity.strategy_release_id, opportunity.strategy_content_hash,",
        "opportunity.strategy_release_id::text, opportunity.strategy_content_hash::text,",
    ),
    (
        "opportunity.feature_schema_hash, opportunity.vector_hash",
        "opportunity.feature_schema_hash::text, opportunity.vector_hash::text",
    ),
)

FUNCTION_SIGNATURE = "public.wp06_claim_learning_training_lineage(uuid,uuid)"
FUNCTION_DEFINITION_HASHES = {
    FUNCTION_SIGNATURE: {
        "preimage": "28de25db6d7e0a0d7e1bc8c93614aa227dad9389d0d9955e258994fa990132d5",
        "postimage": "157bd184dcf82f397686725003f37d14ee34254619e19b1372cc77dfda1ef83b",
    }
}


def _rewrite_function(
    patches: tuple[tuple[str, str, int], ...], *, expected_before: str, expected_after: str
) -> None:
    """Chỉ rewrite khi toàn bộ definition khớp preimage/postimage đã ghim."""
    patch_sql = "\n".join(
        f"""
            IF (
                length(v_definition)
                - length(replace(v_definition, $previous${previous}$previous$, ''))
            )
                / length($previous${previous}$previous$) <> {count} THEN
                RAISE EXCEPTION 'WP06_TRAINER_LINEAGE_DEFINITION_UNEXPECTED';
            END IF;
            v_definition := replace(
                v_definition,
                $previous${previous}$previous$,
                $replacement${replacement}$replacement$
            );"""
        for previous, replacement, count in patches
    )
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            v_definition := pg_catalog.pg_get_functiondef(
                '{FUNCTION_SIGNATURE}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            )
                <> '{expected_before}' THEN
                RAISE EXCEPTION 'WP06_TRAINER_LINEAGE_PREIMAGE_UNEXPECTED';
            END IF;
            {patch_sql}
            EXECUTE v_definition;
            v_definition := pg_catalog.pg_get_functiondef(
                '{FUNCTION_SIGNATURE}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            )
                <> '{expected_after}' THEN
                RAISE EXCEPTION 'WP06_TRAINER_LINEAGE_POSTIMAGE_UNEXPECTED';
            END IF;
        END $migration$""")


def upgrade() -> None:
    """Chỉ đọc metadata candidate; không cấp payload split hoặc TEST cho trainer."""
    op.execute(
        f"CREATE POLICY wp06_training_lineage_candidate_metadata_read ON public.{TABLE} "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ({COLUMNS}) ON TABLE public.{TABLE} TO {READER}")
    _rewrite_function(
        (
            (LEGACY_LINK, SOURCE_TRIAL_LINK, 2),
            *((previous, replacement, 1) for previous, replacement in RETURN_CASTS),
        ),
        expected_before=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["preimage"],
        expected_after=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["postimage"],
    )


def downgrade() -> None:
    """Khôi phục function cũ và thu hồi đúng metadata grant mới; không xóa dữ liệu."""
    _rewrite_function(
        (
            *((replacement, previous, 1) for previous, replacement in reversed(RETURN_CASTS)),
            (SOURCE_TRIAL_LINK, LEGACY_LINK, 2),
        ),
        expected_before=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["postimage"],
        expected_after=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["preimage"],
    )
    op.execute(f"REVOKE SELECT ({COLUMNS}) ON TABLE public.{TABLE} FROM {READER}")
    op.execute(f"DROP POLICY wp06_training_lineage_candidate_metadata_read ON public.{TABLE}")
