"""WP05C: thời điểm DB ghi nhận và URL metadata fail-closed."""

from collections.abc import Sequence

from alembic import op

revision: str = "d0f2a4c6e8b1"
down_revision: str | None = "c9e1f3a5b7d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Không suy diễn thời gian tri thức cũ từ timestamp caller từng được phép
    # khai báo: hàng có sẵn chỉ được biết từ lúc migration backfill cột mới.
    op.execute(
        "ALTER TABLE public.source_item_revision ADD COLUMN recorded_at timestamptz "
        "NOT NULL DEFAULT clock_timestamp()"
    )
    op.execute(
        """
        CREATE FUNCTION public.stamp_source_item_recording() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.recorded_at := clock_timestamp();
            NEW.created_at := NEW.recorded_at;
            RETURN NEW;
        END;
        $$
        """
    )
    # Trigger cùng loại chạy theo tên; timestamp được đóng sau append guard đã
    # lấy source lock, tránh đóng dấu trước khi chờ writer khác hoàn tất.
    op.execute(
        "CREATE TRIGGER source_item_revision_recorded BEFORE INSERT ON public.source_item_revision "
        "FOR EACH ROW EXECUTE FUNCTION public.stamp_source_item_recording()"
    )
    op.execute("REVOKE ALL ON FUNCTION public.stamp_source_item_recording() FROM PUBLIC")
    op.execute(
        """
        CREATE FUNCTION public.source_item_url_valid(value text) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE
            remainder text;
            authority text;
            suffix text;
            hostname text;
            port_text text := '';
            parts text[];
            label text;
            closing integer;
            address inet;
        BEGIN
            IF left(value, 8) <> 'https://' THEN RETURN false; END IF;
            remainder := substring(value FROM 9);
            authority := substring(remainder FROM '^[^/?]*');
            suffix := substring(remainder FROM length(authority) + 1);
            IF authority = '' OR authority <> lower(authority)
               OR suffix COLLATE "C" !~ '^[A-Za-z0-9._~!$&''()*+,;=:@/?%-]*$'
               OR position('%' IN regexp_replace(value, '%[0-9A-Fa-f]{2}', '', 'g')) > 0
            THEN RETURN false; END IF;

            IF left(authority, 1) = '[' THEN
                closing := position(']' IN authority);
                IF closing < 3 THEN RETURN false; END IF;
                hostname := substring(authority FROM 2 FOR closing - 2);
                port_text := substring(authority FROM closing + 1);
                IF port_text <> '' THEN
                    IF left(port_text, 1) <> ':' THEN RETURN false; END IF;
                    port_text := substring(port_text FROM 2);
                    IF port_text = '' THEN RETURN false; END IF;
                END IF;
                IF hostname COLLATE "C" !~ '^[0-9a-f:.]+$' THEN RETURN false; END IF;
                address := hostname::inet;
                IF family(address) <> 6 THEN RETURN false; END IF;
                IF position('.' IN hostname) > 0 THEN
                    parts := string_to_array(regexp_replace(hostname, '^.*:', ''), '.');
                    IF cardinality(parts) <> 4 THEN RETURN false; END IF;
                    FOREACH label IN ARRAY parts LOOP
                        IF label COLLATE "C" !~ '^(0|[1-9][0-9]{0,2})$'
                           OR label::integer > 255 THEN RETURN false; END IF;
                    END LOOP;
                END IF;
            ELSE
                parts := string_to_array(authority, ':');
                IF cardinality(parts) > 2 THEN RETURN false; END IF;
                hostname := parts[1];
                IF cardinality(parts) = 2 THEN
                    port_text := parts[2];
                    IF port_text = '' THEN RETURN false; END IF;
                END IF;
                IF hostname = '' OR length(hostname) > 253 THEN RETURN false; END IF;
                FOREACH label IN ARRAY string_to_array(hostname, '.') LOOP
                    IF label COLLATE "C" !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'
                    THEN RETURN false; END IF;
                END LOOP;
            END IF;
            IF port_text <> '' AND (
                length(port_text) > 128 OR port_text COLLATE "C" !~ '^[0-9]+$'
            ) THEN RETURN false; END IF;
            IF port_text <> '' AND (port_text::numeric < 1 OR port_text::numeric > 65535)
            THEN RETURN false; END IF;
            RETURN true;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN false;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.source_item_url_valid(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.source_item_url_valid(text) TO crypidian_ingestion_worker"
    )
    # Validate cả hàng có sẵn; metadata cũ sai phải làm upgrade fail, không sửa
    # lén URL hoặc hash để che lịch sử đã từng ghi sai.
    op.execute(
        "ALTER TABLE public.source_item_revision ADD CONSTRAINT "
        "ck_source_item_revision_source_item_url_domain_valid "
        "CHECK (public.source_item_url_valid(canonical_url))"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE public.source_item_revision IN ACCESS EXCLUSIVE MODE")
    op.execute("ALTER TABLE public.source_item_revision DISABLE ROW LEVEL SECURITY")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.source_item_revision) THEN "
        "RAISE EXCEPTION 'WP05_ITEM_HISTORY_DOWNGRADE_BLOCKED'; END IF; END $$"
    )
    op.execute("ALTER TABLE public.source_item_revision ENABLE ROW LEVEL SECURITY")
    op.execute(
        "ALTER TABLE public.source_item_revision DROP CONSTRAINT "
        "ck_source_item_revision_source_item_url_domain_valid"
    )
    op.execute("DROP FUNCTION public.source_item_url_valid(text)")
    op.execute("DROP TRIGGER source_item_revision_recorded ON public.source_item_revision")
    op.execute("DROP FUNCTION public.stamp_source_item_recording()")
    op.execute("ALTER TABLE public.source_item_revision DROP COLUMN recorded_at")
