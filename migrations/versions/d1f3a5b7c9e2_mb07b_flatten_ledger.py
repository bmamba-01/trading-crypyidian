"""MB-07b: snapshot/operation ledger bền vững cho flatten-all PAPER.

Preview chỉ được admin-web đọc/ghi theo membership hiện tại. Runtime nhận quyền đọc queue và cập
nhật trạng thái operation/item theo workspace; không có quyền đổi preview hoặc xóa lịch sử.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d1f3a5b7c9e2"
down_revision = "c8e4a2f6d190"
branch_labels = None
depends_on = None

ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_MEMBERSHIP = "membership_id = NULLIF(current_setting('app.membership_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "flatten_preview",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_payload", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(preview_hash) = 64", name="flatten_preview_hash_sha256"),
        sa.CheckConstraint("length(snapshot_hash) = 64", name="flatten_preview_snapshot_sha256"),
        sa.CheckConstraint("expires_at > created_at", name="flatten_preview_expiry_valid"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_flatten_preview_workspace_id"),
    )
    op.create_index("ix_flatten_preview_expiry", "flatten_preview", ["workspace_id", "expires_at"])

    op.create_table(
        "flatten_operation",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="QUEUED", nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(idempotency_key) BETWEEN 1 AND 128", name="flatten_key_length"),
        sa.CheckConstraint("length(request_hash) = 64", name="flatten_request_hash_sha256"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'PARTIAL', 'BLOCKED', 'COMPLETED')",
            name="flatten_operation_status_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "preview_id"],
            ["flatten_preview.workspace_id", "flatten_preview.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_flatten_operation_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "membership_id", "idempotency_key", name="uq_flatten_operation_key"
        ),
    )
    op.create_index(
        "ix_flatten_operation_pending",
        "flatten_operation",
        ["workspace_id", "status", "created_at"],
    )

    op.create_table(
        "flatten_operation_item",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(240), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("target_status", sa.String(24), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("mark_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("mark_record_id", sa.Uuid(), nullable=True),
        sa.Column("mark_revision", sa.Integer(), nullable=True),
        sa.Column("mark_checksum", sa.String(64), nullable=True),
        sa.Column("mark_close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "result", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('POSITION', 'OPEN_ORDER')", name="flatten_target_type_valid"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'BLOCKED', 'COMPLETED')",
            name="flatten_item_status_valid",
        ),
        sa.CheckConstraint("quantity > 0", name="flatten_item_quantity_positive"),
        sa.CheckConstraint(
            "mark_price IS NULL OR mark_price > 0", name="flatten_item_mark_positive"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["flatten_operation.workspace_id", "flatten_operation.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "operation_id", "id", name="uq_flatten_operation_item_scope_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "operation_id",
            "bot_id",
            "target_type",
            "target_id",
            name="uq_flatten_operation_target",
        ),
    )
    op.create_index(
        "ix_flatten_item_worker",
        "flatten_operation_item",
        ["workspace_id", "bot_id", "status", "updated_at"],
    )

    for table in ("flatten_preview", "flatten_operation", "flatten_operation_item"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        f"CREATE POLICY flatten_preview_read ON flatten_preview FOR SELECT TO {ADMIN} "
        f"USING (({_WORKSPACE}) AND ({_MEMBERSHIP}))"
    )
    op.execute(
        f"CREATE POLICY flatten_preview_insert ON flatten_preview FOR INSERT TO {ADMIN} "
        f"WITH CHECK (({_WORKSPACE}) AND ({_MEMBERSHIP}))"
    )
    op.execute(
        f"CREATE POLICY flatten_preview_update ON flatten_preview FOR UPDATE TO {ADMIN} "
        f"USING (({_WORKSPACE}) AND ({_MEMBERSHIP})) "
        f"WITH CHECK (({_WORKSPACE}) AND ({_MEMBERSHIP}))"
    )
    for table in ("flatten_operation", "flatten_operation_item"):
        op.execute(
            f"CREATE POLICY {table}_admin_scope ON {table} FOR ALL TO {ADMIN} "
            f"USING (({_WORKSPACE}) AND ({_MEMBERSHIP})) "
            f"WITH CHECK (({_WORKSPACE}) AND ({_MEMBERSHIP}))"
        )
        op.execute(
            f"CREATE POLICY {table}_runtime_scope ON {table} FOR SELECT TO {RUNTIME} "
            f"USING ({_WORKSPACE})"
        )
    op.execute(
        f"CREATE POLICY flatten_operation_runtime_update ON flatten_operation "
        f"FOR UPDATE TO {RUNTIME} "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY flatten_operation_item_runtime_update ON flatten_operation_item "
        f"FOR UPDATE TO {RUNTIME} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )

    op.execute(
        "REVOKE ALL ON TABLE flatten_preview, flatten_operation, flatten_operation_item "
        "FROM PUBLIC, crypidian_app"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE flatten_preview TO {ADMIN}")
    op.execute(f"GRANT UPDATE (consumed_at) ON TABLE flatten_preview TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE flatten_operation TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE flatten_operation_item TO {ADMIN}")
    op.execute(f"GRANT SELECT ON TABLE flatten_operation, flatten_operation_item TO {RUNTIME}")
    op.execute(f"GRANT UPDATE (status, updated_at) ON TABLE flatten_operation TO {RUNTIME}")
    op.execute(
        f"GRANT UPDATE (status, attempt_count, result, last_error_code, updated_at) "
        f"ON TABLE flatten_operation_item TO {RUNTIME}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE ALL ON TABLE flatten_preview, flatten_operation, flatten_operation_item "
        f"FROM {ADMIN}, {RUNTIME}"
    )
    op.drop_index("ix_flatten_item_worker", table_name="flatten_operation_item")
    op.drop_table("flatten_operation_item")
    op.drop_index("ix_flatten_operation_pending", table_name="flatten_operation")
    op.drop_table("flatten_operation")
    op.drop_index("ix_flatten_preview_expiry", table_name="flatten_preview")
    op.drop_table("flatten_preview")
