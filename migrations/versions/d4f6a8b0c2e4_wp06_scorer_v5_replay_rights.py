"""WP06 lát B: scorer recheck/deployment đọc manifest v5 có lineage replay bundle.

Revision ID: d4f6a8b0c2e4
Revises: c2e4a6b8d0f1

Từ `b7d9f1a3c5e8` manifest TRAINING phụ thuộc nguồn chỉ được là `dataset-manifest-v5` có
`source_use_lineage`, và receipt TRAINING/EVALUATION dựng scope 24 trường (license, operation,
scope hash). Nhưng `wp06_scorer_manifest_source_refs` (d7f9a1b3c5e8) chỉ nhận v1–v3 và
`wp06_recheck_scorer_current_rights` vẫn dựng `training_scope` dạng cũ 12 trường, nên scorer
đã train từ v5 không bao giờ qua được recheck/deployment
(`WP06_SCORER_RIGHTS_MANIFEST_MISSING`), còn digest C3/C4 lệch với access receipt.

Theo spec `2026-11-13-wp06-source-use-scope-consumers-design.md` (mục Scorer TEST/deployment,
bảng operation dòng 31–34: TRAINING evaluation = training + aggregate_report +
aggregate_reporting), DB dựng quyền từ persisted proof/source bundle, không từ caller:

1. Helper nội bộ `wp06_scorer_replay_evaluation_scope` sinh từ chính định nghĩa đã ghim của
   `wp06_authorize_replay_training_bundle` (cùng mọi kiểm bundle/child/MarketBar/policy/
   evidence/scope covers), chỉ khác: không kiểm login và chỉ nhận operation `evaluation`.
   Owner `crypidian_learning_source_rights_reader`; EXECUTE chỉ cho role NOLOGIN
   `crypidian_scorer_deployment_rights_reader` (owner của recheck). Không cấp quyền bảng.
2. `wp06_scorer_manifest_source_refs` nhận v5: kiểm sample đúng dạng lineage replay (không
   source-item ref, hash bundle/lineage-set hợp lệ) rồi trả tập rỗng; v1–v3 giữ nguyên.
3. Recheck thay câu dựng `training_scope` cũ bằng scope EVALUATION hiện hành từ helper, đúng
   `ReplayTrainingRightsReceipt.scope_payload` (key sắp xếp, timestamp ISO UTC) mà
   `_current_training_scope_digest` đã ghim vào access receipt. Kiểm cũ khác giữ nguyên.

Mọi rewrite ghim hash toàn định nghĩa trước/sau và số lần thay; downgrade đảo patch, xóa
helper, không sửa dữ liệu.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4f6a8b0c2e4"
down_revision: str | None = "c2e4a6b8d0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_RIGHTS_READER = "crypidian_learning_source_rights_reader"
SCORER_RIGHTS_READER = "crypidian_scorer_deployment_rights_reader"
GATE_SIGNATURE = "public.wp06_authorize_replay_training_bundle(uuid,text,text)"
HELPER_SIGNATURE = "public.wp06_scorer_replay_evaluation_scope(uuid,text,text)"
SOURCE_REFS_SIGNATURE = "public.wp06_scorer_manifest_source_refs(uuid,uuid)"
RECHECK_SIGNATURE = "public.wp06_recheck_scorer_current_rights(uuid,uuid)"

FUNCTION_DEFINITION_HASHES = {
    GATE_SIGNATURE: {
        "preimage": "11d0e3bf30df9642b40c614d78f95a575827c22f27d7c13f1ff455be8f88ad67"
    },
    HELPER_SIGNATURE: {
        "postimage": "f1e400287e6bce0ddb0dc61a467e2b9348fb501ba99b03855aa7d1739ba082e3"
    },
    SOURCE_REFS_SIGNATURE: {
        "preimage": "1f4fce257dbecf11392fc25d168759261632ac6c18adfbd06a54edf69734a53b",
        "postimage": "48b14a344ec9b38c4f6882dd71b7a7ab47b52ceddd4beed573bdd16491198406",
    },
    RECHECK_SIGNATURE: {
        "preimage": "c46e15a5bf2518faeac621bb22c8bbec3a3245bacfbced545eb4b2714108cecb",
        "postimage": "b7f14a22c92fd2943ced9e3f29435ac01e38421e3b357050c482889bbcd66716",
    },
}

HELPER_PATCHES = (
    (
        "CREATE OR REPLACE FUNCTION public.wp06_authorize_replay_training_bundle(",
        "CREATE FUNCTION public.wp06_scorer_replay_evaluation_scope(",
    ),
    (
        "IF (target_operation = 'training' AND session_user <> 'crypidian_candidate_builder')\n"
        "               OR (target_operation = 'evaluation' AND session_user NOT IN (\n"
        "                   'crypidian_learning_evaluator', 'crypidian_candidate_builder'))\n"
        "               OR target_operation NOT IN ('training', 'evaluation') THEN",
        # Helper chỉ phục vụ recheck SECURITY DEFINER; quyền gọi do EXECUTE grant quyết định.
        "IF target_operation IS DISTINCT FROM 'evaluation' THEN",
    ),
)

SOURCE_REFS_PATCHES = (
    (
        "manifest_version NOT IN (\n"
        "                    'dataset-manifest-v1','dataset-manifest-v2','dataset-manifest-v3')",
        "manifest_version NOT IN (\n"
        "                    'dataset-manifest-v1','dataset-manifest-v2','dataset-manifest-v3',\n"
        "                    'dataset-manifest-v5')",
    ),
    (
        "            IF manifest_version IN ('dataset-manifest-v1','dataset-manifest-v2') THEN",
        """            IF manifest_version = 'dataset-manifest-v5' THEN
                -- v5 ghim lineage replay bundle, không có source-item ref; quyền replay
                -- được recheck riêng từ bundle của proof TEST.
                IF pg_catalog.jsonb_typeof(canonical_manifest->'source_use_lineage')
                        IS DISTINCT FROM 'array'
                   OR EXISTS (
                    SELECT 1
                      FROM pg_catalog.jsonb_array_elements(
                               COALESCE(canonical_manifest->'samples', '[]'::jsonb)
                           ) AS sample(value)
                     WHERE pg_catalog.jsonb_typeof(sample.value->'source_dependent')
                                IS DISTINCT FROM 'boolean'
                        OR sample.value ? 'source_references'
                        OR sample.value ? 'source_rights_hashes'
                        OR sample.value ? 'source_revision_refs'
                        OR NOT (sample.value ? 'source_replay_bundle_hash')
                        OR NOT (sample.value ? 'source_use_lineage_set_hash')
                        OR (sample.value->>'source_dependent' = 'true'
                            AND (COALESCE(sample.value->>'source_replay_bundle_hash', '')
                                    !~ '^[0-9a-f]{64}$'
                                 OR COALESCE(sample.value->>'source_use_lineage_set_hash', '')
                                    !~ '^[0-9a-f]{64}$'))
                        OR (sample.value->>'source_dependent' = 'false'
                            AND (pg_catalog.jsonb_typeof(
                                     sample.value->'source_replay_bundle_hash')
                                    IS DISTINCT FROM 'null'
                                 OR pg_catalog.jsonb_typeof(
                                     sample.value->'source_use_lineage_set_hash')
                                    IS DISTINCT FROM 'null'))
                ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_TYPED_SOURCE_LINEAGE_INVALID';
                END IF;
                RETURN;
            END IF;
            IF manifest_version IN ('dataset-manifest-v1','dataset-manifest-v2') THEN""",
    ),
)

LEGACY_TRAINING_SCOPE_STATEMENT = """\
            SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
                       'bars_content_hash', child.manifest_json->>'bars_content_hash',
                       'bundle_hash', proof_row.bundle_hash,
                       'child_dataset_hash', child.dataset_hash,
                       'interval', child.interval,
                       'policy_fingerprint', current_policy_fingerprint,
                       'policy_id', policy_row.policy_id,
                       'provider', target_source_id,
                       'purpose', 'training',
                       'symbol', bundle_row.symbol,
                       'terms_url', policy_row.evidence_ref,
                       'verified_at', public.wp06_canonical_utc_timestamp_json(
                           policy_row.reviewed_at),
                       'workspace_id', target_workspace::text
                   ) ORDER BY child.dataset_hash COLLATE "C"), '[]'::json)
              INTO training_scope
              FROM pg_catalog.jsonb_array_elements(bundle_row.bundle_json->'components')
                   AS component(value)
              JOIN public.backtest_dataset_manifest AS child
                ON child.workspace_id = target_workspace
               AND child.dataset_hash = component.value->>'dataset_hash'
             WHERE child.status = 'READY'
               AND child.symbol = bundle_row.symbol
               AND child.interval = component.value->>'interval'
               AND child.policy_fingerprint = bundle_row.policy_fingerprint
               AND child.evaluation_start = bundle_row.evaluation_start
               AND child.range_end = bundle_row.range_end
               AND child.manifest_json->>'dataset_hash' = child.dataset_hash
               AND child.manifest_json->>'status' = child.status
               AND child.manifest_json->>'symbol' = child.symbol
               AND child.manifest_json->>'interval' = child.interval
               AND child.manifest_json->>'warmup_start' =
                    (public.wp06_canonical_utc_timestamp_json(child.warmup_start)#>>'{}')
               AND child.manifest_json->>'prepared_at' =
                    (public.wp06_canonical_utc_timestamp_json(child.prepared_at)#>>'{}')
               AND child.manifest_json->>'bars_content_hash' =
                    component.value->>'bars_content_hash'
               AND child.manifest_json->>'rights_workspace_id' = target_workspace::text
               AND child.manifest_json->>'provider' = bundle_row.rights_provider
               AND child.manifest_json->>'purpose' = bundle_row.rights_purpose
               AND child.manifest_json->>'policy_fingerprint' = child.policy_fingerprint
               AND child.manifest_json->>'evaluation_start' =
                    (public.wp06_canonical_utc_timestamp_json(bundle_row.evaluation_start)#>>'{}')
               AND child.manifest_json->>'range_end' =
                    (public.wp06_canonical_utc_timestamp_json(bundle_row.range_end)#>>'{}')
               AND child.manifest_json = child.canonical_manifest::jsonb
               AND public.wp06_compact_canonical_json(child.canonical_manifest::json)
                    IS NOT DISTINCT FROM child.canonical_manifest
               AND pg_catalog.length(child.canonical_manifest) - pg_catalog.length(
                    pg_catalog.replace(child.canonical_manifest,
                        ',"dataset_hash":"' || child.dataset_hash || '"', ''))
                    = pg_catalog.length(',"dataset_hash":"' || child.dataset_hash || '"')
               AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.replace(child.canonical_manifest,
                        ',"dataset_hash":"' || child.dataset_hash || '"', ''), 'UTF8')),
                    'hex') IS NOT DISTINCT FROM child.dataset_hash;"""

CURRENT_TRAINING_SCOPE_STATEMENT = """            -- Scope EVALUATION hiện hành theo đúng
            -- ReplayTrainingRightsReceipt.scope_payload (key sắp xếp; checked_at và
            -- valid_until không thuộc scope) mà access receipt đã ghim.
            SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
                       'bars_content_hash', scope.bars_content_hash,
                       'bundle_hash', scope.bundle_hash,
                       'bundle_policy_fingerprint', scope.bundle_policy_fingerprint,
                       'child_dataset_hash', scope.child_dataset_hash,
                       'contract', scope.contract,
                       'current_policy_fingerprint', scope.current_policy_fingerprint,
                       'data_end', public.wp06_canonical_utc_timestamp_json(scope.data_end),
                       'data_start', public.wp06_canonical_utc_timestamp_json(scope.data_start),
                       'evidence_digest', scope.evidence_digest,
                       'interval', scope."interval",
                       'license_evidence_id', scope.license_evidence_id::text,
                       'license_scope_hash', scope.license_scope_hash,
                       'operation', 'evaluation',
                       'policy_fingerprint', scope.policy_fingerprint,
                       'policy_id', scope.policy_id,
                       'product', scope.product,
                       'provider', scope.provider,
                       'purpose', scope.purpose,
                       'scope_hash', scope.scope_hash,
                       'symbol', scope.symbol,
                       'terms_url', scope.terms_url,
                       'venue', scope.venue,
                       'verified_at', public.wp06_canonical_utc_timestamp_json(scope.verified_at),
                       'workspace_id', scope.workspace_id::text
                   ) ORDER BY scope.child_dataset_hash COLLATE "C"), '[]'::json)
              INTO training_scope
              FROM public.wp06_scorer_replay_evaluation_scope(
                       target_workspace, proof_row.bundle_hash, 'evaluation') AS scope
             WHERE scope.workspace_id = target_workspace
               AND scope.bundle_hash = proof_row.bundle_hash
               AND scope.purpose = 'training'
               AND scope.provider = target_source_id;"""

RECHECK_PATCHES = ((LEGACY_TRAINING_SCOPE_STATEMENT, CURRENT_TRAINING_SCOPE_STATEMENT),)


def _dollar(tag: str, value: str) -> str:
    """Bọc literal bằng dollar-quote riêng; tag không được xuất hiện trong nội dung."""
    if f"${tag}$" in value:
        raise RuntimeError("WP06_SCORER_V5_RIGHTS_DOLLAR_TAG_COLLISION")
    return f"${tag}${value}${tag}$"


def _patch_sql(patches: tuple[tuple[str, str], ...], *, forward: bool, exception: str) -> str:
    """Mỗi patch phải xuất hiện đúng một lần trước khi thay."""
    statements = []
    for index, (previous, replacement) in enumerate(patches):
        before, after = (previous, replacement) if forward else (replacement, previous)
        old = _dollar(f"old{index}", before)
        new = _dollar(f"new{index}", after)
        statements.append(f"""
            IF (pg_catalog.length(v_definition) - pg_catalog.length(
                    pg_catalog.replace(v_definition, {old}, ''))) / pg_catalog.length({old}) <> 1
            THEN
                RAISE EXCEPTION '{exception}_DEFINITION_UNEXPECTED';
            END IF;
            v_definition := pg_catalog.replace(v_definition, {old}, {new});""")
    return "".join(statements)


def _hash_check(signature: str, expected: str, exception: str) -> str:
    return f"""
            IF pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.pg_get_functiondef('{signature}'::regprocedure), 'UTF8')), 'hex')
                <> '{expected}' THEN
                RAISE EXCEPTION '{exception}';
            END IF;"""


def _rewrite(
    signature: str,
    patches: tuple[tuple[str, str], ...],
    *,
    forward: bool,
    expected_before: str,
    expected_after: str,
    exception: str,
) -> None:
    """CREATE OR REPLACE từ pg_get_functiondef: giữ owner/ACL/SECURITY DEFINER/search_path."""
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            {_hash_check(signature, expected_before, exception + "_PREIMAGE_UNEXPECTED")}
            v_definition := pg_catalog.pg_get_functiondef('{signature}'::regprocedure);
            {_patch_sql(patches, forward=forward, exception=exception)}
            EXECUTE v_definition;
            {_hash_check(signature, expected_after, exception + "_POSTIMAGE_UNEXPECTED")}
        END $migration$""")


