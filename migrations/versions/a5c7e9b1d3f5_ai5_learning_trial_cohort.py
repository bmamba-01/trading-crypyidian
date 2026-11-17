"""AI-5 (CRY-236): dataset học gom nhiều trial/bundle (trial cohort nhiều coin).

Revision ID: a5c7e9b1d3f5
Revises: d4f6a8b0c2e4

Trước đây một candidate/manifest v5 chỉ được ghim **một** replay bundle và **một** trial
(`WP06_TRAINING_REPLAY_MIXED_BUNDLE`/`MIXED_RUN`), mà bundle chỉ có một symbol → dataset
huấn luyện bị giới hạn một coin (BTC H03 15m 180 ngày = 190 lệnh, không thể đạt ngưỡng TEST
≥ 1.000 mẫu). Thiết kế: `docs/superpowers/plans/2026-11-17-ai5-multi-coin-learning-dataset.md`.

1. Bảng con `learning_split_candidate_source`: mọi trial (một bundle/coin) của candidate.
   `trial_record_id` scalar của candidate vẫn là trial "neo" (nhỏ nhất) nên FK/unique cũ giữ
   nguyên. Append-only, RLS theo workspace; builder ghi, reader quyền/lineage chỉ đọc.
2. `wp06_replay_bundle_set_hash(text[])`: một bundle → chính hash đó; nhiều → sha256 JSON
   compact của danh sách đã sắp. Cùng công thức Python `replay_bundle_set_hash`.
3. `wp06_scorer_manifest_replay_bundle` (không ghim hash) trả bundle-set hash thay vì lỗi
   khi manifest có nhiều bundle; thêm `wp06_scorer_manifest_replay_bundles` trả danh sách.
4. Rewrite ghim preimage/postimage:
   - `wp06_check_replay_training_scope`: kiểm từng bundle/run một lần; lineage lọc theo bundle
     của sample; tổng child phải phủ hết lineage.
   - `wp06_recheck_scorer_current_rights`: lặp bundle của manifest, buộc set-hash = bundle của
     proof TEST, gộp scope EVALUATION mọi bundle (sắp theo child).
   - `wp06_claim_learning_training_lineage`: outcome thuộc trial neo **hoặc** trial trong cohort.

**Quyền mới (owner cần xem lại):** `crypidian_learning_evaluator` được EXECUTE
`wp06_scorer_manifest_replay_bundles` (SECURITY DEFINER, search_path đóng, kiểm
`app.workspace_id`, chỉ trả danh sách hash bundle — không payload) để gateway TEST cấp quyền
từng bundle. Review WP-06 17/11: thêm `wp06_scorer_proof_replay_bundles` và vá
`wp06_verify_scorer_deployment_rights` (3 join theo hash proof → fail-open khi N>1).
Giới hạn: mọi bundle của cohort phải cùng provider (chặn ở stage và ở recheck).

Với N = 1 mọi hành vi và hash giữ nguyên (candidate/proof/receipt cũ vẫn hợp lệ).
Downgrade đảo patch, xóa hàm/bảng mới (từ chối nếu đã có cohort N > 1).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5c7e9b1d3f5"
down_revision: str | None = "d4f6a8b0c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_split_candidate_source"
BUILDER = "crypidian_candidate_builder"
ADMIN = "crypidian_admin_web"
RIGHTS_READER = "crypidian_learning_source_rights_reader"
BATCH_READER = "crypidian_learning_batch_reader"
SCORER_RIGHTS_READER = "crypidian_scorer_deployment_rights_reader"
COMPARATOR = "crypidian_manifest_comparator"
EVALUATOR = "crypidian_learning_evaluator"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
READ_COLUMNS = "workspace_id, manifest_hash, fold_plan_hash, trial_record_id, bundle_hash"

SCOPE_SIGNATURE = (
    "public.wp06_check_replay_training_scope(uuid,text,jsonb,uuid,text,text,uuid,boolean)"
)
RECHECK_SIGNATURE = "public.wp06_recheck_scorer_current_rights(uuid,uuid)"
CLAIM_SIGNATURE = "public.wp06_claim_learning_training_lineage(uuid,uuid)"
SET_HASH_SIGNATURE = "public.wp06_replay_bundle_set_hash(text[])"
REPLAY_BUNDLE_SIGNATURE = "public.wp06_scorer_manifest_replay_bundle(uuid,uuid)"
REPLAY_BUNDLES_SIGNATURE = "public.wp06_scorer_manifest_replay_bundles(uuid,uuid)"
PROOF_BUNDLES_SIGNATURE = "public.wp06_scorer_proof_replay_bundles(uuid,uuid)"
DEPLOY_SIGNATURE = "public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid)"

FUNCTION_DEFINITION_HASHES = {
    SCOPE_SIGNATURE: {
        "preimage": "0fececbe14e702f523c00115e2fb83e3dbb444e00b132ee6fcc47f72409d1543",
        "postimage": "51b2340f44a7862cb36753d4cc70a12ebee8f79b59866b47ca4f4105260742f0",
    },
    RECHECK_SIGNATURE: {
        "preimage": "61a6f4f38a6de89d26cdbff7850663730ce77488b0e8ba5042c67a6010b522f3",
        "postimage": "2f6075fd0519a81a8861d53e33e1a22e62ab7441a09c2084738464f609a22f2e",
    },
    DEPLOY_SIGNATURE: {
        "preimage": "c6d4628be43041410b8bc44df912b64b6c95c23fdb4e7794cc20eede0c9dbe92",
        "postimage": "c2f9a5be3f1fba2037602d2a385792f4f84d93c4bc9b3a18f96e94fa1fc39256",
    },
    CLAIM_SIGNATURE: {
        "preimage": "157bd184dcf82f397686725003f37d14ee34254619e19b1372cc77dfda1ef83b",
        "postimage": "66db8c81f84e4b38d2c382d3d790ef98af8bcb54c15aff8b2937bbf8c534785a",
    },
}

COHORT_TRIAL = (
    "EXISTS (\n"
    "                        SELECT 1 FROM public.learning_split_candidate_source AS cohort\n"
    "                         WHERE cohort.workspace_id = {ws}\n"
    "                           AND cohort.manifest_hash = {manifest}\n"
    "                           AND cohort.fold_plan_hash = {fold}\n"
    "                           AND cohort.trial_record_id = {trial}{extra}\n"
    "                    )"
)

SCOPE_PATCHES: tuple[tuple[str, str], ...] = (
    (
        "            source_sample_count integer := 0;\n",
        "            source_sample_count integer := 0;\n"
        "            seen_bundles text[] := ARRAY[]::text[];\n"
        "            seen_runs uuid[] := ARRAY[]::uuid[];\n"
        "            seen_trials uuid[] := ARRAY[]::uuid[];\n"
        "            seen_run_manifests text[] := ARRAY[]::text[];\n"
        "            bundle_index integer;\n"
        "            lineage_total integer := 0;\n",
    ),
    (
        "                bundle_hash := sample_entry->>'source_replay_bundle_hash';\n"
        "                IF source_bundle_hash IS NULL THEN\n"
        "                    source_bundle_hash := bundle_hash;\n"
        "                ELSIF source_bundle_hash IS DISTINCT FROM bundle_hash THEN\n"
        "                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MIXED_BUNDLE';\n"
        "                END IF;\n",
        # AI-5: nhiều bundle hợp lệ; mỗi bundle kiểm đầy đủ một lần ở sample đầu của nó.
        "                bundle_hash := sample_entry->>'source_replay_bundle_hash';\n"
        "                source_bundle_hash := bundle_hash;\n"
        "                bundle_index := pg_catalog.array_position(seen_bundles, bundle_hash);\n",
    ),
    (
        "                           AND candidate.trial_record_id = trial_record.id\n",
        "                           AND (candidate.trial_record_id = trial_record.id\n"
        "                                OR "
        + COHORT_TRIAL.format(
            ws="target_workspace",
            manifest="target_manifest_hash",
            fold="candidate.fold_plan_hash",
            trial="trial_record.id",
            extra="\n                           AND cohort.bundle_hash = bundle_hash",
        )
        + ")\n",
    ),
    (
        "                IF run_id IS NULL THEN\n"
        "                    run_id := run_record.id;\n"
        "                    trial_id := trial_record.id;\n"
        "                    run_manifest_hash := run_record.manifest_hash;\n"
        "                ELSIF run_id IS DISTINCT FROM run_record.id\n"
        "                   OR trial_id IS DISTINCT FROM trial_record.id\n"
        "                   OR run_manifest_hash IS DISTINCT FROM run_record.manifest_hash THEN\n"
        "                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MIXED_RUN';\n"
        "                END IF;\n"
        "\n"
        "                IF source_sample_count = 1 THEN\n",
        # Mỗi bundle đúng một run/trial; hai bundle không được dùng chung run/trial.
        "                IF bundle_index IS NULL THEN\n"
        "                    IF run_record.id = ANY(seen_runs)\n"
        "                       OR trial_record.id = ANY(seen_trials) THEN\n"
        "                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MIXED_RUN';\n"
        "                    END IF;\n"
        "                    seen_bundles := seen_bundles || bundle_hash;\n"
        "                    seen_runs := seen_runs || run_record.id;\n"
        "                    seen_trials := seen_trials || trial_record.id;\n"
        "                    seen_run_manifests := seen_run_manifests"
        " || run_record.manifest_hash;\n"
        "                ELSIF seen_runs[bundle_index] IS DISTINCT FROM run_record.id\n"
        "                   OR seen_trials[bundle_index] IS DISTINCT FROM trial_record.id\n"
        "                   OR seen_run_manifests[bundle_index]\n"
        "                        IS DISTINCT FROM run_record.manifest_hash THEN\n"
        "                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MIXED_RUN';\n"
        "                END IF;\n"
        "                run_id := run_record.id;\n"
        "                trial_id := trial_record.id;\n"
        "                run_manifest_hash := run_record.manifest_hash;\n"
        "\n"
        "                IF bundle_index IS NULL THEN\n",
    ),
    (
        "                   OR pg_catalog.jsonb_array_length(stored_manifest->'source_use_lineage')"
        " <> component_count\n",
        "                   OR (SELECT count(*) FROM pg_catalog.jsonb_array_elements(\n"
        "                          stored_manifest->'source_use_lineage') AS lineage(value)\n"
        "                        WHERE lineage.value->>'bundle_hash' = bundle_hash)"
        " <> component_count\n",
    ),
    (
        "                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CHILD_COUNT_MISMATCH';\n"
        "                END IF;\n",
        "                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CHILD_COUNT_MISMATCH';\n"
        "                END IF;\n"
        "                lineage_total := lineage_total + component_count;\n",
    ),
    (
        "                FOR lineage_entry IN\n"
        "                    SELECT lineage.value\n"
        "                      FROM pg_catalog.jsonb_array_elements(\n"
        "                          stored_manifest->'source_use_lineage') AS lineage(value)\n"
        "                LOOP\n",
        "                FOR lineage_entry IN\n"
        "                    SELECT lineage.value\n"
        "                      FROM pg_catalog.jsonb_array_elements(\n"
        "                          stored_manifest->'source_use_lineage') AS lineage(value)\n"
        "                     WHERE lineage.value->>'bundle_hash' = bundle_hash\n"
        "                LOOP\n",
    ),
    (
        "            FOR source_key IN\n",
        "            IF source_sample_count > 0 AND lineage_total <>\n"
        "                    pg_catalog.jsonb_array_length(stored_manifest->'source_use_lineage')"
        " THEN\n"
        "                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CHILD_COUNT_MISMATCH';\n"
        "            END IF;\n"
        "            FOR source_key IN\n",
    ),
    (
        "                bundle_hash := sample_entry->>'source_replay_bundle_hash';\n"
        "                FOR lineage_entry IN\n"
        "                    SELECT lineage.value FROM pg_catalog.jsonb_array_elements(\n"
        "                        stored_manifest->'source_use_lineage') AS lineage(value)\n"
        "                LOOP\n",
        "                bundle_hash := sample_entry->>'source_replay_bundle_hash';\n"
        "                SELECT bundle.bundle_json->'components' INTO bundle_components\n"
        "                  FROM public.backtest_dataset_replay_bundle AS bundle\n"
        "                 WHERE bundle.workspace_id = target_workspace\n"
        "                   AND bundle.bundle_hash = bundle_hash;\n"
        "                FOR lineage_entry IN\n"
        "                    SELECT lineage.value FROM pg_catalog.jsonb_array_elements(\n"
        "                        stored_manifest->'source_use_lineage') AS lineage(value)\n"
        "                     WHERE lineage.value->>'bundle_hash' = bundle_hash\n"
        "                LOOP\n",
    ),
)

RECHECK_PATCHES: tuple[tuple[str, str], ...] = (
    (
        "            bundle_row record;\n",
        "            bundle_row record;\n"
        "            v_bundle_hashes text[];\n"
        "            v_bundle text;\n"
        "            v_component_total integer := 0;\n",
    ),
    (
        "            SELECT bundle.workspace_id, bundle.bundle_hash, bundle.schema_version,"
        " bundle.symbol,\n",
        # AI-5: danh sách bundle của manifest (một phần tử với manifest cũ/không phải v5).
        "            v_bundle_hashes := public.wp06_scorer_manifest_replay_bundles(\n"
        "                target_workspace, v_manifest_id);\n"
        "            IF v_bundle_hashes IS NULL OR pg_catalog.cardinality(v_bundle_hashes) = 0"
        " THEN\n"
        "                v_bundle_hashes := ARRAY[proof_row.bundle_hash];\n"
        "            END IF;\n"
        "            IF public.wp06_replay_bundle_set_hash(v_bundle_hashes)\n"
        "                    IS DISTINCT FROM proof_row.bundle_hash THEN\n"
        "                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_V5_LINEAGE_MISMATCH';\n"
        "            END IF;\n"
        "            target_source_id := NULL;\n"
        "            FOREACH v_bundle IN ARRAY v_bundle_hashes LOOP\n"
        "            SELECT bundle.workspace_id, bundle.bundle_hash, bundle.schema_version,"
        " bundle.symbol,\n",
    ),
    (
        "               AND bundle.bundle_hash = proof_row.bundle_hash;\n"
        "            target_source_id := bundle_row.rights_provider;\n"
        "            IF NOT FOUND OR bundle_row.bundle_hash"
        " IS DISTINCT FROM proof_row.bundle_hash\n",
        "               AND bundle.bundle_hash = v_bundle;\n"
        "            IF target_source_id IS NOT NULL\n"
        "               AND target_source_id IS DISTINCT FROM bundle_row.rights_provider THEN\n"
        "                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUNDLE_INVALID';\n"
        "            END IF;\n"
        "            target_source_id := bundle_row.rights_provider;\n"
        "            IF NOT FOUND OR bundle_row.bundle_hash IS DISTINCT FROM v_bundle\n",
    ),
    (
        "                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUNDLE_INVALID';\n"
        "            END IF;\n"
        "\n"
        "            SELECT policy.source_id,",
        "                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUNDLE_INVALID';\n"
        "            END IF;\n"
        "            v_component_total := v_component_total\n"
        "                + pg_catalog.jsonb_array_length(bundle_row.bundle_json->'components');\n"
        "            END LOOP;\n"
        "\n"
        "            SELECT policy.source_id,",
    ),
    (
        "              FROM public.wp06_scorer_replay_evaluation_scope(\n"
        "                       target_workspace, proof_row.bundle_hash, 'evaluation') AS scope\n"
        "             WHERE scope.workspace_id = target_workspace\n"
        "               AND scope.bundle_hash = proof_row.bundle_hash\n",
        "              FROM pg_catalog.unnest(v_bundle_hashes) AS bundle_item(value)\n"
        "             CROSS JOIN LATERAL public.wp06_scorer_replay_evaluation_scope(\n"
        "                       target_workspace, bundle_item.value, 'evaluation') AS scope\n"
        "             WHERE scope.workspace_id = target_workspace\n"
        "               AND scope.bundle_hash = bundle_item.value\n",
    ),
    (
        "                    <> pg_catalog.jsonb_array_length(bundle_row.bundle_json->'components')"
        " THEN\n",
        "                    <> v_component_total THEN\n",
    ),
)

CLAIM_PATCHES: tuple[tuple[str, str], ...] = (
    (
        "                       AND source_candidate.trial_record_id = outcome.trial_record_id\n",
        "                       AND (source_candidate.trial_record_id = outcome.trial_record_id\n"
        "                            OR "
        + COHORT_TRIAL.format(
            ws="sample.workspace_id",
            manifest="sample.manifest_hash",
            fold="sample.fold_plan_hash",
            trial="outcome.trial_record_id",
            extra="",
        )
        + ")\n",
    ),
)

# Review WP-06 (17/11): với N>1 bundle-set hash không trùng dòng nào của bảng bundle → join
# `bundle.bundle_hash = proof.bundle_hash` rỗng → bỏ qua kiểm quyền PAPER / chặn QA (fail-open).
# Thay bằng danh sách bundle đã xác minh của proof (helper ném lỗi nếu set-hash lệch).
DEPLOY_PATCHES: tuple[tuple[str, str], ...] = tuple(
    (
        f"{indent}AND {alias}.bundle_hash = {proof}.bundle_hash\n",
        f"{indent}AND {alias}.bundle_hash = ANY(public.wp06_scorer_proof_replay_bundles(\n"
        f"{indent}    target_workspace, {proof}.id))\n",
    )
    for indent, alias, proof in (
        ("                     ", "bundle", "source_proof"),
        ("                   ", "qa_bundle", "qa_proof"),
        ("                         ", "bundle", "proof_row"),
    )
)

PATCH_COUNTS = {
    DEPLOY_SIGNATURE: (1, 1, 1),
    SCOPE_SIGNATURE: (1, 1, 1, 1, 1, 1, 1, 1, 1),
    RECHECK_SIGNATURE: (1, 1, 1, 1, 1, 1),
    CLAIM_SIGNATURE: (2,),
}

REPLAY_BUNDLE_BODY = """
        DECLARE
            stored_manifest jsonb;
            bundle_hashes text[];
        BEGIN
            IF target_workspace IS NULL OR target_manifest_id IS NULL
               OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_WORKSPACE_DENIED';
            END IF;
            SELECT manifest.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.id = target_manifest_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_MANIFEST_MISSING';
            END IF;
            IF stored_manifest->>'manifest_version' IS DISTINCT FROM 'dataset-manifest-v5' THEN
                RETURN NULL;
            END IF;
            SELECT pg_catalog.array_agg(DISTINCT sample.value->>'source_replay_bundle_hash'
                                        ORDER BY sample.value->>'source_replay_bundle_hash')
              INTO bundle_hashes
              FROM pg_catalog.jsonb_array_elements(
                       COALESCE(stored_manifest->'samples', '[]'::jsonb)) AS sample(value)
             WHERE sample.value->>'source_dependent' = 'true';
            IF bundle_hashes IS NULL THEN
                RETURN NULL;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.unnest(bundle_hashes) AS item(value)
                 WHERE item.value IS NULL OR item.value !~ '^[0-9a-f]{64}$'
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_V5_LINEAGE_MISMATCH';
            END IF;
            RETURN %s;
        END
