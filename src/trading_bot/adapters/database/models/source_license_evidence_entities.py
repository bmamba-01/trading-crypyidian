"""Ledger append-only chỉ giữ metadata bằng chứng license, không giữ tài liệu gốc."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class SourceLicenseEvidenceRecord(UuidPrimaryKey, CreatedAt, Base):
    """Digest và scope do pending policy cung cấp, scoped theo workspace/source."""

    __tablename__ = "source_license_evidence"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_id", "id", name="uq_source_license_evidence_source"
        ),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_source_license_evidence_key"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "policy_id"],
            [
                "source_policy_revision.workspace_id",
                "source_policy_revision.source_id",
                "source_policy_revision.policy_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="source_license_digest_valid"),
        CheckConstraint(
            "byte_count BETWEEN 1 AND 10485760", name="source_license_byte_count_valid"
        ),
        CheckConstraint(
            "media_type IN ('application/pdf','text/plain','text/html',"
            "'application/vnd.openxmlformats-officedocument.wordprocessingml.document')",
            name="source_license_media_type_valid",
        ),
        CheckConstraint(
            "scope_version = 'wp06-license-scope-v1'", name="source_license_version_valid"
        ),
        CheckConstraint(
            "canonical_scope::jsonb = license_scope AND scope_hash = "
            "encode(sha256(convert_to(canonical_scope, 'UTF8')), 'hex')",
            name="source_license_scope_digest_valid",
        ),
        CheckConstraint(
            "public.wp06_license_scope_valid(license_scope) AND "
            "canonical_scope = public.wp06_license_canonical_json(license_scope)",
            name="source_license_scope_canonical_valid",
        ),
        CheckConstraint("length(btrim(evidence_ref)) > 0", name="source_license_ref_present"),
        CheckConstraint("length(btrim(actor_id)) > 0", name="source_license_actor_present"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(96), nullable=False)
    scope_version: Mapped[str] = mapped_column(String(32), nullable=False)
    license_scope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    canonical_scope: Mapped[str] = mapped_column(Text, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
