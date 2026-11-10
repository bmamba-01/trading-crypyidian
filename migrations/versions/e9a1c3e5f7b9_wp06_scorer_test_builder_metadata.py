"""WP06: cho builder đọc metadata attempt/scorer để dựng proof TEST."""

from collections.abc import Sequence

from alembic import op

revision: str = "e9a1c3e5f7b9"
down_revision: str | None = "e8f0a2c4d6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILDER = "crypidian_candidate_builder"
TABLES = ("learning_training_attempt", "learning_scorer_artifact")
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    """Chỉ cấp đọc hai bảng metadata, giữ nguyên FORCE RLS và quyền ghi hiện có."""
    for table in TABLES:
        op.execute(
            f"CREATE POLICY wp06_scorer_test_builder_metadata_read ON public.{table} "
            f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {BUILDER}")


def downgrade() -> None:
    """Thu hồi đúng quyền và policy được thêm ở vòng này."""
    for table in reversed(TABLES):
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {BUILDER}")
        op.execute(f"DROP POLICY wp06_scorer_test_builder_metadata_read ON public.{table}")
