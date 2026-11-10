"""WP06: sổ kiểm toán triển khai scorer và con trỏ triển khai theo workspace."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e7a9b1d3f5"
down_revision: str | None = "b3d5f7a9c1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT = "learning_scoring_deployment_event"
POINTER = "learning_scoring_deployment_pointer"
ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_learning_scorer_artifact_workspace_id",
        "learning_scorer_artifact",
        ["workspace_id", "id"],
    )

    op.create_table(
        EVENT,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("registry_id", sa.Uuid(), nullable=True),
        sa.Column("registry_revision", sa.Integer(), nullable=True),
        sa.Column("active_pointer_version", sa.Integer(), nullable=False),
        sa.Column("scorer_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("scorer_artifact_hash", sa.String(64), nullable=True),
        sa.Column("evaluation_report_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_report_hash", sa.String(64), nullable=True),
        sa.Column("evaluation_status", sa.String(40), nullable=True),
        sa.Column("proof_id", sa.Uuid(), nullable=True),
        sa.Column("proof_hash", sa.String(64), nullable=True),
        sa.Column("candidate_completeness_verified", sa.Boolean(), nullable=True),
        sa.Column("report_scope_hash", sa.String(64), nullable=True),
        sa.Column("proof_scope_hash", sa.String(64), nullable=True),
        sa.Column("policy_id", sa.String(128), nullable=True),
        sa.Column("policy_hash", sa.String(64), nullable=True),
        sa.Column("current_rights_receipt_hash", sa.String(64), nullable=True),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mfa_method", sa.String(16), nullable=False),
        sa.Column("expected_pointer_version", sa.Integer(), nullable=False),
        sa.Column("expected_deployment_revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("previous_event_id", sa.Uuid(), nullable=True),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("canonical_event", sa.Text(), nullable=False),
        sa.Column("event_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "mode IN ('DISABLED','SHADOW','PAPER_GATE')",
            name="learning_scoring_deployment_mode_valid",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND expected_pointer_version >= 0 "
            "AND expected_deployment_revision >= 0 "
            "AND revision = expected_deployment_revision + 1",
            name="learning_scoring_deployment_revision_valid",
        ),
        sa.CheckConstraint(
            "mfa_method = 'TOTP' AND mfa_verified_at IS NOT NULL",
            name="learning_scoring_deployment_mfa_valid",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 12 AND 2000 "
            "AND event_hash ~ '^[0-9a-f]{64}$' "
            "AND intent_hash ~ '^[0-9a-f]{64}$' "
            "AND (current_rights_receipt_hash IS NULL "
            "OR current_rights_receipt_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scoring_deployment_audit_valid",
        ),
        sa.CheckConstraint(
            "(previous_event_id IS NULL AND previous_event_hash IS NULL) OR "
            "(previous_event_id IS NOT NULL AND previous_event_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scoring_deployment_previous_pair_valid",
        ),
        sa.CheckConstraint(
            "(mode = 'DISABLED' AND registry_id IS NULL AND registry_revision IS NULL "
            "AND scorer_artifact_id IS NULL AND scorer_artifact_hash IS NULL "
            "AND evaluation_report_id IS NULL AND evaluation_report_hash IS NULL "
            "AND evaluation_status IS NULL AND proof_id IS NULL AND proof_hash IS NULL "
            "AND candidate_completeness_verified IS NULL AND report_scope_hash IS NULL "
            "AND proof_scope_hash IS NULL AND policy_id IS NULL AND policy_hash IS NULL "
            "AND current_rights_receipt_hash IS NULL) OR "
            "(mode IN ('SHADOW','PAPER_GATE') AND registry_id IS NOT NULL "
            "AND registry_revision >= 1 AND scorer_artifact_id IS NOT NULL "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND evaluation_report_id IS NOT NULL "
            "AND evaluation_report_hash ~ '^[0-9a-f]{64}$' "
            "AND evaluation_status IN ('ACCEPTED_SHADOW_ONLY',"
            "'ACCEPTED_PAPER_GATE_ELIGIBLE') AND proof_id IS NOT NULL "
            "AND proof_hash ~ '^[0-9a-f]{64}$' "
            "AND candidate_completeness_verified IS NOT NULL "
            "AND report_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND proof_scope_hash = report_scope_hash "
            "AND policy_id IS NOT NULL AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scoring_deployment_target_valid",
        ),
        sa.CheckConstraint(
            "mode <> 'PAPER_GATE' OR "
            "(evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE' "
            "AND candidate_completeness_verified IS TRUE)",
            name="learning_scoring_deployment_paper_gate_valid",
        ),
        sa.CheckConstraint(
            "event_payload ? 'schema_version' "
            "AND NOT (event_payload ?| ARRAY['features','feature_values','labels',"
            "'sample_ids','test_payload','raw_source'])",
            name="learning_scoring_deployment_metadata_only",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_registry",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scorer_artifact_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_artifact",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evaluation_report_id"],
            [
                "learning_scorer_test_evaluation_report.workspace_id",
                "learning_scorer_test_evaluation_report.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_report",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_proof",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_actor",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "previous_event_id"],
            [f"{EVENT}.workspace_id", f"{EVENT}.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_previous",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_scoring_deployment_event")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_scoring_deployment_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_scoring_deployment_key"
        ),
        sa.UniqueConstraint(
            "workspace_id", "revision", name="uq_learning_scoring_deployment_revision"
        ),
    )
    op.create_index(
        "ix_learning_scoring_deployment_workspace_created", EVENT, ["workspace_id", "created_at"]
    )

    op.create_table(
        POINTER,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_pointer_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "revision >= 0 AND active_pointer_version >= 0",
            name="learning_scoring_deployment_pointer_versions_valid",
        ),
        sa.CheckConstraint(
            "mode IN ('DISABLED','SHADOW','PAPER_GATE')",
            name="learning_scoring_deployment_pointer_mode_valid",
        ),
        sa.CheckConstraint(
            "(revision = 0 AND event_id IS NULL AND mode = 'DISABLED') OR "
            "(revision > 0 AND event_id IS NOT NULL)",
            name="learning_scoring_deployment_pointer_initial_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "event_id"],
            [f"{EVENT}.workspace_id", f"{EVENT}.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_pointer_event",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", name=op.f("pk_learning_scoring_deployment_pointer")
        ),
    )

    op.execute(f"ALTER TABLE public.{EVENT} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{EVENT} FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{POINTER} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{POINTER} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"REVOKE ALL ON TABLE public.{EVENT}, public.{POINTER} FROM PUBLIC, {ADMIN}, {RUNTIME}"
    )
    for table in (EVENT, POINTER):
        op.execute(
            f"CREATE POLICY {table}_admin_scope ON public.{table} "
            f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
        )
    op.execute(
        f"CREATE POLICY {EVENT}_admin_insert ON public.{EVENT} "
        f"FOR INSERT TO {ADMIN} WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY {EVENT}_runtime_scope ON public.{EVENT} "
        f"FOR SELECT TO {RUNTIME} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY {POINTER}_runtime_scope ON public.{POINTER} "
        f"FOR SELECT TO {RUNTIME} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{EVENT} TO {ADMIN}")
    op.execute(f"GRANT SELECT ON TABLE public.{EVENT}, public.{POINTER} TO {RUNTIME}")
    op.execute(f"GRANT SELECT ON TABLE public.{POINTER} TO {ADMIN}")

    op.execute("""
        CREATE FUNCTION public.wp06_scoring_deployment_payload_has_forbidden(value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE item record;
        DECLARE forbidden text[] := ARRAY[
            'features','feature_values','labels','sample_ids','sample_id',
            'test_payload','raw_source','outcomes','trade_rows'
        ];
        BEGIN
          IF pg_catalog.jsonb_typeof(value) = 'object' THEN
            FOR item IN SELECT key, val FROM pg_catalog.jsonb_each(value) AS pair(key,val)
            LOOP
              IF item.key = ANY(forbidden)
                 OR public.wp06_scoring_deployment_payload_has_forbidden(item.val) THEN
                RETURN true;
              END IF;
            END LOOP;
          ELSIF pg_catalog.jsonb_typeof(value) = 'array' THEN
            FOR item IN SELECT val FROM pg_catalog.jsonb_array_elements(value) AS pair(val)
            LOOP
              IF public.wp06_scoring_deployment_payload_has_forbidden(item.val) THEN
                RETURN true;
              END IF;
            END LOOP;
          END IF;
          RETURN false;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_scoring_deployment_payload_has_forbidden(jsonb) FROM PUBLIC"
    )
    op.execute(f"""
        CREATE FUNCTION public.wp06_scoring_deployment_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE pointer public.learning_scoring_deployment_pointer%ROWTYPE;
        DECLARE registry_pointer public.learning_model_active_pointer%ROWTYPE;
        DECLARE registry public.learning_model_registry%ROWTYPE;
        DECLARE artifact public.learning_scorer_artifact%ROWTYPE;
        DECLARE report_row public.learning_scorer_test_evaluation_report%ROWTYPE;
        DECLARE proof public.learning_scorer_test_opportunity_proof%ROWTYPE;
        DECLARE target jsonb;
        DECLARE now_at timestamptz := pg_catalog.clock_timestamp();
        BEGIN
          IF session_user <> '{ADMIN}' THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_CALLER_DENIED';
          END IF;
          IF NEW.workspace_id IS DISTINCT FROM
             NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_WORKSPACE_DENIED';
          END IF;
          IF NEW.event_payload IS DISTINCT FROM NEW.canonical_event::jsonb
             OR pg_catalog.encode(pg_catalog.sha256(
                  pg_catalog.convert_to(NEW.canonical_event, 'UTF8')), 'hex')
                  IS DISTINCT FROM NEW.event_hash
             OR public.wp06_scoring_deployment_payload_has_forbidden(NEW.event_payload)
             OR NEW.event_payload->>'schema_version' IS DISTINCT FROM
                  'crypidian.learning.scorer-deployment.v1'
             OR NEW.event_payload->>'event_id' IS DISTINCT FROM NEW.id::text
             OR NEW.event_payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
             OR NEW.event_payload->>'mode' IS DISTINCT FROM NEW.mode
             OR (NEW.event_payload->>'revision')::integer IS DISTINCT FROM NEW.revision
             OR (NEW.event_payload->>'expected_pointer_version')::integer
                  IS DISTINCT FROM NEW.expected_pointer_version
             OR (NEW.event_payload->>'expected_deployment_revision')::integer
                  IS DISTINCT FROM NEW.expected_deployment_revision
             OR NEW.event_payload->>'actor_membership_id' IS DISTINCT FROM
                  NEW.actor_membership_id::text
             OR NEW.event_payload->>'mfa_method' IS DISTINCT FROM NEW.mfa_method
             OR (NEW.event_payload->>'mfa_verified_at')::timestamptz
                  IS DISTINCT FROM NEW.mfa_verified_at
             OR NEW.event_payload->>'reason' IS DISTINCT FROM NEW.reason
             OR NEW.event_payload->>'idempotency_key' IS DISTINCT FROM NEW.idempotency_key::text
             OR NEW.event_payload->>'intent_hash' IS DISTINCT FROM NEW.intent_hash
             OR NEW.active_pointer_version IS DISTINCT FROM NEW.expected_pointer_version THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_CANONICAL_INVALID';
          END IF;
          IF NEW.mfa_method <> 'TOTP' OR NEW.mfa_verified_at > now_at
             OR NEW.mfa_verified_at < now_at - interval '15 minutes' THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_MFA_REQUIRED';
          END IF;
          IF NEW.created_at > now_at THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_CREATED_AT_FUTURE';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.user_membership member
            JOIN public.role_binding binding
              ON binding.workspace_id = member.workspace_id
             AND binding.membership_id = member.id
            WHERE member.workspace_id = NEW.workspace_id
              AND member.id = NEW.actor_membership_id AND member.status = 'ACTIVE'
              AND binding.role IN ('owner','analyst_reviewer')
          ) THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_ACTOR_INVALID';
          END IF;

          PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
            'learning-scoring-deployment:' || NEW.workspace_id::text ||
            chr(58) || 'current-pointer', 0
          ));
          SELECT * INTO pointer FROM public.learning_scoring_deployment_pointer p
           WHERE p.workspace_id = NEW.workspace_id FOR UPDATE;
          IF NOT FOUND OR pointer.revision <> NEW.expected_deployment_revision
             OR pointer.revision + 1 <> NEW.revision
             OR pointer.event_id IS DISTINCT FROM NEW.previous_event_id THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_STALE_REVISION';
          END IF;
          IF NEW.previous_event_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.learning_scoring_deployment_event e
             WHERE e.workspace_id = NEW.workspace_id AND e.id = NEW.previous_event_id
               AND e.event_hash = NEW.previous_event_hash
          ) THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_PREDECESSOR_MISMATCH';
          END IF;
          IF NEW.mode = 'DISABLED' THEN
            IF NEW.event_payload->'target' IS DISTINCT FROM 'null'::jsonb THEN
              RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_DISABLED_TARGET_INVALID';
            END IF;
            SELECT * INTO registry_pointer FROM public.learning_model_active_pointer p
             WHERE p.workspace_id = NEW.workspace_id FOR KEY SHARE;
            IF NOT FOUND THEN
              IF NEW.expected_pointer_version <> 0 THEN
                RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_REGISTRY_POINTER_MISSING';
              END IF;
            ELSIF registry_pointer.version <> NEW.expected_pointer_version THEN
              RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_STALE_REGISTRY_POINTER';
            END IF;
            RETURN NEW;
          END IF;

          -- Current TRAINING bundle rights and WP05 exact source references are not
          -- yet available through one safe, non-consuming deployment gateway.
          -- Refuse every active mode until that gateway can revalidate rights under
          -- the source lock; metadata captured in an earlier report is insufficient.
          RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_RIGHTS_GATE_UNAVAILABLE';

          target := NEW.event_payload->'target';
          IF target IS NULL OR target = 'null'::jsonb
             OR target->>'registry_id' IS DISTINCT FROM NEW.registry_id::text
             OR (target->>'registry_revision')::integer IS DISTINCT FROM NEW.registry_revision
             OR (target->>'active_pointer_version')::integer
                  IS DISTINCT FROM NEW.active_pointer_version
             OR target->>'scorer_artifact_id' IS DISTINCT FROM NEW.scorer_artifact_id::text
             OR target->>'scorer_artifact_hash' IS DISTINCT FROM NEW.scorer_artifact_hash
             OR target->>'evaluation_report_id' IS DISTINCT FROM NEW.evaluation_report_id::text
             OR target->>'evaluation_report_hash' IS DISTINCT FROM NEW.evaluation_report_hash
             OR target->>'evaluation_status' IS DISTINCT FROM NEW.evaluation_status
             OR target->>'proof_id' IS DISTINCT FROM NEW.proof_id::text
             OR target->>'proof_hash' IS DISTINCT FROM NEW.proof_hash
             OR target->>'report_scope_hash' IS DISTINCT FROM NEW.report_scope_hash
             OR target->>'proof_scope_hash' IS DISTINCT FROM NEW.proof_scope_hash
             OR target->>'policy_id' IS DISTINCT FROM NEW.policy_id
             OR target->>'policy_hash' IS DISTINCT FROM NEW.policy_hash
             OR target->>'current_rights_receipt_hash'
                  IS DISTINCT FROM NEW.current_rights_receipt_hash
             OR (target->>'candidate_completeness_verified')::boolean
                  IS DISTINCT FROM NEW.candidate_completeness_verified THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_TARGET_CANONICAL_INVALID';
          END IF;
          IF registry_pointer.version <> NEW.active_pointer_version
             OR registry_pointer.registry_id IS DISTINCT FROM NEW.registry_id THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_REGISTRY_POINTER_MISMATCH';
          END IF;

          SELECT * INTO registry FROM public.learning_model_registry r
           WHERE r.workspace_id = NEW.workspace_id AND r.id = NEW.registry_id FOR KEY SHARE;
          SELECT * INTO artifact FROM public.learning_scorer_artifact a
           WHERE a.workspace_id = NEW.workspace_id AND a.id = NEW.scorer_artifact_id FOR KEY SHARE;
          SELECT * INTO report_row FROM public.learning_scorer_test_evaluation_report r
           WHERE r.workspace_id = NEW.workspace_id AND r.id = NEW.evaluation_report_id
           FOR KEY SHARE;
          SELECT * INTO proof FROM public.learning_scorer_test_opportunity_proof p
           WHERE p.workspace_id = NEW.workspace_id AND p.id = NEW.proof_id FOR KEY SHARE;
          IF NOT FOUND OR registry.id IS NULL OR artifact.id IS NULL OR report_row.id IS NULL
             OR proof.id IS NULL OR registry.state <> 'paper_active'
             OR registry.revision <> NEW.registry_revision
             OR registry.artifact_id <> artifact.base_artifact_id
             OR registry.artifact_hash <> artifact.base_artifact_hash
             OR registry.approval_hash <> artifact.approval_hash
             OR artifact.artifact_hash <> NEW.scorer_artifact_hash
             OR artifact.state <> 'ready_for_evaluation'
             OR report_row.report_hash <> NEW.evaluation_report_hash
             OR report_row.scorer_artifact_hash <> artifact.artifact_hash
             OR report_row.evaluation_status <> NEW.evaluation_status
             OR report_row.policy_version <> NEW.policy_id
             OR report_row.policy_hash <> NEW.policy_hash
             OR report_row.proof_id <> NEW.proof_id
             OR report_row.proof_hash <> NEW.proof_hash
             OR report_row.source_scope_hash <> NEW.report_scope_hash
             OR report_row.current_rights_receipt_hash <> NEW.current_rights_receipt_hash
             OR report_row.candidate_completeness_verified
                  IS DISTINCT FROM NEW.candidate_completeness_verified
             OR proof.proof_hash <> NEW.proof_hash
             OR proof.source_scope_hash <> NEW.proof_scope_hash
             OR proof.completeness_verified
                  IS DISTINCT FROM NEW.candidate_completeness_verified THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_LINEAGE_MISMATCH';
          END IF;
          IF NEW.evaluation_status NOT IN (
               'ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE') THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_REPORT_NOT_ACCEPTED';
          END IF;
          IF NEW.mode = 'PAPER_GATE'
             AND (NEW.evaluation_status <> 'ACCEPTED_PAPER_GATE_ELIGIBLE'
                  OR NEW.candidate_completeness_verified IS NOT TRUE) THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_PAPER_GATE_INELIGIBLE';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_scoring_deployment_apply()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        BEGIN
          PERFORM pg_catalog.set_config('app.wp06_scoring_deployment_apply', NEW.id::text, true);
          UPDATE public.learning_scoring_deployment_pointer
             SET event_id = NEW.id, revision = NEW.revision,
                 active_pointer_version = NEW.active_pointer_version, mode = NEW.mode,
                 changed_at = NEW.created_at
           WHERE workspace_id = NEW.workspace_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_POINTER_MISSING';
          END IF;
          PERFORM pg_catalog.set_config('app.wp06_scoring_deployment_apply', '', true);
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_scoring_deployment_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_EVENT_IMMUTABLE'; END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_scoring_deployment_pointer_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR
             pg_catalog.current_setting('app.wp06_scoring_deployment_apply', true)
                IS DISTINCT FROM NEW.event_id::text THEN
            RAISE EXCEPTION 'WP06_SCORING_DEPLOYMENT_POINTER_IMMUTABLE';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_scoring_deployment_workspace_seed()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        BEGIN
          INSERT INTO public.{POINTER}(
            workspace_id, event_id, revision, active_pointer_version, mode
          ) VALUES (NEW.id, NULL, 0, 0, 'DISABLED')
          ON CONFLICT (workspace_id) DO NOTHING;
          RETURN NEW;
        END $$
    """)
    for signature in (
        "wp06_scoring_deployment_guard()",
        "wp06_scoring_deployment_apply()",
        "wp06_scoring_deployment_immutable()",
        "wp06_scoring_deployment_pointer_guard()",
        "wp06_scoring_deployment_workspace_seed()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {EVENT}_guard BEFORE INSERT ON public.{EVENT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_scoring_deployment_guard()"
    )
    op.execute(
        f"CREATE TRIGGER {EVENT}_apply AFTER INSERT ON public.{EVENT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_scoring_deployment_apply()"
    )
    op.execute(
        f"CREATE TRIGGER {EVENT}_immutable BEFORE UPDATE OR DELETE ON public.{EVENT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_scoring_deployment_immutable()"
    )
    op.execute(
        f"CREATE TRIGGER {POINTER}_guard BEFORE UPDATE OR DELETE ON public.{POINTER} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_scoring_deployment_pointer_guard()"
    )
    op.execute(
        "CREATE TRIGGER workspace_scoring_deployment_seed AFTER INSERT ON public.workspace "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_scoring_deployment_workspace_seed()"
    )
    op.execute(f"""
        INSERT INTO public.{POINTER}(workspace_id, event_id, revision, active_pointer_version, mode)
        SELECT id, NULL, 0, 0, 'DISABLED' FROM public.workspace
        ON CONFLICT (workspace_id) DO NOTHING
    """)


