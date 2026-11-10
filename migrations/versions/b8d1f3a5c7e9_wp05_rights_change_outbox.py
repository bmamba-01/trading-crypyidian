"""WP05G.2: phát sự kiện thay đổi quyền nguồn cùng transaction ghi history."""

from collections.abc import Sequence

from alembic import op

revision: str = "b8d1f3a5c7e9"
down_revision: str | None = "f6b8d0e2a4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RIGHTS_EVENT_TYPE = "research.rights_changed.v1"


def upgrade() -> None:
    # Các hàm SECURITY DEFINER chỉ ghi metadata allowlist, dùng tên bảng đầy đủ
    # và cố định search_path để không thực thi đối tượng do caller tạo.
    op.execute(
        f"""
        CREATE FUNCTION public.emit_source_policy_rights_event() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            event_id uuid := pg_catalog.gen_random_uuid();
            identity_hash text := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    NEW.workspace_id::text || ':' || NEW.source_id || ':' || NEW.sequence::text,
                    'UTF8'
                )),
                'hex'
            );
        BEGIN
            INSERT INTO public.outbox_event (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                occurred_at, correlation_id, idempotency_key, schema_version, payload
            ) VALUES (
                event_id,
                NEW.workspace_id,
                '{_RIGHTS_EVENT_TYPE}',
                'source_policy_revision',
                NEW.id,
                pg_catalog.clock_timestamp(),
                NEW.id,
                'source-policy-revision:' || identity_hash,
                1,
                pg_catalog.jsonb_build_object(
                    'workspace_id', NEW.workspace_id,
                    'source_id', NEW.source_id,
                    'change_type', 'policy_revision',
                    'policy_sequence', NEW.sequence,
                    'policy_state', NEW.state,
                    'reason_code', 'policy_' || NEW.state
                )
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.emit_source_item_revision_rights_event() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            event_id uuid := pg_catalog.gen_random_uuid();
            identity_hash text := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    NEW.workspace_id::text || ':' || NEW.source_id || ':' ||
                    NEW.item_id || ':' || NEW.revision_id,
                    'UTF8'
                )),
                'hex'
            );
        BEGIN
            INSERT INTO public.outbox_event (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                occurred_at, correlation_id, idempotency_key, schema_version, payload
            ) VALUES (
                event_id,
                NEW.workspace_id,
                '{_RIGHTS_EVENT_TYPE}',
                'source_item_revision',
                NEW.id,
                pg_catalog.clock_timestamp(),
                NEW.id,
                'source-item-revision:' || identity_hash,
                1,
                pg_catalog.jsonb_build_object(
                    'workspace_id', NEW.workspace_id,
                    'source_id', NEW.source_id,
                    'item_id', NEW.item_id,
                    'change_type', CASE WHEN NEW.tombstoned
                        THEN 'item_tombstoned' ELSE 'item_revision' END,
                    'revision_id', NEW.revision_id,
                    'item_sequence', NEW.sequence
                )
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.emit_source_item_takedown_rights_event() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            event_id uuid := pg_catalog.gen_random_uuid();
            identity_hash text := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    NEW.workspace_id::text || ':' || NEW.source_id || ':' || NEW.item_id,
                    'UTF8'
                )),
                'hex'
            );
        BEGIN
            INSERT INTO public.outbox_event (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                occurred_at, correlation_id, idempotency_key, schema_version, payload
            ) VALUES (
                event_id,
                NEW.workspace_id,
                '{_RIGHTS_EVENT_TYPE}',
                'source_item_takedown',
                NEW.id,
                pg_catalog.clock_timestamp(),
                NEW.id,
                'source-item-takedown:' || identity_hash,
                1,
                pg_catalog.jsonb_build_object(
                    'workspace_id', NEW.workspace_id,
                    'source_id', NEW.source_id,
                    'item_id', NEW.item_id,
                    'change_type', 'item_takedown',
                    'reason_code', NEW.reason_code
                )
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.guard_source_rights_event_insert() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            emitter_owner name;
        BEGIN
            IF NEW.event_type = '{_RIGHTS_EVENT_TYPE}' THEN
                SELECT pg_catalog.pg_get_userbyid(proowner)
                  INTO emitter_owner
                  FROM pg_catalog.pg_proc
                 WHERE oid = 'public.emit_source_policy_rights_event()'::pg_catalog.regprocedure;
                IF emitter_owner IS NULL OR current_user <> emitter_owner THEN
                    RAISE EXCEPTION 'WP05_RIGHTS_EVENT_TRIGGER_ONLY';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_outbox_event_immutability() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'OUTBOX_EVENT_APPEND_ONLY';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.event_type IS DISTINCT FROM OLD.event_type
               OR NEW.aggregate_type IS DISTINCT FROM OLD.aggregate_type
               OR NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id
               OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
               OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
               OR NEW.payload IS DISTINCT FROM OLD.payload
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'OUTBOX_EVENT_PAYLOAD_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    op.execute(
        "CREATE TRIGGER source_policy_rights_outbox AFTER INSERT ON source_policy_revision "
        "FOR EACH ROW EXECUTE FUNCTION public.emit_source_policy_rights_event()"
    )
    op.execute(
        "CREATE TRIGGER source_item_revision_rights_outbox AFTER INSERT ON source_item_revision "
        "FOR EACH ROW EXECUTE FUNCTION public.emit_source_item_revision_rights_event()"
    )
    op.execute(
        "CREATE TRIGGER source_item_takedown_rights_outbox AFTER INSERT ON source_item_takedown "
        "FOR EACH ROW EXECUTE FUNCTION public.emit_source_item_takedown_rights_event()"
    )
    op.execute(
        "CREATE TRIGGER source_rights_event_insert_guard BEFORE INSERT ON outbox_event "
        "FOR EACH ROW EXECUTE FUNCTION public.guard_source_rights_event_insert()"
    )
    op.execute(
        "CREATE TRIGGER outbox_event_immutable_update BEFORE UPDATE OR DELETE ON outbox_event "
        "FOR EACH ROW EXECUTE FUNCTION public.enforce_outbox_event_immutability()"
    )

    for function in (
        "emit_source_policy_rights_event()",
        "emit_source_item_revision_rights_event()",
        "emit_source_item_takedown_rights_event()",
        "guard_source_rights_event_insert()",
        "enforce_outbox_event_immutability()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER outbox_event_immutable_update ON outbox_event")
    op.execute("DROP TRIGGER source_rights_event_insert_guard ON outbox_event")
    op.execute("DROP TRIGGER source_item_takedown_rights_outbox ON source_item_takedown")
    op.execute("DROP TRIGGER source_item_revision_rights_outbox ON source_item_revision")
    op.execute("DROP TRIGGER source_policy_rights_outbox ON source_policy_revision")
    op.execute("DROP FUNCTION public.enforce_outbox_event_immutability()")
    op.execute("DROP FUNCTION public.guard_source_rights_event_insert()")
    op.execute("DROP FUNCTION public.emit_source_item_takedown_rights_event()")
    op.execute("DROP FUNCTION public.emit_source_item_revision_rights_event()")
    op.execute("DROP FUNCTION public.emit_source_policy_rights_event()")
