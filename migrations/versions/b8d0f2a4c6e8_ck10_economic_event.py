"""CK-10: lịch sự kiện kinh tế do owner nhập (Tin & Lịch, điều kiện "Tin tức" của bảng Entry).

Revision ID: b8d0f2a4c6e8
Revises: a7c9e1b3d5f6

Bảng ``economic_event`` theo workspace, RLS bắt buộc. Admin-web chỉ SELECT/INSERT/UPDATE: xóa là
xóa mềm (``deleted_at``) để giữ lịch sử; mọi thêm/sửa/xóa ghi ``audit_event`` trong cùng
transaction. Runtime và market-signal chỉ đọc (để nối vào rule sau khi có quyết định/backtest).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d0f2a4c6e8"
down_revision: str | None = "a7c9e1b3d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "economic_event"
_READERS = ("crypidian_trading_runtime", "crypidian_market_signal_worker")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("impact", sa.String(length=8), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by_membership_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "impact IN ('LOW', 'MEDIUM', 'HIGH')", name="economic_event_impact_valid"
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="economic_event_title_present"),
        sa.CheckConstraint("region ~ '^[A-Z]{2,16}$'", name="economic_event_region_valid"),
        sa.CheckConstraint("revision >= 1", name="economic_event_revision_positive"),
        sa.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="economic_event_delete_after_create",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_economic_event")),
    )
    op.create_index("ix_economic_event_workspace_time", _TABLE, ["workspace_id", "event_at"])
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "{_TABLE}" USING ({policy}) WITH CHECK ({policy})'
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE economic_event TO crypidian_admin_web")
    op.execute("GRANT SELECT ON TABLE economic_event TO crypidian_trading_runtime")
    op.execute("GRANT SELECT ON TABLE economic_event TO crypidian_market_signal_worker")


def downgrade() -> None:
    for role in ("crypidian_admin_web", *_READERS):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {_TABLE} FROM {role}")
    op.execute(f'DROP POLICY workspace_isolation ON "{_TABLE}"')
    op.drop_index("ix_economic_event_workspace_time", table_name=_TABLE)
    op.drop_table(_TABLE)
