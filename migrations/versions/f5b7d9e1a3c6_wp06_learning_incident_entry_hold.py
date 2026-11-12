"""WP06: ledger incident append-only và projection entry theo workspace."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9e1a3c6"
down_revision: str | None = "f4a6c8e0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_incident_entry_hold_event"
READER = "crypidian_learning_incident_reader"
ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_STRIP_WHITESPACE_CODEPOINTS = (
    9,
    10,
    11,
    12,
    13,
    28,
    29,
    30,
    31,
    32,
    133,
    160,
    5760,
    *range(8192, 8203),
    8232,
    8233,
    8239,
    8287,
    12288,
)


def _strip_whitespace_sql() -> str:
    """Tập ký tự tương ứng với khoảng trắng mà `str.strip()` loại bỏ."""
    return " || ".join(f"pg_catalog.chr({codepoint})" for codepoint in _STRIP_WHITESPACE_CODEPOINTS)


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE learning_incident_entry_hold_event (
    workspace_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    proposal_hash VARCHAR(64) NOT NULL,
    assessment_id UUID NOT NULL,
    assessment_hash VARCHAR(64) NOT NULL,
    action VARCHAR(16) NOT NULL,
    revision INTEGER NOT NULL,
    previous_event_id UUID,
    previous_event_hash VARCHAR(64),
    incumbent_registry_id UUID NOT NULL,
    incumbent_registry_revision INTEGER NOT NULL,
    incumbent_artifact_hash VARCHAR(64) NOT NULL,
    active_pointer_version INTEGER NOT NULL,
    deployment_revision INTEGER NOT NULL,
    actor_membership_id UUID NOT NULL,
    mfa_verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    mfa_method VARCHAR(16) NOT NULL,
    reason VARCHAR(500) NOT NULL,
    idempotency_key UUID NOT NULL,
    request_id VARCHAR(200) NOT NULL,
    intent_hash VARCHAR(64) NOT NULL,
    event_hash VARCHAR(64) NOT NULL,
    canonical_event TEXT NOT NULL,
    event_payload JSONB NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_learning_incident_entry_hold_event PRIMARY KEY (id),
    CONSTRAINT fk_learning_incident_entry_hold_event_workspace_id_workspace FOREIGN
        KEY(workspace_id) REFERENCES workspace (id) ON DELETE RESTRICT,
    CONSTRAINT fk_learning_incident_proposal FOREIGN KEY(workspace_id, proposal_id) REFERENCES
        learning_retrain_proposal (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_learning_incident_assessment FOREIGN KEY(workspace_id, assessment_id,
        assessment_hash) REFERENCES learning_drift_assessment (workspace_id, id, content_hash)
        ON DELETE RESTRICT,
    CONSTRAINT fk_learning_incident_registry FOREIGN KEY(workspace_id, incumbent_registry_id)
        REFERENCES learning_model_registry (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_learning_incident_actor FOREIGN KEY(workspace_id, actor_membership_id)
        REFERENCES user_membership (workspace_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_learning_incident_predecessor FOREIGN KEY(workspace_id, proposal_id,
        previous_event_id, previous_event_hash) REFERENCES learning_incident_entry_hold_event
        (workspace_id, proposal_id, id, event_hash) ON DELETE RESTRICT,
    CONSTRAINT uq_learning_incident_revision UNIQUE (workspace_id, proposal_id, revision),
    CONSTRAINT uq_learning_incident_idempotency UNIQUE (workspace_id, idempotency_key),
    CONSTRAINT uq_learning_incident_successor UNIQUE (workspace_id, previous_event_id),
    CONSTRAINT uq_learning_incident_exact_event UNIQUE (workspace_id, proposal_id, id,
        event_hash),
    CONSTRAINT ck_learning_incident_entry_hold_event_learning_incident_ba51 CHECK ((action =
        'HOLD' AND revision = 1 AND previous_event_id IS NULL AND previous_event_hash IS NULL)
        OR (action = 'RELEASE' AND revision = 2 AND previous_event_id IS NOT NULL AND
        previous_event_hash IS NOT NULL AND previous_event_id <> id)),
    CONSTRAINT ck_learning_incident_entry_hold_event_learning_incident_e735 CHECK
        (proposal_hash ~ '^[0-9a-f]{64}$' AND assessment_hash ~ '^[0-9a-f]{64}$' AND
        incumbent_artifact_hash ~ '^[0-9a-f]{64}$' AND event_hash ~ '^[0-9a-f]{64}$' AND
        intent_hash ~ '^[0-9a-f]{64}$' AND (previous_event_hash IS NULL OR previous_event_hash
        ~ '^[0-9a-f]{64}$' )),
    CONSTRAINT ck_learning_incident_entry_hold_event_learning_incident_86ae CHECK
        (incumbent_registry_revision >= 1 AND active_pointer_version >= 1 AND
        deployment_revision >= 1),
    CONSTRAINT ck_learning_incident_entry_hold_event_learning_incident_109a CHECK
        (length(btrim(reason, __WP06_TRIM_CHARS__)) BETWEEN 1 AND 500 AND
        length(reason) <= 500 AND
        length(btrim(request_id, __WP06_TRIM_CHARS__)) BETWEEN 1 AND 200 AND
        mfa_method = 'TOTP' AND mfa_verified_at
        <= created_at AND created_at - mfa_verified_at <= interval '15 minutes' )
)

""".replace("__WP06_TRIM_CHARS__", f"({_strip_whitespace_sql()})")
    )
    op.execute(
        """CREATE INDEX ix_learning_incident_workspace_proposal
        ON learning_incident_entry_hold_event (workspace_id, proposal_id, revision)"""
    )
    _create_privileges()
    _create_locks()
    _create_eligibility()
    _create_guard()
    _create_projection()


