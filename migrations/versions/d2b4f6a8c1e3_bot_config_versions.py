"""Thêm phiên bản cấu hình bot bất biến và sổ Idempotency-Key cho Admin API.

Revision ID: d2b4f6a8c1e3
Revises: c7a90d3f8e21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d2b4f6a8c1e3"
down_revision = "c7a90d3f8e21"
branch_labels = None
depends_on = None

_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_MEMBER = "NULLIF(current_setting('app.membership_id', true), '')::uuid"
_BOT = "NULLIF(current_setting('app.bot_id', true), '')::uuid"
_SHA256 = "length({column}) = 64 AND {column} = lower({column}) AND {column} !~ '[^0-9a-f]'"


def _member_policy(table: str) -> str:
    """Cùng mẫu MB-01: membership ACTIVE với role workspace-wide hoặc grant bot còn hiệu lực."""
    return f"""({_WORKSPACE}) AND EXISTS (SELECT 1 FROM user_membership m
        WHERE m.workspace_id = {table}.workspace_id AND m.id = {_MEMBER} AND m.status = 'ACTIVE'
        AND (EXISTS (SELECT 1 FROM role_binding r WHERE r.workspace_id = m.workspace_id
                AND r.membership_id = m.id AND r.role IN ('owner','strategy_admin','risk_admin'))
            OR EXISTS (SELECT 1 FROM bot_grant g WHERE g.workspace_id = m.workspace_id
                AND g.membership_id = m.id AND g.bot_id = {table}.bot_id
                AND g.revoked_at IS NULL)))"""


def _membership_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", column],
        ["user_membership.workspace_id", "user_membership.id"],
        name=f"fk_{table}_{column}",
    )


def upgrade() -> None:
    op.create_table(
        "bot_config_version",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        sa.Column("source_version_id", sa.Uuid(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("overlay_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("overlay_hash", sa.String(64), nullable=False),
        sa.Column("base_settings_version_id", sa.Uuid(), nullable=False),
        sa.Column("base_settings_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_config_version")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_bot_config_version_workspace_id"),
        sa.UniqueConstraint("workspace_id", "bot_id", "id", name="uq_bot_config_version_bot_id"),
        sa.UniqueConstraint(
            "workspace_id", "bot_id", "version", name="uq_bot_config_version_number"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_bot_config_version_bot_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "parent_version_id"],
            [
                "bot_config_version.workspace_id",
                "bot_config_version.bot_id",
                "bot_config_version.id",
            ],
            name="fk_bot_config_version_parent",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "source_version_id"],
            [
                "bot_config_version.workspace_id",
                "bot_config_version.bot_id",
                "bot_config_version.id",
            ],
            name="fk_bot_config_version_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "base_settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
            name="fk_bot_config_version_base_settings",
        ),
        _membership_fk("bot_config_version", "created_by"),
        _membership_fk("bot_config_version", "updated_by"),
        _membership_fk("bot_config_version", "approved_by"),
        sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="status_valid"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.CheckConstraint("schema_version >= 1", name="schema_positive"),
        sa.CheckConstraint(_SHA256.format(column="overlay_hash"), name="overlay_hash_sha256"),
        sa.CheckConstraint(_SHA256.format(column="base_settings_hash"), name="base_hash_sha256"),
        sa.CheckConstraint(
            "(status = 'DRAFT' AND approved_by IS NULL AND published_at IS NULL "
            "AND effective_at IS NULL) OR (status = 'PUBLISHED' AND approved_by IS NOT NULL "
            "AND published_at IS NOT NULL AND effective_at IS NOT NULL)",
            name="publication_consistent",
        ),
    )
    # FK phía con không có index tự động; RLS và resolver lọc workspace+bot ở mọi truy vấn.
    op.create_index(
        "ix_bot_config_version_bot_scope",
        "bot_config_version",
        ["workspace_id", "bot_id", "account_id"],
    )
    op.create_index(
        "ix_bot_config_version_effective",
        "bot_config_version",
        ["workspace_id", "bot_id", "status", "effective_at"],
    )
    op.create_table(
        "bot_config_request",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("bot_config_version_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_config_request")),
        sa.UniqueConstraint(
            "workspace_id",
            "bot_id",
            "operation",
            "idempotency_key",
            name="uq_bot_config_request_key",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_bot_config_request_bot_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_config_version_id"],
            [
                "bot_config_version.workspace_id",
                "bot_config_version.bot_id",
                "bot_config_version.id",
            ],
            name="fk_bot_config_request_version",
        ),
        _membership_fk("bot_config_request", "created_by"),
        sa.CheckConstraint(
            "operation IN ('draft.create', 'draft.update', 'publish', 'rollback')",
            name="operation_valid",
        ),
        sa.CheckConstraint("idempotency_key ~ '^[A-Za-z0-9._:-]{8,120}$'", name="key_format"),
        sa.CheckConstraint(_SHA256.format(column="request_hash"), name="request_hash_sha256"),
    )
    op.create_index(
        "ix_bot_config_request_bot_scope",
        "bot_config_request",
        ["workspace_id", "bot_id", "account_id"],
    )
    _create_guards()
    _create_policies()


def _create_guards() -> None:
    op.execute("""CREATE FUNCTION public.guard_bot_config_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                -- Publish chỉ đi qua chuyển trạng thái DRAFT -> PUBLISHED có kiểm tra.
                IF NEW.status <> 'DRAFT' THEN
                    RAISE EXCEPTION 'MB02_BOT_CONFIG_INSERT_MUST_BE_DRAFT';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'MB02_BOT_CONFIG_VERSION_APPEND_ONLY';
            END IF;
            IF OLD.status = 'PUBLISHED' THEN
                RAISE EXCEPTION 'MB02_BOT_CONFIG_VERSION_IMMUTABLE';
            END IF;
            IF (NEW.id, NEW.workspace_id, NEW.bot_id, NEW.account_id, NEW.version,
                NEW.parent_version_id, NEW.source_version_id, NEW.created_by, NEW.created_at)
                IS DISTINCT FROM
                (OLD.id, OLD.workspace_id, OLD.bot_id, OLD.account_id, OLD.version,
                OLD.parent_version_id, OLD.source_version_id, OLD.created_by, OLD.created_at) THEN
                RAISE EXCEPTION 'MB02_BOT_CONFIG_IDENTITY_IMMUTABLE';
            END IF;
            IF NEW.status = 'PUBLISHED' AND (NEW.schema_version, NEW.overlay_json,
                NEW.overlay_hash, NEW.revision) IS DISTINCT FROM (OLD.schema_version,
                OLD.overlay_json, OLD.overlay_hash, OLD.revision) THEN
                RAISE EXCEPTION 'MB02_PUBLISH_CANNOT_CHANGE_DRAFT';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER bot_config_version_history_guard "
        "BEFORE INSERT OR UPDATE OR DELETE ON bot_config_version "
        "FOR EACH ROW EXECUTE FUNCTION public.guard_bot_config_version()"
    )
    op.execute("""CREATE FUNCTION public.guard_bot_config_request() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'MB02_BOT_CONFIG_REQUEST_APPEND_ONLY';
        END $$""")
    op.execute(
        "CREATE TRIGGER bot_config_request_append_only BEFORE UPDATE OR DELETE "
        "ON bot_config_request FOR EACH ROW EXECUTE FUNCTION public.guard_bot_config_request()"
    )


