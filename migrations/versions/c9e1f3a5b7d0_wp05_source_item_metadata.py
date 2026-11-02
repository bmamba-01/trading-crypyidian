"""WP05C: lịch sử metadata revision và takedown bất biến theo workspace."""

from collections.abc import Sequence

from alembic import op

revision: str = "c9e1f3a5b7d0"
down_revision: str | None = "b8d0f1a3c5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_TABLES = ("source_item_revision", "source_item_takedown")


def upgrade() -> None:
    op.execute("""
CREATE TABLE source_item_revision (
    workspace_id UUID NOT NULL,
    source_id VARCHAR(128) NOT NULL,
    item_id VARCHAR(128) NOT NULL,
    revision_id VARCHAR(128) NOT NULL,
    sequence INTEGER NOT NULL,
    canonical_url TEXT NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE,
    event_at TIMESTAMP WITH TIME ZONE,
    published_at TIMESTAMP WITH TIME ZONE,
    edited_at TIMESTAMP WITH TIME ZONE,
    tombstoned BOOLEAN NOT NULL,
    predecessor_revision_id VARCHAR(128),
    predecessor_hash VARCHAR(64),
    revision_hash VARCHAR(64) NOT NULL,
    retention_expires_at TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_source_item_revision PRIMARY KEY (id),
    CONSTRAINT uq_source_item_revision_identity UNIQUE (workspace_id, source_id, item_id,
revision_id),
    CONSTRAINT uq_source_item_revision_sequence UNIQUE (workspace_id, source_id, item_id,
sequence),
    CONSTRAINT fk_source_item_revision_workspace_id_workspace FOREIGN KEY(workspace_id)
REFERENCES workspace (id) ON DELETE RESTRICT,
    CONSTRAINT ck_source_item_revision_source_item_sequence_positive CHECK (sequence >= 1),
    CONSTRAINT ck_source_item_revision_source_item_identifiers_valid CHECK (source_id ~
'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND
revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    CONSTRAINT ck_source_item_revision_source_item_hashes_valid CHECK (revision_hash ~
'^[0-9a-f]{64}$' AND (predecessor_hash IS NULL OR predecessor_hash ~ '^[0-9a-f]{64}$')),
    CONSTRAINT ck_source_item_revision_source_item_predecessor_present CHECK ((sequence = 1 AND
predecessor_revision_id IS NULL AND predecessor_hash IS NULL) OR (sequence > 1 AND
predecessor_revision_id IS NOT NULL AND predecessor_hash IS NOT NULL)),
    CONSTRAINT ck_source_item_revision_source_item_timestamps_causal CHECK ((available_at IS
NULL OR available_at >= observed_at) AND (published_at IS NULL OR published_at <= observed_at)
AND (edited_at IS NULL OR edited_at <= observed_at) AND (published_at IS NULL OR edited_at IS
NULL OR published_at <= edited_at)),
    CONSTRAINT ck_source_item_revision_source_item_url_metadata CHECK (canonical_url LIKE
'https://%' AND position('#' in canonical_url) = 0 AND canonical_url !~ '[[:space:]]' AND
position('@' in split_part(canonical_url, '/', 3)) = 0)
)

""")
    op.execute(
        "CREATE INDEX ix_source_item_revision_available ON source_item_revision "
        "(workspace_id, source_id, item_id, available_at)"
    )
    op.execute("""
CREATE TABLE source_item_takedown (
    workspace_id UUID NOT NULL,
    source_id VARCHAR(128) NOT NULL,
    item_id VARCHAR(128) NOT NULL,
    taken_down_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    id UUID NOT NULL,
    CONSTRAINT pk_source_item_takedown PRIMARY KEY (id),
    CONSTRAINT uq_source_item_takedown_identity UNIQUE (workspace_id, source_id, item_id),
    CONSTRAINT fk_source_item_takedown_workspace_id_workspace FOREIGN KEY(workspace_id)
REFERENCES workspace (id) ON DELETE RESTRICT,
    CONSTRAINT ck_source_item_takedown_source_item_takedown_identifiers_valid CHECK (source_id ~
'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    CONSTRAINT ck_source_item_takedown_source_item_takedown_reason_valid CHECK (reason_code ~
'^[a-z][a-z0-9_]{0,63}$')
)

""")
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_select ON "{table}" FOR SELECT TO {ADMIN}, {INGESTION} '
            f"USING ({_WORKSPACE})"
        )
        writer = INGESTION if table == "source_item_revision" else ADMIN
        op.execute(
            f'CREATE POLICY workspace_insert ON "{table}" FOR INSERT TO {writer} '
            f"WITH CHECK ({_WORKSPACE})"
        )
        op.execute(
            f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.reject_source_registry_mutation()"
        )
        op.execute(
            f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM PUBLIC, crypidian_app, '
            f"{ADMIN}, {INGESTION}, crypidian_trading_runtime, crypidian_trainer_worker, "
            "crypidian_execution_gateway"
        )
        op.execute(f'GRANT SELECT ON TABLE "{table}" TO {ADMIN}, {INGESTION}')
        op.execute(f'GRANT INSERT ON TABLE "{table}" TO {writer}')
    op.execute(
        """
        CREATE FUNCTION public.source_item_timestamp(value timestamptz) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT AS $$
            SELECT to_char(value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
                || CASE WHEN extract(microseconds FROM value)::bigint % 1000000 = 0
                   THEN '' ELSE '.' || to_char(value AT TIME ZONE 'UTC', 'US') END
                || '+00:00'
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_source_item_append() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous public.source_item_revision%ROWTYPE;
            canonical text;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'research-source:' || NEW.workspace_id::text || ':' || NEW.source_id, 0
            ));
            IF EXISTS (
                SELECT 1 FROM public.source_item_takedown
                 WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
                   AND item_id = NEW.item_id
            ) THEN
                RAISE EXCEPTION 'WP05_ITEM_TAKEN_DOWN';
            END IF;
            SELECT * INTO previous FROM public.source_item_revision
             WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
               AND item_id = NEW.item_id ORDER BY sequence DESC LIMIT 1;
            IF previous.id IS NULL THEN
                IF NEW.sequence <> 1 OR NEW.predecessor_revision_id IS NOT NULL
                   OR NEW.predecessor_hash IS NOT NULL THEN
                    RAISE EXCEPTION 'WP05_ITEM_PREDECESSOR_INVALID';
                END IF;
            ELSE
                IF previous.tombstoned THEN
                    RAISE EXCEPTION 'WP05_ITEM_TOMBSTONED';
                END IF;
                IF NEW.sequence <> previous.sequence + 1
                   OR NEW.predecessor_revision_id IS DISTINCT FROM previous.revision_id
                   OR NEW.predecessor_hash IS DISTINCT FROM previous.revision_hash THEN
                    RAISE EXCEPTION 'WP05_ITEM_PREDECESSOR_INVALID';
                END IF;
                IF NEW.observed_at < previous.observed_at THEN
                    RAISE EXCEPTION 'WP05_ITEM_OBSERVATION_REVERSED';
                END IF;
            END IF;
            canonical := json_build_object(
                'available_at', public.source_item_timestamp(NEW.available_at),
                'canonical_url', NEW.canonical_url,
                'edited_at', public.source_item_timestamp(NEW.edited_at),
                'event_at', public.source_item_timestamp(NEW.event_at),
                'item_id', NEW.item_id,
                'observed_at', public.source_item_timestamp(NEW.observed_at),
                'predecessor_hash', NEW.predecessor_hash,
                'published_at', public.source_item_timestamp(NEW.published_at),
                'revision_id', NEW.revision_id,
                'source_id', NEW.source_id,
                'tombstoned', NEW.tombstoned
            )::text;
            canonical := replace(replace(canonical, ' : ', ':'), ', ', ',');
            IF NEW.revision_hash <> encode(sha256(convert_to(canonical, 'UTF8')), 'hex') THEN
                RAISE EXCEPTION 'WP05_ITEM_HASH_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_source_item_takedown() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'research-source:' || NEW.workspace_id::text || ':' || NEW.source_id, 0
            ));
            NEW.taken_down_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER source_item_revision_insert BEFORE INSERT ON source_item_revision "
        "FOR EACH ROW EXECUTE FUNCTION public.enforce_source_item_append()"
    )
    op.execute(
        "CREATE TRIGGER source_item_takedown_insert BEFORE INSERT ON source_item_takedown "
        "FOR EACH ROW EXECUTE FUNCTION public.enforce_source_item_takedown()"
    )
    for function in (
        "enforce_source_item_append()",
        "enforce_source_item_takedown()",
        "source_item_timestamp(timestamptz)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.source_item_timestamp(timestamptz) TO {INGESTION}"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE source_item_revision, source_item_takedown IN ACCESS EXCLUSIVE MODE")
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_item_revision) "
        "OR EXISTS (SELECT 1 FROM source_item_takedown) THEN "
        "RAISE EXCEPTION 'WP05_ITEM_HISTORY_DOWNGRADE_BLOCKED'; END IF; END $$"
    )
    for table in _TABLES:
        op.execute(f'DROP TRIGGER "{table}_insert" ON "{table}"')
    op.execute("DROP FUNCTION public.enforce_source_item_append()")
    op.execute("DROP FUNCTION public.enforce_source_item_takedown()")
    op.execute("DROP FUNCTION public.source_item_timestamp(timestamptz)")
    for table in reversed(_TABLES):
        op.execute(f'DROP TABLE "{table}"')
