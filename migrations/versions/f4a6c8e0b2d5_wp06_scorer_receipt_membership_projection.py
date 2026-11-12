"""Bổ sung TEST membership digest vào projection receipt của deployment verifier."""

import hashlib
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "f4a6c8e0b2d5"
down_revision: str = "c3f5a7b9d1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid)"
PREVIOUS_SOURCE_HASH = "4cef8c81e58bcd8daf127d7216f0dc2be633dffcd3f3518f9cd1f393fe477ea6"
CORRECTED_SOURCE_HASH = "76b032e258af08f6871181f10c83f0100a30507148af09ee64df9ba4aebba53b"
PREVIOUS_PROJECTION = (
    "receipt.current_rights_receipt_hash, receipt.receipt_hash\n              INTO receipt_row"
)
CORRECTED_PROJECTION = (
    "receipt.current_rights_receipt_hash, receipt.receipt_hash,\n"
    "                   receipt.test_membership_digest\n"
    "              INTO receipt_row"
)


def _rewrite_definition(definition: str, source: str, *, forward: bool) -> str:
    """Chỉ đổi projection trên toàn bộ function body đúng preimage đã kiểm chứng."""
    expected_hash, next_hash = (
        (PREVIOUS_SOURCE_HASH, CORRECTED_SOURCE_HASH)
        if forward
        else (CORRECTED_SOURCE_HASH, PREVIOUS_SOURCE_HASH)
    )
    previous, replacement = (
        (PREVIOUS_PROJECTION, CORRECTED_PROJECTION)
        if forward
        else (CORRECTED_PROJECTION, PREVIOUS_PROJECTION)
    )
    direction = "UPGRADE" if forward else "DOWNGRADE"
    marker = f"WP06_SCORER_RECEIPT_PROJECTION_{direction}"
    if (
        hashlib.sha256(source.encode("utf-8")).hexdigest() != expected_hash
        or source.count(previous) != 1
        or definition.count(source) != 1
    ):
        raise RuntimeError(f"{marker}_PREIMAGE_UNEXPECTED")
    corrected_source = source.replace(previous, replacement, 1)
    if hashlib.sha256(corrected_source.encode("utf-8")).hexdigest() != next_hash:
        raise RuntimeError(f"{marker}_POSTIMAGE_UNEXPECTED")
    return definition.replace(source, corrected_source, 1)


def _rewrite(*, forward: bool) -> None:
    """Giữ nguyên chữ ký, owner, ACL và thuộc tính bảo mật khi CREATE OR REPLACE."""
    connection = op.get_bind()
    definition, source = connection.execute(
        text(
            "SELECT pg_catalog.pg_get_functiondef(oid), prosrc FROM pg_catalog.pg_proc "
            "WHERE oid=CAST(:signature AS regprocedure)"
        ),
        {"signature": SIGNATURE},
    ).one()
    if not isinstance(definition, str) or not isinstance(source, str):
        raise RuntimeError("WP06_SCORER_RECEIPT_PROJECTION_SOURCE_INVALID")
    connection.exec_driver_sql(_rewrite_definition(definition, source, forward=forward))
    installed_source: object = connection.execute(
        text("SELECT prosrc FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"),
        {"signature": SIGNATURE},
    ).scalar_one()
    expected_hash = CORRECTED_SOURCE_HASH if forward else PREVIOUS_SOURCE_HASH
    if (
        not isinstance(installed_source, str)
        or hashlib.sha256(installed_source.encode("utf-8")).hexdigest() != expected_hash
    ):
        raise RuntimeError("WP06_SCORER_RECEIPT_PROJECTION_INSTALLED_POSTIMAGE_UNEXPECTED")


def upgrade() -> None:
    """Sửa field record bị thiếu; không thay quyền hoặc điều kiện eligible."""
    _rewrite(forward=True)


def downgrade() -> None:
    """Khôi phục đúng body lịch sử, từ chối nếu function có drift ngoài dự kiến."""
    _rewrite(forward=False)
