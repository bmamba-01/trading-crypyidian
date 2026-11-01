"""MB-07b: mã Telegram dùng một lần, nonce callback và lịch sử rebind."""

import sqlalchemy as sa
from alembic import op

revision = "e2c4a6f8b1d3"
down_revision = "d1f3a5b7c9e2"
branch_labels = None
depends_on = None

ADMIN = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.drop_constraint("uq_telegram_binding_user", "telegram_binding", type_="unique")
    op.create_index(
        "uq_telegram_binding_active_user",
        "telegram_binding",
        ["workspace_id", "telegram_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_telegram_binding_active_membership",
        "telegram_binding",
        ["workspace_id", "membership_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "telegram_link_request",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(token_digest) = 64", name="telegram_link_digest_sha256"),
        sa.CheckConstraint("expires_at > created_at", name="telegram_link_expiry_valid"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_telegram_link_request_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_telegram_link_request_created_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "token_digest", name="uq_telegram_link_digest"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_telegram_link_request_workspace_id"),
    )
    op.create_index(
        "ix_telegram_link_request_expiry", "telegram_link_request", ["workspace_id", "expires_at"]
    )
    op.create_table(
        "telegram_binding_challenge",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("link_request_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.String(64), nullable=False),
        sa.Column("callback_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(callback_digest) = 64", name="telegram_callback_digest_sha256"),
        sa.CheckConstraint("expires_at > created_at", name="telegram_callback_expiry_valid"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "link_request_id"],
            ["telegram_link_request.workspace_id", "telegram_link_request.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "callback_digest", name="uq_telegram_callback_digest"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_telegram_challenge_workspace_id"),
    )
    op.create_index(
        "ix_telegram_binding_challenge_expiry",
        "telegram_binding_challenge",
        ["workspace_id", "expires_at"],
    )

    for table in ("telegram_link_request", "telegram_binding_challenge"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" TO {ADMIN} '
            f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
        )
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO {ADMIN}')

    op.execute(f"GRANT UPDATE (expires_at, consumed_at) ON TABLE telegram_link_request TO {ADMIN}")
    op.execute(f"GRANT UPDATE (consumed_at) ON TABLE telegram_binding_challenge TO {ADMIN}")

    op.execute(f"GRANT INSERT ON TABLE telegram_binding TO {ADMIN}")
    op.execute(f"GRANT UPDATE (revoked_at) ON TABLE telegram_binding TO {ADMIN}")
    op.execute(
        f"CREATE POLICY telegram_binding_admin_insert ON telegram_binding FOR INSERT TO {ADMIN} "
        f"WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY telegram_binding_admin_revoke ON telegram_binding FOR UPDATE TO {ADMIN} "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM telegram_binding
                GROUP BY workspace_id, telegram_user_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'MB07B_TELEGRAM_REBIND_HISTORY_PREVENTS_DOWNGRADE';
            END IF;
        END $$"""
    )
    op.execute("DROP POLICY IF EXISTS telegram_binding_admin_revoke ON telegram_binding")
    op.execute("DROP POLICY IF EXISTS telegram_binding_admin_insert ON telegram_binding")
    op.execute("REVOKE INSERT ON TABLE telegram_binding FROM crypidian_admin_web")
    op.execute("REVOKE UPDATE (revoked_at) ON TABLE telegram_binding FROM crypidian_admin_web")
    op.execute(
        "REVOKE UPDATE (expires_at, consumed_at) ON TABLE telegram_link_request "
        "FROM crypidian_admin_web"
    )
    op.execute(
        "REVOKE UPDATE (consumed_at) ON TABLE telegram_binding_challenge FROM crypidian_admin_web"
    )
    for table in ("telegram_binding_challenge", "telegram_link_request"):
        op.execute(f'DROP POLICY IF EXISTS workspace_isolation ON "{table}"')
        op.execute(f'REVOKE ALL ON TABLE "{table}" FROM {ADMIN}')
        op.drop_table(table)
    op.drop_index("uq_telegram_binding_active_membership", table_name="telegram_binding")
    op.drop_index("uq_telegram_binding_active_user", table_name="telegram_binding")
    op.create_unique_constraint(
        "uq_telegram_binding_user", "telegram_binding", ["workspace_id", "telegram_user_id"]
    )