"""

LEGACY_REPLAY_BUNDLE_BODY = """
        DECLARE
            stored_manifest jsonb;
            bundle_hashes text[];
        BEGIN
            IF target_workspace IS NULL OR target_manifest_id IS NULL
               OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_WORKSPACE_DENIED';
            END IF;
            SELECT manifest.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.id = target_manifest_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_MANIFEST_MISSING';
            END IF;
            IF stored_manifest->>'manifest_version' IS DISTINCT FROM 'dataset-manifest-v5' THEN
                RETURN NULL;
            END IF;
            SELECT pg_catalog.array_agg(DISTINCT sample.value->>'source_replay_bundle_hash')
              INTO bundle_hashes
              FROM pg_catalog.jsonb_array_elements(
                       COALESCE(stored_manifest->'samples', '[]'::jsonb)) AS sample(value)
             WHERE sample.value->>'source_dependent' = 'true';
            IF bundle_hashes IS NULL THEN
                RETURN NULL;
            END IF;
            IF pg_catalog.array_length(bundle_hashes, 1) <> 1
               OR bundle_hashes[1] IS NULL
               OR bundle_hashes[1] !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_V5_LINEAGE_MISMATCH';
            END IF;
            RETURN bundle_hashes[1];
        END
"""


def _dollar(tag: str, value: str) -> str:
    marker = f"${tag}$"
    if marker in value:
        raise ValueError(f"Patch chứa dollar tag {marker}.")
    return f"{marker}{value}{marker}"


def _patch_sql(
    patches: tuple[tuple[str, str], ...], counts: tuple[int, ...], *, forward: bool, exception: str
) -> str:
    statements = []
    for (before, after), count in zip(patches, counts, strict=True):
        previous, replacement = (before, after) if forward else (after, before)
        statements.append(f"""
            IF (length(v_definition) - length(replace(v_definition, {_dollar("p", previous)}, '')))
                / length({_dollar("p", previous)}) <> {count} THEN
                RAISE EXCEPTION '{exception}_DEFINITION_UNEXPECTED';
            END IF;
            v_definition := replace(
                v_definition, {_dollar("p", previous)}, {_dollar("r", replacement)});""")
    return "\n".join(statements)


def _hash_check(signature: str, expected: str, exception: str) -> str:
    if expected == "PENDING":  # chỉ dùng khi đo postimage lần đầu trên DB tạm
        return ""
    return f"""
            IF pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.pg_get_functiondef('{signature}'::regprocedure), 'UTF8')), 'hex')
                    IS DISTINCT FROM '{expected}' THEN
                RAISE EXCEPTION '{exception}';
            END IF;"""


def _rewrite(signature: str, patches: tuple[tuple[str, str], ...], *, forward: bool) -> None:
    """CREATE OR REPLACE từ pg_get_functiondef: giữ owner/ACL/SECURITY DEFINER/search_path."""
    hashes = FUNCTION_DEFINITION_HASHES[signature]
    before, after = (
        (hashes["preimage"], hashes["postimage"])
        if forward
        else (hashes["postimage"], hashes["preimage"])
    )
    counts = PATCH_COUNTS[signature]
    ordered = patches if forward else tuple(reversed(patches))
    ordered_counts = counts if forward else tuple(reversed(counts))
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            {_hash_check(signature, before, "AI5_COHORT_PREIMAGE_UNEXPECTED")}
            v_definition := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            {_patch_sql(ordered, ordered_counts, forward=forward, exception="AI5_COHORT")}
            EXECUTE v_definition;
            {_hash_check(signature, after, "AI5_COHORT_POSTIMAGE_UNEXPECTED")}
        END $migration$""")


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("trial_record_id", sa.Uuid(), nullable=False),
        sa.Column("proof_record_id", sa.Uuid(), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "candidate_id"],
            ["learning_split_candidate.workspace_id", "learning_split_candidate.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_record_id"],
            ["candidate_universe_proof.workspace_id", "candidate_universe_proof.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "candidate_id", "trial_record_id", name="uq_ai5_cohort_trial"
        ),
        sa.UniqueConstraint(
            "workspace_id", "candidate_id", "bundle_hash", name="uq_ai5_cohort_bundle"
        ),
        sa.UniqueConstraint("workspace_id", "candidate_id", "ordinal", name="uq_ai5_cohort_ord"),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND proof_hash ~ '^[0-9a-f]{64}$' AND bundle_hash ~ '^[0-9a-f]{64}$' "
            "AND ordinal >= 0",
            name="learning_split_candidate_source_valid",
        ),
    )
    op.create_index(
        "ix_learning_split_candidate_source_scope",
        TABLE,
        ["workspace_id", "manifest_hash", "fold_plan_hash", "trial_record_id"],
    )
    op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE public."{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY ai5_cohort_builder_scope ON public."{TABLE}" FOR ALL '
        f"TO {BUILDER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    for role in (ADMIN, RIGHTS_READER, BATCH_READER):
        op.execute(
            f"CREATE POLICY ai5_cohort_{role.removeprefix('crypidian_')}_select "
            f'ON public."{TABLE}" FOR SELECT TO {role} USING ({WORKSPACE})'
        )
    # Dòng con phải khớp manifest/fold/neo của candidate cha; append-only.
    op.execute("""
        CREATE FUNCTION public.ai5_guard_learning_split_candidate_source() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'AI5_LEARNING_SPLIT_CANDIDATE_SOURCE_IMMUTABLE';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.learning_split_candidate AS candidate
                 WHERE candidate.workspace_id = NEW.workspace_id
                   AND candidate.id = NEW.candidate_id
                   AND candidate.manifest_hash = NEW.manifest_hash
                   AND candidate.fold_plan_hash = NEW.fold_plan_hash
                   AND (NEW.ordinal > 0 OR (
                        candidate.trial_record_id = NEW.trial_record_id
                        AND candidate.proof_record_id = NEW.proof_record_id
                        AND candidate.proof_hash = NEW.proof_hash))
            ) THEN
                RAISE EXCEPTION 'AI5_LEARNING_SPLIT_CANDIDATE_SOURCE_MISMATCH';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE TRIGGER ai5_learning_split_candidate_source_guard BEFORE INSERT OR UPDATE "
        f'OR DELETE ON public."{TABLE}" FOR EACH ROW '
        "EXECUTE FUNCTION public.ai5_guard_learning_split_candidate_source()"
    )
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE public."{TABLE}" FROM PUBLIC, {ADMIN}, {BUILDER}, '
        "crypidian_app, crypidian_trading_runtime, crypidian_trainer_worker, "
        "crypidian_backtest_worker"
    )
    op.execute(f'GRANT SELECT, INSERT ON TABLE public."{TABLE}" TO {BUILDER}')
    op.execute(f'GRANT SELECT ON TABLE public."{TABLE}" TO {ADMIN}')
    for role in (RIGHTS_READER, BATCH_READER):
        op.execute(f'GRANT SELECT ({READ_COLUMNS}) ON TABLE public."{TABLE}" TO {role}')


def _create_functions() -> None:
    # Công thức trùng Python `replay_bundle_set_hash` (JSON compact, hex không cần escape).
    op.execute("""
        CREATE FUNCTION public.wp06_replay_bundle_set_hash(bundle_hashes text[])
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            ordered text[];
        BEGIN
            SELECT pg_catalog.array_agg(DISTINCT item.value ORDER BY item.value)
              INTO ordered
              FROM pg_catalog.unnest(bundle_hashes) AS item(value);
            IF ordered IS NULL
               OR pg_catalog.cardinality(ordered) <> pg_catalog.cardinality(bundle_hashes)
               OR EXISTS (SELECT 1 FROM pg_catalog.unnest(ordered) AS item(value)
                           WHERE item.value IS NULL OR item.value !~ '^[0-9a-f]{64}$') THEN
                RAISE EXCEPTION 'AI5_REPLAY_BUNDLE_SET_INVALID';
            END IF;
            IF pg_catalog.cardinality(ordered) = 1 THEN
                RETURN ordered[1];
            END IF;
            RETURN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                '[' || pg_catalog.array_to_string(
                    ARRAY(SELECT '"' || item.value || '"'
                            FROM pg_catalog.unnest(ordered) AS item(value)), ',') || ']',
                'UTF8')), 'hex');
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {SET_HASH_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SET_HASH_SIGNATURE} TO {COMPARATOR}, {SCORER_RIGHTS_READER}, "
        f"{RIGHTS_READER}, {BUILDER}"
    )
    op.execute(
        f"CREATE OR REPLACE FUNCTION {REPLAY_BUNDLE_SIGNATURE.replace('(uuid,uuid)', '')}("
        "target_workspace uuid, target_manifest_id uuid) RETURNS text LANGUAGE plpgsql "
        "STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$"
        + REPLAY_BUNDLE_BODY % "public.wp06_replay_bundle_set_hash(bundle_hashes)"
        + "$fn$"
    )
    op.execute(
        f"CREATE FUNCTION {REPLAY_BUNDLES_SIGNATURE.replace('(uuid,uuid)', '')}("
        "target_workspace uuid, target_manifest_id uuid) RETURNS text[] LANGUAGE plpgsql "
        "STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$"
        + REPLAY_BUNDLE_BODY % "bundle_hashes"
        + "$fn$"
    )
    op.execute(f"ALTER FUNCTION {REPLAY_BUNDLES_SIGNATURE} OWNER TO {COMPARATOR}")
    op.execute(f"REVOKE ALL ON FUNCTION {REPLAY_BUNDLES_SIGNATURE} FROM PUBLIC")
    # Evaluator chỉ nhận danh sách hash bundle (không payload) để cấp quyền từng bundle.
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {REPLAY_BUNDLES_SIGNATURE} TO {SCORER_RIGHTS_READER}, "
        f"{EVALUATOR}"
    )


