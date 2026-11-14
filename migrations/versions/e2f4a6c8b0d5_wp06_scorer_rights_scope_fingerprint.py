"""Đồng bộ fingerprint TEST với rights scope có license và operation hiện hành."""

# Ruff: chuỗi migration giữ nguyên SQL để đối chiếu preimage trong PostgreSQL.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "e2f4a6c8b0d5"
down_revision: str | Sequence[str] | None = "e2f4a6c8b0d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
RIGHTS_FUNCTION = "public.wp06_recheck_scorer_current_rights(uuid,uuid)"
BUNDLE_GATE = "public.wp06_authorize_replay_training_bundle"
BUNDLE_GATE_SIGNATURE = f"{BUNDLE_GATE}(uuid,text,text)"

_LEGACY_TRAINING_SCOPE = """            SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
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
                        ',\"dataset_hash\":\"' || child.dataset_hash || '\"', ''))
                    = pg_catalog.length(',\"dataset_hash\":\"' || child.dataset_hash || '\"')
               AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.replace(child.canonical_manifest,
                        ',\"dataset_hash\":\"' || child.dataset_hash || '\"', ''), 'UTF8')),
                    'hex') IS NOT DISTINCT FROM child.dataset_hash;
"""

_LICENSED_TRAINING_SCOPE = """            SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
                       'bars_content_hash', rights.bars_content_hash,
                       'bundle_hash', rights.bundle_hash,
                       'bundle_policy_fingerprint', rights.bundle_policy_fingerprint,
                       'child_dataset_hash', rights.child_dataset_hash,
                       'contract', rights.contract,
                       'current_policy_fingerprint', rights.current_policy_fingerprint,
                       'data_end', public.wp06_canonical_utc_timestamp_json(rights.data_end),
                       'data_start', public.wp06_canonical_utc_timestamp_json(rights.data_start),
                       'evidence_digest', rights.evidence_digest,
                       'interval', rights.interval,
                       'license_evidence_id', rights.license_evidence_id::text,
                       'license_scope_hash', rights.license_scope_hash,
                       'operation', 'evaluation',
                       'policy_fingerprint', rights.policy_fingerprint,
                       'policy_id', rights.policy_id,
                       'product', rights.product,
                       'provider', rights.provider,
                       'purpose', rights.purpose,
                       'scope_hash', rights.scope_hash,
                       'symbol', rights.symbol,
                       'terms_url', rights.terms_url,
                       'venue', rights.venue,
                       'verified_at', public.wp06_canonical_utc_timestamp_json(rights.verified_at),
                       'workspace_id', rights.workspace_id::text
                   ) ORDER BY rights.child_dataset_hash COLLATE "C"), '[]'::json)
              INTO training_scope
              FROM public.wp06_authorize_replay_training_bundle(
                  target_workspace, proof_row.bundle_hash, 'evaluation'
              ) AS rights;
"""

_LEGACY_EVALUATION_CALLER_GUARD = (
    "target_operation = 'evaluation' AND session_user <> 'crypidian_learning_evaluator'"
)
_LICENSED_EVALUATION_CALLER_GUARD = (
    "target_operation = 'evaluation' AND session_user NOT IN "
    "('crypidian_learning_evaluator', 'crypidian_admin_web', "
    "'crypidian_trading_runtime', 'crypidian_market_signal_worker')"
)


