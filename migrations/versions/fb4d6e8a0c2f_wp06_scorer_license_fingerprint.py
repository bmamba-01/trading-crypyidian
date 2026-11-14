"""Sửa scorer current-rights để fingerprint gắn license evidence hiện hành."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "fb4d6e8a0c2f"
down_revision: str | Sequence[str] | None = "fa2c4e6b8d0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "public.wp06_recheck_scorer_current_rights(uuid,uuid)"

_LEGACY_PER_ITEM = (
    "                source_rights_hash := "
    "public.wp06_scorer_source_policy_fingerprint(\n"
    "                    policy_row.source_id, policy_row.sequence, policy_row.policy_id,\n"
    "                    policy_row.state, policy_row.allowed_purposes, policy_row.evidence_ref,\n"
    "                    policy_row.reviewed_at, policy_row.valid_until, policy_row.approved_by,\n"
    "                    policy_row.publisher, policy_row.endpoint_url, policy_row.access_method,\n"
    "                    policy_row.auth_scope, policy_row.attribution, policy_row.retention_ref,\n"
    "                    policy_row.quota_window_seconds, policy_row.quota_max_requests,\n"
    "                    policy_row.quota_max_bytes, policy_row.quota_max_cost,\n"
    "                    policy_row.quota_cost_unit);"
)
_LICENSED_PER_ITEM = (
    "                SELECT public.wp06_license_source_policy_fingerprint(policy)\n"
    "                  INTO source_rights_hash\n"
    "                  FROM public.source_policy_revision AS policy\n"
    "                 WHERE policy.workspace_id = target_workspace\n"
    "                   AND policy.source_id = policy_row.source_id\n"
    "                   AND policy.sequence = policy_row.sequence\n"
    "                   AND policy.policy_id = policy_row.policy_id;"
)

_LEGACY_CURRENT_POLICY = (
    "            current_policy_fingerprint := "
    "public.wp06_scorer_source_policy_fingerprint(\n"
    "                policy_row.source_id, policy_row.sequence, policy_row.policy_id,\n"
    "                policy_row.state, policy_row.allowed_purposes, policy_row.evidence_ref,\n"
    "                policy_row.reviewed_at, policy_row.valid_until, policy_row.approved_by,\n"
    "                policy_row.publisher, policy_row.endpoint_url, policy_row.access_method,\n"
    "                policy_row.auth_scope, policy_row.attribution, policy_row.retention_ref,\n"
    "                policy_row.quota_window_seconds, policy_row.quota_max_requests,\n"
    "                policy_row.quota_max_bytes, policy_row.quota_max_cost,\n"
    "                policy_row.quota_cost_unit);"
)
_LICENSED_CURRENT_POLICY = (
    "            SELECT public.wp06_license_source_policy_"
    "fingerprint(policy)\n"
    "              INTO current_policy_fingerprint\n"
    "              FROM public.source_policy_revision AS policy\n"
    "             WHERE policy.workspace_id = target_workspace\n"
    "               AND policy.source_id = policy_row.source_id\n"
    "               AND policy.sequence = policy_row.sequence\n"
    "               AND policy.policy_id = policy_row.policy_id;\n"
    "            IF NOT FOUND OR current_policy_fingerprint IS NULL THEN\n"
    "                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BINANCE_POLICY_DENIED';\n"
    "            END IF;"
)


def _rewrite_source(source: str, *, forward: bool) -> str:
    """Chỉ đổi đúng hai phép băm policy trong current-rights scorer gateway."""
    pairs = (
        (_LEGACY_PER_ITEM, _LICENSED_PER_ITEM),
        (_LEGACY_CURRENT_POLICY, _LICENSED_CURRENT_POLICY),
    )
    rewritten = source
    for legacy, licensed in pairs:
        expected, replacement = (legacy, licensed) if forward else (licensed, legacy)
        if rewritten.count(expected) != 1:
            direction = "UPGRADE" if forward else "DOWNGRADE"
            raise RuntimeError(f"WP06_SCORER_LICENSE_FINGERPRINT_{direction}_PREIMAGE_UNEXPECTED")
        rewritten = rewritten.replace(expected, replacement, 1)
    return rewritten


def _rewrite(*, forward: bool) -> None:
    """Giữ owner/ACL/thuộc tính gateway bằng CREATE OR REPLACE từ định nghĩa hiện hành."""
    connection = op.get_bind()
    statement = text(
        "SELECT pg_catalog.pg_get_functiondef(oid), prosrc "
        "FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"
    )
    definition, source = connection.execute(statement, {"signature": SIGNATURE}).one()
    if not isinstance(definition, str) or not isinstance(source, str):
        raise RuntimeError("WP06_SCORER_LICENSE_FINGERPRINT_SOURCE_INVALID")
    corrected_source = _rewrite_source(source, forward=forward)
    if definition.count(source) != 1:
        raise RuntimeError("WP06_SCORER_LICENSE_FINGERPRINT_DEFINITION_UNEXPECTED")
    corrected_definition = definition.replace(source, corrected_source, 1)
    connection.exec_driver_sql(corrected_definition)

    installed_definition, installed_source = connection.execute(
        statement, {"signature": SIGNATURE}
    ).one()
    if not isinstance(installed_definition, str) or not isinstance(installed_source, str):
        raise RuntimeError("WP06_SCORER_LICENSE_FINGERPRINT_INSTALLED_SOURCE_INVALID")
    if (
        _rewrite_source(installed_source, forward=not forward) != source
        or hashlib.sha256(installed_source.encode("utf-8")).digest()
        != hashlib.sha256(corrected_source.encode("utf-8")).digest()
    ):
        raise RuntimeError("WP06_SCORER_LICENSE_FINGERPRINT_POSTIMAGE_UNEXPECTED")


def upgrade() -> None:
    """Đọc license-aware hash khi xác minh receipt và fingerprint current policy."""
    _rewrite(forward=True)


def downgrade() -> None:
    """Khôi phục đúng hai helper lịch sử mà không đổi quyền hoặc dữ liệu."""
    _rewrite(forward=False)
