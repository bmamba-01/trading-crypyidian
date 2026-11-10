"""WP05G.3: quét grant hết hạn bằng DB clock và phát expiry event idempotent."""

from collections.abc import Sequence

from alembic import op

revision: str = "c1e3f5a7b9d2"
down_revision: str | None = "b8d1f3a5c7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTION = "crypidian_ingestion_worker"
RIGHTS_EVENT_TYPE = "research.rights_changed.v1"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.enqueue_expired_source_policy_events(p_batch_size integer)
        RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            scoped_workspace uuid;
            inserted_count integer;
        BEGIN
            IF p_batch_size IS NULL OR p_batch_size < 1 OR p_batch_size > 500 THEN
                RAISE EXCEPTION 'WP05_EXPIRY_BATCH_SIZE_INVALID';
            END IF;
            scoped_workspace := NULLIF(
                pg_catalog.current_setting('app.workspace_id', true), ''
            )::uuid;
            IF scoped_workspace IS NULL THEN
                RAISE EXCEPTION 'WP05_EXPIRY_WORKSPACE_SCOPE_REQUIRED';
            END IF;

            WITH expired AS (
                SELECT
                    policy.workspace_id,
                    policy.source_id,
                    policy.sequence,
                    policy.id,
                    policy.policy_id,
                    policy.state
                FROM public.source_policy_revision AS policy
                WHERE policy.workspace_id = scoped_workspace
                  AND policy.state = 'verified'
                  AND policy.valid_until IS NOT NULL
                  AND policy.valid_until <= pg_catalog.clock_timestamp()
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.outbox_event AS existing
                      WHERE existing.workspace_id = policy.workspace_id
                        AND existing.event_type = '{RIGHTS_EVENT_TYPE}'
                        AND existing.idempotency_key = 'source-policy-expiry:' ||
                            pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                                policy.workspace_id::text || ':' || policy.source_id || ':' ||
                                policy.sequence::text,
                                'UTF8'
                            )), 'hex')
                  )
                ORDER BY policy.valid_until, policy.source_id, policy.sequence
                LIMIT p_batch_size
            )
            INSERT INTO public.outbox_event (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                occurred_at, correlation_id, idempotency_key, schema_version, payload
            )
            SELECT
                pg_catalog.gen_random_uuid(),
                expired.workspace_id,
                '{RIGHTS_EVENT_TYPE}',
                'source_policy_revision',
                expired.id,
                pg_catalog.clock_timestamp(),
                expired.id,
                'source-policy-expiry:' || pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(
                        expired.workspace_id::text || ':' || expired.source_id || ':' ||
                        expired.sequence::text,
                        'UTF8'
                    )
                ), 'hex'),
                1,
                pg_catalog.jsonb_build_object(
                    'workspace_id', expired.workspace_id,
                    'source_id', expired.source_id,
                    'change_type', 'policy_expired',
                    'policy_sequence', expired.sequence,
                    'policy_state', expired.state,
                    'reason_code', 'policy_expired'
                )
            FROM expired
            ON CONFLICT (workspace_id, event_type, idempotency_key) DO NOTHING;

            GET DIAGNOSTICS inserted_count = ROW_COUNT;
            RETURN inserted_count;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.enqueue_expired_source_policy_events(integer) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.enqueue_expired_source_policy_events(integer) "
        f"TO {INGESTION}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE ALL ON FUNCTION public.enqueue_expired_source_policy_events(integer) "
        f"FROM {INGESTION}"
    )
    op.execute("DROP FUNCTION public.enqueue_expired_source_policy_events(integer)")
