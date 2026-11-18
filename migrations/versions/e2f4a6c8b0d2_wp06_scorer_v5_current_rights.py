"""Cho scorer recheck manifest v5 qua persisted replay scope hiện hành."""

# Ruff: giữ nguyên các đoạn SQL để xác thực chính xác preimage của function trong DB.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

revision: str = "e2f4a6c8b0d2"
down_revision: str | Sequence[str] | None = "fb4d6e8a0c2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
COMPARATOR_ROLE = "crypidian_manifest_comparator"
SCOPE_CHECK = "public.wp06_check_replay_training_scope"
SCOPE_CHECK_SIGNATURE = (
    "public.wp06_check_replay_training_scope(uuid,text,jsonb,uuid,text,text,uuid,boolean)"
)
MANIFEST_SOURCE_REFS = "public.wp06_scorer_manifest_source_refs(uuid,uuid)"
CURRENT_RIGHTS = "public.wp06_recheck_scorer_current_rights(uuid,uuid)"

_LEGACY_MANIFEST_VERSION_GATE = """            IF canonical_manifest IS NULL OR manifest_version NOT IN (
                    'dataset-manifest-v1','dataset-manifest-v2','dataset-manifest-v3')
               OR canonical_payload::jsonb IS DISTINCT FROM canonical_manifest
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    canonical_payload, 'UTF8')), 'hex') IS DISTINCT FROM canonical_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_MANIFEST_MISSING';
            END IF;"""
_LICENSED_MANIFEST_VERSION_GATE = """            IF canonical_manifest IS NULL OR manifest_version IS NULL
               OR manifest_version NOT IN ('dataset-manifest-v1','dataset-manifest-v2',
                                           'dataset-manifest-v3','dataset-manifest-v5')
               OR canonical_payload::jsonb IS DISTINCT FROM canonical_manifest
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    canonical_payload, 'UTF8')), 'hex') IS DISTINCT FROM canonical_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_MANIFEST_MISSING';
            END IF;"""
_LEGACY_MANIFEST_BRANCH = (
    "            IF manifest_version IN ('dataset-manifest-v1','dataset-manifest-v2') THEN"
)
_LICENSED_MANIFEST_BRANCH = """            IF manifest_version = 'dataset-manifest-v5' THEN
                IF pg_catalog.jsonb_typeof(canonical_manifest->'source_use_lineage')
                        IS DISTINCT FROM 'array'
                   OR EXISTS (
                       SELECT 1 FROM public.dataset_manifest_source_ref AS indexed_ref
                        WHERE indexed_ref.workspace_id = target_workspace
                          AND indexed_ref.manifest_id = target_manifest_id
                   ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_SOURCE_REF_INDEX_MISMATCH';
                END IF;
                RETURN;
            END IF;
            IF manifest_version IN ('dataset-manifest-v1','dataset-manifest-v2') THEN"""

_LEGACY_SCOPE_CALLER_GUARD = """            IF (target_access_kind = 'normalization' AND session_user <> 'crypidian_candidate_builder')
               OR (target_access_kind = 'evaluation' AND session_user <> 'crypidian_learning_evaluator')
               OR (target_access_kind = 'training' AND session_user <> 'crypidian_trainer_worker')
               OR (target_access_kind = 'report_read' AND session_user <> 'crypidian_admin_web')
               OR target_access_kind NOT IN ('normalization', 'evaluation', 'training', 'report_read') THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CALLER_DENIED';
            END IF;"""
_TRAINING_SCOPE_FALLBACK = """                    use_outputs := CASE WHEN target_access_kind IN ('evaluation', 'report_read')
                        THEN '["aggregate_report"]'::jsonb
                        ELSE '["derived_features","trained_model"]'::jsonb END;
                    use_derived := CASE WHEN target_access_kind IN ('evaluation', 'report_read')
                        THEN '["aggregate_reporting"]'::jsonb
                        ELSE '["feature_extraction","model_training"]'::jsonb END;"""
