"""WP05C: timestamp metadata chỉ nằm trong miền datetime UTC của Python."""

from collections.abc import Sequence

from alembic import op

revision: str = "d1a3c5e7f9b2"
down_revision: str | None = "d0f2a4c6e8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIELDS = {
    "source_item_revision": (
        ("recorded_at", False),
        ("created_at", False),
        ("observed_at", False),
        ("available_at", True),
        ("event_at", True),
        ("published_at", True),
        ("edited_at", True),
        ("retention_expires_at", True),
    ),
    "source_item_takedown": (("taken_down_at", False),),
}


def _name(table: str, field: str) -> str:
    prefix = "sir" if table == "source_item_revision" else "sit"
    return f"ck_{table}_{prefix}_{field}_pytime"


def upgrade() -> None:
    # ADD CHECK validate mọi hàng cũ trong cùng transaction; không NOT VALID,
    # không sửa thời điểm hoặc hash. Row sai làm upgrade fail với tên cột rõ.
    for table, fields in _FIELDS.items():
        for field, nullable in fields:
            predicate = (
                f"isfinite({field}) AND {field} >= TIMESTAMPTZ '0001-01-01 00:00:00+00' "
                f"AND {field} < TIMESTAMPTZ '10000-01-01 00:00:00+00'"
            )
            if nullable:
                predicate = f"{field} IS NULL OR ({predicate})"
            op.execute(
                f'ALTER TABLE public."{table}" ADD CONSTRAINT "{_name(table, field)}" '
                f"CHECK ({predicate})"
            )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.source_item_revision, public.source_item_takedown "
        "IN ACCESS EXCLUSIVE MODE"
    )
    for table in _FIELDS:
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.source_item_revision) "
        "OR EXISTS (SELECT 1 FROM public.source_item_takedown) THEN "
        "RAISE EXCEPTION 'WP05_ITEM_HISTORY_DOWNGRADE_BLOCKED'; END IF; END $$"
    )
    for table, fields in _FIELDS.items():
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        for field, _ in fields:
            op.execute(f'ALTER TABLE public."{table}" DROP CONSTRAINT "{_name(table, field)}"')
