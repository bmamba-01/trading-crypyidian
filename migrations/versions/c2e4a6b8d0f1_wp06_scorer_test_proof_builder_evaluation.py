"""WP06: builder được gọi operation EVALUATION của gate replay khi dựng scorer TEST proof.

Revision ID: c2e4a6b8d0f1
Revises: 60a62c705a89

Từ `7c9b749` (14/11/2026), `LearningScorerTestOpportunityProofRepository` xin quyền replay
bằng operation `evaluation` (spec `2026-11-13-wp06-source-use-scope-consumers-design.md`
dòng 34: scorer TEST là TRAINING evaluation, scope training + aggregate_report). Nhưng
`b7d9f1a3c5e8` chỉ cho session_user `crypidian_learning_evaluator` gọi `evaluation`, trong khi
repository/trigger/GRANT INSERT của proof (`e6f8a0c2d4b7`) chỉ dành cho
`crypidian_candidate_builder` — không role nào dựng được proof.

Quyết định owner 16/11/2026 (hướng B): mở caller check của operation `evaluation` cho thêm
builder. Không cấp thêm quyền bảng/hàm; builder vẫn là role duy nhất đọc lineage và ghi
proof; operation `training` vẫn chỉ dành cho builder; mọi kiểm scope/purpose/output giữ nguyên.
Rewrite bằng `CREATE OR REPLACE` từ `pg_get_functiondef` nên giữ owner, ACL, SECURITY DEFINER
và `search_path`; hash toàn bộ định nghĩa được ghim trước/sau ở cả hai chiều.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: str | None = "60a62c705a89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "public.wp06_authorize_replay_training_bundle(uuid,text,text)"
PREVIOUS_CALLER_CHECK = (
    "OR (target_operation = 'evaluation' AND session_user <> 'crypidian_learning_evaluator')"
)
CORRECTED_CALLER_CHECK = (
    "OR (target_operation = 'evaluation' AND session_user NOT IN (\n"
    "                   'crypidian_learning_evaluator', 'crypidian_candidate_builder'))"
)
FUNCTION_DEFINITION_HASHES = {
    SIGNATURE: {
        "preimage": "4eba7bc906cf5ba4d8184cb5ceb7efb515a790b9386761a60738a487fb206c92",
        "postimage": "11d0e3bf30df9642b40c614d78f95a575827c22f27d7c13f1ff455be8f88ad67",
    }
}


def _rewrite_caller_check(
    previous: str, replacement: str, *, expected_before: str, expected_after: str
) -> None:
    """Chỉ đổi caller check khi toàn bộ định nghĩa khớp hash đã ghim và đổi đúng một chỗ."""
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
            v_previous text := $previous${previous}$previous$;
            v_replacement text := $replacement${replacement}$replacement$;
        BEGIN
            v_definition := pg_catalog.pg_get_functiondef('{SIGNATURE}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            ) <> '{expected_before}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_EVAL_CALLER_PREIMAGE_UNEXPECTED';
            END IF;
            IF (length(v_definition) - length(replace(v_definition, v_previous, '')))
                / length(v_previous) <> 1 THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_EVAL_CALLER_DEFINITION_UNEXPECTED';
            END IF;
            EXECUTE replace(v_definition, v_previous, v_replacement);
            v_definition := pg_catalog.pg_get_functiondef('{SIGNATURE}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            ) <> '{expected_after}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_EVAL_CALLER_POSTIMAGE_UNEXPECTED';
            END IF;
        END $migration$""")


def upgrade() -> None:
    """Cho builder gọi `evaluation`; operation `training` và mọi kiểm khác không đổi."""
    hashes = FUNCTION_DEFINITION_HASHES[SIGNATURE]
    _rewrite_caller_check(
        PREVIOUS_CALLER_CHECK,
        CORRECTED_CALLER_CHECK,
        expected_before=hashes["preimage"],
        expected_after=hashes["postimage"],
    )


def downgrade() -> None:
    """Khôi phục caller check chỉ evaluator.

    Không cần guard dữ liệu: downgrade chỉ siết caller, không xóa/đổi proof hay receipt đã
    lưu; proof cũ vẫn được trigger/receipt gateway kiểm bất biến như trước.
    """
    hashes = FUNCTION_DEFINITION_HASHES[SIGNATURE]
    _rewrite_caller_check(
        CORRECTED_CALLER_CHECK,
        PREVIOUS_CALLER_CHECK,
        expected_before=hashes["postimage"],
        expected_after=hashes["preimage"],
    )
