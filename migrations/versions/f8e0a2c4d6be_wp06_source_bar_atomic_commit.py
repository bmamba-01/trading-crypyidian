"""WP06: chỉ nhận source-bar lineage cùng transaction tạo BotDecision."""

from collections.abc import Sequence

from alembic import op

revision: str = "f8e0a2c4d6be"
down_revision: str | None = "f8e0a2c4d6bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Đóng membership lineage để decision đã commit không thể nhận bar muộn."""
    # Khóa trước cả hai bảng để DDL không chen giữa các lần INSERT decision/source bar.
    # NOWAIT giúp migration dừng và retry thay vì giữ nửa bộ khóa trong runtime.
    op.execute(
        "LOCK TABLE public.bot_decision, public.bot_decision_source_bar "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    op.execute("ALTER TABLE public.bot_decision ADD COLUMN source_bar_created_xid xid8")
    op.execute(
        """CREATE FUNCTION public.wp06_stamp_bot_decision_source_bar_xid()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            NEW.source_bar_created_xid := pg_catalog.pg_current_xact_id();
            RETURN NEW;
        END; $$"""
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_stamp_bot_decision_source_bar_xid() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER zz_wp06_stamp_bot_decision_source_bar_xid "
        "BEFORE INSERT ON public.bot_decision FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_stamp_bot_decision_source_bar_xid()"
    )
    op.execute(
        """CREATE FUNCTION public.wp06_require_source_bar_parent_transaction()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE parent_xid xid8;
        BEGIN
            SELECT decision.source_bar_created_xid
              INTO parent_xid
              FROM public.bot_decision AS decision
             WHERE decision.workspace_id = NEW.workspace_id
               AND decision.bot_id = NEW.bot_id
               AND decision.id = NEW.bot_decision_id;
            IF NOT FOUND
               OR parent_xid IS DISTINCT FROM pg_catalog.pg_current_xact_id() THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_PARENT_TRANSACTION_MISMATCH';
            END IF;
            RETURN NEW;
        END; $$"""
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_require_source_bar_parent_transaction() FROM PUBLIC"
    )
    # Tên trigger đặt trước `..._validate`: lineage cũ bị từ chối trước khi đọc bar/evidence.
    op.execute(
        "CREATE TRIGGER bot_decision_source_bar_parent_transaction "
        "BEFORE INSERT ON public.bot_decision_source_bar FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_require_source_bar_parent_transaction()"
    )


def downgrade() -> None:
    """Gỡ dấu transaction và trigger sau khi lấy đồng thời cả hai bảng."""
    # Nếu writer đang hoạt động, dừng downgrade ngay để người vận hành retry an toàn.
    op.execute(
        "LOCK TABLE public.bot_decision, public.bot_decision_source_bar "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    op.execute(
        "DROP TRIGGER bot_decision_source_bar_parent_transaction ON public.bot_decision_source_bar"
    )
    op.execute("DROP FUNCTION public.wp06_require_source_bar_parent_transaction()")
    op.execute("DROP TRIGGER zz_wp06_stamp_bot_decision_source_bar_xid ON public.bot_decision")
    op.execute("DROP FUNCTION public.wp06_stamp_bot_decision_source_bar_xid()")
    op.execute("ALTER TABLE public.bot_decision DROP COLUMN source_bar_created_xid")