_TRAINING_SCOPE_FALLBACK_PATTERN = re.compile(
    r"use_outputs := CASE WHEN target_access_kind IN \('evaluation', 'report_read'\)\s+"
    r"THEN '\[\"aggregate_report\"\]'::jsonb\s+"
    r"ELSE '\[\"derived_features\",\"trained_model\"\]'::jsonb END;\s+"
    r"use_derived := CASE WHEN target_access_kind IN \('evaluation', 'report_read'\)\s+"
    r"THEN '\[\"aggregate_reporting\"\]'::jsonb\s+"
    r"ELSE '\[\"feature_extraction\",\"model_training\"\]'::jsonb END;"
)

_LEGACY_SOURCE_LOCK_LOOP = """                SELECT lock_source.source_id
                  FROM (
                      SELECT DISTINCT reference.source_id
                        FROM public.wp06_scorer_manifest_source_refs(
                            target_workspace, v_manifest_id
                        ) AS reference
                      UNION SELECT unnest(ARRAY['BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA']::text[]) AS source_id
                  ) AS lock_source
                 ORDER BY lock_source.source_id"""
_V5_SOURCE_LOCK_LOOP = """                SELECT lock_source.source_id
                  FROM (
                      SELECT DISTINCT reference.source_id
                        FROM public.wp06_scorer_manifest_source_refs(
                            target_workspace, v_manifest_id
                        ) AS reference
                      UNION
                      SELECT DISTINCT lineage.value->>'source_id'
                        FROM public.dataset_manifest AS manifest
                        CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                            CASE WHEN pg_catalog.jsonb_typeof(
                                           manifest.manifest->'source_use_lineage') = 'array'
                                 THEN manifest.manifest->'source_use_lineage'
                                 ELSE '[]'::jsonb END
                        ) AS lineage(value)
                       WHERE manifest.workspace_id = target_workspace
                         AND manifest.id = v_manifest_id
                         AND manifest.manifest->>'manifest_version' = 'dataset-manifest-v5'
                      UNION SELECT unnest(ARRAY['BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA']::text[]) AS source_id
                  ) AS lock_source
                 ORDER BY lock_source.source_id"""
_LEGACY_INDEX_CHECK = (
    """            IF (SELECT count(*) FROM public.dataset_manifest_source_ref AS indexed_ref"""
)
_V5_SCOPE_CHECK = """            IF EXISTS (
                SELECT 1 FROM public.dataset_manifest AS source_manifest
                 WHERE source_manifest.workspace_id = target_workspace
                   AND source_manifest.id = v_manifest_id
                   AND source_manifest.manifest->>'manifest_version' = 'dataset-manifest-v5'
            ) THEN
                PERFORM public.wp06_check_replay_training_scope(
                    target_workspace, artifact_row.manifest_hash,
                    (SELECT source_manifest.manifest
                       FROM public.dataset_manifest AS source_manifest
                      WHERE source_manifest.workspace_id = target_workspace
                        AND source_manifest.id = v_manifest_id),
                    artifact_row.normalized_dataset_id, artifact_row.fold_plan_hash,
                    'deployment_recheck', access_row.evaluation_run_id, false
                );
            END IF;
            IF (SELECT count(*) FROM public.dataset_manifest_source_ref AS indexed_ref"""


def _rewrite_pairs(source: str, pairs: tuple[tuple[str, str], ...], *, forward: bool) -> str:
    """Đổi duy nhất các đoạn đã kiểm chứng và đảo lại được chính xác."""
    rewritten = source
    for index, (legacy, current) in enumerate(pairs, start=1):
        expected, replacement = (legacy, current) if forward else (current, legacy)
        if forward and rewritten.count(expected) == 0 and rewritten.count(replacement) == 1:
            continue
        if rewritten.count(expected) != 1:
            direction = "UPGRADE" if forward else "DOWNGRADE"
            raise RuntimeError(
                f"WP06_SCORER_V5_CURRENT_RIGHTS_{direction}_PREIMAGE_UNEXPECTED_PAIR_{index}"
            )
        rewritten = rewritten.replace(expected, replacement, 1)
    return rewritten


