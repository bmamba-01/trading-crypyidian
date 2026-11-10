"""WP06: admin chỉ đọc metadata normalized dataset để điều phối job."""

from collections.abc import Sequence

from alembic import op

revision: str = "d0e2f4a6c8b1"
down_revision: str | None = "c2d4e6f8a0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
METADATA_COLUMNS = (
    "workspace_id",
    "id",
    "manifest_hash",
    "split_policy_version",
    "fold_plan_hash",
    "approval_hash",
    "content_hash",
    "sample_count",
    "assignment_count",
    "created_at",
)


def upgrade() -> None:
    op.execute(
        "CREATE POLICY normalized_dataset_admin_metadata_read "
        "ON public.learning_normalized_dataset FOR SELECT TO "
        f"{ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"GRANT SELECT ({', '.join(METADATA_COLUMNS)}) "
        f"ON TABLE public.learning_normalized_dataset TO {ADMIN}"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY normalized_dataset_admin_metadata_read ON public.learning_normalized_dataset"
    )
    op.execute(
        "REVOKE SELECT (" + ", ".join(METADATA_COLUMNS) + ") "
        f"ON TABLE public.learning_normalized_dataset FROM {ADMIN}"
    )
