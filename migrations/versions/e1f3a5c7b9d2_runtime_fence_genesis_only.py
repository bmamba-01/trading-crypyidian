"""MB-05 Part B: đóng cửa sổ fence tùy chọn ở DB — checkpoint NULL token chỉ còn cho genesis.

Revision ID: e1f3a5c7b9d2
Revises: e4f6a8c0d2b5

Part A cho phép `paper_execution_checkpoint.fencing_token IS NULL` khi bot chưa từng có hàng lease
(`transitional_null`) để runtime chưa có registry vẫn chạy. Từ MB05.5 registry luôn lấy lease khi
bootstrap nên không runtime writer nào ghi mà không token; NULL chỉ còn hợp lệ cho checkpoint
**genesis** (revision 1) do seed/khởi tạo account ghi trước lần chạy runtime đầu
(`genesis_null`). Trigger state/incident giữ `strict`. Không đổi grant/RLS.

Hàm trigger vẫn SECURITY INVOKER, `search_path` pin, revoke PUBLIC. Lease được đọc dưới RLS của
chính writer: policy ghi checkpoint trùng policy đọc lease (ghim bằng unit test M4), và nhánh
NULL chỉ nhận revision 1 (unique theo account) nên hàng lease bị ẩn cũng không mở được ghi NULL
ở revision sau.
"""

from alembic import op

revision = "e1f3a5c7b9d2"
down_revision = "e4f6a8c0d2b5"
branch_labels = None
depends_on = None

_FUNCTION = """CREATE OR REPLACE FUNCTION public.mb05_require_runtime_fence() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE v_token bigint; v_expires timestamptz;
    BEGIN
        IF NEW.fencing_token IS NULL THEN
            IF {null_condition} AND NOT EXISTS (
                SELECT 1 FROM public.bot_runtime_lease l
                WHERE l.workspace_id = NEW.workspace_id AND l.bot_id = NEW.bot_id) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'MB05_FENCING_TOKEN_REQUIRED';
        END IF;
        SELECT l.fencing_token, l.expires_at INTO v_token, v_expires
        FROM public.bot_runtime_lease l
        WHERE l.workspace_id = NEW.workspace_id AND l.bot_id = NEW.bot_id
        FOR SHARE;
        IF NOT FOUND OR v_token IS DISTINCT FROM NEW.fencing_token THEN
            RAISE EXCEPTION 'MB05_STALE_FENCING_TOKEN';
        END IF;
        IF v_expires <= pg_catalog.clock_timestamp() THEN
            RAISE EXCEPTION 'MB05_LEASE_EXPIRED';
        END IF;
        RETURN NEW;
    END $$"""
# Genesis: revision 1 của account (checkpoint đầu tiên) và bot chưa từng có lease.
_GENESIS = "TG_ARGV[0] = 'genesis_null' AND NEW.revision = 1"
_TRANSITIONAL = "TG_ARGV[0] = 'transitional_null'"


def _replace_checkpoint_trigger(mode: str) -> None:
    op.execute('DROP TRIGGER "paper_execution_checkpoint_fence" ON "paper_execution_checkpoint"')
    op.execute(
        'CREATE TRIGGER "paper_execution_checkpoint_fence" BEFORE INSERT '
        'ON "paper_execution_checkpoint" FOR EACH ROW '
        f"EXECUTE FUNCTION public.mb05_require_runtime_fence('{mode}')"
    )


def upgrade() -> None:
    op.execute(_FUNCTION.format(null_condition=_GENESIS))
    op.execute("REVOKE ALL ON FUNCTION public.mb05_require_runtime_fence() FROM PUBLIC")
    _replace_checkpoint_trigger("genesis_null")


def downgrade() -> None:
    # Nới lại về ngữ nghĩa Part A (không có dữ liệu nào bị mất); không cần guard dữ liệu.
    op.execute(_FUNCTION.format(null_condition=_TRANSITIONAL))
    op.execute("REVOKE ALL ON FUNCTION public.mb05_require_runtime_fence() FROM PUBLIC")
    _replace_checkpoint_trigger("transitional_null")
