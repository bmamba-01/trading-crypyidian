"""Thêm identity bot, account binding, grant và scope cấp hàng.

Revision ID: c7a90d3f8e21
Revises: b2e7a9d4c631
"""

import sqlalchemy as sa
from alembic import op

revision = "c7a90d3f8e21"
down_revision = "b2e7a9d4c631"
branch_labels = None
depends_on = None

_ACCOUNT_COLUMNS = {
    "paper_account_seed": "trading_account_id",
    "config_version": "trading_account_id",
    "approval_request": "trading_account_id",
    "trade_plan": "account_id",
    "risk_decision": "account_id",
    "risk_reservation": "account_id",
    "order_intent": "account_id",
    "exchange_order": "account_id",
    "fill": "account_id",
    "position_event": "account_id",
    "trade": "account_id",
    "trade_cost": "account_id",
    "paper_execution_checkpoint": "account_id",
}
_TRIGGERS = {
    "paper_account_seed": ("paper_account_seed_immutable",),
    "trade_plan": ("trade_plan_immutable",),
    "risk_decision": ("risk_decision_immutable",),
    "risk_reservation": ("risk_reservation_transition_guard", "risk_reservation_expiry_immutable"),
    "order_intent": ("order_intent_transition_guard",),
    "exchange_order": ("exchange_order_transition_guard",),
    "fill": ("fill_append_only",),
    "position_event": ("position_event_append_only",),
    "trade": ("trade_close_guard",),
    "trade_cost": ("trade_cost_append_only",),
    "paper_execution_checkpoint": ("paper_execution_checkpoint_immutable",),
}
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_MEMBER = "NULLIF(current_setting('app.membership_id', true), '')::uuid"
_BOT = "NULLIF(current_setting('app.bot_id', true), '')::uuid"
# Bảng cho phép hàng workspace-only: bot_id/trading_account_id cùng NULL (ruling 8).
_WORKSPACE_SCOPED_ROWS = frozenset({"approval_request"})


def _member_access(bot_column: str) -> str:
    return f"""EXISTS (SELECT 1 FROM user_membership m WHERE m.workspace_id = workspace_id
        AND m.id = {_MEMBER} AND m.status = 'ACTIVE' AND (
            EXISTS (SELECT 1 FROM role_binding r WHERE r.workspace_id = m.workspace_id
                AND r.membership_id = m.id AND r.role IN ('owner','strategy_admin','risk_admin'))
            OR EXISTS (SELECT 1 FROM bot_grant g WHERE g.workspace_id = m.workspace_id
                AND g.membership_id = m.id AND g.bot_id = {bot_column}
                AND g.revoked_at IS NULL)))"""


