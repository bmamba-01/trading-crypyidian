"""WP-05: append-only source policy registry và quota reservations tenant-scoped."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6e8a0b2d4f6"
down_revision: str | None = "e2c4a6f8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_TABLES = ("source_policy_revision", "source_quota_event")


def upgrade() -> None:
    op.create_table(
        "source_policy_revision",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.String(length=160), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("allowed_purposes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publisher", sa.String(length=240), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("access_method", sa.String(length=32), nullable=False),
        sa.Column("auth_scope", sa.String(length=240), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("retention_ref", sa.String(length=240), nullable=False),
        sa.Column("quota_window_seconds", sa.BigInteger(), nullable=False),
        sa.Column("quota_max_requests", sa.BigInteger(), nullable=False),
        sa.Column("quota_max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("quota_max_cost", sa.Numeric(384, 128), nullable=False),
        sa.Column("quota_cost_unit", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 1", name="source_policy_sequence_positive"),
        sa.CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_policy_source_id_valid",
        ),
        sa.CheckConstraint("length(policy_id) BETWEEN 1 AND 160", name="source_policy_id_valid"),
        sa.CheckConstraint(
            "state IN ('discovered', 'rights_pending', 'verified', 'blocked', "
            "'revoked', 'expired')",
            name="source_policy_state_valid",
        ),
        sa.CheckConstraint(
            "access_method ~ '^[A-Z][A-Z0-9_]{0,31}$'",
            name="source_policy_access_method_valid",
        ),
        sa.CheckConstraint("quota_window_seconds > 0", name="source_quota_window_positive"),
        sa.CheckConstraint("quota_max_requests > 0", name="source_quota_requests_positive"),
        sa.CheckConstraint("quota_max_bytes > 0", name="source_quota_bytes_positive"),
        sa.CheckConstraint(
            "quota_max_cost >= 0 AND quota_max_cost <> 'NaN'::numeric",
            name="source_quota_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR reviewed_at IS NULL OR valid_until > reviewed_at",
            name="source_policy_validity_ordered",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_purposes) = 'array' "
            'AND allowed_purposes <@ \'["metadata","display","embedding",'
            '"training","redistribution"]\'::jsonb',
            name="source_policy_purposes_valid",
        ),
        sa.CheckConstraint(
            "state <> 'verified' OR (jsonb_typeof(allowed_purposes) = 'array' "
            "AND jsonb_array_length(allowed_purposes) > 0 AND evidence_ref IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND approved_by IS NOT NULL "
            "AND approved_by <> actor_id)",
            name="source_policy_verified_has_approval",
        ),
        sa.CheckConstraint(
            "state = 'verified' OR approved_by IS NULL",
            name="source_policy_approval_state_consistent",
        ),
        sa.CheckConstraint(
            "reviewed_at <= created_at OR reviewed_at IS NULL",
            name="source_policy_review_not_after_creation",
        ),
        sa.CheckConstraint(
            "endpoint_url LIKE 'https://%' AND position('#' in endpoint_url) = 0 "
            "AND split_part(split_part(endpoint_url, '/', 3), '@', 1) "
            "= split_part(split_part(endpoint_url, '/', 3), '/', 1)",
            name="source_policy_endpoint_https_no_credentials",
        ),
        sa.CheckConstraint("length(publisher) > 0", name="source_policy_publisher_present"),
        sa.CheckConstraint("length(auth_scope) > 0", name="source_policy_auth_scope_present"),
        sa.CheckConstraint("length(retention_ref) > 0", name="source_policy_retention_present"),
        sa.CheckConstraint("length(quota_cost_unit) > 0", name="source_quota_cost_unit_present"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_policy_revision")),
        sa.UniqueConstraint("workspace_id", "policy_id", name="uq_source_policy_revision_policy"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_id",
            "sequence",
            name="uq_source_policy_revision_workspace_source_sequence",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_source_policy_revision_workspace_id"),
    )
    op.create_index(
        "ix_source_policy_revision_latest",
        "source_policy_revision",
        ["workspace_id", "source_id", "sequence"],
    )
    op.create_index(
        "ix_source_policy_revision_validity",
        "source_policy_revision",
        ["workspace_id", "state", "valid_until"],
    )

    op.create_table(
        "source_quota_event",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("policy_sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_requests", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost", sa.Numeric(384, 128), nullable=False),
        sa.Column("cost_unit", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 240", name="source_quota_key_valid"
        ),
        sa.CheckConstraint(
            "purpose IN ('metadata', 'display', 'embedding', 'training', 'redistribution')",
            name="source_quota_purpose_valid",
        ),
        sa.CheckConstraint("window_started_at <= occurred_at", name="source_quota_window_ordered"),
        sa.CheckConstraint("reserved_requests > 0", name="source_quota_reserved_requests_positive"),
        sa.CheckConstraint("reserved_bytes >= 0", name="source_quota_reserved_bytes_nonnegative"),
        sa.CheckConstraint(
            "reserved_cost >= 0 AND reserved_cost <> 'NaN'::numeric",
            name="source_quota_reserved_cost_nonnegative",
        ),
        sa.CheckConstraint("length(cost_unit) > 0", name="source_quota_cost_unit_present"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "policy_sequence"],
            [
                "source_policy_revision.workspace_id",
                "source_policy_revision.source_id",
                "source_policy_revision.sequence",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_quota_event")),
        sa.UniqueConstraint(
            "workspace_id", "source_id", "idempotency_key", name="uq_source_quota_idempotency"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_source_quota_workspace_id"),
    )
    op.create_index(
        "ix_source_quota_window",
        "source_quota_event",
        ["workspace_id", "source_id", "window_started_at", "occurred_at"],
    )
    op.create_index(
        "ix_source_quota_policy",
        "source_quota_event",
        ["workspace_id", "source_id", "policy_sequence"],
    )

    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" '
            f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
        )
    op.execute(
        "CREATE POLICY source_policy_insert ON source_policy_revision "
        f"FOR INSERT TO {ADMIN} WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY source_quota_insert ON source_quota_event "
        f"FOR INSERT TO {INGESTION} WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        """
        CREATE FUNCTION public.reject_source_registry_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'WP05_SOURCE_HISTORY_APPEND_ONLY';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_source_policy_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous_sequence integer;
            previous_state text;
            transition_allowed boolean := false;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'research-source:' || NEW.workspace_id::text || ':' || NEW.source_id,
                    0
                )
            );
            SELECT sequence, state
              INTO previous_sequence, previous_state
              FROM source_policy_revision
             WHERE workspace_id = NEW.workspace_id
               AND source_id = NEW.source_id
             ORDER BY sequence DESC
             LIMIT 1;

            IF previous_sequence IS NULL THEN
                IF NEW.sequence <> 1 THEN
                    RAISE EXCEPTION 'WP05_POLICY_SEQUENCE_INVALID';
                END IF;
                IF NEW.state <> 'discovered' THEN
                    RAISE EXCEPTION 'WP05_POLICY_FIRST_REVISION_MUST_BE_DISCOVERED';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.sequence <> previous_sequence + 1 THEN
                RAISE EXCEPTION 'WP05_POLICY_SEQUENCE_INVALID';
            END IF;
            transition_allowed := CASE previous_state
                WHEN 'discovered' THEN NEW.state IN ('rights_pending', 'blocked')
                WHEN 'rights_pending' THEN NEW.state IN ('verified', 'blocked')
                WHEN 'verified' THEN NEW.state IN (
                    'rights_pending', 'blocked', 'revoked', 'expired'
                )
                WHEN 'blocked' THEN NEW.state = 'rights_pending'
                WHEN 'revoked' THEN NEW.state = 'rights_pending'
                WHEN 'expired' THEN NEW.state = 'rights_pending'
                ELSE false
            END;
            IF NOT transition_allowed THEN
                RAISE EXCEPTION 'WP05_POLICY_TRANSITION_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in _TABLES:
        op.execute(
            f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.reject_source_registry_mutation()"
        )
    op.execute(
        "CREATE TRIGGER source_policy_revision_transition "
        "BEFORE INSERT ON source_policy_revision FOR EACH ROW "
        "EXECUTE FUNCTION public.enforce_source_policy_transition()"
    )
    op.execute("REVOKE ALL ON FUNCTION public.reject_source_registry_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.enforce_source_policy_transition() FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON TABLE source_policy_revision, source_quota_event FROM PUBLIC, crypidian_app"
    )
    op.execute(f"GRANT SELECT ON TABLE source_policy_revision TO {ADMIN}, {INGESTION}")
    op.execute(f"GRANT INSERT ON TABLE source_policy_revision TO {ADMIN}")
    op.execute(f"GRANT SELECT ON TABLE source_quota_event TO {ADMIN}, {INGESTION}")
    op.execute(f"GRANT INSERT ON TABLE source_quota_event TO {INGESTION}")