def _create_helper() -> None:
    """Sinh helper từ định nghĩa gate đã ghim; không sao chép tay logic quyền."""
    hashes = FUNCTION_DEFINITION_HASHES
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            {
        _hash_check(
            GATE_SIGNATURE,
            hashes[GATE_SIGNATURE]["preimage"],
            "WP06_SCORER_V5_RIGHTS_GATE_PREIMAGE_UNEXPECTED",
        )
    }
            v_definition := pg_catalog.pg_get_functiondef('{GATE_SIGNATURE}'::regprocedure);
            {_patch_sql(HELPER_PATCHES, forward=True, exception="WP06_SCORER_V5_RIGHTS_HELPER")}
            EXECUTE v_definition;
            {
        _hash_check(
            HELPER_SIGNATURE,
            hashes[HELPER_SIGNATURE]["postimage"],
            "WP06_SCORER_V5_RIGHTS_HELPER_POSTIMAGE_UNEXPECTED",
        )
    }
        END $migration$""")
    op.execute(f"ALTER FUNCTION {HELPER_SIGNATURE} OWNER TO {SOURCE_RIGHTS_READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {HELPER_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {HELPER_SIGNATURE} TO {SCORER_RIGHTS_READER}")


def upgrade() -> None:
    """Helper trước, rồi source refs v5, cuối cùng recheck dùng helper."""
    hashes = FUNCTION_DEFINITION_HASHES
    _create_helper()
    _rewrite(
        SOURCE_REFS_SIGNATURE,
        SOURCE_REFS_PATCHES,
        forward=True,
        expected_before=hashes[SOURCE_REFS_SIGNATURE]["preimage"],
        expected_after=hashes[SOURCE_REFS_SIGNATURE]["postimage"],
        exception="WP06_SCORER_V5_RIGHTS_SOURCE_REFS",
    )
    _rewrite(
        RECHECK_SIGNATURE,
        RECHECK_PATCHES,
        forward=True,
        expected_before=hashes[RECHECK_SIGNATURE]["preimage"],
        expected_after=hashes[RECHECK_SIGNATURE]["postimage"],
        exception="WP06_SCORER_V5_RIGHTS_RECHECK",
    )


def downgrade() -> None:
    """Đảo đúng thứ tự: recheck về scope cũ, source refs về v1–v3, rồi xóa helper.

    Không đụng dữ liệu; artifact/report v5 đã ghi sẽ lại bị recheck cũ từ chối (fail closed).
    """
    hashes = FUNCTION_DEFINITION_HASHES
    _rewrite(
        RECHECK_SIGNATURE,
        RECHECK_PATCHES,
        forward=False,
        expected_before=hashes[RECHECK_SIGNATURE]["postimage"],
        expected_after=hashes[RECHECK_SIGNATURE]["preimage"],
        exception="WP06_SCORER_V5_RIGHTS_RECHECK",
    )
    _rewrite(
        SOURCE_REFS_SIGNATURE,
        SOURCE_REFS_PATCHES,
        forward=False,
        expected_before=hashes[SOURCE_REFS_SIGNATURE]["postimage"],
        expected_after=hashes[SOURCE_REFS_SIGNATURE]["preimage"],
        exception="WP06_SCORER_V5_RIGHTS_SOURCE_REFS",
    )
    op.execute(f"DROP FUNCTION {HELPER_SIGNATURE}")
