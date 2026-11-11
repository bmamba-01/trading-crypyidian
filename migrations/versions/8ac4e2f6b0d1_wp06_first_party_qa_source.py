"""WP06: cho C3 kiểm thử QA có kiểm soát và chặn QA kích hoạt scorer."""

from collections.abc import Sequence

from alembic import op

revision: str = "8ac4e2f6b0d1"
down_revision: str | None = "55c504fe5b86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QA_PROVIDER = "CRYPIDIAN_FIRST_PARTY_QA"
QA_EVIDENCE_REF = (
    "https://github.com/bmamba-01/trading-crypyidian/blob/dev/"
    "docs/research/wp06-first-party-qa-fixture.md"
)
TEST_LOCK_SIGNATURE = "public.wp06_lock_scorer_test_sources(uuid, uuid)"
CURRENT_RIGHTS_SIGNATURE = "public.wp06_recheck_scorer_current_rights(uuid, uuid)"
DEPLOYMENT_SIGNATURE = "public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid)"

TEST_LOCK_PATCHES = (
    (
        "SELECT 'BINANCE_USDM_REST'",
        "SELECT unnest(ARRAY['BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA']::text[])",
        1,
    ),
)
CURRENT_RIGHTS_PATCHES = (
    (
        "            source_fingerprint text;\n        BEGIN",
        "            source_fingerprint text;\n            target_source_id text;\n        BEGIN",
        1,
    ),
    (
        "UNION SELECT 'BINANCE_USDM_REST'::text AS source_id",
        "UNION SELECT unnest(ARRAY['BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA']::text[]) "
        "AS source_id",
        1,
    ),
    (
        "               AND bundle.bundle_hash = proof_row.bundle_hash;\n            IF NOT FOUND",
        "               AND bundle.bundle_hash = proof_row.bundle_hash;\n"
        "            target_source_id := bundle_row.rights_provider;\n            IF NOT FOUND",
        1,
    ),
    (
        "OR bundle_row.rights_provider <> 'BINANCE_USDM_REST'",
        "OR bundle_row.rights_provider NOT IN ('BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA')",
        1,
    ),
    (
        "source_id = 'BINANCE_USDM_REST'",
        "source_id = target_source_id",
        5,
    ),
    (
        "OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$' THEN",
        "OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$'\n"
        "               OR (target_source_id = 'CRYPIDIAN_FIRST_PARTY_QA'\n"
        "                   AND policy_row.evidence_ref IS DISTINCT FROM\n"
        f"                       '{QA_EVIDENCE_REF}') THEN",
        1,
    ),
    (
        "'provider', 'BINANCE_USDM_REST',",
        "'provider', target_source_id,",
        1,
    ),
)
DEPLOYMENT_PATCHES = (
    (
        "IF report_row.evaluation_status NOT IN (\n"
        "                'ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE') THEN",
        "IF EXISTS (\n"
        "                SELECT 1\n"
        "                  FROM public.learning_scorer_test_opportunity_proof AS qa_proof\n"
        "                  JOIN public.backtest_dataset_replay_bundle AS qa_bundle\n"
        "                    ON qa_bundle.workspace_id = qa_proof.workspace_id\n"
        "                   AND qa_bundle.bundle_hash = qa_proof.bundle_hash\n"
        "                 WHERE qa_proof.workspace_id = target_workspace\n"
        "                   AND qa_proof.id = report_row.proof_id\n"
        "                   AND qa_bundle.rights_provider = 'CRYPIDIAN_FIRST_PARTY_QA'\n"
        "            ) THEN\n"
        "                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED';\n"
        "            END IF;\n"
        "            IF report_row.evaluation_status NOT IN (\n"
        "                'ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE') THEN",
        1,
    ),
)


