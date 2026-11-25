"""CK-9: heartbeat bền cho ingestion-worker và quyền đọc Admin."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0095c0fdba8f"
down_revision: str | None = "a3c7e9f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = ("f0a2c4e6b8e0",)

_TABLE = "worker_heartbeat"
_WRITER = "crypidian_ingestion_worker"
_READER = "crypidian_admin_web"


def _resolve_admin_role() -> str:
    has_safe: bool = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'crypidian_admin_web_safe'"
                ")"
            )
        )
        .scalar_one()
    )
    return "crypidian_admin_web_safe" if has_safe else _READER


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("component", sa.String(length=80), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=120), nullable=True),
        sa.Column("detail_vi", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "counters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('ready', 'degraded', 'not_ready')", name="worker_heartbeat_state_valid"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(counters) = 'object'", name="worker_heartbeat_counters_object"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "workspace_id", "component", "instance_id", name=op.f("pk_worker_heartbeat")
        ),
    )
    op.create_index(
        "ix_worker_heartbeat_latest",
        _TABLE,
        ["workspace_id", "component", "heartbeat_at"],
    )
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "{_TABLE}" USING ({policy}) WITH CHECK ({policy})'
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {_TABLE} TO {_WRITER}")
    op.execute(f"GRANT SELECT ON TABLE {_TABLE} TO {_resolve_admin_role()}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {_TABLE} FROM {_WRITER}")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {_TABLE} FROM {_resolve_admin_role()}")
    op.execute(f'DROP POLICY workspace_isolation ON "{_TABLE}"')
    op.drop_index("ix_worker_heartbeat_latest", table_name=_TABLE)
    op.drop_table(_TABLE)
