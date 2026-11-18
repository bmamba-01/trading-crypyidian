"""Giữ current-rights fingerprint trong ACL cột metadata tối thiểu."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "e2f4a6c8b0d3"
down_revision: str | Sequence[str] | None = "e2f4a6c8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "public.wp06_recheck_scorer_current_rights(uuid,uuid)"

_WHOLE_ROW_LICENSED_POLICY = (
    "            SELECT public.wp06_license_source_policy_"
    "fingerprint(policy)\n"
    "              INTO current_policy_fingerprint\n"
    "              FROM public.source_policy_revision AS policy\n"
    "             WHERE policy.workspace_id = target_workspace\n"
    "               AND policy.source_id = policy_row.source_id\n"
    "               AND policy.sequence = policy_row.sequence\n"
    "               AND policy.policy_id = policy_row.policy_id;\n"
)

_SCOPED_LICENSED_POLICY = (
    "            SELECT public.wp06_license_source_policy_fingerprint(\n"
    "                pg_catalog.jsonb_populate_record(\n"
    "                    NULL::public.source_policy_revision,\n"
    "                    pg_catalog.jsonb_build_object(\n"
    "                        'source_id', policy.source_id,\n"
    "                        'sequence', policy.sequence,\n"
    "                        'policy_id', policy.policy_id,\n"
    "                        'state', policy.state,\n"
    "                        'allowed_purposes', policy.allowed_purposes,\n"
    "                        'evidence_ref', policy.evidence_ref,\n"
    "                        'reviewed_at', policy.reviewed_at,\n"
    "                        'valid_until', policy.valid_until,\n"
    "                        'approved_by', policy.approved_by,\n"
    "                        'publisher', policy.publisher,\n"
    "                        'endpoint_url', policy.endpoint_url,\n"
    "                        'access_method', policy.access_method,\n"
    "                        'auth_scope', policy.auth_scope,\n"
    "                        'attribution', policy.attribution,\n"
    "                        'retention_ref', policy.retention_ref,\n"
    "                        'quota_window_seconds', policy.quota_window_seconds,\n"
    "                        'quota_max_requests', policy.quota_max_requests,\n"
    "                        'quota_max_bytes', policy.quota_max_bytes,\n"
    "                        'quota_max_cost', policy.quota_max_cost,\n"
    "                        'quota_cost_unit', policy.quota_cost_unit,\n"
    "                        'license_digest', policy.license_digest,\n"
    "                        'license_evidence_id', policy.license_evidence_id,\n"
    "                        'license_scope_hash', policy.license_scope_hash\n"
    "                    )\n"
    "                )\n"
    "            )\n"
    "              INTO current_policy_fingerprint\n"
    "              FROM public.source_policy_revision AS policy\n"
    "             WHERE policy.workspace_id = target_workspace\n"
    "               AND policy.source_id = policy_row.source_id\n"
    "               AND policy.sequence = policy_row.sequence\n"
    "               AND policy.policy_id = policy_row.policy_id;\n"
)


def _rewrite_source(source: str, *, forward: bool) -> str:
    """Thay tham chiếu toàn hàng bằng danh sách cột fingerprint rõ ràng."""
    expected, replacement = (
        (_WHOLE_ROW_LICENSED_POLICY, _SCOPED_LICENSED_POLICY)
        if forward
        else (_SCOPED_LICENSED_POLICY, _WHOLE_ROW_LICENSED_POLICY)
    )
    if source.count(expected) != 1:
        direction = "UPGRADE" if forward else "DOWNGRADE"
        raise RuntimeError(f"WP06_POLICY_METADATA_FINGERPRINT_{direction}_PREIMAGE_UNEXPECTED")
    return source.replace(expected, replacement, 1)


def _rewrite_function(*, forward: bool) -> None:
    """Giữ owner, quyền gọi và thuộc tính của current-rights gateway."""
    connection = op.get_bind()
    statement = text(
        "SELECT pg_catalog.pg_get_functiondef(oid), prosrc "
        "FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"
    )
    definition, source = connection.execute(statement, {"signature": SIGNATURE}).one()
    if not isinstance(definition, str) or not isinstance(source, str):
        raise RuntimeError("WP06_POLICY_METADATA_FINGERPRINT_FUNCTION_INVALID")
    rewritten_source = _rewrite_source(source, forward=forward)
    rewritten_definition = definition.replace(source, rewritten_source, 1).replace("%", "%%")
    connection.exec_driver_sql(rewritten_definition)
    installed_definition, installed_source = connection.execute(
        statement, {"signature": SIGNATURE}
    ).one()
    if (
        not isinstance(installed_definition, str)
        or installed_source != rewritten_source
        or _rewrite_source(installed_source, forward=not forward) != source
        or installed_definition.count(installed_source) != 1
    ):
        raise RuntimeError("WP06_POLICY_METADATA_FINGERPRINT_POSTIMAGE_UNEXPECTED")


def upgrade() -> None:
    """Tính fingerprint license từ các cột metadata đã được cấp theo workspace."""
    _rewrite_function(forward=True)


def downgrade() -> None:
    """Khôi phục định nghĩa trước migration, không thay ACL hay owner."""
    _rewrite_function(forward=False)