def _rewrite_current_rights_source(source: str, *, forward: bool) -> str:
    """Dựng digest từ đúng payload license-aware mà repository ghi vào receipt."""
    expected, replacement = (
        (_LEGACY_TRAINING_SCOPE, _LICENSED_TRAINING_SCOPE)
        if forward
        else (_LICENSED_TRAINING_SCOPE, _LEGACY_TRAINING_SCOPE)
    )
    markers = tuple(
        re.finditer(
            r"(?m)^[ \t]*SELECT\s+COALESCE\(pg_catalog\.json_agg\("
            r"pg_catalog\.json_build_object\(",
            source,
        )
    )
    if len(markers) != 1 or source.count("INTO training_scope") != 1:
        direction = "UPGRADE" if forward else "DOWNGRADE"
        raise RuntimeError(f"WP06_SCORER_RIGHTS_SCOPE_{direction}_PREIMAGE_UNEXPECTED")
    statement_start = markers[0].start()
    into_scope = source.index("INTO training_scope", statement_start)
    statement_end = source.find(";", into_scope)
    if statement_end < 0:
        direction = "UPGRADE" if forward else "DOWNGRADE"
        raise RuntimeError(f"WP06_SCORER_RIGHTS_SCOPE_{direction}_PREIMAGE_UNEXPECTED")
    statement_end += 1
    actual = source[statement_start:statement_end]
    if re.sub(r"\s+", "", actual) != re.sub(r"\s+", "", expected):
        direction = "UPGRADE" if forward else "DOWNGRADE"
        raise RuntimeError(f"WP06_SCORER_RIGHTS_SCOPE_{direction}_PREIMAGE_UNEXPECTED")
    return source[:statement_start] + replacement.rstrip("\n") + source[statement_end:]


def _rewrite_bundle_gate_source(source: str, *, forward: bool) -> str:
    """Chỉ cho phép nested C4 caller qua EXECUTE đã cấp cho role nội bộ."""
    expected, replacement = (
        (_LEGACY_EVALUATION_CALLER_GUARD, _LICENSED_EVALUATION_CALLER_GUARD)
        if forward
        else (_LICENSED_EVALUATION_CALLER_GUARD, _LEGACY_EVALUATION_CALLER_GUARD)
    )
    if source.count(expected) != 1:
        direction = "UPGRADE" if forward else "DOWNGRADE"
        raise RuntimeError(f"WP06_BUNDLE_GATE_CALLER_{direction}_PREIMAGE_UNEXPECTED")
    return source.replace(expected, replacement, 1)


def _rewrite_function(signature: str, *, forward: bool, bundle_gate: bool) -> None:
    """CREATE OR REPLACE giữ lại owner, ACL, volatility và security properties."""
    connection = op.get_bind()
    statement = text(
        "SELECT pg_catalog.pg_get_functiondef(oid), prosrc "
        "FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"
    )
    definition, source = connection.execute(statement, {"signature": signature}).one()
    if not isinstance(definition, str) or not isinstance(source, str):
        raise RuntimeError("WP06_SCORER_RIGHTS_SCOPE_FUNCTION_INVALID")
    transform = _rewrite_bundle_gate_source if bundle_gate else _rewrite_current_rights_source
    rewritten_source = transform(source, forward=forward)
    if definition.count(source) != 1:
        raise RuntimeError("WP06_SCORER_RIGHTS_SCOPE_DEFINITION_UNEXPECTED")
    connection.exec_driver_sql(definition.replace(source, rewritten_source, 1).replace("%", "%%"))
    installed_definition, installed_source = connection.execute(
        statement, {"signature": signature}
    ).one()
    if (
        not isinstance(installed_definition, str)
        or installed_source != rewritten_source
        or transform(installed_source, forward=not forward) != source
        or installed_definition.count(installed_source) != 1
    ):
        raise RuntimeError("WP06_SCORER_RIGHTS_SCOPE_POSTIMAGE_UNEXPECTED")


def upgrade() -> None:
    """Ghim C4 vào gateway bundle duy nhất có receipt rights license-aware."""
    _rewrite_function(BUNDLE_GATE_SIGNATURE, forward=True, bundle_gate=True)
    op.execute(f"GRANT EXECUTE ON FUNCTION {BUNDLE_GATE_SIGNATURE} TO {RIGHTS_ROLE}")
    _rewrite_function(RIGHTS_FUNCTION, forward=True, bundle_gate=False)


def downgrade() -> None:
    """Khôi phục payload cũ và thu hồi quyền nested gateway hẹp."""
    _rewrite_function(RIGHTS_FUNCTION, forward=False, bundle_gate=False)
    op.execute(f"REVOKE EXECUTE ON FUNCTION {BUNDLE_GATE_SIGNATURE} FROM {RIGHTS_ROLE}")
    _rewrite_function(BUNDLE_GATE_SIGNATURE, forward=False, bundle_gate=True)
