"""Đợt 3: Circuit Breaker, Scorer History role/RLS, Entry Block và Signal bar_sources.

Revision ID: d8e2f4a6b8c0
Revises: d4f6a8b0c2e1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8e2f4a6b8c0"
down_revision: str | None = "d4f6a8b0c2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
SCORER_HISTORY = "crypidian_scorer_history"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    # 1. CRY-266: bảng circuit_breaker_event (append-only, RLS)
    op.create_table(
        "circuit_breaker_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("scope_key", sa.String(length=80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column(
            "tripped_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("detail_vi", sa.String(length=500), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("resumed_by", sa.Uuid(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_reason", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "scope_key",
            "revision",
            name="uq_circuit_breaker_event_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
            name="fk_circuit_breaker_event_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            ondelete="CASCADE",
            name="fk_circuit_breaker_event_bot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "resumed_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_circuit_breaker_event_resumed_by",
        ),
        sa.CheckConstraint(
            "state IN ('CLOSED', 'OPEN', 'COOLDOWN_PASSED')",
            name="circuit_breaker_event_state_valid",
        ),
        sa.CheckConstraint("revision >= 1", name="circuit_breaker_event_revision_positive"),
        sa.CheckConstraint(
            "length(trim(scope_key)) > 0 AND length(trim(policy_version)) > 0 "
            "AND length(trim(detail_vi)) > 0",
            name="circuit_breaker_event_nonempty",
        ),
        sa.CheckConstraint(
            "(bot_id IS NULL AND scope_key = 'workspace') OR "
            "(bot_id IS NOT NULL AND scope_key = 'bot:' || bot_id::text)",
            name="circuit_breaker_event_scope_consistent",
        ),
    )
    op.create_index(
        "ix_circuit_breaker_event_latest",
        "circuit_breaker_event",
        ["workspace_id", "scope_key", "revision"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.circuit_breaker_event_reject_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'CIRCUIT_BREAKER_EVENT_IMMUTABLE';
        END;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.circuit_breaker_event_reject_mutation() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER circuit_breaker_event_immutable "
        "BEFORE UPDATE OR DELETE ON public.circuit_breaker_event "
        "FOR EACH ROW EXECUTE FUNCTION public.circuit_breaker_event_reject_mutation()"
    )
    op.execute("ALTER TABLE public.circuit_breaker_event ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.circuit_breaker_event FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON public.circuit_breaker_event "
        f"TO {ADMIN}, {RUNTIME} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.circuit_breaker_event TO {ADMIN}, {RUNTIME}")

    # 2. CRY-440: bảng paper_entry_operator_block (append-only, RLS)
    op.create_table(
        "paper_entry_operator_block",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("bot_decision_id", sa.Uuid(), nullable=False),
        sa.Column("blocked_by", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "bot_decision_id",
            name="uq_paper_entry_operator_block_decision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="CASCADE",
            name="fk_paper_entry_operator_block_decision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blocked_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_paper_entry_operator_block_actor",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="paper_entry_operator_block_reason_nonempty",
        ),
    )
    op.create_index(
        "ix_paper_entry_operator_block_workspace_created",
        "paper_entry_operator_block",
        ["workspace_id", "created_at"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.paper_entry_operator_block_reject_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'PAPER_ENTRY_OPERATOR_BLOCK_IMMUTABLE';
        END;
        $$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.paper_entry_operator_block_reject_mutation() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER paper_entry_operator_block_immutable "
        "BEFORE UPDATE OR DELETE ON public.paper_entry_operator_block "
        "FOR EACH ROW EXECUTE FUNCTION public.paper_entry_operator_block_reject_mutation()"
    )
    op.execute("ALTER TABLE public.paper_entry_operator_block ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.paper_entry_operator_block FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON public.paper_entry_operator_block "
        f"TO {ADMIN}, {RUNTIME} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.paper_entry_operator_block TO {ADMIN}")
    op.execute(f"GRANT SELECT ON TABLE public.paper_entry_operator_block TO {RUNTIME}")

    # 3. CRY-439: role riêng cho scorer history với grant đúng allowlist cột và RLS workspace
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{SCORER_HISTORY}'
            ) THEN
                CREATE ROLE {SCORER_HISTORY}
                    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles r
                WHERE r.rolname = '{SCORER_HISTORY}'
                    AND (r.rolsuper OR r.rolbypassrls OR r.rolinherit
                        OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                        OR EXISTS (
                            SELECT 1 FROM pg_catalog.pg_auth_members m
                            WHERE m.roleid = r.oid OR m.member = r.oid
                        ))
            ) THEN
                RAISE EXCEPTION 'WP06F_SCORER_HISTORY_ROLE_UNSAFE';
            END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {SCORER_HISTORY}")
    op.execute(
        f"GRANT SELECT (id, workspace_id, created_at, mode, decision, reason_code, "
        f"probability, confidence, uncertainty) ON TABLE public.learning_score_event "
        f"TO {SCORER_HISTORY}"
    )
    op.execute(
        f"CREATE POLICY scorer_history_workspace_isolation ON public.learning_score_event "
        f"FOR SELECT TO {SCORER_HISTORY} USING ({WORKSPACE})"
    )

    # 4. CRY-438: cột bar_sources trên signal_outcome để kiểm quyền TRAINING từng nến
    op.add_column(
        "signal_outcome",
        sa.Column(
            "bar_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("signal_outcome", "bar_sources")

    op.execute(
        "DROP POLICY IF EXISTS scorer_history_workspace_isolation ON public.learning_score_event"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.learning_score_event FROM {SCORER_HISTORY}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {SCORER_HISTORY}")

    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.paper_entry_operator_block FROM {RUNTIME}")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.paper_entry_operator_block FROM {ADMIN}")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON public.paper_entry_operator_block")
    op.execute(
        "DROP TRIGGER IF EXISTS paper_entry_operator_block_immutable "
        "ON public.paper_entry_operator_block"
    )
    op.execute("DROP FUNCTION IF EXISTS public.paper_entry_operator_block_reject_mutation()")
    op.drop_index(
        "ix_paper_entry_operator_block_workspace_created",
        table_name="paper_entry_operator_block",
    )
    op.drop_table("paper_entry_operator_block")

    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.circuit_breaker_event FROM {ADMIN}, {RUNTIME}"
    )
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON public.circuit_breaker_event")
    op.execute(
        "DROP TRIGGER IF EXISTS circuit_breaker_event_immutable ON public.circuit_breaker_event"
    )
    op.execute("DROP FUNCTION IF EXISTS public.circuit_breaker_event_reject_mutation()")
    op.drop_index("ix_circuit_breaker_event_latest", table_name="circuit_breaker_event")
    op.drop_table("circuit_breaker_event")
