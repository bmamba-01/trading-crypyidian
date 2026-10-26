"""MB-05: registry runtime PAPER nhiều bot — control, lease/fencing, state và incident activity.

Revision ID: d9b2f4a6c8e1
Revises: c8f1e3a5b7d9

- `workspace_runtime_control`/`bot_runtime_control`: lịch sử kill/pause append-only (ruling 25).
- `bot_runtime_lease`: một writer cho mỗi bot và mỗi account; fencing token đơn điệu, thời gian
  theo `clock_timestamp()` của DB (ruling 8, 11).
- `bot_runtime_state`, `account_activity_incident`: chỉ process giữ lease ghi (fence trigger).
- `paper_execution_checkpoint.fencing_token`: fence DB cho mọi transition PAPER (ruling 9b);
  NULL chỉ hợp lệ khi bot chưa từng có hàng lease (chuyển tiếp).
- `bot_decision_job`: thêm `attempts`/`last_error_code`/`next_attempt_at` và trạng thái `DEAD`.

Bảng bot-scoped theo mẫu MB-01: FK ghép tới `bot_instance`, ENABLE + FORCE RLS, policy theo
từng role, grant trực tiếp tối thiểu (không qua `crypidian_app`), UPDATE chỉ cấp cột (SEC R10).
Trigger là SECURITY INVOKER với `search_path` pin; fence đọc lease theo RLS của chính writer.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9b2f4a6c8e1"
down_revision = "c8f1e3a5b7d9"
branch_labels = None
depends_on = None

RUNTIME = "crypidian_trading_runtime"
ADMIN = "crypidian_admin_web"
NEW_TABLES = (
    "workspace_runtime_control",
    "bot_runtime_control",
    "bot_runtime_lease",
    "bot_runtime_state",
    "account_activity_incident",
)
BOT_SCOPED = NEW_TABLES[1:]
# Trùng `MAX_LEASE_TTL` của domain `runtime_control.py`.
MAX_LEASE_TTL_SECONDS = 300
_LEASE_UPDATE = "UPDATE (owner_id, fencing_token, acquired_at, renewed_at, expires_at)"
_STATE_UPDATE = (
    "UPDATE (readiness, reason_code, applied_bot_control_revision, "
    "applied_workspace_control_revision, last_checkpoint_revision, activity_cursor, "
    "activity_checked_at, activity_status, fanout_exclusion_code, heartbeat_at, "
    "fencing_token, updated_at)"
)
_INCIDENT_RESOLUTION_UPDATE = (
    "UPDATE (status, resolution_code, resolution_reason, resolved_by, resolved_at)"
)
GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "workspace_runtime_control": {RUNTIME: ("SELECT",), ADMIN: ("SELECT", "INSERT")},
    "bot_runtime_control": {RUNTIME: ("SELECT",), ADMIN: ("SELECT", "INSERT")},
    "bot_runtime_lease": {RUNTIME: ("SELECT", "INSERT", _LEASE_UPDATE), ADMIN: ("SELECT",)},
    "bot_runtime_state": {RUNTIME: ("SELECT", "INSERT", _STATE_UPDATE), ADMIN: ("SELECT",)},
    "account_activity_incident": {
        RUNTIME: ("SELECT", "INSERT"),
        ADMIN: ("SELECT", _INCIDENT_RESOLUTION_UPDATE),
    },
}
JOB_RUNTIME_UPDATE = "UPDATE (attempts, last_error_code, next_attempt_at)"
TRIGGERS: dict[str, tuple[str, ...]] = {
    "workspace_runtime_control": ("workspace_runtime_control_append_only",),
    "bot_runtime_control": ("bot_runtime_control_append_only",),
    "bot_runtime_lease": ("bot_runtime_lease_guard",),
    "bot_runtime_state": ("bot_runtime_state_fence", "bot_runtime_state_identity_guard"),
    "account_activity_incident": (
        "account_activity_incident_fence",
        "account_activity_incident_transition_guard",
    ),
    "paper_execution_checkpoint": ("paper_execution_checkpoint_fence",),
}
FUNCTIONS = (
    "mb05_guard_workspace_runtime_control",
    "mb05_guard_bot_runtime_control",
    "mb05_guard_runtime_lease",
    "mb05_require_runtime_fence",
    "mb05_guard_runtime_state_identity",
    "mb05_guard_activity_incident",
)
CHECK_NAMES: dict[str, tuple[str, ...]] = {
    "workspace_runtime_control": ("kill_state_valid", "revision_positive", "reason_nonempty"),
    "bot_runtime_control": ("entry_state_valid", "revision_positive", "reason_nonempty"),
    "bot_runtime_lease": ("token_positive", "owner_nonempty", "expiry_after_renewal"),
    "bot_runtime_state": (
        "readiness_valid",
        "activity_status_valid",
        "applied_revisions_valid",
        "token_positive",
    ),
    "account_activity_incident": (
        "status_valid",
        "source_valid",
        "hashes_sha256",
        "foreign_state_arrays",
        "token_positive",
        "resolution_code_valid",
        "resolution_consistent",
    ),
    "bot_decision_job": ("bot_decision_job_status_valid", "bot_decision_job_attempts_valid"),
    "paper_execution_checkpoint": ("paper_checkpoint_token_positive",),
}
_BOT_INSTANCE_SCOPE = [
    "bot_instance.workspace_id",
    "bot_instance.id",
    "bot_instance.trading_account_id",
]
_MEMBERSHIP = ["user_membership.workspace_id", "user_membership.id"]
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_MEMBER = "NULLIF(current_setting('app.membership_id', true), '')::uuid"
_BOT = "NULLIF(current_setting('app.bot_id', true), '')::uuid"
# Trần quyền ghi control workspace/resolution incident (ruling 30).
_KILL_ROLES = "'owner','risk_admin'"
_TIMESTAMP = sa.DateTime(timezone=True)
_CLOCK = sa.text("clock_timestamp()")
# Hợp đồng job MB-03 (e3c5a7b9d2f4); downgrade khôi phục nguyên văn.
_MB03_JOB_GUARD = """CREATE OR REPLACE FUNCTION public.guard_bot_decision_job() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP = 'DELETE' OR OLD.status <> 'PENDING' OR NEW.status <> 'COMPLETED'
                OR (to_jsonb(NEW) - 'status' - 'completed_at')
                    <> (to_jsonb(OLD) - 'status' - 'completed_at') THEN
                RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
            END IF;
            RETURN NEW;
        END $$"""


def _active_member(table: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM user_membership m WHERE m.workspace_id = {table}.workspace_id "
        f"AND m.id = {_MEMBER} AND m.status = 'ACTIVE')"
    )


def _member_with_roles(table: str, roles: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM user_membership m JOIN role_binding r "
        f"ON (r.workspace_id, r.membership_id) = (m.workspace_id, m.id) "
        f"WHERE m.workspace_id = {table}.workspace_id AND m.id = {_MEMBER} "
        f"AND m.status = 'ACTIVE' AND r.role IN ({roles}))"
    )


def _member_policy(table: str) -> str:
    """Admin web đọc bot-scoped: membership ACTIVE role workspace-wide hoặc bot_grant hiệu lực."""
    template = f"""EXISTS (SELECT 1 FROM user_membership m WHERE m.workspace_id = workspace_id
        AND m.id = {_MEMBER} AND m.status = 'ACTIVE' AND (
            EXISTS (SELECT 1 FROM role_binding r WHERE r.workspace_id = m.workspace_id
                AND r.membership_id = m.id AND r.role IN ('owner','strategy_admin','risk_admin'))
            OR EXISTS (SELECT 1 FROM bot_grant g WHERE g.workspace_id = m.workspace_id
                AND g.membership_id = m.id AND g.bot_id = {table}.bot_id
                AND g.revoked_at IS NULL)))"""
    unqualified = "m.workspace_id = workspace_id"
    # Thay đúng một lần; nếu template đổi mà không thay được, policy có thể thành luôn đúng.
    if template.count(unqualified) != 1:
        raise RuntimeError("MB05_POLICY_TEMPLATE_CHANGED")
    member = template.replace(unqualified, f"m.workspace_id = {table}.workspace_id")
    return f"({_WORKSPACE}) AND ({member})"


def _bot_scope(table: str) -> list[sa.SchemaItem]:
    return [
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            _BOT_INSTANCE_SCOPE,
            name=f"fk_{table}_bot_account",
        ),
    ]


def _identity() -> list[sa.SchemaItem]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TIMESTAMP, server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    _create_tables()
    _alter_existing_tables()
    _create_policies()
    _create_guards()
    _grant_privileges()


def _create_tables() -> None:
    op.create_table(
        "workspace_runtime_control",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kill_state", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("reason_text", sa.String(500), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column("changed_at", _TIMESTAMP, server_default=_CLOCK, nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        *_identity(),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_runtime_control"),
        sa.UniqueConstraint(
            "workspace_id", "revision", name="uq_workspace_runtime_control_revision"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name="fk_workspace_runtime_control_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "changed_by"],
            _MEMBERSHIP,
            name="fk_workspace_runtime_control_changed_by",
        ),
        sa.CheckConstraint("kill_state IN ('ACTIVE', 'KILLED')", name="kill_state_valid"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.CheckConstraint(
            "length(trim(reason_code)) > 0 AND length(trim(reason_text)) > 0",
            name="reason_nonempty",
        ),
    )
    op.create_table(
        "bot_runtime_control",
        *_bot_scope("bot_runtime_control"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("entry_state", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("reason_text", sa.String(500), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column("changed_at", _TIMESTAMP, server_default=_CLOCK, nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        *_identity(),
        sa.PrimaryKeyConstraint("id", name="pk_bot_runtime_control"),
        sa.UniqueConstraint(
            "workspace_id", "bot_id", "revision", name="uq_bot_runtime_control_revision"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "changed_by"], _MEMBERSHIP, name="fk_bot_runtime_control_changed_by"
        ),
        sa.CheckConstraint("entry_state IN ('ENABLED', 'PAUSED')", name="entry_state_valid"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.CheckConstraint(
            "length(trim(reason_code)) > 0 AND length(trim(reason_text)) > 0",
            name="reason_nonempty",
        ),
    )
    op.create_table(
        "bot_runtime_lease",
        *_bot_scope("bot_runtime_lease"),
        sa.Column("owner_id", sa.String(120), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", _TIMESTAMP, server_default=_CLOCK, nullable=False),
        sa.Column("renewed_at", _TIMESTAMP, server_default=_CLOCK, nullable=False),
        sa.Column("expires_at", _TIMESTAMP, nullable=False),
        *_identity(),
        sa.PrimaryKeyConstraint("id", name="pk_bot_runtime_lease"),
        sa.UniqueConstraint("workspace_id", "bot_id", name="uq_bot_runtime_lease_bot"),
        sa.UniqueConstraint("workspace_id", "account_id", name="uq_bot_runtime_lease_account"),
        sa.CheckConstraint("fencing_token >= 1", name="token_positive"),
        sa.CheckConstraint("length(trim(owner_id)) > 0", name="owner_nonempty"),
        sa.CheckConstraint("expires_at >= renewed_at", name="expiry_after_renewal"),
    )
    op.create_table(
        "bot_runtime_state",
        *_bot_scope("bot_runtime_state"),
        sa.Column("readiness", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column(
            "applied_bot_control_revision",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "applied_workspace_control_revision",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_checkpoint_revision", sa.Integer(), nullable=True),
        sa.Column("activity_cursor", sa.String(240), nullable=True),
        sa.Column("activity_checked_at", _TIMESTAMP, nullable=True),
        sa.Column("activity_status", sa.String(16), nullable=True),
        sa.Column("fanout_exclusion_code", sa.String(80), nullable=True),
        sa.Column("heartbeat_at", _TIMESTAMP, nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_CLOCK, nullable=False),
        *_identity(),
        sa.PrimaryKeyConstraint("id", name="pk_bot_runtime_state"),
        sa.UniqueConstraint("workspace_id", "bot_id", name="uq_bot_runtime_state_bot"),
        sa.CheckConstraint(
            "readiness IN ('STARTING', 'READY', 'RECOVERY_REQUIRED', 'STANDBY')",
            name="readiness_valid",
        ),
        sa.CheckConstraint(
            "activity_status IS NULL OR activity_status IN ('VERIFIED', 'UNVERIFIED')",
            name="activity_status_valid",
        ),
        sa.CheckConstraint(
            "applied_bot_control_revision >= 0 AND applied_workspace_control_revision >= 0",
            name="applied_revisions_valid",
        ),
        sa.CheckConstraint("fencing_token >= 1", name="token_positive"),
    )
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "account_activity_incident",
        *_bot_scope("account_activity_incident"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detected_at", _TIMESTAMP, server_default=_CLOCK, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("cursor", sa.String(240), nullable=True),
        sa.Column("foreign_orders", jsonb, nullable=False),
        sa.Column("foreign_positions", jsonb, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("resolution_code", sa.String(48), nullable=True),
        sa.Column("resolution_reason", sa.String(500), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", _TIMESTAMP, nullable=True),
        *_identity(),
        sa.PrimaryKeyConstraint("id", name="pk_account_activity_incident"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "resolved_by"],
            _MEMBERSHIP,
            name="fk_account_activity_incident_resolved_by",
        ),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="status_valid"),
        sa.CheckConstraint("source IN ('PAPER_LEDGER', 'SCRIPTED', 'VENUE')", name="source_valid"),
        sa.CheckConstraint(
            "length(snapshot_hash) = 64 AND length(fingerprint) = 64", name="hashes_sha256"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(foreign_orders) = 'array' AND jsonb_typeof(foreign_positions) = 'array'",
            name="foreign_state_arrays",
        ),
        sa.CheckConstraint("fencing_token >= 1", name="token_positive"),
        sa.CheckConstraint(
            "resolution_code IS NULL OR resolution_code IN "
            "('EXTERNAL_STATE_REMOVED_BY_OPERATOR', 'FALSE_POSITIVE_CONFIRMED')",
            name="resolution_code_valid",
        ),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolution_code IS NULL AND resolution_reason IS NULL "
            "AND resolved_by IS NULL AND resolved_at IS NULL) OR (status = 'RESOLVED' "
            "AND resolution_code IS NOT NULL AND length(trim(resolution_reason)) > 0 "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolution_consistent",
        ),
    )
    # Unique chỉ theo fingerprint bot-scoped: không thành covert channel dữ liệu bot khác.
    op.create_index(
        "uq_account_activity_incident_open",
        "account_activity_incident",
        ["workspace_id", "bot_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    for table in BOT_SCOPED:
        # FK phía con không có index tự động; RLS lọc workspace+bot trên mọi truy vấn.
        op.create_index(f"ix_{table}_bot_scope", table, ["workspace_id", "bot_id", "account_id"])


def _alter_existing_tables() -> None:
    op.add_column(
        "bot_decision_job",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("bot_decision_job", sa.Column("last_error_code", sa.String(80), nullable=True))
    op.add_column("bot_decision_job", sa.Column("next_attempt_at", _TIMESTAMP, nullable=True))
    op.drop_constraint(
        op.f("ck_bot_decision_job_bot_decision_job_status_valid"), "bot_decision_job", "check"
    )
    op.create_check_constraint(
        "bot_decision_job_status_valid",
        "bot_decision_job",
        "status IN ('PENDING', 'COMPLETED', 'DEAD')",
    )
    op.create_check_constraint(
        "bot_decision_job_attempts_valid", "bot_decision_job", "attempts >= 0"
    )
    op.add_column(
        "paper_execution_checkpoint", sa.Column("fencing_token", sa.BigInteger(), nullable=True)
    )
    op.create_check_constraint(
        "paper_checkpoint_token_positive",
        "paper_execution_checkpoint",
        "fencing_token IS NULL OR fencing_token >= 1",
    )


def _create_policies() -> None:
    for table in NEW_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    # Control workspace: runtime đọc theo workspace; admin đọc khi là member ACTIVE, chỉ
    # owner/risk_admin được ghi và phải tự ký tên mình (ruling 30).
    table = "workspace_runtime_control"
    op.execute(
        f'CREATE POLICY workspace_isolation ON "{table}" FOR SELECT TO {RUNTIME} '
        f"USING ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY member_read ON "{table}" FOR SELECT TO {ADMIN} '
        f"USING (({_WORKSPACE}) AND {_active_member(table)})"
    )
    op.execute(
        f'CREATE POLICY kill_write ON "{table}" FOR INSERT TO {ADMIN} '
        f"WITH CHECK (({_WORKSPACE}) AND changed_by = {_MEMBER} "
        f"AND {_member_with_roles(table, _KILL_ROLES)})"
    )
    runtime_bot = f"({_WORKSPACE}) AND bot_id = {_BOT}"
    for table in BOT_SCOPED:
        if table in ("bot_runtime_lease", "bot_runtime_state", "account_activity_incident"):
            # Runtime là writer duy nhất (grant quyết định lệnh nào được phép).
            op.execute(
                f'CREATE POLICY workspace_isolation ON "{table}" TO {RUNTIME} '
                f"USING ({runtime_bot}) WITH CHECK ({runtime_bot})"
            )
        else:
            op.execute(
                f'CREATE POLICY workspace_isolation ON "{table}" FOR SELECT TO {RUNTIME} '
                f"USING ({runtime_bot})"
            )
        op.execute(
            f'CREATE POLICY bot_member_isolation ON "{table}" FOR SELECT TO {ADMIN} '
            f"USING ({_member_policy(table)})"
        )
    table = "bot_runtime_control"
    pause_grant = (
        f"EXISTS (SELECT 1 FROM bot_grant g WHERE g.workspace_id = {table}.workspace_id "
        f"AND g.membership_id = {_MEMBER} AND g.bot_id = {table}.bot_id "
        "AND g.permission IN ('bot:runtime:pause', 'bot:runtime:resume') "
        "AND g.revoked_at IS NULL)"
    )
    op.execute(
        f'CREATE POLICY pause_write ON "{table}" FOR INSERT TO {ADMIN} '
        f"WITH CHECK (({_WORKSPACE}) AND changed_by = {_MEMBER} AND {_active_member(table)} "
        f"AND ({_member_with_roles(table, _KILL_ROLES)} OR {pause_grant}))"
    )
    table = "account_activity_incident"
    resolver = f"({_WORKSPACE}) AND {_member_with_roles(table, _KILL_ROLES)}"
    op.execute(
        f'CREATE POLICY incident_resolution ON "{table}" FOR UPDATE TO {ADMIN} '
        f"USING ({resolver}) WITH CHECK (({resolver}) AND resolved_by = {_MEMBER})"
    )


def _create_guards() -> None:
    # Control append-only: revision = max + 1, thời điểm theo đồng hồ DB, cấm UPDATE/DELETE.
    for function, table, scope in (
        ("mb05_guard_workspace_runtime_control", "workspace_runtime_control", ""),
        ("mb05_guard_bot_runtime_control", "bot_runtime_control", " AND c.bot_id = NEW.bot_id"),
    ):
        op.execute(f"""CREATE FUNCTION public.{function}() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            DECLARE v_max integer;
            BEGIN
                IF TG_OP <> 'INSERT' THEN
                    RAISE EXCEPTION 'MB05_RUNTIME_CONTROL_APPEND_ONLY';
                END IF;
                SELECT coalesce(max(c.revision), 0) INTO v_max FROM public.{table} c
                WHERE c.workspace_id = NEW.workspace_id{scope};
                IF NEW.revision IS DISTINCT FROM v_max + 1 THEN
                    RAISE EXCEPTION 'MB05_CONTROL_REVISION_CONFLICT';
                END IF;
                NEW.changed_at := pg_catalog.clock_timestamp();
                RETURN NEW;
            END $$""")
    for table, trigger, function in (
        (
            "workspace_runtime_control",
            "workspace_runtime_control_append_only",
            "mb05_guard_workspace_runtime_control",
        ),
        (
            "bot_runtime_control",
            "bot_runtime_control_append_only",
            "mb05_guard_bot_runtime_control",
        ),
    ):
        op.execute(
            f'CREATE TRIGGER "{trigger}" BEFORE INSERT OR UPDATE OR DELETE ON "{table}" '
            f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
        )
    # Lease: CAS theo đồng hồ DB; token chỉ tăng đúng 1 khi lấy lại sau hết hạn/đổi chủ.
    ttl = f"interval '{MAX_LEASE_TTL_SECONDS} seconds'"
    op.execute(f"""CREATE FUNCTION public.mb05_guard_runtime_lease() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'MB05_LEASE_DELETE_FORBIDDEN';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.fencing_token IS DISTINCT FROM 1 THEN
                    RAISE EXCEPTION 'MB05_LEASE_TOKEN_INVALID';
                END IF;
                IF NEW.expires_at <= v_now OR NEW.expires_at > v_now + {ttl} THEN
                    RAISE EXCEPTION 'MB05_LEASE_EXPIRY_INVALID';
                END IF;
                NEW.acquired_at := v_now;
                NEW.renewed_at := v_now;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.workspace_id, NEW.bot_id, NEW.account_id)
                IS DISTINCT FROM (OLD.id, OLD.workspace_id, OLD.bot_id, OLD.account_id)
                OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'MB05_LEASE_IDENTITY_IMMUTABLE';
            END IF;
            IF NEW.expires_at <= v_now THEN
                -- Release chủ động: đúng chủ, đúng token, lease còn hiệu lực.
                IF NEW.owner_id IS DISTINCT FROM OLD.owner_id
                    OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
                    OR OLD.expires_at <= v_now THEN
                    RAISE EXCEPTION 'MB05_LEASE_RELEASE_INVALID';
                END IF;
                NEW.acquired_at := OLD.acquired_at;
                NEW.renewed_at := OLD.renewed_at;
                NEW.expires_at := v_now;
                RETURN NEW;
            END IF;
            IF NEW.expires_at > v_now + {ttl} THEN
                RAISE EXCEPTION 'MB05_LEASE_EXPIRY_INVALID';
            END IF;
            IF OLD.expires_at > v_now THEN
                IF NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
                    RAISE EXCEPTION 'MB05_LEASE_HELD_ELSEWHERE';
                END IF;
                IF NEW.fencing_token IS DISTINCT FROM OLD.fencing_token THEN
                    RAISE EXCEPTION 'MB05_LEASE_TOKEN_INVALID';
                END IF;
                NEW.acquired_at := OLD.acquired_at;
            ELSE
                IF NEW.fencing_token IS DISTINCT FROM OLD.fencing_token + 1 THEN
                    RAISE EXCEPTION 'MB05_LEASE_TOKEN_INVALID';
                END IF;
                NEW.acquired_at := v_now;
            END IF;
            NEW.renewed_at := v_now;
            RETURN NEW;
        END $$""")
    op.execute(
        'CREATE TRIGGER "bot_runtime_lease_guard" BEFORE INSERT OR UPDATE OR DELETE '
        'ON "bot_runtime_lease" FOR EACH ROW EXECUTE FUNCTION public.mb05_guard_runtime_lease()'
    )
    # Fence DB (ruling 9b): token phải bằng token lease hiện hành và lease chưa hết hạn.
    # `FOR SHARE` làm takeover phải chờ writer hiện tại commit; READ COMMITTED trả phiên bản
    # mới nhất sau khi chờ nên token cũ bị từ chối. Đối số `transitional_null` cho phép NULL
    # chỉ khi bot chưa từng có hàng lease (seed/khởi tạo trước lần chạy runtime đầu).
    op.execute("""CREATE FUNCTION public.mb05_require_runtime_fence() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_token bigint; v_expires timestamptz;
        BEGIN
            IF NEW.fencing_token IS NULL THEN
                IF TG_ARGV[0] = 'transitional_null' AND NOT EXISTS (
                    SELECT 1 FROM public.bot_runtime_lease l
                    WHERE l.workspace_id = NEW.workspace_id AND l.bot_id = NEW.bot_id) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'MB05_FENCING_TOKEN_REQUIRED';
            END IF;
            SELECT l.fencing_token, l.expires_at INTO v_token, v_expires
            FROM public.bot_runtime_lease l
            WHERE l.workspace_id = NEW.workspace_id AND l.bot_id = NEW.bot_id
            FOR SHARE;
            IF NOT FOUND OR v_token IS DISTINCT FROM NEW.fencing_token THEN
                RAISE EXCEPTION 'MB05_STALE_FENCING_TOKEN';
            END IF;
            IF v_expires <= pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'MB05_LEASE_EXPIRED';
            END IF;
            RETURN NEW;
        END $$""")
    for table, trigger, events, mode in (
        ("bot_runtime_state", "bot_runtime_state_fence", "INSERT OR UPDATE", "strict"),
        ("account_activity_incident", "account_activity_incident_fence", "INSERT", "strict"),
        (
            "paper_execution_checkpoint",
            "paper_execution_checkpoint_fence",
            "INSERT",
            "transitional_null",
        ),
    ):
        op.execute(
            f'CREATE TRIGGER "{trigger}" BEFORE {events} ON "{table}" FOR EACH ROW '
            f"EXECUTE FUNCTION public.mb05_require_runtime_fence('{mode}')"
        )
    op.execute("""CREATE FUNCTION public.mb05_guard_runtime_state_identity() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR (NEW.id, NEW.workspace_id, NEW.bot_id, NEW.account_id)
                IS DISTINCT FROM (OLD.id, OLD.workspace_id, OLD.bot_id, OLD.account_id)
                OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'MB05_RUNTIME_STATE_IDENTITY_IMMUTABLE';
            END IF;
            NEW.updated_at := pg_catalog.clock_timestamp();
            RETURN NEW;
        END $$""")
    op.execute(
        'CREATE TRIGGER "bot_runtime_state_identity_guard" BEFORE UPDATE OR DELETE '
        'ON "bot_runtime_state" FOR EACH ROW '
        "EXECUTE FUNCTION public.mb05_guard_runtime_state_identity()"
    )
    resolution = (
        "'status' - 'resolution_code' - 'resolution_reason' - 'resolved_by' - 'resolved_at'"
    )
    op.execute(f"""CREATE FUNCTION public.mb05_guard_activity_incident() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'MB05_INCIDENT_DELETE_FORBIDDEN';
            END IF;
            IF OLD.status <> 'OPEN' OR NEW.status <> 'RESOLVED' THEN
                RAISE EXCEPTION 'MB05_INCIDENT_TRANSITION_INVALID';
            END IF;
            IF (to_jsonb(NEW) - {resolution}) <> (to_jsonb(OLD) - {resolution}) THEN
                RAISE EXCEPTION 'MB05_INCIDENT_DETECTION_IMMUTABLE';
            END IF;
            IF NEW.resolution_code IS NULL OR NEW.resolved_by IS NULL
                OR NEW.resolution_reason IS NULL OR length(trim(NEW.resolution_reason)) = 0 THEN
                RAISE EXCEPTION 'MB05_INCIDENT_RESOLUTION_INCOMPLETE';
            END IF;
            NEW.resolved_at := pg_catalog.clock_timestamp();
            RETURN NEW;
        END $$""")
    op.execute(
        'CREATE TRIGGER "account_activity_incident_transition_guard" BEFORE UPDATE OR DELETE '
        'ON "account_activity_incident" FOR EACH ROW '
        "EXECUTE FUNCTION public.mb05_guard_activity_incident()"
    )
    for function in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")
    # Job: PENDING -> PENDING (retry, attempts +1), PENDING -> DEAD, PENDING -> COMPLETED.
    op.execute("""CREATE OR REPLACE FUNCTION public.guard_bot_decision_job() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            IF TG_OP = 'DELETE' OR OLD.status <> 'PENDING' THEN
                RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
            END IF;
            IF NEW.status = 'COMPLETED' THEN
                IF (to_jsonb(NEW) - 'status' - 'completed_at')
                    <> (to_jsonb(OLD) - 'status' - 'completed_at') THEN
                    RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
                END IF;
            ELSIF NEW.status = 'PENDING' THEN
                IF NEW.attempts IS DISTINCT FROM OLD.attempts + 1
                    OR (to_jsonb(NEW) - 'attempts' - 'last_error_code' - 'next_attempt_at')
                    <> (to_jsonb(OLD) - 'attempts' - 'last_error_code' - 'next_attempt_at') THEN
                    RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
                END IF;
            ELSIF NEW.status = 'DEAD' THEN
                IF NEW.attempts < OLD.attempts OR NEW.last_error_code IS NULL
                    OR (to_jsonb(NEW) - 'status' - 'attempts' - 'last_error_code'
                        - 'next_attempt_at')
                    <> (to_jsonb(OLD) - 'status' - 'attempts' - 'last_error_code'
                        - 'next_attempt_at') THEN
                    RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
                END IF;
            ELSE
                RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
            END IF;
            RETURN NEW;
        END $$""")


def _grant_privileges() -> None:
    # crypidian_app được admin-web/runtime kế thừa: không cấp qua group (ruling 6 MB-01).
    for table in NEW_TABLES:
        op.execute(f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC, crypidian_app')
    for table, roles in GRANTS.items():
        for role, privileges in roles.items():
            for privilege in privileges:
                op.execute(f'GRANT {privilege} ON TABLE "{table}" TO {role}')
    op.execute(f'GRANT {JOB_RUNTIME_UPDATE} ON TABLE "bot_decision_job" TO {RUNTIME}')


def downgrade() -> None:
    # Rollback không được drop lịch sử control/lease/incident hay token đã ghi (contract §8.5).
    tables = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in NEW_TABLES)
    op.execute(f"""DO $$ BEGIN
        IF {tables}
            OR EXISTS (SELECT 1 FROM bot_decision_job WHERE status = 'DEAD' OR attempts > 0
                OR last_error_code IS NOT NULL OR next_attempt_at IS NOT NULL)
            OR EXISTS (SELECT 1 FROM paper_execution_checkpoint
                WHERE fencing_token IS NOT NULL) THEN
            RAISE EXCEPTION 'MB05_DOWNGRADE_REQUIRES_EXPORT_OF_RUNTIME_STATE';
        END IF;
    END $$""")
    op.execute(_MB03_JOB_GUARD)
    op.execute(f'REVOKE {JOB_RUNTIME_UPDATE} ON TABLE "bot_decision_job" FROM {RUNTIME}')
    op.drop_constraint(
        op.f("ck_bot_decision_job_bot_decision_job_attempts_valid"), "bot_decision_job", "check"
    )
    op.drop_constraint(
        op.f("ck_bot_decision_job_bot_decision_job_status_valid"), "bot_decision_job", "check"
    )
    op.create_check_constraint(
        "bot_decision_job_status_valid",
        "bot_decision_job",
        "status IN ('PENDING', 'COMPLETED')",
    )
    op.drop_column("bot_decision_job", "next_attempt_at")
    op.drop_column("bot_decision_job", "last_error_code")
    op.drop_column("bot_decision_job", "attempts")
    op.execute('DROP TRIGGER "paper_execution_checkpoint_fence" ON "paper_execution_checkpoint"')
    op.drop_constraint(
        op.f("ck_paper_execution_checkpoint_paper_checkpoint_token_positive"),
        "paper_execution_checkpoint",
        "check",
    )
    op.drop_column("paper_execution_checkpoint", "fencing_token")
    for table in reversed(NEW_TABLES):
        for trigger in TRIGGERS[table]:
            op.execute(f'DROP TRIGGER "{trigger}" ON "{table}"')
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{function}()")
