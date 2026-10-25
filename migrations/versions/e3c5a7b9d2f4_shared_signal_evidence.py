"""Thêm shared signal evidence (workspace) và job/quyết định strategy theo bot.

Revision ID: e3c5a7b9d2f4
Revises: c7a90d3f8e21

Evidence chung chỉ scope workspace, bất biến và không có cột bot/account/lệnh.
Job/quyết định theo bot dùng FK ghép tới bot_instance, RLS ENABLE + FORCE với policy
riêng từng role và grant trực tiếp tối thiểu (mẫu MB-01). `bot_config_version_id`
chưa có FK vì bảng `bot_config_version` thuộc MB-02; controller thêm khi tích hợp.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e3c5a7b9d2f4"
down_revision = "c7a90d3f8e21"
branch_labels = None
depends_on = None

TABLES = ("shared_signal_evidence", "bot_decision_job", "bot_decision")
_BOT_SCOPED = ("bot_decision_job", "bot_decision")
_IMMUTABLE_TRIGGERS = {
    "shared_signal_evidence": "shared_signal_evidence_immutable",
    "bot_decision": "bot_decision_immutable",
}
_BOT_INSTANCE_SCOPE = [
    "bot_instance.workspace_id",
    "bot_instance.id",
    "bot_instance.trading_account_id",
]
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_MEMBER = "NULLIF(current_setting('app.membership_id', true), '')::uuid"
_BOT = "NULLIF(current_setting('app.bot_id', true), '')::uuid"


def _member_policy(table: str) -> str:
    """Admin web: membership ACTIVE có role workspace-wide hoặc bot_grant còn hiệu lực."""
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
        raise RuntimeError("MB03_POLICY_TEMPLATE_CHANGED")
    member = template.replace(unqualified, f"m.workspace_id = {table}.workspace_id")
    return f"({_WORKSPACE}) AND ({member})"


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    timestamp = sa.DateTime(timezone=True)
    op.create_table(
        "shared_signal_evidence",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("setup_type", sa.String(80), nullable=False),
        sa.Column("observed_at", timestamp, nullable=False),
        sa.Column("available_at", timestamp, nullable=False),
        sa.Column("validity_end", timestamp, nullable=False),
        sa.Column("feature_snapshot_id", sa.String(64), nullable=False),
        sa.Column("source_refs", jsonb, nullable=False),
        sa.Column("model_release_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", timestamp, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_shared_signal_evidence"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_shared_signal_evidence_workspace_id"),
        sa.UniqueConstraint("workspace_id", "evidence_hash", name="uq_shared_signal_evidence_hash"),
        # Observation identity bỏ available_at/validity_end: retry chỉ khác thời gian bị chặn.
        sa.UniqueConstraint(
            "workspace_id", "content_hash", name="uq_shared_signal_evidence_content"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
            name="fk_shared_signal_evidence_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "feature_snapshot_id"],
            ["feature_snapshot.workspace_id", "feature_snapshot.snapshot_id"],
            name="fk_shared_signal_evidence_feature_snapshot",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64 AND length(content_hash) = 64", name="evidence_hash_sha256"
        ),
        sa.CheckConstraint("length(feature_snapshot_id) = 64", name="evidence_feature_sha256"),
        sa.CheckConstraint(
            "observed_at <= available_at AND available_at < validity_end",
            name="evidence_time_order",
        ),
        sa.CheckConstraint(
            "length(trim(symbol)) > 0 AND length(trim(setup_type)) > 0",
            name="evidence_identity_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) > 0",
            name="evidence_sources_nonempty",
        ),
    )
    op.create_index(
        "ix_shared_signal_evidence_asof",
        "shared_signal_evidence",
        ["workspace_id", "symbol", "setup_type", "observed_at", "available_at"],
    )
    op.create_table(
        "bot_decision_job",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("bot_config_version_id", sa.Uuid(), nullable=False),
        sa.Column("evaluated_at", timestamp, nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("context_payload", jsonb, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("completed_at", timestamp, nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", timestamp, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bot_decision_job"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_bot_decision_job_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "bot_id", "account_id", "id", name="uq_bot_decision_job_scope_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "bot_id",
            "evidence_id",
            "bot_config_version_id",
            name="uq_bot_decision_job_key",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            _BOT_INSTANCE_SCOPE,
            name="fk_bot_decision_job_bot_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            name="fk_bot_decision_job_evidence",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')", name="bot_decision_job_status_valid"
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (completed_at IS NOT NULL)",
            name="bot_decision_job_completion_consistent",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64 AND length(context_hash) = 64",
            name="bot_decision_job_hashes_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(context_payload) = 'object'", name="bot_decision_job_context_object"
        ),
    )
    # FK phía con không có index tự động; RLS lọc workspace+bot trên mọi truy vấn.
    op.create_index(
        "ix_bot_decision_job_bot_scope",
        "bot_decision_job",
        ["workspace_id", "bot_id", "account_id"],
    )
    op.create_index(
        "ix_bot_decision_job_pending",
        "bot_decision_job",
        ["workspace_id", "bot_id", "status", "evaluated_at"],
    )
    op.create_table(
        "bot_decision",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("bot_config_version_id", sa.Uuid(), nullable=False),
        sa.Column("bot_config_hash", sa.String(64), nullable=False),
        sa.Column("root_settings_version_id", sa.Uuid(), nullable=False),
        sa.Column("root_settings_hash", sa.String(64), nullable=False),
        sa.Column("strategy_release_id", sa.String(80), nullable=False),
        sa.Column("strategy_release_hash", sa.String(64), nullable=True),
        sa.Column("model_release_id", sa.Uuid(), nullable=True),
        sa.Column("evaluated_at", timestamp, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("payload", jsonb, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", timestamp, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bot_decision"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_bot_decision_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "bot_id", "idempotency_key", name="uq_bot_decision_idempotency"
        ),
        sa.UniqueConstraint("workspace_id", "bot_id", "job_id", name="uq_bot_decision_job"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            _BOT_INSTANCE_SCOPE,
            name="fk_bot_decision_bot_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id", "job_id"],
            [
                "bot_decision_job.workspace_id",
                "bot_decision_job.bot_id",
                "bot_decision_job.account_id",
                "bot_decision_job.id",
            ],
            name="fk_bot_decision_job",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            name="fk_bot_decision_evidence",
        ),
        sa.CheckConstraint(
            "status IN ('CANDIDATE', 'ABSTAIN', 'VETO')", name="bot_decision_status_valid"
        ),
        sa.CheckConstraint(
            "(status = 'CANDIDATE') = (candidate_id IS NOT NULL)",
            name="bot_decision_candidate_consistent",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64 AND length(bot_config_hash) = 64 "
            "AND length(root_settings_hash) = 64 AND length(idempotency_key) = 64 "
            "AND length(decision_hash) = 64",
            name="bot_decision_hashes_sha256",
        ),
        sa.CheckConstraint("length(trim(reason_code)) > 0", name="bot_decision_reason_nonempty"),
    )
    op.create_index(
        "ix_bot_decision_bot_scope", "bot_decision", ["workspace_id", "bot_id", "account_id"]
    )
    op.create_index(
        "ix_bot_decision_bot_time", "bot_decision", ["workspace_id", "bot_id", "created_at"]
    )
    _create_policies()
    _create_guards()
    _grant_privileges()


def _create_policies() -> None:
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    # Evidence là dữ liệu shared workspace: một policy như feature_snapshot/market tables.
    op.execute(
        "CREATE POLICY workspace_isolation ON shared_signal_evidence "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    for table in _BOT_SCOPED:
        runtime = f"({_WORKSPACE}) AND bot_id = {_BOT}"
        admin = _member_policy(table)
        # Mỗi role đúng một policy; không có permissive workspace-only policy ghép OR.
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" TO crypidian_trading_runtime '
            f"USING ({runtime}) WITH CHECK ({runtime})"
        )
        op.execute(
            f'CREATE POLICY bot_member_isolation ON "{table}" TO crypidian_admin_web '
            f"USING ({admin}) WITH CHECK ({admin})"
        )


def _create_guards() -> None:
    op.execute("""CREATE FUNCTION public.reject_signal_decision_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'MB03_SIGNAL_DECISION_IMMUTABLE';
        END $$""")
    for table, trigger in _IMMUTABLE_TRIGGERS.items():
        op.execute(
            f'CREATE TRIGGER "{trigger}" BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.reject_signal_decision_mutation()"
        )
    # Job chỉ được chuyển PENDING -> COMPLETED một lần; mọi cột khác giữ nguyên.
    op.execute("""CREATE FUNCTION public.guard_bot_decision_job() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP = 'DELETE' OR OLD.status <> 'PENDING' OR NEW.status <> 'COMPLETED'
                OR (to_jsonb(NEW) - 'status' - 'completed_at')
                    <> (to_jsonb(OLD) - 'status' - 'completed_at') THEN
                RAISE EXCEPTION 'MB03_BOT_DECISION_JOB_TRANSITION_INVALID';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER bot_decision_job_transition_guard BEFORE UPDATE OR DELETE "
        "ON bot_decision_job FOR EACH ROW EXECUTE FUNCTION public.guard_bot_decision_job()"
    )


