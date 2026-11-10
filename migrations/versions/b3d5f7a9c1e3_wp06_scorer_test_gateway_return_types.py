"""WP06: cast metadata varchar về đúng kiểu text của hai gateway TEST."""

from collections.abc import Sequence

from alembic import op

revision: str = "b3d5f7a9c1e3"
down_revision: str | None = "a4c6e8f0b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN_SIGNATURE = (
    "public.wp06_open_scorer_test_access_receipt(uuid,uuid,uuid,uuid,uuid,text,text,text,text)"
)
CONSUME_SIGNATURE = "public.wp06_consume_scorer_test_payload(uuid,uuid,text)"
FUNCTION_DEFINITION_HASHES = {
    OPEN_SIGNATURE: {
        "preimage": "bb56e9622475e7e0a026bc7c75e6da2e138bdce11ae1c3a58861b8fe87237c26",
        "postimage": "39dbdede289e1e57d0cc19294289f9166015aba39a404e92a18ccca10eaa2260",
    },
    CONSUME_SIGNATURE: {
        "preimage": "52f997da8440e8f70d7deee382de435560b67fed8e4f82d8b38b286348ae67e6",
        "postimage": "7c52428aea1664d2161b1be2f59421086c896ec3afe6a7b3dfda0fd1940c2c4a",
    },
}

# Đối chiếu RETURNS TABLE của E8: chỉ các cột varchar có output declared text.
# Dấu phẩy/prefix chọn đúng projection, không thay predicate hoặc JSON commitment.
OPEN_COLUMNS = (
    "receipt_hash",
    "request_fingerprint",
    "scorer_artifact_hash",
    "policy_hash",
    "proof_hash",
    "current_rights_receipt_hash",
)
CONSUMPTION_COLUMNS = (
    "request_fingerprint",
    "membership_digest",
    "payload_commitment_hash",
    "consumption_hash",
    "current_rights_receipt_hash",
)
SAMPLE_COLUMNS = (
    "outcome_hash",
    "feature_snapshot_hash",
    "feature_schema_hash",
    "label_hash",
)
PATCHES = (
    *(
        (OPEN_SIGNATURE, f"receipt.{column},", f"receipt.{column}::text,", 2)
        for column in OPEN_COLUMNS
    ),
    *(
        (CONSUME_SIGNATURE, f"consumption.{column},", f"consumption.{column}::text,", 1)
        for column in CONSUMPTION_COLUMNS
    ),
    *(
        (CONSUME_SIGNATURE, f"sample.{column},", f"sample.{column}::text,", 1)
        for column in SAMPLE_COLUMNS
    ),
    (
        CONSUME_SIGNATURE,
        "sample.outcome_hash::text, sample.sample_hash,",
        "sample.outcome_hash::text, sample.sample_hash::text,",
        1,
    ),
    (
        CONSUME_SIGNATURE,
        "outcome.id, outcome.outcome_hash,",
        "outcome.id, outcome.outcome_hash::text,",
        1,
    ),
    (CONSUME_SIGNATURE, "outcome.trade_id,", "outcome.trade_id::text,", 1),
    (
        CONSUME_SIGNATURE,
        "outcome.entry_vector_hash, outcome.entry_candidate_id",
        "outcome.entry_vector_hash::text, outcome.entry_candidate_id::text",
        1,
    ),
)


def _replace_function(
    signature: str,
    patches: tuple[tuple[str, str, int], ...],
    *,
    expected_before: str,
    expected_after: str,
) -> None:
    """Chỉ rewrite khi toàn bộ function khớp preimage/postimage đã ghim."""
    patch_sql = "\n".join(
        f"""
            IF (
                length(v_definition)
                - length(replace(v_definition, $previous${previous}$previous$, ''))
            )
                / length($previous${previous}$previous$) <> {count} THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_RETURN_DEFINITION_UNEXPECTED';
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
            v_definition := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            )
                <> '{expected_before}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_RETURN_PREIMAGE_UNEXPECTED';
            END IF;
            {patch_sql}
            EXECUTE v_definition;
            v_definition := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            IF pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_definition, 'UTF8')), 'hex'
            )
                <> '{expected_after}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_RETURN_POSTIMAGE_UNEXPECTED';
            END IF;
        END $migration$""")


def upgrade() -> None:
    """Chỉ đổi projection text, không đổi UUID/int/jsonb/timestamp hay quyền."""
    for signature in (OPEN_SIGNATURE, CONSUME_SIGNATURE):
        _replace_function(
            signature,
            tuple(
                (previous, replacement, count)
                for patch_signature, previous, replacement, count in PATCHES
                if patch_signature == signature
            ),
            expected_before=FUNCTION_DEFINITION_HASHES[signature]["preimage"],
            expected_after=FUNCTION_DEFINITION_HASHES[signature]["postimage"],
        )


def downgrade() -> None:
    """Khôi phục nguyên projection E8, không xóa receipt/consumption/report."""
    for signature in (CONSUME_SIGNATURE, OPEN_SIGNATURE):
        _replace_function(
            signature,
            tuple(
                (replacement, previous, count)
                for patch_signature, previous, replacement, count in reversed(PATCHES)
                if patch_signature == signature
            ),
            expected_before=FUNCTION_DEFINITION_HASHES[signature]["postimage"],
            expected_after=FUNCTION_DEFINITION_HASHES[signature]["preimage"],
        )
