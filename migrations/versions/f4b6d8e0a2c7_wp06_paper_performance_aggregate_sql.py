"""Sửa truy vấn tổng hợp hiệu năng PAPER và giới hạn quyền đọc theo cột."""

from collections.abc import Sequence

from alembic import op

revision: str = "f4b6d8e0a2c7"
down_revision: str | None = "e8a2c4d6f0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVALUATOR = "crypidian_learning_paper_performance_evaluator"
_FUNCTION = "public.wp06_aggregate_paper_performance_unscoped(uuid,uuid,text,uuid,text,timestamptz)"
_SELECT_GRANTS = (
    "(scorer_artifact_hash, base_artifact_hash, policy_hash) "
    "ON TABLE public.learning_scorer_test_evaluation_report",
    "(evaluation_status) ON TABLE public.learning_score_event",
)
_BASELINE_PROJECTION = (
    "SELECT report.id, report.report_hash, report.evaluation_status,\n"
    "                   report.canonical_report, report.report, report.scorer_artifact_hash,\n"
    "                   report.base_artifact_hash, report.policy_hash INTO baseline_row"
)


def _replace_aggregate(*, restore: bool = False) -> None:
    """Thay đúng hai đoạn SQL, giữ owner, ACL và search_path của hàm."""
    projections = ("SELECT report.* INTO baseline_row", _BASELINE_PROJECTION)
    log_losses = ("pg_catalog.ln(pg_catalog.greatest(", "pg_catalog.ln(GREATEST(")
    old_projection, new_projection = projections[::-1] if restore else projections
    old_log_loss, new_log_loss = log_losses[::-1] if restore else log_losses
    op.execute(f"""
        DO $migration$
        DECLARE
            definition text := pg_catalog.pg_get_functiondef('{_FUNCTION}'::regprocedure);
            old_fragment text;
            new_fragment text;
        BEGIN
            FOR old_fragment, new_fragment IN
                VALUES ($old${old_projection}$old$, $new${new_projection}$new$),
                       ($old${old_log_loss}$old$, $new${new_log_loss}$new$)
            LOOP
                IF pg_catalog.length(definition) - pg_catalog.length(
                    pg_catalog.replace(definition, old_fragment, '')
                ) IS DISTINCT FROM pg_catalog.length(old_fragment) THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_AGGREGATE_PREIMAGE_UNEXPECTED';
                END IF;
                definition := pg_catalog.replace(definition, old_fragment, new_fragment);
            END LOOP;
            EXECUTE definition;
        END
        $migration$
    """)


def upgrade() -> None:
    _replace_aggregate()
    for columns in _SELECT_GRANTS:
        op.execute(f"GRANT SELECT {columns} TO {_EVALUATOR}")


def downgrade() -> None:
    op.execute("LOCK TABLE public.learning_paper_performance_report IN ACCESS EXCLUSIVE MODE")
    # Từ chối khi RLS có thể che mất hàng trong phép kiểm toàn bảng.
    op.execute("""
        DO $$
        DECLARE
            previous_row_security text := pg_catalog.current_setting('row_security');
        BEGIN
            SET LOCAL row_security = off;
            IF EXISTS (SELECT 1 FROM public.learning_paper_performance_report) THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_AGGREGATE_DOWNGRADE_POPULATED_DENIED';
            END IF;
            PERFORM pg_catalog.set_config('row_security', previous_row_security, true);
        END $$
    """)
    _replace_aggregate(restore=True)
    for columns in _SELECT_GRANTS:
        op.execute(f"REVOKE SELECT {columns} FROM {_EVALUATOR}")