def _grant_privileges() -> None:
    # crypidian_app được nhiều process role kế thừa: không cấp qua group (ruling 6 MB-01).
    op.execute(
        "REVOKE ALL ON shared_signal_evidence, bot_decision_job, bot_decision "
        "FROM PUBLIC, crypidian_app"
    )
    op.execute("GRANT SELECT, INSERT ON shared_signal_evidence TO crypidian_trading_runtime")
    op.execute("GRANT SELECT ON shared_signal_evidence TO crypidian_admin_web")
    op.execute("GRANT SELECT, INSERT ON bot_decision_job TO crypidian_trading_runtime")
    # Quyền UPDATE cấp cột đủ cho chuyển trạng thái và SELECT ... FOR UPDATE.
    op.execute(
        "GRANT UPDATE (status, completed_at) ON bot_decision_job TO crypidian_trading_runtime"
    )
    op.execute("GRANT SELECT, INSERT ON bot_decision TO crypidian_trading_runtime")
    op.execute("GRANT SELECT ON bot_decision_job, bot_decision TO crypidian_admin_web")


def downgrade() -> None:
    # Rollback không được drop lịch sử evidence/quyết định (contract §8.5).
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM shared_signal_evidence)
            OR EXISTS (SELECT 1 FROM bot_decision_job)
            OR EXISTS (SELECT 1 FROM bot_decision)
            OR EXISTS (SELECT 1 FROM outbox_event
                WHERE event_type = 'signal.evidence.recorded.v1'
                AND status IN ('PENDING', 'PROCESSING')) THEN
            RAISE EXCEPTION 'MB03_DOWNGRADE_REQUIRES_EXPORT_OF_DECISIONS';
        END IF;
    END $$""")
    op.execute("DROP TRIGGER bot_decision_job_transition_guard ON bot_decision_job")
    op.execute("DROP FUNCTION public.guard_bot_decision_job()")
    for table, trigger in _IMMUTABLE_TRIGGERS.items():
        op.execute(f'DROP TRIGGER "{trigger}" ON "{table}"')
    op.execute("DROP FUNCTION public.reject_signal_decision_mutation()")
    op.drop_index("ix_bot_decision_bot_time", table_name="bot_decision")
    op.drop_index("ix_bot_decision_bot_scope", table_name="bot_decision")
    op.drop_table("bot_decision")
    op.drop_index("ix_bot_decision_job_pending", table_name="bot_decision_job")
    op.drop_index("ix_bot_decision_job_bot_scope", table_name="bot_decision_job")
    op.drop_table("bot_decision_job")
    op.drop_index("ix_shared_signal_evidence_asof", table_name="shared_signal_evidence")
    op.drop_table("shared_signal_evidence")
