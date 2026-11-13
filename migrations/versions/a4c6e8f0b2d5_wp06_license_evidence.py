"""WP06: ledger digest/scope append-only và policy ghim evidence theo workspace/source."""

from collections.abc import Sequence

from alembic import op

revision: str = "a4c6e8f0b2d5"
down_revision: str = "f6b8d0e2a4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    """Chỉ thêm metadata; lịch sử legacy không được backfill digest giả."""
    _scope_functions()
    op.execute("""ALTER TABLE source_policy_revision
        ADD COLUMN provider varchar(128), ADD COLUMN product varchar(128),
        ADD COLUMN venue varchar(128), ADD COLUMN contract varchar(128),
        ADD COLUMN license_scope jsonb, ADD COLUMN license_evidence_id uuid,
        ADD COLUMN license_digest varchar(64), ADD COLUMN license_scope_hash varchar(64),
        ADD CONSTRAINT uq_source_policy_revision_source_policy
            UNIQUE (workspace_id, source_id, policy_id)
    """)
    op.execute("""CREATE TABLE source_license_evidence (
        id uuid PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        workspace_id uuid NOT NULL,
        source_id varchar(128) NOT NULL,
        policy_id varchar(160) NOT NULL,
        idempotency_key uuid NOT NULL,
        evidence_ref text NOT NULL,
        digest varchar(64) NOT NULL,
        byte_count bigint NOT NULL,
        media_type varchar(96) NOT NULL,
        scope_version varchar(32) NOT NULL,
        license_scope jsonb NOT NULL,
        canonical_scope text NOT NULL,
        scope_hash varchar(64) NOT NULL,
        actor_id varchar(128) NOT NULL,
        CONSTRAINT uq_source_license_evidence_source UNIQUE (workspace_id, source_id, id),
        CONSTRAINT uq_source_license_evidence_key UNIQUE (workspace_id, idempotency_key),
        CONSTRAINT fk_source_license_evidence_workspace_id_workspace
            FOREIGN KEY (workspace_id) REFERENCES workspace(id) ON DELETE RESTRICT,
        CONSTRAINT fk_source_license_evidence_workspace_id_source_policy_revision
            FOREIGN KEY (workspace_id, source_id, policy_id)
            REFERENCES source_policy_revision(workspace_id, source_id, policy_id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_source_license_evidence_source_license_digest_valid
            CHECK (digest ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_source_license_evidence_source_license_byte_count_valid
            CHECK (byte_count BETWEEN 1 AND 10485760),
        CONSTRAINT ck_source_license_evidence_source_license_media_type_valid CHECK (
            media_type IN ('application/pdf','text/plain','text/html',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
        CONSTRAINT ck_source_license_evidence_source_license_version_valid
            CHECK (scope_version = 'wp06-license-scope-v1'),
        CONSTRAINT ck_source_license_evidence_source_license_scope_digest_valid CHECK (
            canonical_scope::jsonb = license_scope AND scope_hash =
            encode(sha256(convert_to(canonical_scope, 'UTF8')), 'hex')),
        CONSTRAINT ck_source_license_evidence_source_license_scope_canonical_valid CHECK (
            public.wp06_license_scope_valid(license_scope) AND
            canonical_scope = public.wp06_license_canonical_json(license_scope)),
        CONSTRAINT ck_source_license_evidence_source_license_ref_present
            CHECK (length(btrim(evidence_ref)) > 0),
        CONSTRAINT ck_source_license_evidence_source_license_actor_present
            CHECK (length(btrim(actor_id)) > 0)
    )""")
    op.execute("""ALTER TABLE source_policy_revision
        ADD CONSTRAINT fk_source_policy_license_evidence
        FOREIGN KEY (workspace_id, source_id, license_evidence_id)
        REFERENCES source_license_evidence(workspace_id, source_id, id) ON DELETE RESTRICT
    """)
    op.execute("ALTER TABLE source_license_evidence ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE source_license_evidence FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_select ON source_license_evidence FOR SELECT TO {ADMIN} "
        f"USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY workspace_insert ON source_license_evidence FOR INSERT TO {ADMIN} "
        f"WITH CHECK ({WORKSPACE})"
    )
    op.execute("REVOKE ALL ON TABLE source_license_evidence FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT SELECT, INSERT ON TABLE source_license_evidence TO {ADMIN}")
    op.execute("""CREATE TRIGGER source_license_evidence_append_only
        BEFORE UPDATE OR DELETE ON source_license_evidence FOR EACH ROW
        EXECUTE FUNCTION public.reject_source_registry_mutation()""")
    _evidence_guard()
    _policy_guard()
    _fingerprint()


def _scope_functions() -> None:
    """Canonical JSON dùng key C-order, không hash cách in mặc định của JSONB."""
    op.execute("""CREATE FUNCTION public.wp06_license_canonical_json(document jsonb)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE result text;
        BEGIN
            CASE jsonb_typeof(document)
            WHEN 'object' THEN
                SELECT '{' || COALESCE(string_agg(to_json(key)::text || ':' ||
                    public.wp06_license_canonical_json(value), ',' ORDER BY key COLLATE "C"),
                    '') || '}' INTO result FROM jsonb_each(document);
            WHEN 'array' THEN
                SELECT '[' || COALESCE(string_agg(public.wp06_license_canonical_json(value),
                    ',' ORDER BY ordinal), '') || ']' INTO result
                FROM jsonb_array_elements(document) WITH ORDINALITY AS item(value, ordinal);
            ELSE result := document::text;
            END CASE;
            RETURN result;
        END $$""")
    op.execute("""CREATE FUNCTION public.wp06_license_scope_valid(document jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE field text; members jsonb; sorted_members jsonb; start_at timestamptz;
                end_at timestamptz; member_pattern text;
        BEGIN
            IF document IS NULL OR jsonb_typeof(document) <> 'object' OR
               (SELECT array_agg(key ORDER BY key COLLATE "C") FROM jsonb_object_keys(document)
                    AS item(key)) IS DISTINCT FROM ARRAY[
                    'allowed_outputs','allowed_purposes','attribution','contract','data_end',
                    'data_start','derived_use','intervals','max_retention_days','product',
                    'provider','scope_version','source_id','symbols','venue']::text[] OR
               document->>'scope_version' IS DISTINCT FROM 'wp06-license-scope-v1' THEN
                RETURN false;
            END IF;
            FOREACH field IN ARRAY ARRAY['source_id','provider','product','venue','contract'] LOOP
                member_pattern := CASE WHEN field='source_id'
                    THEN '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
                    ELSE '^[A-Z0-9][A-Z0-9_.:-]{0,127}$' END;
                IF jsonb_typeof(document->field) IS DISTINCT FROM 'string'
                   OR (document->>field) !~ member_pattern THEN RETURN false; END IF;
            END LOOP;
            IF jsonb_typeof(document->'attribution') IS DISTINCT FROM 'string'
               OR length(btrim(document->>'attribution', E' \\t\\n\\r')) = 0 THEN
                RETURN false;
            END IF;
            FOREACH field IN ARRAY ARRAY['symbols','intervals','allowed_purposes',
                                        'allowed_outputs','derived_use'] LOOP
                members := document->field;
                IF jsonb_typeof(members) IS DISTINCT FROM 'array'
                   OR jsonb_array_length(members) = 0 OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(members) AS item(value)
                       WHERE jsonb_typeof(value) <> 'string') THEN RETURN false; END IF;
                SELECT jsonb_agg(value ORDER BY value COLLATE "C") INTO sorted_members
                FROM (SELECT DISTINCT value FROM jsonb_array_elements_text(members)
                      AS item(value)) AS unique_items;
                IF members <> sorted_members THEN RETURN false; END IF;
                IF field IN ('symbols','intervals') THEN
                    member_pattern := CASE WHEN field='symbols'
                        THEN '^[A-Z0-9][A-Z0-9_.:-]{0,127}$' ELSE '^[1-9][0-9]*[mhdwM]$' END;
                    IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(members) AS item(value)
                               WHERE value !~ member_pattern) THEN RETURN false; END IF;
                ELSIF field='allowed_purposes' THEN
                    IF NOT members <@ '["metadata","display","embedding","training",
                        "redistribution","backtest_research","paper_demo"]'::jsonb THEN
                        RETURN false; END IF;
                ELSIF field='allowed_outputs' THEN
                    IF NOT members <@ '["derived_features","trained_model","aggregate_report",
                        "paper_inference"]'::jsonb THEN RETURN false; END IF;
                ELSE
                    IF NOT members <@ '["feature_extraction","model_training","paper_inference",
                        "aggregate_reporting"]'::jsonb THEN RETURN false; END IF;
                END IF;
            END LOOP;
            FOREACH field IN ARRAY ARRAY['data_start','data_end'] LOOP
                IF jsonb_typeof(document->field) IS DISTINCT FROM 'string' OR
                   (document->>field) !~
                   '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$'
                    THEN RETURN false; END IF;
                IF to_char((document->>field)::timestamptz AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> document->>field
                    THEN RETURN false; END IF;
            END LOOP;
            start_at := (document->>'data_start')::timestamptz;
            end_at := (document->>'data_end')::timestamptz;
            IF start_at >= end_at OR start_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
               OR end_at >= TIMESTAMPTZ '10000-01-01 00:00:00+00' THEN RETURN false; END IF;
            IF document->'max_retention_days' <> 'null'::jsonb AND (
                jsonb_typeof(document->'max_retention_days') IS DISTINCT FROM 'number' OR
                (document->>'max_retention_days') !~ '^[1-9][0-9]*$') THEN RETURN false; END IF;
            RETURN true;
        EXCEPTION WHEN OTHERS THEN RETURN false;
        END $$""")
    for signature in ("wp06_license_canonical_json(jsonb)", "wp06_license_scope_valid(jsonb)"):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {ADMIN}, {RIGHTS_ROLE}")


def _evidence_guard() -> None:
    """Chỉ nhận scope/reference của pending policy mới nhất dưới cùng source lock."""
    op.execute("""CREATE FUNCTION public.wp06_license_evidence_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE pending public.source_policy_revision;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'research-source:' || NEW.workspace_id::text || ':' || NEW.source_id, 0));
            SELECT * INTO pending FROM public.source_policy_revision
             WHERE workspace_id=NEW.workspace_id AND source_id=NEW.source_id
             ORDER BY sequence DESC LIMIT 1;
            IF pending.policy_id IS DISTINCT FROM NEW.policy_id
               OR pending.state IS DISTINCT FROM 'rights_pending'
               OR pending.evidence_ref IS DISTINCT FROM NEW.evidence_ref
               OR pending.license_scope IS DISTINCT FROM NEW.license_scope
               OR NEW.license_scope->>'source_id' IS DISTINCT FROM NEW.source_id THEN
                RAISE EXCEPTION 'WP06_LICENSE_PENDING_SCOPE_MISMATCH';
            END IF;
            NEW.created_at := clock_timestamp();
            RETURN NEW;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_license_evidence_guard() FROM PUBLIC")
    op.execute("""CREATE TRIGGER source_license_evidence_guard
        BEFORE INSERT ON source_license_evidence FOR EACH ROW
        EXECUTE FUNCTION public.wp06_license_evidence_guard()""")


