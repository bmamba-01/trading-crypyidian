"""Đồng bộ hash Drift có digest quyền với JSON chuẩn của Python."""

from collections.abc import Sequence

from alembic import op

revision: str = "c6e8a0b2d4f9"
down_revision: str | None = "b4c6d8e0f2a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ASSESSMENT_IDEMPOTENCY_KEY = (
    "public.wp06_drift_assessment_idempotency_key(public.learning_drift_assessment)"
)
_ASSESSMENT_CONTENT_HASH = (
    "public.wp06_drift_assessment_content_hash(public.learning_drift_assessment,json)"
)

_WORKSPACE_FIELD = "                'workspace_id', row_value.workspace_id::text"
_WINDOW_FIELDS = """\
                'window_end', public.wp06_canonical_utc_timestamp_json(row_value.window_end),
                'window_start', public.wp06_canonical_utc_timestamp_json(row_value.window_start),
"""
_OLD_END = """
            );
            IF row_value.source_rights_digest IS NOT NULL THEN
                document := document || pg_catalog.jsonb_build_object(
                    'source_rights_digest', row_value.source_rights_digest);
            END IF;
            RETURN public.wp06_sha256_canonical_json(document::json);"""
_NEW_END = """
            );
            RETURN public.wp06_sha256_canonical_json(document);"""


def _replace_hash_builder(signature: str, last_fields: str, *, restore: bool) -> None:
    # Đoạn cuối chỉ xuất hiện trong nhánh có digest. Giữ nguyên nhánh NULL lịch sử.
    before = last_fields + _OLD_END
    after = "                'source_rights_digest', row_value.source_rights_digest,\n"
    after += last_fields + _NEW_END
    fragments = (
        ("DECLARE document jsonb;", "DECLARE document json;"),
        ("document := pg_catalog.jsonb_build_object(", "document := pg_catalog.json_build_object("),
        (before, after),
    )
    if restore:
        fragments = tuple((new, old) for old, new in fragments)
    values = ",\n".join(f"($old${old}$old$, $new${new}$new$)" for old, new in fragments)
    op.execute(f"""
        DO $migration$
        DECLARE
            definition text := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            old_fragment text;
            new_fragment text;
        BEGIN
            FOR old_fragment, new_fragment IN VALUES {values} LOOP
                IF pg_catalog.length(definition) - pg_catalog.length(
                    pg_catalog.replace(definition, old_fragment, '')
                ) IS DISTINCT FROM pg_catalog.length(old_fragment) THEN
                    RAISE EXCEPTION 'WP06_DRIFT_HASH_FUNCTION_PREIMAGE_UNEXPECTED';
                END IF;
                definition := pg_catalog.replace(definition, old_fragment, new_fragment);
            END LOOP;
            EXECUTE definition;
        END
        $migration$
    """)


def upgrade() -> None:
    _replace_hash_builder(_ASSESSMENT_IDEMPOTENCY_KEY, _WORKSPACE_FIELD, restore=False)
    _replace_hash_builder(
        _ASSESSMENT_CONTENT_HASH, _WINDOW_FIELDS + _WORKSPACE_FIELD, restore=False
    )


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
                RAISE EXCEPTION 'WP06_DRIFT_SOURCE_RIGHTS_HASH_DOWNGRADE_POPULATED_DENIED';
            END IF;
            PERFORM pg_catalog.set_config('row_security', previous_row_security, true);
        END $$
    """)
    _replace_hash_builder(_ASSESSMENT_CONTENT_HASH, _WINDOW_FIELDS + _WORKSPACE_FIELD, restore=True)
    _replace_hash_builder(_ASSESSMENT_IDEMPOTENCY_KEY, _WORKSPACE_FIELD, restore=True)