def _rewrite_manifest_source_refs(source: str, *, forward: bool) -> str:
    """Chấp nhận v5 chỉ khi payload canonical và index refs v3 rỗng."""
    if forward and "'dataset-manifest-v5'" in source:
        return source
    if not forward and _LICENSED_MANIFEST_VERSION_GATE not in source:
        return source
    return _rewrite_pairs(
        source,
        (
            (_LEGACY_MANIFEST_VERSION_GATE, _LICENSED_MANIFEST_VERSION_GATE),
            (_LEGACY_MANIFEST_BRANCH, _LICENSED_MANIFEST_BRANCH),
        ),
        forward=forward,
    )


def _rewrite_scope_check(source: str, *, forward: bool) -> str:
    """Thêm caller deployment; operation tiếp tục vào nhánh quyền tạo mô hình."""
    if len(_TRAINING_SCOPE_FALLBACK_PATTERN.findall(source)) != 1:
        direction = "UPGRADE" if forward else "DOWNGRADE"
        raise RuntimeError(f"WP06_SCORER_V5_CURRENT_RIGHTS_{direction}_MAPPING_UNEXPECTED")
    if forward:
        return _extend_scope_caller_guard(source)
    return _revert_scope_caller_guard(source)


def _extend_scope_caller_guard(source: str) -> str:
    if "'deployment_recheck'" in source:
        return source
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)OR target_access_kind NOT IN \((?P<operations>[^)]*)\) THEN",
        re.MULTILINE,
    )
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_UPGRADE_PREIMAGE_UNEXPECTED")
    match = matches[0]
    operations = match.group("operations")
    op_tuple = tuple(re.findall(r"'([^']+)'", operations))
    if op_tuple == (
        "normalization",
        "evaluation",
        "training",
        "report_read",
        "deployment_recheck",
    ):
        return source
    if op_tuple != (
        "normalization",
        "evaluation",
        "training",
        "report_read",
    ):
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_UPGRADE_PREIMAGE_UNEXPECTED")
    indent = match.group("indent")
    replacement = (
        f"{indent}OR (target_access_kind = 'deployment_recheck' AND session_user NOT IN (\n"
        "                    'crypidian_admin_web', 'crypidian_trading_runtime',\n"
        "                    'crypidian_market_signal_worker'))\n"
        f"{indent}OR target_access_kind NOT IN ({operations}, 'deployment_recheck') THEN"
    )
    return source[: match.start()] + replacement + source[match.end() :]


def _revert_scope_caller_guard(source: str) -> str:
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)OR \(target_access_kind = 'deployment_recheck' "
        r"AND session_user NOT IN \(\n"
        r"                    'crypidian_admin_web', 'crypidian_trading_runtime',\n"
        r"                    'crypidian_market_signal_worker'\)\)\n"
        r"(?P=indent)OR target_access_kind NOT IN \((?P<operations>[^)]*), 'deployment_recheck'\) THEN",
        re.MULTILINE,
    )
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_DOWNGRADE_PREIMAGE_UNEXPECTED")
    match = matches[0]
    operations = match.group("operations")
    if tuple(re.findall(r"'([^']+)'", operations)) != (
        "normalization",
        "evaluation",
        "training",
        "report_read",
    ):
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_DOWNGRADE_PREIMAGE_UNEXPECTED")
    replacement = f"{match.group('indent')}OR target_access_kind NOT IN ({operations}) THEN"
    return source[: match.start()] + replacement + source[match.end() :]