def _policy_guard() -> None:
    """INSERT mới kiểm ledger; UPDATE/DELETE vẫn bị append-only trigger hiện hữu chặn."""
    op.execute("""CREATE FUNCTION public.wp06_license_policy_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE previous public.source_policy_revision;
                evidence public.source_license_evidence; field text;
        BEGIN
            SELECT * INTO previous FROM public.source_policy_revision
             WHERE workspace_id=NEW.workspace_id AND source_id=NEW.source_id
               AND sequence < NEW.sequence ORDER BY sequence DESC LIMIT 1;
            FOREACH field IN ARRAY ARRAY['provider','product','venue','contract'] LOOP
                IF (to_jsonb(previous)->>field) IS NOT NULL AND
                   (to_jsonb(previous)->>field) IS DISTINCT FROM (to_jsonb(NEW)->>field) THEN
                    RAISE EXCEPTION 'WP06_LICENSE_IDENTITY_IMMUTABLE';
                END IF;
            END LOOP;
            IF num_nonnulls(NEW.provider,NEW.product,NEW.venue,NEW.contract) NOT IN (0,4) OR
               (NEW.provider IS NOT NULL AND EXISTS (
                   SELECT 1 FROM unnest(ARRAY[NEW.provider,NEW.product,NEW.venue,NEW.contract])
                       AS item(value) WHERE value !~ '^[A-Z0-9][A-Z0-9_.:-]{0,127}$')) THEN
                RAISE EXCEPTION 'WP06_LICENSE_IDENTITY_INVALID';
            END IF;
            IF NEW.license_scope IS NOT NULL AND (
                NOT public.wp06_license_scope_valid(NEW.license_scope) OR
                NEW.license_scope->>'source_id' IS DISTINCT FROM NEW.source_id OR
                NEW.license_scope->>'provider' IS DISTINCT FROM NEW.provider OR
                NEW.license_scope->>'product' IS DISTINCT FROM NEW.product OR
                NEW.license_scope->>'venue' IS DISTINCT FROM NEW.venue OR
                NEW.license_scope->>'contract' IS DISTINCT FROM NEW.contract OR
                NEW.license_scope->>'attribution' IS DISTINCT FROM NEW.attribution OR
                NOT NEW.allowed_purposes <@ (NEW.license_scope->'allowed_purposes')) THEN
                RAISE EXCEPTION 'WP06_LICENSE_POLICY_SCOPE_MISMATCH';
            END IF;
            IF NEW.state='verified' AND NEW.license_evidence_id IS NULL THEN
                RAISE EXCEPTION 'WP06_LICENSE_EVIDENCE_REQUIRED';
            END IF;
            IF NEW.license_evidence_id IS NOT NULL THEN
                SELECT * INTO evidence FROM public.source_license_evidence
                 WHERE workspace_id=NEW.workspace_id AND source_id=NEW.source_id
                   AND id=NEW.license_evidence_id;
                IF NOT FOUND OR NEW.license_scope IS NULL
                   OR evidence.digest IS DISTINCT FROM NEW.license_digest
                   OR evidence.scope_hash IS DISTINCT FROM NEW.license_scope_hash
                   OR evidence.evidence_ref IS DISTINCT FROM NEW.evidence_ref
                   OR evidence.license_scope IS DISTINCT FROM NEW.license_scope THEN
                    RAISE EXCEPTION 'WP06_LICENSE_EVIDENCE_MISMATCH';
                END IF;
            ELSIF NEW.license_digest IS NOT NULL OR NEW.license_scope_hash IS NOT NULL THEN
                RAISE EXCEPTION 'WP06_LICENSE_EVIDENCE_MISMATCH';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_license_policy_guard() FROM PUBLIC")
    op.execute("""CREATE TRIGGER source_policy_revision_license_guard
        AFTER INSERT ON source_policy_revision FOR EACH ROW
        EXECUTE FUNCTION public.wp06_license_policy_guard()""")


def _fingerprint() -> None:
    """Giữ hàm fingerprint cũ; hàm nhận row dùng evidence khi và chỉ khi có ID."""
    op.execute("""CREATE FUNCTION public.wp06_license_source_policy_fingerprint(
            policy public.source_policy_revision
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        SELECT CASE WHEN policy.license_evidence_id IS NULL THEN
            public.wp06_scorer_source_policy_fingerprint(
                policy.source_id, policy.sequence, policy.policy_id, policy.state,
                policy.allowed_purposes, policy.evidence_ref,
                policy.reviewed_at, policy.valid_until,
                policy.approved_by, policy.publisher, policy.endpoint_url, policy.access_method,
                policy.auth_scope, policy.attribution, policy.retention_ref,
                policy.quota_window_seconds, policy.quota_max_requests, policy.quota_max_bytes,
                policy.quota_max_cost, policy.quota_cost_unit)
        ELSE encode(sha256(convert_to(public.wp06_license_canonical_json(jsonb_build_object(
            'access_method', policy.access_method, 'allowed_purposes', (
                SELECT jsonb_agg(value ORDER BY value COLLATE "C")
                FROM jsonb_array_elements_text(policy.allowed_purposes) AS item(value)),
            'approved_by', policy.approved_by, 'attribution', policy.attribution,
            'auth_scope', policy.auth_scope,
            'budget', jsonb_build_object('cost_unit', policy.quota_cost_unit,
                'max_bytes', policy.quota_max_bytes,
                'max_cost', public.wp06_scorer_decimal_python_text(policy.quota_max_cost),
                'max_requests', policy.quota_max_requests,
                'window', jsonb_build_array(div(policy.quota_window_seconds,86400),
                    mod(policy.quota_window_seconds,86400),0)),
            'endpoint_url', policy.endpoint_url, 'evidence_ref', policy.evidence_ref,
            'license_digest', policy.license_digest,
            'license_evidence_id', policy.license_evidence_id::text,
            'license_scope_hash', policy.license_scope_hash,
            'policy_id', policy.policy_id, 'publisher', policy.publisher,
            'retention_ref', policy.retention_ref,
            'reviewed_at', public.wp06_canonical_utc_timestamp_json(policy.reviewed_at),
            'sequence', policy.sequence, 'source_id', policy.source_id, 'state', policy.state,
            'valid_until', public.wp06_canonical_utc_timestamp_json(policy.valid_until)
        )), 'UTF8')), 'hex') END $$""")
    signature = "public.wp06_license_source_policy_fingerprint(public.source_policy_revision)"
    op.execute(f"ALTER FUNCTION {signature} OWNER TO {RIGHTS_ROLE}")
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {ADMIN}, {RIGHTS_ROLE}")


def downgrade() -> None:
    """Không xóa metadata đã ghi hoặc lịch sử policy đã được ghim bằng chứng."""
    op.execute(
        "LOCK TABLE source_policy_revision, source_license_evidence IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM source_license_evidence) OR EXISTS (
            SELECT 1 FROM source_policy_revision WHERE provider IS NOT NULL
                OR product IS NOT NULL OR venue IS NOT NULL OR contract IS NOT NULL
                OR license_scope IS NOT NULL OR license_evidence_id IS NOT NULL
                OR license_digest IS NOT NULL OR license_scope_hash IS NOT NULL) THEN
            RAISE EXCEPTION 'WP06_LICENSE_EVIDENCE_DOWNGRADE_BLOCKED';
        END IF;
    END $$""")
    op.execute(
        "DROP FUNCTION public.wp06_license_source_policy_fingerprint(public.source_policy_revision)"
    )
    op.execute("DROP TRIGGER source_policy_revision_license_guard ON source_policy_revision")
    op.execute("DROP FUNCTION public.wp06_license_policy_guard()")
    op.execute(
        "ALTER TABLE source_policy_revision DROP CONSTRAINT fk_source_policy_license_evidence"
    )
    op.execute("DROP TABLE source_license_evidence")
    op.execute("DROP FUNCTION public.wp06_license_evidence_guard()")
    op.execute("""ALTER TABLE source_policy_revision
        DROP CONSTRAINT uq_source_policy_revision_source_policy,
        DROP COLUMN provider, DROP COLUMN product, DROP COLUMN venue, DROP COLUMN contract,
        DROP COLUMN license_scope, DROP COLUMN license_evidence_id,
        DROP COLUMN license_digest, DROP COLUMN license_scope_hash""")
    op.execute("DROP FUNCTION public.wp06_license_scope_valid(jsonb)")
    op.execute("DROP FUNCTION public.wp06_license_canonical_json(jsonb)")
