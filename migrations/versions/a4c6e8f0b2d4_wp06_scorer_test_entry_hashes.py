"""WP06: khôi phục regex SHA-256 đầy đủ trong trigger proof TEST."""

from collections.abc import Sequence

from alembic import op

revision: str = "a4c6e8f0b2d4"
down_revision: str | None = "f9c1e3a5b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_PATTERN = "^[0-9a-f]64$"
HASH_PATTERN = "^[0-9a-f]{64}$"
FUNCTION_SIGNATURE = "public.validate_learning_scorer_test_opportunity_proof()"
FUNCTION_DEFINITION_HASHES = {
    FUNCTION_SIGNATURE: {
        "preimage": "b75b5067bd020069e4a9ed4c42a48ed9be1137dfbe8730efaf5bd10d5d53eea1",
        "postimage": "7b213d0a00e98ad072765456eb94ce10bb5cfcff42c948d3482d592a124c94bc",
    }
}


def _replace_pattern(
    previous: str, replacement: str, *, expected_before: str, expected_after: str
) -> None:
    """Chỉ đổi regex khi toàn bộ trigger khớp preimage/postimage đã ghim."""
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
            v_previous text := $previous${previous}$previous$;
            v_replacement text := $replacement${replacement}$replacement$;
        BEGIN
            v_definition := pg_catalog.pg_get_functiondef(
                '{FUNCTION_SIGNATURE}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            )
                <> '{expected_before}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_HASH_PREIMAGE_UNEXPECTED';
            END IF;
            IF (length(v_definition) - length(replace(v_definition, v_previous, '')))
                / length(v_previous) <> 7 THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_HASH_DEFINITION_UNEXPECTED';
            END IF;
            EXECUTE replace(v_definition, v_previous, v_replacement);
            v_definition := pg_catalog.pg_get_functiondef(
                '{FUNCTION_SIGNATURE}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            )
                <> '{expected_after}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_HASH_POSTIMAGE_UNEXPECTED';
            END IF;
        END $migration$""")


def upgrade() -> None:
    """Khôi phục điều kiện đúng 64 ký tự hex; không đổi shape/count/lineage."""
    _replace_pattern(
        LEGACY_PATTERN,
        HASH_PATTERN,
        expected_before=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["preimage"],
        expected_after=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["postimage"],
    )


def downgrade() -> None:
    """Khôi phục nguyên định nghĩa trigger cũ, không sửa dữ liệu đã lưu."""
    _replace_pattern(
        HASH_PATTERN,
        LEGACY_PATTERN,
        expected_before=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["postimage"],
        expected_after=FUNCTION_DEFINITION_HASHES[FUNCTION_SIGNATURE]["preimage"],
    )
