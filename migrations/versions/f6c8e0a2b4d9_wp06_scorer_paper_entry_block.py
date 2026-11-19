"""Tạo ledger bất biến cho thao tác chặn một entry PAPER."""

from collections.abc import Sequence

from alembic import op

revision: str = "f6c8e0a2b4d9"
down_revision: str | None = "f6c8e0a2b4d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "public.learning_scorer_paper_entry_block"
ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
READER = "crypidian_scorer_history"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
HISTORY_COLUMNS = (
    "workspace_id, bot_decision_id, score_event_id, actor_user_id, "
    "mfa_verified_at, reason, created_at"
)
SCORE_EVENT_LINK_COLUMNS = "bot_id, bot_decision_id"
IMMUTABLE = "public.wp06_scorer_paper_entry_block_immutable"
_REASON_TRIM_CHARS = (
    r"U&'\0009\000A\000D\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006"
    r"\2007\2008\2009\200A\2028\2029\202F\205F\3000'"
)


def upgrade() -> None:
    # Giữ login đọc độc lập như ở revision cấp quyền lịch sử scorer.
    op.execute(
        f"""DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = '{READER}'
                  AND role.rolcanlogin AND NOT role.rolinherit AND NOT role.rolsuper
                  AND NOT role.rolcreatedb AND NOT role.rolcreaterole
                  AND NOT role.rolreplication AND NOT role.rolbypassrls
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = role.oid
                  )
            ) THEN
                RAISE EXCEPTION 'WP06_PAPER_ENTRY_BLOCK_READER_ROLE_UNSAFE';
            END IF;
        END $$"""
    )

    op.execute(
        rf"""
CREATE TABLE {TABLE} (
    id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    bot_id UUID NOT NULL,
    bot_decision_id UUID NOT NULL,
    bot_decision_hash VARCHAR(64) NOT NULL,
    candidate_id VARCHAR(64) NOT NULL,
    score_event_id UUID NOT NULL,
    score_event_hash VARCHAR(64) NOT NULL,
    deployment_event_id UUID NOT NULL,
    deployment_revision INTEGER NOT NULL,
    registry_id UUID NOT NULL,
    registry_revision INTEGER NOT NULL,
    active_pointer_version INTEGER NOT NULL,
    scorer_artifact_id UUID NOT NULL,
    scorer_artifact_hash VARCHAR(64) NOT NULL,
    actor_user_id UUID NOT NULL,
    mfa_verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason VARCHAR(500) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    idempotency_key UUID NOT NULL,
    CONSTRAINT pk_learning_scorer_paper_entry_block PRIMARY KEY (id),
    CONSTRAINT uq_learning_paper_block_decision UNIQUE (workspace_id, bot_decision_id),
    CONSTRAINT uq_learning_paper_block_key UNIQUE (workspace_id, idempotency_key),
    CONSTRAINT fk_learning_paper_block_decision FOREIGN KEY(workspace_id, bot_id, bot_decision_id)
        REFERENCES public.bot_decision (workspace_id, bot_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_learning_paper_block_score_event FOREIGN KEY(workspace_id, score_event_id)
        REFERENCES public.learning_score_event (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_learning_paper_block_actor FOREIGN KEY(workspace_id, actor_user_id)
        REFERENCES public.user_membership (workspace_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT ck_learning_scorer_paper_entry_block_nonzero_uuids CHECK (
        '00000000-0000-0000-0000-000000000000'::uuid NOT IN (
            id, workspace_id, bot_id, bot_decision_id, score_event_id, deployment_event_id,
            registry_id, scorer_artifact_id, actor_user_id, idempotency_key
        )
    ),
    CONSTRAINT ck_learning_scorer_paper_entry_block_hashes CHECK (
        bot_decision_hash ~ '^[0-9a-f]{{64}}$' AND candidate_id ~ '^[0-9a-f]{{64}}$'
        AND score_event_hash ~ '^[0-9a-f]{{64}}$' AND scorer_artifact_hash ~ '^[0-9a-f]{{64}}$'
    ),
    CONSTRAINT ck_learning_scorer_paper_entry_block_revisions CHECK (
        deployment_revision >= 1 AND registry_revision >= 1 AND active_pointer_version >= 0
    ),
    CONSTRAINT ck_learning_scorer_paper_entry_block_reason CHECK (
        reason = pg_catalog.btrim(reason, {_REASON_TRIM_CHARS})
        AND char_length(reason) BETWEEN 1 AND 500
        AND reason !~ '[\x01-\x08\x0B\x0C\x0E-\x1F]'
    ),
    CONSTRAINT ck_learning_scorer_paper_entry_block_mfa_window CHECK (
        mfa_verified_at <= created_at AND created_at - mfa_verified_at <= interval '15 minutes'
    )
)
"""
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {TABLE} FROM PUBLIC, {ADMIN}, {RUNTIME}, {READER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {TABLE} TO {ADMIN}")
    op.execute(f"GRANT SELECT (workspace_id, bot_decision_id) ON TABLE {TABLE} TO {RUNTIME}")
    op.execute(f"GRANT SELECT ({HISTORY_COLUMNS}) ON TABLE {TABLE} TO {READER}")
    # Cho phép lịch sử scorer chiếu hai khóa typed cần cho thao tác theo đúng event.
    op.execute(
        f"GRANT SELECT ({SCORE_EVENT_LINK_COLUMNS}) "
        f"ON TABLE public.learning_score_event TO {READER}"
    )
    op.execute(
        f"CREATE POLICY workspace_guard ON {TABLE} AS RESTRICTIVE FOR ALL TO PUBLIC "
        f"USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY ledger_select ON {TABLE} FOR SELECT TO {ADMIN}, {RUNTIME}, {READER} "
        "USING (true)"
    )
    op.execute(f"CREATE POLICY ledger_insert ON {TABLE} FOR INSERT TO {ADMIN} WITH CHECK (true)")
    op.execute(
        f"""CREATE FUNCTION {IMMUTABLE}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'WP06_PAPER_ENTRY_BLOCK_IMMUTABLE';
        END $$"""
    )
    op.execute(f"REVOKE ALL ON FUNCTION {IMMUTABLE}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER reject_row_change BEFORE UPDATE OR DELETE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE}()"
    )
    op.execute(
        f"CREATE TRIGGER reject_truncate BEFORE TRUNCATE ON {TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {IMMUTABLE}()"
    )


def downgrade() -> None:
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    # Cho chủ bảng thấy toàn bộ ledger trong transaction giữ khóa độc quyền.
    # Khi còn dữ liệu, rollback cũng khôi phục FORCE RLS.
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""DO $$
        DECLARE
            previous_row_security text := pg_catalog.current_setting('row_security');
        BEGIN
            SET LOCAL row_security = off;
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION 'WP06_PAPER_ENTRY_BLOCK_DOWNGRADE_REQUIRES_EMPTY_TABLE';
            END IF;
            PERFORM pg_catalog.set_config('row_security', previous_row_security, true);
        END $$"""
    )
    op.execute(
        f"REVOKE SELECT ({SCORE_EVENT_LINK_COLUMNS}) "
        f"ON TABLE public.learning_score_event FROM {READER}"
    )
    op.drop_table("learning_scorer_paper_entry_block", schema="public")
    op.execute(f"DROP FUNCTION {IMMUTABLE}()")