def _create_privileges() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{READER}') THEN
            CREATE ROLE {READER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles role WHERE role.rolname='{READER}'
             AND (role.rolcanlogin OR role.rolsuper OR role.rolbypassrls OR role.rolinherit
                  OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication
                  OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members membership
                       WHERE membership.roleid=role.oid OR membership.member=role.oid))
        ) THEN RAISE EXCEPTION 'WP06_INCIDENT_READER_ROLE_UNSAFE'; END IF;
    END $$""")
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, {ADMIN}, {RUNTIME}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(
        f"CREATE POLICY learning_incident_admin_read ON public.{TABLE} FOR SELECT TO "
        f"{ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_incident_admin_insert ON public.{TABLE} FOR INSERT TO "
        f"{ADMIN} WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
    op.execute(f"GRANT SELECT (workspace_id, proposal_id, action) ON public.{TABLE} TO {READER}")
    op.execute(
        f"CREATE POLICY learning_incident_projection_read ON public.{TABLE} FOR SELECT "
        f"TO {READER} USING ({WORKSPACE})"
    )


def _create_locks() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_lock_learning_incident_scope(
            target_workspace uuid, target_registry uuid
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> 'crypidian_admin_web' OR target_workspace IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_INCIDENT_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_INCIDENT_READ_COMMITTED_REQUIRED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'workspace-settings:' || target_workspace::text, 0));
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-scoring-deployment:' || target_workspace::text || chr(58) ||
                    'current-pointer' , 0));
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-model-registry:' || target_workspace::text || chr(58) ||
                    'active-pointer' , 0));
            IF target_registry IS NULL THEN
                SELECT pointer.registry_id INTO target_registry
                  FROM public.learning_model_active_pointer pointer
                 WHERE pointer.workspace_id = target_workspace;
            END IF;
            IF target_registry IS NOT NULL THEN
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'learning-model-registry:' || target_workspace::text || chr(58) ||
                        'registry-entry' || chr(58)
                    || target_registry::text, 0));
            END IF;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_lock_learning_incident_scope(uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_lock_learning_incident_scope(uuid,uuid) TO {ADMIN}"
    )


def _create_eligibility() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_learning_incident_eligible_target(
            target_workspace uuid, target_proposal uuid
        ) RETURNS TABLE (
            incumbent_registry_id uuid, incumbent_registry_revision integer,
            incumbent_artifact_hash text, active_pointer_version integer,
            deployment_revision integer
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            proposal public.learning_retrain_proposal%ROWTYPE;
            assessment public.learning_drift_assessment%ROWTYPE;
            registry public.learning_model_registry%ROWTYPE;
            incumbent record;
            resolved record;
            membership_count integer;
            tuple_count integer;
        BEGIN
            PERFORM public.wp06_lock_learning_incident_scope(target_workspace, NULL);
            SELECT * INTO proposal FROM public.learning_retrain_proposal p
             WHERE p.workspace_id = target_workspace AND p.id = target_proposal;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_INCIDENT_PROPOSAL_MISSING'; END IF;
            SELECT * INTO assessment FROM public.learning_drift_assessment a
             WHERE a.workspace_id = target_workspace AND a.id = proposal.assessment_id;
            IF NOT FOUND OR proposal.assessment_hash IS DISTINCT FROM assessment.content_hash
               OR proposal.membership_digest IS DISTINCT FROM assessment.membership_digest
               OR proposal.policy_version IS DISTINCT FROM assessment.policy_version
               OR assessment.data_quality_status <> 'detected'
               OR NOT (assessment.reason_codes ?| ARRAY[
                    'quality_below_threshold','coverage_below_threshold']) THEN
                RAISE EXCEPTION 'WP06_INCIDENT_ASSESSMENT_INELIGIBLE';
            END IF;
            SELECT count(*), count(DISTINCT (observation.active_registry_id,
                   observation.active_registry_revision, observation.active_artifact_hash,
                   observation.active_pointer_version))
              INTO membership_count, tuple_count
              FROM public.learning_drift_assessment_member member
              JOIN public.learning_shadow_observation observation
                ON observation.workspace_id = member.workspace_id
               AND observation.id = member.observation_id
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id;
            IF membership_count <> assessment.member_count OR membership_count = 0
               OR tuple_count <> 1 THEN
                RAISE EXCEPTION 'WP06_INCIDENT_MIXED_INCUMBENT';
            END IF;
            SELECT observation.active_registry_id AS registry_id,
                   observation.active_registry_revision AS registry_revision,
                   observation.active_artifact_hash AS artifact_hash,
                   observation.active_pointer_version AS pointer_version
              INTO incumbent
              FROM public.learning_drift_assessment_member member
              JOIN public.learning_shadow_observation observation
                ON observation.workspace_id = member.workspace_id
               AND observation.id = member.observation_id
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id LIMIT 1;
            -- Gateway giữ khóa target/quyền hiện hành; không nhận artifact payload từ caller.
            SELECT gateway.registry_id, gateway.registry_revision,
                   gateway.active_pointer_version, gateway.deployment_revision
              INTO resolved
              FROM public.wp06_resolve_scorer_runtime_target(
                  target_workspace, NULL::text, NULL::jsonb, NULL::uuid,
                  NULL::integer, false) gateway;
            IF NOT FOUND OR resolved.registry_id IS DISTINCT FROM incumbent.registry_id
               OR resolved.registry_revision IS DISTINCT FROM incumbent.registry_revision
               OR resolved.active_pointer_version IS DISTINCT FROM incumbent.pointer_version THEN
                RAISE EXCEPTION 'WP06_INCIDENT_TARGET_STALE';
            END IF;
            SELECT * INTO registry FROM public.learning_model_registry r
             WHERE r.workspace_id = target_workspace AND r.id = incumbent.registry_id;
            IF NOT FOUND OR registry.artifact_hash IS DISTINCT FROM incumbent.artifact_hash THEN
                RAISE EXCEPTION 'WP06_INCIDENT_TARGET_STALE';
            END IF;
            -- Cohort lock sau source locks của gateway, cùng thứ tự với drift evaluator.
            PERFORM public.wp06_lock_learning_shadow_cohort(
                target_workspace, assessment.cohort_registry_id, assessment.cohort_revision);
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment newer
                 WHERE newer.workspace_id = target_workspace
                   AND newer.cohort_registry_id = assessment.cohort_registry_id
                   AND newer.cohort_revision = assessment.cohort_revision
                   AND newer.policy_version = assessment.policy_version
                   AND (newer.created_at, newer.id) > (assessment.created_at, assessment.id)
            ) THEN RAISE EXCEPTION 'WP06_INCIDENT_LATEST_ASSESSMENT_REQUIRED'; END IF;
            RETURN QUERY SELECT incumbent.registry_id, incumbent.registry_revision,
                incumbent.artifact_hash::text, incumbent.pointer_version,
                resolved.deployment_revision;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_learning_incident_eligible_target(uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"public.wp06_learning_incident_eligible_target(uuid,uuid) TO {ADMIN}"
    )


def _create_guard() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_learning_incident_canonical(document jsonb)
        RETURNS text LANGUAGE sql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_compact_canonical_json(
                pg_catalog.json_object_agg(entry.key, entry.value ORDER BY entry.key COLLATE "C")
            ) FROM pg_catalog.jsonb_each(document) entry
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_learning_incident_canonical(jsonb) FROM PUBLIC")
    op.execute(
        """
        CREATE FUNCTION public.wp06_learning_incident_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            expected jsonb;
            intent jsonb;
            predecessor public.learning_incident_entry_hold_event%ROWTYPE;
            eligible record;
            now_at timestamptz;
        BEGIN
            IF session_user <> 'crypidian_admin_web' THEN
                RAISE EXCEPTION 'WP06_INCIDENT_CALLER_DENIED';
            END IF;
            IF NEW.action = 'HOLD' THEN
                PERFORM public.wp06_lock_learning_incident_scope(
                    NEW.workspace_id, NEW.incumbent_registry_id);
            ELSIF NEW.action = 'RELEASE' THEN
                IF NEW.workspace_id IS NULL OR NEW.workspace_id IS DISTINCT FROM
                        NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_WORKSPACE_DENIED';
                END IF;
                IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_READ_COMMITTED_REQUIRED';
                END IF;
                -- RELEASE chỉ cần tuần tự hóa với entry; không phụ thuộc scorer target.
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'workspace-settings:' || NEW.workspace_id::text, 0));
            ELSE
                RAISE EXCEPTION 'WP06_INCIDENT_ACTION_INVALID';
            END IF;
            expected := pg_catalog.jsonb_build_object(
                'action', NEW.action,
                'actor_membership_id', NEW.actor_membership_id::text,
                'active_pointer_version', NEW.active_pointer_version,
                'assessment_hash', NEW.assessment_hash,
                'assessment_id', NEW.assessment_id::text,
                'created_at', public.wp06_canonical_utc_timestamp_json(NEW.created_at)::jsonb,
                'deployment_revision', NEW.deployment_revision,
                'event_id', NEW.id::text,
                'expected_revision', NEW.revision - 1,
                'idempotency_key', NEW.idempotency_key::text,
                'incumbent_artifact_hash', NEW.incumbent_artifact_hash,
                'incumbent_registry_id', NEW.incumbent_registry_id::text,
                'incumbent_registry_revision', NEW.incumbent_registry_revision,
                'intent_hash', NEW.intent_hash,
                'mfa_method', NEW.mfa_method,
                'mfa_verified_at',
                    public.wp06_canonical_utc_timestamp_json(NEW.mfa_verified_at)::jsonb,
                'previous_event_hash', NEW.previous_event_hash,
                'previous_event_id', NEW.previous_event_id::text,
                'proposal_hash', NEW.proposal_hash,
                'proposal_id', NEW.proposal_id::text,
                'reason', NEW.reason,
                'request_id', NEW.request_id,
                'revision', NEW.revision,
                'schema_version', 'crypidian.learning.incident-entry-hold.v1',
                'workspace_id', NEW.workspace_id::text);
            intent := expected - ARRAY['created_at','event_id','intent_hash','mfa_method',
                'mfa_verified_at'
                    ,'previous_event_hash','previous_event_id','request_id','revision'];
            intent := pg_catalog.jsonb_set(intent, '{schema_version}',
                '"crypidian.learning.incident-entry-hold-intent.v1"'::jsonb);
            IF NEW.event_payload IS DISTINCT FROM expected
               OR NEW.canonical_event IS DISTINCT FROM
                   public.wp06_learning_incident_canonical(expected)
               OR NEW.event_hash IS DISTINCT FROM pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(NEW.canonical_event, 'UTF8')), 'hex')
               OR NEW.intent_hash IS DISTINCT FROM pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(public.wp06_learning_incident_canonical(intent),
                        'UTF8' )), 'hex' )
               OR NEW.reason <> pg_catalog.btrim(
                    NEW.reason, __WP06_TRIM_CHARS__)
               OR length(pg_catalog.btrim(
                    NEW.reason, __WP06_TRIM_CHARS__)) = 0
               OR NEW.id = '00000000-0000-0000-0000-000000000000'::uuid
               OR NEW.idempotency_key = '00000000-0000-0000-0000-000000000000'::uuid THEN
                RAISE EXCEPTION 'WP06_INCIDENT_CANONICAL_INVALID';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.user_membership member
                JOIN public.role_binding binding
                  ON binding.workspace_id = member.workspace_id AND binding.membership_id =
                      member.id
                 WHERE member.workspace_id = NEW.workspace_id
                   AND member.id = NEW.actor_membership_id AND member.status = 'ACTIVE'
                   AND binding.role IN ('owner','risk_admin')
            ) THEN RAISE EXCEPTION 'WP06_INCIDENT_ACTOR_INVALID'; END IF;
            SELECT * INTO predecessor FROM public.learning_incident_entry_hold_event event
             WHERE event.workspace_id = NEW.workspace_id AND event.proposal_id = NEW.proposal_id
             ORDER BY event.revision DESC LIMIT 1;
            IF NEW.action = 'HOLD' THEN
                IF FOUND OR NEW.revision <> 1 OR NEW.previous_event_id IS NOT NULL
                   OR NEW.previous_event_hash IS NOT NULL THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_STALE_REVISION';
                END IF;
                SELECT * INTO eligible FROM public.wp06_learning_incident_eligible_target(
                    NEW.workspace_id, NEW.proposal_id);
                IF NOT FOUND OR (eligible.incumbent_registry_id,
                    eligible.incumbent_registry_revision, eligible.incumbent_artifact_hash,
                    eligible.active_pointer_version, eligible.deployment_revision)
                    IS DISTINCT FROM (NEW.incumbent_registry_id,
                    NEW.incumbent_registry_revision, NEW.incumbent_artifact_hash,
                    NEW.active_pointer_version, NEW.deployment_revision)
                    OR NOT EXISTS (
                        SELECT 1 FROM public.learning_retrain_proposal proposal
                         WHERE proposal.workspace_id = NEW.workspace_id AND proposal.id =
                             NEW.proposal_id
                           AND proposal.content_hash = NEW.proposal_hash
                           AND proposal.assessment_id = NEW.assessment_id
                           AND proposal.assessment_hash = NEW.assessment_hash
                           AND proposal.created_at <= NEW.created_at
                    ) THEN RAISE EXCEPTION 'WP06_INCIDENT_TARGET_STALE'; END IF;
            ELSE
                IF NOT FOUND OR predecessor.action <> 'HOLD' OR predecessor.revision <> 1
                   OR NEW.action <> 'RELEASE' OR NEW.revision <> 2 THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_STALE_REVISION';
                END IF;
                IF predecessor.id IS DISTINCT FROM NEW.previous_event_id
                   OR predecessor.event_hash IS DISTINCT FROM NEW.previous_event_hash
                   OR predecessor.idempotency_key = NEW.idempotency_key
                   OR predecessor.created_at > NEW.created_at
                   OR (predecessor.proposal_hash, predecessor.assessment_id,
                       predecessor.assessment_hash, predecessor.incumbent_registry_id,
                       predecessor.incumbent_registry_revision, predecessor.incumbent_artifact_hash,
                       predecessor.active_pointer_version, predecessor.deployment_revision)
                       IS DISTINCT FROM (NEW.proposal_hash, NEW.assessment_id,
                       NEW.assessment_hash, NEW.incumbent_registry_id,
                       NEW.incumbent_registry_revision, NEW.incumbent_artifact_hash,
                       NEW.active_pointer_version, NEW.deployment_revision) THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_PREDECESSOR_MISMATCH';
                END IF;
            END IF;
            -- Kiểm TOTP sau mọi khóa/gateway để thời gian chờ không làm cũ bằng chứng MFA.
            now_at := pg_catalog.clock_timestamp();
            IF NEW.mfa_method <> 'TOTP' OR NEW.mfa_verified_at > NEW.created_at
               OR NEW.mfa_verified_at > now_at
               OR NEW.mfa_verified_at < now_at - interval '15 minutes'
               OR NEW.created_at > now_at THEN
                RAISE EXCEPTION 'WP06_INCIDENT_MFA_REQUIRED';
            END IF;
            RETURN NEW;
        END $$
    """.replace("__WP06_TRIM_CHARS__", f"({_strip_whitespace_sql()})")
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_learning_incident_guard() FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION public.wp06_learning_incident_immutable()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN RAISE EXCEPTION 'WP06_INCIDENT_IMMUTABLE'; END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_learning_incident_immutable() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER learning_incident_guard BEFORE INSERT ON public.{TABLE} FOR "
        f"EACH ROW EXECUTE FUNCTION public.wp06_learning_incident_guard()"
    )
    op.execute(
        f"CREATE TRIGGER learning_incident_immutable BEFORE UPDATE OR DELETE ON "
        f"public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        f"public.wp06_learning_incident_immutable()"
    )


def _create_projection() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_learning_incident_hold_active(target_workspace uuid)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user NOT IN ('crypidian_trading_runtime','crypidian_admin_web')
               OR target_workspace IS NULL OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_INCIDENT_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_INCIDENT_READ_COMMITTED_REQUIRED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'workspace-settings:' || target_workspace::text, 0));
            RETURN EXISTS (
                SELECT 1 FROM public.learning_incident_entry_hold_event held
                 WHERE held.workspace_id = target_workspace AND held.action = 'HOLD'
                   AND NOT EXISTS (
                       SELECT 1 FROM public.learning_incident_entry_hold_event released
                        WHERE released.workspace_id = target_workspace
                          AND released.proposal_id = held.proposal_id AND released.action =
                              'RELEASE' )
            );
        END $$
    """)
    op.execute(f"ALTER FUNCTION public.wp06_learning_incident_hold_active(uuid) OWNER TO {READER}")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_learning_incident_hold_active(uuid) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_hold_active(uuid) TO "
        f"{RUNTIME}, {ADMIN}"
    )


def downgrade() -> None:
    populated: int = (
        op.get_bind().execute(sa.text(f"SELECT count(*) FROM public.{TABLE}")).scalar_one()
    )
    if populated:
        raise RuntimeError("WP06_INCIDENT_POPULATED_DOWNGRADE_DENIED; lịch sử incident còn tồn tại")
    op.execute(f"DROP TRIGGER learning_incident_immutable ON public.{TABLE}")
    op.execute(f"DROP TRIGGER learning_incident_guard ON public.{TABLE}")
    for signature in (
        "wp06_learning_incident_hold_active(uuid)",
        "wp06_learning_incident_guard()",
        "wp06_learning_incident_immutable()",
        "wp06_learning_incident_canonical(jsonb)",
        "wp06_learning_incident_eligible_target(uuid,uuid)",
        "wp06_lock_learning_incident_scope(uuid,uuid)",
    ):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.drop_table(TABLE)
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {READER}")
    op.execute(f"DROP ROLE {READER}")
