# SQL migration giữ nguyên câu lệnh dài để đối chiếu trigger và DDL khi review.
# ruff: noqa: E501
"""WP05: receipt quan sát source commit từ transaction top-level khác."""

from collections.abc import Sequence

from alembic import op

revision: str = "e0b2d4f6a8c1"
down_revision: str | None = "d1a3c5e7f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_TABLES = ("source_policy_observation", "source_item_observation")


def upgrade() -> None:
    # Legacy dùng xid migration làm barrier và không được tạo receipt giả.
    for table in ("source_policy_revision", "source_item_revision"):
        op.execute(
            f'ALTER TABLE public."{table}" ADD COLUMN barrier_txid xid8 NOT NULL DEFAULT pg_catalog.pg_current_xact_id()'
        )
    op.execute("""
CREATE TABLE source_policy_observation (
	workspace_id UUID NOT NULL,
	source_id VARCHAR(128) NOT NULL,
	sequence INTEGER NOT NULL,
	policy_id VARCHAR(160) NOT NULL,
	known_at TIMESTAMP WITH TIME ZONE DEFAULT pg_catalog.clock_timestamp() NOT NULL,
	id UUID NOT NULL,
	CONSTRAINT pk_source_policy_observation PRIMARY KEY (id),
	CONSTRAINT uq_source_policy_observation_sequence UNIQUE (workspace_id, source_id, sequence),
	CONSTRAINT uq_source_policy_observation_policy UNIQUE (workspace_id, policy_id),
	CONSTRAINT fk_policy_observation_sequence FOREIGN KEY(workspace_id, source_id, sequence) REFERENCES source_policy_revision (workspace_id, source_id, sequence) ON DELETE RESTRICT,
	CONSTRAINT fk_policy_observation_policy FOREIGN KEY(workspace_id, policy_id) REFERENCES source_policy_revision (workspace_id, policy_id) ON DELETE RESTRICT,
	CONSTRAINT ck_source_policy_observation_spo_known_at_pytime CHECK (isfinite(known_at) AND known_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00' AND known_at < TIMESTAMPTZ '10000-01-01 00:00:00+00')
)

""")
    op.execute("""
CREATE TABLE source_item_observation (
	workspace_id UUID NOT NULL,
	source_id VARCHAR(128) NOT NULL,
	item_id VARCHAR(128) NOT NULL,
	sequence INTEGER NOT NULL,
	revision_id VARCHAR(128) NOT NULL,
	known_at TIMESTAMP WITH TIME ZONE DEFAULT pg_catalog.clock_timestamp() NOT NULL,
	id UUID NOT NULL,
	CONSTRAINT pk_source_item_observation PRIMARY KEY (id),
	CONSTRAINT uq_source_item_observation_sequence UNIQUE (workspace_id, source_id, item_id, sequence),
	CONSTRAINT uq_source_item_observation_revision UNIQUE (workspace_id, source_id, item_id, revision_id),
	CONSTRAINT fk_source_item_observation_workspace_id_source_item_revision FOREIGN KEY(workspace_id, source_id, item_id, revision_id) REFERENCES source_item_revision (workspace_id, source_id, item_id, revision_id) ON DELETE RESTRICT,
	CONSTRAINT ck_source_item_observation_sio_known_at_pytime CHECK (isfinite(known_at) AND known_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00' AND known_at < TIMESTAMPTZ '10000-01-01 00:00:00+00')
)

""")
    op.execute("""
        CREATE FUNCTION public.force_source_barrier_txid() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            NEW.barrier_txid := pg_catalog.pg_current_xact_id();
            RETURN NEW;
        END; $$
    """)
    for table in ("source_policy_revision", "source_item_revision"):
        op.execute(
            f'CREATE TRIGGER "{table}_tx_barrier" BEFORE INSERT ON public."{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.force_source_barrier_txid()"
        )
    op.execute("REVOKE ALL ON FUNCTION public.force_source_barrier_txid() FROM PUBLIC")
    for table in _TABLES:
        writer = ADMIN if table == "source_policy_observation" else INGESTION
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_select ON public."{table}" FOR SELECT TO {ADMIN}, {INGESTION} USING ({_WORKSPACE})'
        )
        op.execute(
            f'CREATE POLICY workspace_insert ON public."{table}" FOR INSERT TO {writer} WITH CHECK ({_WORKSPACE})'
        )
        op.execute(
            f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON public."{table}" FOR EACH ROW EXECUTE FUNCTION public.reject_source_registry_mutation()'
        )
        op.execute(
            f'REVOKE ALL ON TABLE public."{table}" FROM PUBLIC, crypidian_app, {ADMIN}, {INGESTION}, crypidian_trading_runtime, crypidian_trainer_worker, crypidian_execution_gateway'
        )
        op.execute(f'GRANT SELECT ON TABLE public."{table}" TO {ADMIN}, {INGESTION}')
        op.execute(f'GRANT INSERT ON TABLE public."{table}" TO {writer}')
    op.execute("""
        CREATE FUNCTION public.enforce_source_policy_observation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source public.source_policy_revision%ROWTYPE;
            predecessor public.source_policy_observation%ROWTYPE;
            previous_time timestamptz;
            observed timestamptz;
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP05_OBSERVATION_SCOPE_INVALID';
            END IF;
            SELECT * INTO source FROM public.source_policy_revision
             WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
               AND policy_id = NEW.policy_id;
            IF source.id IS NULL THEN RAISE EXCEPTION 'WP05_OBSERVATION_SOURCE_NOT_VISIBLE'; END IF;
            IF source.barrier_txid = pg_catalog.pg_current_xact_id() THEN
                RAISE EXCEPTION 'WP05_OBSERVATION_REQUIRES_NEW_TRANSACTION';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'research-source:' || NEW.workspace_id::text || ':' || NEW.source_id, 0));
            IF source.sequence > 1 THEN
                SELECT * INTO predecessor FROM public.source_policy_observation
                 WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
                   AND sequence = source.sequence - 1;
                IF predecessor.id IS NULL THEN RAISE EXCEPTION 'WP05_OBSERVATION_PREDECESSOR_MISSING'; END IF;
                SELECT created_at INTO previous_time FROM public.source_policy_revision
                 WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
                   AND sequence = source.sequence - 1;
            END IF;
            observed := pg_catalog.clock_timestamp();
            IF observed < source.created_at OR observed < source.reviewed_at
               OR observed < previous_time OR observed < predecessor.known_at THEN
                RAISE EXCEPTION 'WP05_OBSERVATION_CLOCK_REGRESSED';
            END IF;
            NEW.sequence := source.sequence;
            NEW.known_at := observed;
            RETURN NEW;
        END; $$
    """)
    op.execute("""
        CREATE FUNCTION public.enforce_source_item_observation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source public.source_item_revision%ROWTYPE;
            predecessor public.source_item_observation%ROWTYPE;
            previous_time timestamptz;
            observed timestamptz;
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP05_OBSERVATION_SCOPE_INVALID';
            END IF;
            SELECT * INTO source FROM public.source_item_revision
             WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
               AND item_id = NEW.item_id AND revision_id = NEW.revision_id;
            IF source.id IS NULL THEN RAISE EXCEPTION 'WP05_OBSERVATION_SOURCE_NOT_VISIBLE'; END IF;
            IF source.barrier_txid = pg_catalog.pg_current_xact_id() THEN
                RAISE EXCEPTION 'WP05_OBSERVATION_REQUIRES_NEW_TRANSACTION';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'research-source:' || NEW.workspace_id::text || ':' || NEW.source_id, 0));
            IF source.sequence > 1 THEN
                SELECT * INTO predecessor FROM public.source_item_observation
                 WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
                   AND item_id = NEW.item_id AND sequence = source.sequence - 1;
                IF predecessor.id IS NULL OR predecessor.revision_id IS DISTINCT FROM source.predecessor_revision_id THEN
                    RAISE EXCEPTION 'WP05_OBSERVATION_PREDECESSOR_MISSING';
                END IF;
                SELECT recorded_at INTO previous_time FROM public.source_item_revision
                 WHERE workspace_id = NEW.workspace_id AND source_id = NEW.source_id
                   AND item_id = NEW.item_id AND sequence = source.sequence - 1;
            END IF;
            observed := pg_catalog.clock_timestamp();
            IF observed < source.recorded_at OR observed < source.created_at
               OR observed < previous_time OR observed < predecessor.known_at THEN
                RAISE EXCEPTION 'WP05_OBSERVATION_CLOCK_REGRESSED';
            END IF;
            NEW.sequence := source.sequence;
            NEW.known_at := observed;
            RETURN NEW;
        END; $$
    """)
    for table, function in (
        ("source_policy_observation", "enforce_source_policy_observation"),
        ("source_item_observation", "enforce_source_item_observation"),
    ):
        op.execute(
            f'CREATE TRIGGER "{table}_insert" BEFORE INSERT ON public."{table}" '
            f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
        )
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def downgrade() -> None:
    op.execute(
        "LOCK TABLE source_policy_observation, source_item_observation, source_policy_revision, source_item_revision IN ACCESS EXCLUSIVE MODE"
    )
    for table in (*_TABLES, "source_policy_revision", "source_item_revision"):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_policy_revision) OR EXISTS (SELECT 1 FROM source_item_revision) OR EXISTS (SELECT 1 FROM source_policy_observation) OR EXISTS (SELECT 1 FROM source_item_observation) THEN RAISE EXCEPTION 'WP05_SOURCE_OBSERVATION_DOWNGRADE_BLOCKED'; END IF; END $$"
    )
    for table in ("source_policy_revision", "source_item_revision"):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP TRIGGER "{table}_tx_barrier" ON public."{table}"')
        op.execute(f'ALTER TABLE public."{table}" DROP COLUMN barrier_txid')
    for table in reversed(_TABLES):
        op.execute(f'DROP TABLE public."{table}"')
    for function in (
        "enforce_source_item_observation",
        "enforce_source_policy_observation",
        "force_source_barrier_txid",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