def _create_policies() -> None:
    for table in ("bot_config_version", "bot_config_request"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        admin = _member_policy(table)
        op.execute(
            f"CREATE POLICY bot_member_isolation ON {table} TO crypidian_admin_web "
            f"USING ({admin}) WITH CHECK ({admin})"
        )
    # Runtime chỉ đọc đúng bot của transaction và chỉ hàng đã publish: draft không có hiệu lực.
    runtime = f"({_WORKSPACE}) AND bot_id = {_BOT} AND status = 'PUBLISHED'"
    op.execute(
        "CREATE POLICY bot_config_runtime_read ON bot_config_version FOR SELECT "
        f"TO crypidian_trading_runtime USING ({runtime})"
    )
    op.execute("REVOKE ALL ON bot_config_version, bot_config_request FROM PUBLIC, crypidian_app")
    op.execute("GRANT SELECT ON bot_config_version TO crypidian_trading_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON bot_config_version TO crypidian_admin_web")
    op.execute("GRANT SELECT, INSERT ON bot_config_request TO crypidian_admin_web")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM bot_config_version) THEN
            RAISE EXCEPTION 'MB02_DOWNGRADE_REQUIRES_EXPORT_OF_BOT_CONFIG';
        END IF;
    END $$""")
    op.execute("DROP TRIGGER bot_config_request_append_only ON bot_config_request")
    op.execute("DROP FUNCTION public.guard_bot_config_request()")
    op.execute("DROP TRIGGER bot_config_version_history_guard ON bot_config_version")
    op.execute("DROP FUNCTION public.guard_bot_config_version()")
    op.drop_index("ix_bot_config_request_bot_scope", table_name="bot_config_request")
    op.drop_table("bot_config_request")
    op.drop_index("ix_bot_config_version_effective", table_name="bot_config_version")
    op.drop_index("ix_bot_config_version_bot_scope", table_name="bot_config_version")
    op.drop_table("bot_config_version")