def _rewrite_current_rights(source: str, *, forward: bool) -> str:
    """Ghim provider locks và gọi exact b7 checker chỉ cho manifest v5."""
    if "wp06_scorer_replay_evaluation_scope" in source or (
        forward and "'dataset-manifest-v5'" in source
    ):
        return source
    return _rewrite_pairs(
        source,
        (
            (_LEGACY_SOURCE_LOCK_LOOP, _V5_SOURCE_LOCK_LOOP),
            (_LEGACY_INDEX_CHECK, _V5_SCOPE_CHECK),
        ),
        forward=forward,
    )


def _rewrite_function(
    signature: str,
    transform: Callable[..., str],
    *,
    forward: bool,
) -> None:
    """CREATE OR REPLACE giữ nguyên owner, ACL và cấu hình security hiện hành."""
    connection = op.get_bind()
    statement = text(
        "SELECT pg_catalog.pg_get_functiondef(oid), prosrc "
        "FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"
    )
    definition, source = connection.execute(statement, {"signature": signature}).one()
    if not isinstance(definition, str) or not isinstance(source, str):
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_SOURCE_INVALID")
    try:
        rewritten_source = transform(source, forward=forward)
    except RuntimeError as error:
        raise RuntimeError(f"{signature}: {error}") from error
    if rewritten_source == source:
        return
    if definition.count(source) != 1:
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_DEFINITION_UNEXPECTED")
    rewritten_definition = definition.replace(source, rewritten_source, 1).replace("%", "%%")
    connection.exec_driver_sql(rewritten_definition)
    installed_definition, installed_source = connection.execute(
        statement, {"signature": signature}
    ).one()
    if not isinstance(installed_definition, str) or not isinstance(installed_source, str):
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_INSTALLED_SOURCE_INVALID")
    if (
        installed_source != rewritten_source
        or transform(installed_source, forward=not forward) != source
        or installed_definition.count(installed_source) != 1
    ):
        raise RuntimeError("WP06_SCORER_V5_CURRENT_RIGHTS_POSTIMAGE_UNEXPECTED")


def upgrade() -> None:
    """Chạy b7 exact-scope recheck trong caller hiện hành của scorer deployment."""
    op.execute(
        "CREATE POLICY dataset_manifest_source_ref_comparator_select "
        "ON public.dataset_manifest_source_ref FOR SELECT "
        f"TO {COMPARATOR_ROLE} USING (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        "GRANT SELECT (workspace_id,manifest_id) "
        f"ON public.dataset_manifest_source_ref TO {COMPARATOR_ROLE}"
    )
    _rewrite_function(
        SCOPE_CHECK_SIGNATURE,
        _rewrite_scope_check,
        forward=True,
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCOPE_CHECK_SIGNATURE} TO {RIGHTS_ROLE}")
    _rewrite_function(
        MANIFEST_SOURCE_REFS,
        _rewrite_manifest_source_refs,
        forward=True,
    )
    _rewrite_function(
        CURRENT_RIGHTS,
        _rewrite_current_rights,
        forward=True,
    )
    op.execute(f"GRANT SELECT (manifest) ON public.dataset_manifest TO {RIGHTS_ROLE}")


def downgrade() -> None:
    """Khôi phục helper cũ trước khi thu hồi quyền nội bộ bổ sung."""
    _rewrite_function(
        CURRENT_RIGHTS,
        _rewrite_current_rights,
        forward=False,
    )
    op.execute(f"REVOKE SELECT (manifest) ON public.dataset_manifest FROM {RIGHTS_ROLE}")
    _rewrite_function(
        MANIFEST_SOURCE_REFS,
        _rewrite_manifest_source_refs,
        forward=False,
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SCOPE_CHECK_SIGNATURE} FROM {RIGHTS_ROLE}")
    _rewrite_function(
        SCOPE_CHECK_SIGNATURE,
        _rewrite_scope_check,
        forward=False,
    )
    op.execute(
        "DROP POLICY dataset_manifest_source_ref_comparator_select "
        "ON public.dataset_manifest_source_ref"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,manifest_id) "
        f"ON public.dataset_manifest_source_ref FROM {COMPARATOR_ROLE}"
    )