def _member_policy(table: str, bot_column: str = "bot_id") -> str:
    template = _member_access(f"{table}.{bot_column}")
    unqualified = "m.workspace_id = workspace_id"
    # Thay đúng một lần; nếu template đổi mà không thay được, policy có thể thành luôn đúng.
    if template.count(unqualified) != 1:
        raise RuntimeError("MB01_POLICY_TEMPLATE_CHANGED")
    member = template.replace(unqualified, f"m.workspace_id = {table}.workspace_id")
    return f"({_WORKSPACE}) AND ({member})"


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM trading_account WHERE mode <> 'PAPER'
            OR status NOT IN ('ENABLED','DISABLED')) THEN
            RAISE EXCEPTION 'MB01_UNSUPPORTED_ACCOUNT_MODE_OR_STATUS';
        END IF;
    END $$""")
    op.create_table(
        "bot_instance",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trading_account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_bot_instance_workspace_id"),
        sa.UniqueConstraint("workspace_id", "trading_account_id", name="uq_bot_instance_account"),
        sa.UniqueConstraint(
            "workspace_id", "id", "trading_account_id", name="uq_bot_instance_scope"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name="fk_bot_instance_account",
        ),
        sa.CheckConstraint("status IN ('ENABLED', 'DISABLED')", name="bot_instance_status_valid"),
        sa.CheckConstraint("length(trim(name)) > 0", name="bot_instance_name_nonempty"),
    )
    op.create_table(
        "bot_grant",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("permission", sa.String(40), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            name="fk_bot_grant_bot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_grant_member",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "granted_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_grant_grantor",
        ),
        sa.CheckConstraint(
            "permission IN ('bot:read', 'bot:config:write', 'bot:config:publish', "
            "'bot:runtime:pause', 'bot:runtime:resume', 'bot:report:read')",
            name="bot_grant_permission_valid",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="bot_grant_revocation_valid"
        ),
    )
    op.create_index(
        "uq_bot_grant_active",
        "bot_grant",
        ["workspace_id", "bot_id", "membership_id", "permission"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.execute(
        "INSERT INTO bot_instance (id, workspace_id, trading_account_id, name, status) "
        "SELECT gen_random_uuid(), workspace_id, id, name, status FROM trading_account"
    )
    for table, account in _ACCOUNT_COLUMNS.items():
        op.add_column(table, sa.Column("bot_id", sa.Uuid(), nullable=True))
        for trigger in _TRIGGERS.get(table, ()):
            op.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
        op.execute(
            f'UPDATE "{table}" t SET bot_id = b.id FROM bot_instance b '
            f"WHERE b.workspace_id = t.workspace_id AND b.trading_account_id = t.{account}"
        )
        for trigger in _TRIGGERS.get(table, ()):
            op.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER "{trigger}"')
        if table != "approval_request":
            op.alter_column(
                table,
                "bot_id",
                nullable=False,
                server_default=sa.text("NULLIF(current_setting('app.bot_id', true), '')::uuid"),
            )
        op.create_foreign_key(
            f"fk_{table}_bot_account",
            table,
            "bot_instance",
            ["workspace_id", "bot_id", account],
            ["workspace_id", "id", "trading_account_id"],
        )
        # FK phía con không có index tự động; RLS lọc workspace+bot trên mọi truy vấn.
        op.create_index(f"ix_{table}_bot_scope", table, ["workspace_id", "bot_id", account])
    op.create_check_constraint(
        "approval_request_bot_scope_consistent",
        "approval_request",
        "(bot_id IS NULL) = (trading_account_id IS NULL)",
    )
    op.create_unique_constraint(
        "uq_risk_reservation_bot_account_id",
        "risk_reservation",
        ["workspace_id", "bot_id", "account_id", "id"],
    )
    op.create_foreign_key(
        "fk_order_intent_bot_reservation",
        "order_intent",
        "risk_reservation",
        ["workspace_id", "bot_id", "account_id", "risk_reservation_id"],
        ["workspace_id", "bot_id", "account_id", "id"],
    )
    _create_policies()
    _create_account_binding()


def _create_policies() -> None:
    for table in _ACCOUNT_COLUMNS:
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        runtime = f"({_WORKSPACE}) AND bot_id = {_BOT}"
        admin = _member_policy(table)
        # Hai policy có role đích riêng; không còn permissive workspace-only policy.
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" TO crypidian_trading_runtime '
            f"USING ({runtime}) WITH CHECK ({runtime})"
        )
        op.execute(
            f'CREATE POLICY bot_member_isolation ON "{table}" TO crypidian_admin_web '
            f"USING ({admin}) WITH CHECK ({admin})"
        )
    for table in _WORKSPACE_SCOPED_ROWS:
        # Hàng workspace-only (bot NULL) chỉ ĐỌC được: runtime cùng workspace, admin có
        # membership ACTIVE. Ghi vẫn bot-strict (role workspace-wide qua bot_member_isolation),
        # nên không bot nào sửa hoặc chuyển hàng của mình thành workspace-only.
        active_member = (
            f"EXISTS (SELECT 1 FROM user_membership m WHERE m.workspace_id = {table}.workspace_id "
            f"AND m.id = {_MEMBER} AND m.status = 'ACTIVE')"
        )
        op.execute(
            f'CREATE POLICY workspace_row_read ON "{table}" FOR SELECT '
            f"TO crypidian_trading_runtime USING (({_WORKSPACE}) AND bot_id IS NULL)"
        )
        op.execute(
            f'CREATE POLICY workspace_row_member_read ON "{table}" FOR SELECT '
            f"TO crypidian_admin_web USING (({_WORKSPACE}) AND bot_id IS NULL AND {active_member})"
        )
    for table in ("bot_instance", "bot_grant"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    # Runtime được khám phá identity workspace; dữ liệu giao dịch vẫn yêu cầu bot context.
    admin_bot = _member_policy("bot_instance", "id")
    op.execute(
        "CREATE POLICY workspace_isolation ON bot_instance TO crypidian_trading_runtime "
        f"USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY bot_member_isolation ON bot_instance TO crypidian_admin_web "
        f"USING ({admin_bot}) WITH CHECK ({admin_bot})"
    )
    owner = (
        "EXISTS (SELECT 1 FROM role_binding r JOIN user_membership m ON "
        "(m.workspace_id, m.id) = (r.workspace_id, r.membership_id) "
        "WHERE r.workspace_id = bot_grant.workspace_id "
        f"AND r.membership_id = {_MEMBER} AND m.status = 'ACTIVE' AND r.role = 'owner')"
    )
    read = (
        f"({_WORKSPACE}) AND current_user = 'crypidian_admin_web' "
        f"AND (membership_id = {_MEMBER} OR {owner})"
    )
    write = f"({_WORKSPACE}) AND current_user = 'crypidian_admin_web' AND {owner}"
    op.execute(f"CREATE POLICY bot_grant_read ON bot_grant FOR SELECT USING ({read})")
    op.execute(
        "CREATE POLICY bot_grant_insert ON bot_grant FOR INSERT "
        f"WITH CHECK (({write}) AND granted_by = {_MEMBER})"
    )
    op.execute(
        f"CREATE POLICY bot_grant_revoke ON bot_grant FOR UPDATE "
        f"USING ({write}) WITH CHECK ({write})"
    )
    op.execute("REVOKE ALL ON bot_instance, bot_grant FROM PUBLIC, crypidian_app")
    op.execute("GRANT SELECT ON bot_instance TO crypidian_admin_web")
    op.execute("GRANT SELECT ON bot_instance TO crypidian_trading_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON bot_grant TO crypidian_admin_web")


def _create_account_binding() -> None:
    # Role NOLOGIN chỉ được INSERT identity qua trigger, không có quyền ledger/secret.
    # Role là đối tượng cấp cluster: database khác cùng server có thể đã tạo trước.
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crypidian_bot_provisioner') THEN
            CREATE ROLE crypidian_bot_provisioner NOLOGIN NOSUPERUSER NOBYPASSRLS;
        ELSIF EXISTS (
            SELECT 1 FROM pg_roles r WHERE r.rolname = 'crypidian_bot_provisioner'
            AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolreplication
                OR r.rolcreaterole OR r.rolcreatedb
                OR EXISTS (SELECT 1 FROM pg_auth_members a WHERE a.roleid = r.oid))
        ) THEN
            -- Policy của provisioner là USING (true); role có login/thành viên sẽ vượt RLS.
            RAISE EXCEPTION 'MB01_PROVISIONER_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO crypidian_bot_provisioner")
    op.execute("GRANT INSERT ON public.bot_instance TO crypidian_bot_provisioner")
    # Đồng bộ name/status từ account; UPDATE có WHERE nên cần SELECT và SELECT policy.
    op.execute(
        "GRANT SELECT (workspace_id, trading_account_id), "
        "UPDATE (name, status, updated_at) ON public.bot_instance TO crypidian_bot_provisioner"
    )
    op.execute(
        "CREATE POLICY provision_account_bot ON bot_instance FOR INSERT "
        "TO crypidian_bot_provisioner WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY provision_account_bot_read ON bot_instance FOR SELECT "
        "TO crypidian_bot_provisioner USING (true)"
    )
    op.execute(
        "CREATE POLICY provision_account_bot_sync ON bot_instance FOR UPDATE "
        "TO crypidian_bot_provisioner USING (true) WITH CHECK (true)"
    )
    op.execute("""CREATE FUNCTION public.provision_account_bot() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.mode <> 'PAPER' OR NEW.status NOT IN ('ENABLED','DISABLED') THEN
                RAISE EXCEPTION 'MB01_UNSUPPORTED_ACCOUNT_MODE_OR_STATUS';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                -- MB-01: bot mirror trạng thái/tên account (ruling 2); MB-05 xem lại lifecycle.
                UPDATE public.bot_instance
                SET name = NEW.name, status = NEW.status, updated_at = pg_catalog.now()
                WHERE workspace_id = NEW.workspace_id AND trading_account_id = NEW.id;
                RETURN NEW;
            END IF;
            INSERT INTO public.bot_instance (id, workspace_id, trading_account_id, name, status)
            VALUES (gen_random_uuid(), NEW.workspace_id, NEW.id, NEW.name, NEW.status);
            RETURN NEW;
        END $$""")
    op.execute("ALTER FUNCTION public.provision_account_bot() OWNER TO crypidian_bot_provisioner")
    op.execute("REVOKE ALL ON FUNCTION public.provision_account_bot() FROM PUBLIC")
    op.execute("REVOKE CREATE ON SCHEMA public FROM crypidian_bot_provisioner")
    op.execute(
        "CREATE TRIGGER trading_account_provision_bot AFTER INSERT OR UPDATE OF mode, status, name "
        "ON trading_account FOR EACH ROW EXECUTE FUNCTION public.provision_account_bot()"
    )
    op.execute("""CREATE FUNCTION public.guard_bot_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR (NEW.id, NEW.workspace_id, NEW.trading_account_id)
                IS DISTINCT FROM (OLD.id, OLD.workspace_id, OLD.trading_account_id) THEN
                RAISE EXCEPTION 'MB01_BOT_IDENTITY_IMMUTABLE';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER bot_instance_identity_guard BEFORE UPDATE OR DELETE ON bot_instance "
        "FOR EACH ROW EXECUTE FUNCTION public.guard_bot_identity()"
    )
    op.execute("""CREATE FUNCTION public.guard_bot_grant_history() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP = 'DELETE'
                OR (to_jsonb(NEW) - 'revoked_at') <> (to_jsonb(OLD) - 'revoked_at')
                OR (OLD.revoked_at IS NOT NULL
                    AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
                RAISE EXCEPTION 'MB01_BOT_GRANT_HISTORY_IMMUTABLE';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER bot_grant_history_guard BEFORE UPDATE OR DELETE ON bot_grant "
        "FOR EACH ROW EXECUTE FUNCTION public.guard_bot_grant_history()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM bot_grant) OR EXISTS (
            SELECT 1 FROM bot_instance b JOIN trading_account a
            ON (a.workspace_id,a.id) = (b.workspace_id,b.trading_account_id)
            WHERE b.name <> a.name OR b.status <> a.status) THEN
            RAISE EXCEPTION 'MB01_DOWNGRADE_REQUIRES_EXPORT_OF_BOT_STATE';
        END IF;
    END $$""")
    op.execute("DROP TRIGGER trading_account_provision_bot ON trading_account")
    op.execute("DROP FUNCTION public.provision_account_bot()")
    op.execute("DROP TRIGGER bot_instance_identity_guard ON bot_instance")
    op.execute("DROP FUNCTION public.guard_bot_identity()")
    op.execute("DROP TRIGGER bot_grant_history_guard ON bot_grant")
    op.execute("DROP FUNCTION public.guard_bot_grant_history()")
    op.drop_constraint("fk_order_intent_bot_reservation", "order_intent", type_="foreignkey")
    op.drop_constraint("uq_risk_reservation_bot_account_id", "risk_reservation", type_="unique")
    op.drop_constraint(
        op.f("ck_approval_request_approval_request_bot_scope_consistent"),
        "approval_request",
        type_="check",
    )
    for table in reversed(_ACCOUNT_COLUMNS):
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'DROP POLICY bot_member_isolation ON "{table}"')
        if table in _WORKSPACE_SCOPED_ROWS:
            op.execute(f'DROP POLICY workspace_row_read ON "{table}"')
            op.execute(f'DROP POLICY workspace_row_member_read ON "{table}"')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" '
            f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
        )
        op.drop_index(f"ix_{table}_bot_scope", table_name=table)
        op.drop_constraint(f"fk_{table}_bot_account", table, type_="foreignkey")
        op.drop_column(table, "bot_id")
    op.execute("DROP POLICY bot_member_isolation ON bot_instance")
    op.drop_table("bot_grant")
    op.drop_table("bot_instance")
    op.execute("REVOKE USAGE ON SCHEMA public FROM crypidian_bot_provisioner")
    # Role cấp cluster: chỉ xóa khi database khác không còn phụ thuộc vào nó.
    op.execute("""DO $$ BEGIN
        DROP ROLE IF EXISTS crypidian_bot_provisioner;
    EXCEPTION WHEN dependent_objects_still_exist THEN
        RAISE NOTICE 'MB01_PROVISIONER_ROLE_KEPT_FOR_OTHER_DATABASES';
    END $$""")