def _rewrite_function(
    *,
    signature: str,
    patches: tuple[tuple[str, str, int], ...],
    marker: str,
    marker_before: bool,
    marker_after: bool,
    exception: str,
) -> None:
    """Chỉ rewrite function khi mọi clause và marker ở đúng trạng thái dự kiến."""
    patch_sql = "\n".join(
        f"""
        IF (length(v_definition) - length(replace(
                v_definition, $previous{index}${previous}$previous{index}$, '')))
                / length($previous{index}${previous}$previous{index}$) <> {count} THEN
            RAISE EXCEPTION '{exception}_PREIMAGE_UNEXPECTED';
        END IF;
        v_definition := replace(v_definition,
            $previous{index}${previous}$previous{index}$,
            $replacement{index}${replacement}$replacement{index}$);"""
        for index, (previous, replacement, count) in enumerate(patches)
    )
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            v_definition := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            IF ({"true" if marker_before else "false"}
                    AND pg_catalog.strpos(v_definition, '{marker}') = 0)
               OR (NOT {"true" if marker_before else "false"}
                    AND pg_catalog.strpos(v_definition, '{marker}') <> 0) THEN
                RAISE EXCEPTION '{exception}_MARKER_PREIMAGE_UNEXPECTED';
            END IF;
            {patch_sql}
            EXECUTE v_definition;
            v_definition := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            IF ({"true" if marker_after else "false"}
                    AND pg_catalog.strpos(v_definition, '{marker}') = 0)
               OR (NOT {"true" if marker_after else "false"}
                    AND pg_catalog.strpos(v_definition, '{marker}') <> 0) THEN
                RAISE EXCEPTION '{exception}_MARKER_POSTIMAGE_UNEXPECTED';
            END IF;
        END $migration$""")


def upgrade() -> None:
    """Mở QA cho cơ chế TEST, đồng thời cấm QA làm nguồn quyền deployment."""
    _rewrite_function(
        signature=TEST_LOCK_SIGNATURE,
        patches=TEST_LOCK_PATCHES,
        marker=QA_PROVIDER,
        marker_before=False,
        marker_after=True,
        exception="WP06_SCORER_TEST_QA_LOCK",
    )
    _rewrite_function(
        signature=CURRENT_RIGHTS_SIGNATURE,
        patches=CURRENT_RIGHTS_PATCHES,
        marker=QA_PROVIDER,
        marker_before=False,
        marker_after=True,
        exception="WP06_SCORER_CURRENT_RIGHTS_QA",
    )
    _rewrite_function(
        signature=DEPLOYMENT_SIGNATURE,
        patches=DEPLOYMENT_PATCHES,
        marker="WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED",
        marker_before=False,
        marker_after=True,
        exception="WP06_SCORER_DEPLOYMENT_QA_GUARD",
    )


def downgrade() -> None:
    """Khôi phục function cũ; không xóa report, quyền hoặc evidence đã lưu."""
    _rewrite_function(
        signature=DEPLOYMENT_SIGNATURE,
        patches=tuple(
            (replacement, previous, count) for previous, replacement, count in DEPLOYMENT_PATCHES
        ),
        marker="WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED",
        marker_before=True,
        marker_after=False,
        exception="WP06_SCORER_DEPLOYMENT_QA_GUARD_DOWNGRADE",
    )
    _rewrite_function(
        signature=CURRENT_RIGHTS_SIGNATURE,
        patches=tuple(
            (replacement, previous, count)
            for previous, replacement, count in reversed(CURRENT_RIGHTS_PATCHES)
        ),
        marker=QA_PROVIDER,
        marker_before=True,
        marker_after=False,
        exception="WP06_SCORER_CURRENT_RIGHTS_QA_DOWNGRADE",
    )
    _rewrite_function(
        signature=TEST_LOCK_SIGNATURE,
        patches=tuple(
            (replacement, previous, count) for previous, replacement, count in TEST_LOCK_PATCHES
        ),
        marker=QA_PROVIDER,
        marker_before=True,
        marker_after=False,
        exception="WP06_SCORER_TEST_QA_LOCK_DOWNGRADE",
    )