def downgrade() -> None:
    op.execute('LOCK TABLE "source_policy_revision", "source_quota_event" IN ACCESS EXCLUSIVE MODE')
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_policy_revision)
               OR EXISTS (SELECT 1 FROM source_quota_event) THEN
                RAISE EXCEPTION 'WP05_SOURCE_HISTORY_DOWNGRADE_BLOCKED';
            END IF;
        END;
        $$
        """
    )
    for table in _TABLES:
        op.execute(f'REVOKE ALL ON TABLE "{table}" FROM {ADMIN}, {INGESTION}, crypidian_app')
        op.execute(f'DROP TRIGGER "{table}_append_only" ON "{table}"')
        op.execute(
            f'DROP POLICY source_policy_insert ON "{table}"'
        ) if table == "source_policy_revision" else None
        op.execute(
            f'DROP POLICY source_quota_insert ON "{table}"'
        ) if table == "source_quota_event" else None
        if table == "source_policy_revision":
            op.execute('DROP TRIGGER source_policy_revision_transition ON "source_policy_revision"')
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute("DROP FUNCTION public.reject_source_registry_mutation()")
    op.execute("DROP FUNCTION public.enforce_source_policy_transition()")
    op.drop_index("ix_source_quota_policy", table_name="source_quota_event")
    op.drop_index("ix_source_quota_window", table_name="source_quota_event")
    op.drop_table("source_quota_event")
    op.drop_index("ix_source_policy_revision_validity", table_name="source_policy_revision")
    op.drop_index("ix_source_policy_revision_latest", table_name="source_policy_revision")
    op.drop_table("source_policy_revision")
