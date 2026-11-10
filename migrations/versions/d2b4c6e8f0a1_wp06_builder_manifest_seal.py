"""WP06: builder chỉ có thể append manifest sau approval exact candidate."""

from collections.abc import Sequence

from alembic import op

revision: str = "d2b4c6e8f0a1"
down_revision: str | None = "d1e3f5a7c9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILDER = "crypidian_candidate_builder"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        "CREATE POLICY dataset_manifest_builder_insert ON public.dataset_manifest "
        f"FOR INSERT TO {BUILDER} WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT INSERT ON TABLE public.dataset_manifest TO {BUILDER}")
    op.execute(f"""
        CREATE FUNCTION public.wp06_require_candidate_approval_before_manifest_seal()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF current_user = '{BUILDER}' THEN
                IF NOT EXISTS (
                    SELECT 1
                      FROM public.learning_split_candidate AS candidate
                      JOIN public.learning_split_approval AS approval
                        ON approval.workspace_id = candidate.workspace_id
                       AND approval.manifest_hash = candidate.manifest_hash
                       AND approval.split_policy_version = candidate.split_policy_version
                       AND approval.fold_plan_hash = candidate.fold_plan_hash
                     WHERE candidate.workspace_id = NEW.workspace_id
                       AND candidate.manifest_hash = NEW.manifest_hash
                       AND candidate.split_policy_version = NEW.split_policy_version
                       AND candidate.canonical_manifest = NEW.canonical_manifest
                ) THEN
                    RAISE EXCEPTION 'WP06_BUILDER_MANIFEST_REQUIRES_APPROVED_CANDIDATE';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER dataset_manifest_candidate_approval_guard "
        "BEFORE INSERT ON public.dataset_manifest FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_require_candidate_approval_before_manifest_seal()"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_require_candidate_approval_before_manifest_seal() "
        "FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER dataset_manifest_candidate_approval_guard ON public.dataset_manifest")
    op.execute("DROP FUNCTION public.wp06_require_candidate_approval_before_manifest_seal()")
    op.execute("DROP POLICY dataset_manifest_builder_insert ON public.dataset_manifest")
    op.execute(f"REVOKE INSERT ON TABLE public.dataset_manifest FROM {BUILDER}")