def downgrade() -> None:
    event_count = (
        op.get_bind().execute(sa.text(f"SELECT count(*) FROM public.{EVENT}")).scalar_one()
    )
    revision_count = (
        op.get_bind()
        .execute(sa.text(f"SELECT count(*) FROM public.{POINTER} WHERE revision > 0"))
        .scalar_one()
    )
    if event_count or revision_count:
        raise RuntimeError(
            "WP06_SCORING_DEPLOYMENT_POPULATED_DOWNGRADE_DENIED; deployment history exists"
        )
    op.execute("DROP TRIGGER workspace_scoring_deployment_seed ON public.workspace")
    op.execute(f"DROP TRIGGER {POINTER}_guard ON public.{POINTER}")
    op.execute(f"DROP TRIGGER {EVENT}_immutable ON public.{EVENT}")
    op.execute(f"DROP TRIGGER {EVENT}_apply ON public.{EVENT}")
    op.execute(f"DROP TRIGGER {EVENT}_guard ON public.{EVENT}")
    for function in (
        "wp06_scoring_deployment_workspace_seed",
        "wp06_scoring_deployment_pointer_guard",
        "wp06_scoring_deployment_immutable",
        "wp06_scoring_deployment_apply",
        "wp06_scoring_deployment_guard",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
    op.execute("DROP FUNCTION public.wp06_scoring_deployment_payload_has_forbidden(jsonb)")
    op.drop_table(POINTER)
    op.drop_index("ix_learning_scoring_deployment_workspace_created", table_name=EVENT)
    op.drop_table(EVENT)
    op.drop_constraint(
        "uq_learning_scorer_artifact_workspace_id",
        "learning_scorer_artifact",
        type_="unique",
    )
