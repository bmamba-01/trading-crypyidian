"""Thêm transactional outbox, consumer receipt và audit append-only.

Revision ID: a63d41e7b205
Revises: 9f3c2d8a6e41
Create Date: 2026-10-22 11:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a63d41e7b205"
down_revision: str | None = "9f3c2d8a6e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_event",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("schema_version >= 1", name="outbox_schema_version_positive"),
        sa.CheckConstraint("attempts >= 0", name="outbox_attempts_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'DEAD')",
            name="outbox_status_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_event")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_outbox_event_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "event_type",
            "idempotency_key",
            name="uq_outbox_event_idempotency",
        ),
    )
    op.create_index(
        "ix_outbox_event_ready",
        "outbox_event",
        ["workspace_id", "status", "available_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "consumer_receipt",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_name", sa.String(length=120), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "outbox_event_id"],
            ["outbox_event.workspace_id", "outbox_event.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumer_receipt")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_consumer_receipt_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "outbox_event_id", "consumer_name", name="uq_consumer_receipt_event"
        ),
    )
    op.create_table(
        "audit_event",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_hash", sa.String(length=64), nullable=True),
        sa.Column("after_hash", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_audit_event_workspace_id"),
    )
    op.create_index(
        "ix_audit_event_workspace_time",
        "audit_event",
        ["workspace_id", "occurred_at"],
        unique=False,
    )

    for table_name in ("outbox_event", "consumer_receipt", "audit_event"):
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table_name}" '
            f"USING ({policy}) WITH CHECK ({policy})"
        )

    op.execute("REVOKE ALL PRIVILEGES ON TABLE outbox_event FROM crypidian_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE outbox_event TO crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE consumer_receipt FROM crypidian_app")
    op.execute("GRANT SELECT, INSERT ON TABLE consumer_receipt TO crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE audit_event FROM crypidian_app")
    for role_name in (
        "crypidian_admin_web",
        "crypidian_trading_runtime",
        "crypidian_execution_gateway",
        "crypidian_ingestion_worker",
        "crypidian_trainer_worker",
    ):
        op.execute(f"GRANT INSERT ON TABLE audit_event TO {role_name}")
    op.execute("GRANT SELECT ON TABLE audit_event TO crypidian_admin_web")
    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_event_append_only
        BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_event_append_only ON audit_event")
    op.execute("DROP FUNCTION reject_audit_event_mutation()")
    for table_name in ("audit_event", "consumer_receipt", "outbox_event"):
        op.execute(f'DROP POLICY workspace_isolation ON "{table_name}"')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_audit_event_workspace_time", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_table("consumer_receipt")
    op.drop_index("ix_outbox_event_ready", table_name="outbox_event")
    op.drop_table("outbox_event")