def _create_proof_bundles_helper() -> None:
    """Danh sách bundle đã xác minh của một proof TEST (fail-closed nếu set-hash lệch)."""
    op.execute(f"""
        CREATE FUNCTION {PROOF_BUNDLES_SIGNATURE.replace("(uuid,uuid)", "")}(
            target_workspace uuid, target_proof_id uuid
        ) RETURNS text[] LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $fn$
        DECLARE
            proof_bundle text;
            manifest_id uuid;
            bundles text[];
        BEGIN
            IF target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_WORKSPACE_DENIED';
            END IF;
            SELECT proof.bundle_hash, manifest.id INTO proof_bundle, manifest_id
              FROM public.learning_scorer_test_opportunity_proof AS proof
              LEFT JOIN public.dataset_manifest AS manifest
                ON manifest.workspace_id = proof.workspace_id
               AND manifest.manifest_hash = proof.manifest_hash
             WHERE proof.workspace_id = target_workspace AND proof.id = target_proof_id;
            IF proof_bundle IS NULL THEN
                RETURN ARRAY[]::text[];
            END IF;
            IF manifest_id IS NOT NULL THEN
                bundles := public.wp06_scorer_manifest_replay_bundles(
                    target_workspace, manifest_id);
            END IF;
            IF bundles IS NULL OR pg_catalog.cardinality(bundles) = 0 THEN
                bundles := ARRAY[proof_bundle];
            END IF;
            IF public.wp06_replay_bundle_set_hash(bundles) IS DISTINCT FROM proof_bundle THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_V5_LINEAGE_MISMATCH';
            END IF;
            RETURN bundles;
        END $fn$
    """)
    op.execute(f"ALTER FUNCTION {PROOF_BUNDLES_SIGNATURE} OWNER TO {SCORER_RIGHTS_READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {PROOF_BUNDLES_SIGNATURE} FROM PUBLIC")


