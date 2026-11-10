"""WP05/WP06: cấp quyền workspace-scoped tối thiểu cho rights maintenance worker."""

from collections.abc import Sequence

from alembic import op

revision: str = "e4b7d9f1a3c5"
down_revision: str | None = "d2f4a6c8e0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTION = "crypidian_ingestion_worker"
EXPIRY_SCANNER = "crypidian_source_expiry_scanner"
RIGHTS_EMITTER = "crypidian_source_rights_emitter"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_RIGHTS_EVENT = "research.rights_changed.v1"
_RIGHTS_CONSUMER = "wp06.dataset-rights-eligibility.v1"


def upgrade() -> None:
    # Hàm SECURITY DEFINER chỉ do role chuyên biệt không đăng nhập sở hữu; RLS giới hạn tenant.
    for role_name in (EXPIRY_SCANNER, RIGHTS_EMITTER):
        op.execute(
            f"""DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role_name}'
                ) THEN
                    CREATE ROLE {role_name} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                        NOCREATEDB NOCREATEROLE NOREPLICATION;
                ELSIF EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = '{role_name}'
                    AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolreplication
                        OR r.rolcreaterole OR r.rolcreatedb OR r.rolinherit
                        OR EXISTS (
                            SELECT 1 FROM pg_catalog.pg_auth_members m
                            WHERE m.roleid = r.oid OR m.member = r.oid
                        ))
                ) THEN
                    RAISE EXCEPTION 'WP05_RIGHTS_DEFINER_ROLE_UNSAFE';
                END IF;
            END $$"""
        )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role_name}")

    op.execute(
        f"CREATE POLICY rights_emitter_outbox_insert ON public.outbox_event FOR INSERT "
        f"TO {RIGHTS_EMITTER} WITH CHECK ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}')"
    )
    op.execute(
        "GRANT INSERT (id, workspace_id, event_type, aggregate_type, aggregate_id, occurred_at, "
        "correlation_id, idempotency_key, schema_version, payload) "
        "ON TABLE public.outbox_event TO " + RIGHTS_EMITTER
    )
    for signature in (
        "public.emit_source_policy_rights_event()",
        "public.emit_source_item_revision_rights_event()",
        "public.emit_source_item_takedown_rights_event()",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {RIGHTS_EMITTER}")
    op.execute(
        "CREATE POLICY expiry_scanner_source_policy_select "
        'ON public."source_policy_revision" FOR SELECT '
        f"TO {EXPIRY_SCANNER} USING ({_WORKSPACE})"
    )
    op.execute(
        "GRANT SELECT (workspace_id, source_id, sequence, id, policy_id, state, valid_until) "
        'ON TABLE public."source_policy_revision" TO ' + EXPIRY_SCANNER
    )
    op.execute(
        f"CREATE POLICY expiry_scanner_rights_event_select ON public.outbox_event FOR SELECT "
        f"TO {EXPIRY_SCANNER} USING ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}')"
    )
    op.execute(
        f"CREATE POLICY expiry_scanner_rights_event_insert ON public.outbox_event FOR INSERT "
        f"TO {EXPIRY_SCANNER} WITH CHECK ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}')"
    )
    op.execute(
        "GRANT SELECT (workspace_id, event_type, idempotency_key) "
        "ON TABLE public.outbox_event TO " + EXPIRY_SCANNER
    )
    op.execute(
        "GRANT INSERT (id, workspace_id, event_type, aggregate_type, aggregate_id, occurred_at, "
        "correlation_id, idempotency_key, schema_version, payload) "
        "ON TABLE public.outbox_event TO " + EXPIRY_SCANNER
    )
    op.execute(
        f"ALTER FUNCTION public.enqueue_expired_source_policy_events(integer) "
        f"OWNER TO {EXPIRY_SCANNER}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.enqueue_expired_source_policy_events(integer) "
        f"TO {INGESTION}"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.guard_source_rights_event_insert() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            emitter_owner name;
            expiry_scanner_owner name;
        BEGIN
            IF NEW.event_type = '{_RIGHTS_EVENT}' THEN
                SELECT pg_catalog.pg_get_userbyid(proowner)
                  INTO emitter_owner
                  FROM pg_catalog.pg_proc
                 WHERE oid = 'public.emit_source_policy_rights_event()'::pg_catalog.regprocedure;
                SELECT pg_catalog.pg_get_userbyid(proowner)
                  INTO expiry_scanner_owner
                  FROM pg_catalog.pg_proc
                 WHERE oid = 'public.enqueue_expired_source_policy_events(integer)'
                       ::pg_catalog.regprocedure;
                IF emitter_owner IS NULL OR (
                    current_user <> emitter_owner
                    AND NOT (
                        current_user = expiry_scanner_owner
                        AND NEW.aggregate_type = 'source_policy_revision'
                        AND NEW.payload ->> 'change_type' = 'policy_expired'
                    )
                ) THEN
                    RAISE EXCEPTION 'WP05_RIGHTS_EVENT_TRIGGER_ONLY';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    # Worker chỉ đọc outbox rights của workspace được bind và cập nhật cột giao nhận.
    op.execute(
        f"CREATE POLICY ingestion_rights_event_select ON public.outbox_event FOR SELECT "
        f"TO {INGESTION} USING ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}')"
    )
    op.execute(
        f"CREATE POLICY ingestion_rights_event_update ON public.outbox_event FOR UPDATE "
        f"TO {INGESTION} USING ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}') "
        f"WITH CHECK ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}')"
    )
    op.execute(f"GRANT SELECT ON TABLE public.outbox_event TO {INGESTION}")
    op.execute(
        "GRANT UPDATE (status, attempts, available_at, lease_owner, lease_expires_at, "
        f"completed_at, last_error_code) ON TABLE public.outbox_event TO {INGESTION}"
    )

    # Consumer chỉ kiểm tra receipt của mình và chỉ được thêm receipt đúng loại.
    op.execute(
        f"CREATE POLICY ingestion_rights_receipt_select ON public.consumer_receipt FOR SELECT "
        f"TO {INGESTION} USING ({_WORKSPACE} AND consumer_name = '{_RIGHTS_CONSUMER}')"
    )
    op.execute(
        f"CREATE POLICY ingestion_rights_receipt_insert ON public.consumer_receipt FOR INSERT "
        f"TO {INGESTION} WITH CHECK ({_WORKSPACE} AND consumer_name = '{_RIGHTS_CONSUMER}')"
    )
    op.execute(
        "GRANT SELECT (id, created_at, workspace_id, outbox_event_id, consumer_name) "
        "ON TABLE public.consumer_receipt TO " + INGESTION
    )
    op.execute("GRANT INSERT ON TABLE public.consumer_receipt TO " + INGESTION)

    # Invalidation là append-only; worker cần trả ID vừa chèn và không được sửa/xóa ledger.
    op.execute(
        'CREATE POLICY ingestion_eligibility_select ON public."dataset_manifest_eligibility_event" '
        f"FOR SELECT TO {INGESTION} USING ({_WORKSPACE})"
    )
    op.execute(
        'CREATE POLICY ingestion_eligibility_insert ON public."dataset_manifest_eligibility_event" '
        f"FOR INSERT TO {INGESTION} WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, outbox_event_id, manifest_id, sample_id, source_id, "
        "item_id, revision_id, policy_id, policy_sequence, purpose) ON TABLE "
        'public."dataset_manifest_eligibility_event" TO ' + INGESTION
    )
    op.execute('GRANT INSERT ON TABLE public."dataset_manifest_eligibility_event" TO ' + INGESTION)

    # Reverse lookup chỉ cần ref rows và ba cột nhận diện/thời gian của manifest.
    op.execute(
        'CREATE POLICY ingestion_workspace_select ON public."dataset_manifest" FOR SELECT '
        f"TO {INGESTION} USING ({_WORKSPACE})"
    )
    op.execute(
        'GRANT SELECT (workspace_id, id, created_at) ON TABLE public."dataset_manifest" TO '
        + INGESTION
    )
    op.execute(
        'CREATE POLICY ingestion_workspace_select ON public."dataset_manifest_source_ref" '
        f"FOR SELECT TO {INGESTION} USING ({_WORKSPACE})"
    )
    op.execute('GRANT SELECT ON TABLE public."dataset_manifest_source_ref" TO ' + INGESTION)


def downgrade() -> None:
    # Không hạ chủ sở hữu expiry function về role đăng nhập của migration.
    # Role/policy scanner được giữ để bảo đảm an toàn cho revision c1e3f5a7b9d2.
    op.execute('DROP POLICY ingestion_workspace_select ON public."dataset_manifest_source_ref"')
    op.execute('REVOKE SELECT ON TABLE public."dataset_manifest_source_ref" FROM ' + INGESTION)
    op.execute('DROP POLICY ingestion_workspace_select ON public."dataset_manifest"')
    op.execute(
        'REVOKE SELECT (workspace_id, id, created_at) ON TABLE public."dataset_manifest" FROM '
        + INGESTION
    )

    op.execute(
        'DROP POLICY ingestion_eligibility_insert ON public."dataset_manifest_eligibility_event"'
    )
    op.execute(
        'DROP POLICY ingestion_eligibility_select ON public."dataset_manifest_eligibility_event"'
    )
    op.execute(
        'REVOKE INSERT ON TABLE public."dataset_manifest_eligibility_event" FROM ' + INGESTION
    )
    op.execute(
        "REVOKE SELECT (id, workspace_id, outbox_event_id, manifest_id, sample_id, source_id, "
        "item_id, revision_id, policy_id, policy_sequence, purpose) ON TABLE "
        'public."dataset_manifest_eligibility_event" FROM ' + INGESTION
    )

    op.execute("DROP POLICY ingestion_rights_receipt_insert ON public.consumer_receipt")
    op.execute("DROP POLICY ingestion_rights_receipt_select ON public.consumer_receipt")
    op.execute("REVOKE INSERT ON TABLE public.consumer_receipt FROM " + INGESTION)
    op.execute(
        "REVOKE SELECT (id, created_at, workspace_id, outbox_event_id, consumer_name) "
        "ON TABLE public.consumer_receipt FROM " + INGESTION
    )

    op.execute("DROP POLICY ingestion_rights_event_update ON public.outbox_event")
    op.execute("DROP POLICY ingestion_rights_event_select ON public.outbox_event")
    op.execute("REVOKE SELECT ON TABLE public.outbox_event FROM " + INGESTION)
    op.execute(
        "REVOKE UPDATE (status, attempts, available_at, lease_owner, lease_expires_at, "
        f"completed_at, last_error_code) ON TABLE public.outbox_event FROM {INGESTION}"
    )