def upgrade() -> None:
    _create_table()
    _create_functions()
    _create_proof_bundles_helper()
    _rewrite(DEPLOY_SIGNATURE, DEPLOY_PATCHES, forward=True)
    _rewrite(SCOPE_SIGNATURE, SCOPE_PATCHES, forward=True)
    _rewrite(RECHECK_SIGNATURE, RECHECK_PATCHES, forward=True)
    _rewrite(CLAIM_SIGNATURE, CLAIM_PATCHES, forward=True)


def downgrade() -> None:
    op.execute(f"""DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM public."{TABLE}" WHERE ordinal > 0) THEN
                RAISE EXCEPTION 'AI5_COHORT_DOWNGRADE_HAS_MULTI_TRIAL_CANDIDATES';
            END IF;
        END $migration$""")
    _rewrite(CLAIM_SIGNATURE, CLAIM_PATCHES, forward=False)
    _rewrite(DEPLOY_SIGNATURE, DEPLOY_PATCHES, forward=False)
    op.execute(f"DROP FUNCTION {PROOF_BUNDLES_SIGNATURE}")
    _rewrite(RECHECK_SIGNATURE, RECHECK_PATCHES, forward=False)
    _rewrite(SCOPE_SIGNATURE, SCOPE_PATCHES, forward=False)
    op.execute(f"DROP FUNCTION {REPLAY_BUNDLES_SIGNATURE}")
    op.execute(
        f"CREATE OR REPLACE FUNCTION {REPLAY_BUNDLE_SIGNATURE.replace('(uuid,uuid)', '')}("
        "target_workspace uuid, target_manifest_id uuid) RETURNS text LANGUAGE plpgsql "
        "STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$"
        + LEGACY_REPLAY_BUNDLE_BODY
        + "$fn$"
    )
    op.execute(f"DROP FUNCTION {SET_HASH_SIGNATURE}")
    op.execute(f'DROP TRIGGER ai5_learning_split_candidate_source_guard ON public."{TABLE}"')
    op.execute("DROP FUNCTION public.ai5_guard_learning_split_candidate_source()")
    op.drop_index("ix_learning_split_candidate_source_scope", table_name=TABLE)
    op.drop_table(TABLE)
