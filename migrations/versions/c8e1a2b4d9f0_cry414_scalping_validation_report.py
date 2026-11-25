"""CRY-414: lưu báo cáo kiểm định scalping SIG-5 vào DB theo workspace."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e1a2b4d9f0"
down_revision: str | None = "b7d2e4f8c1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scalping_validation_report"
_ADMIN_ROLE = "crypidian_admin_web"


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
    return "crypidian_admin_web_safe" if has_safe else _ADMIN_ROLE


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.String(length=64), nullable=False),
        sa.Column("release_content_hash", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("costs_version", sa.String(length=64), nullable=False),
        sa.Column("evaluation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recommend_paper", sa.Boolean(), nullable=False),
        sa.Column("out_of_sample_n", sa.Integer(), nullable=False),
        sa.Column("stress_expectancy_r", sa.String(length=32), nullable=True),
        sa.Column("validation_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "report_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(report_payload) = 'object'",
            name="scalping_validation_payload_object",
        ),
        sa.CheckConstraint(
            "out_of_sample_n >= 0",
            name="scalping_validation_oos_non_negative",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_scalping_validation_report"),
    )
    op.create_index(
        "ux_scalping_validation_hash",
        _TABLE,
        ["workspace_id", "validation_hash"],
        unique=True,
    )
    op.create_index(
        "ix_scalping_validation_lookup",
        _TABLE,
        ["workspace_id", "release_id", "symbol", "created_at"],
    )
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "{_TABLE}" USING ({policy}) WITH CHECK ({policy})'
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {_TABLE} TO {_resolve_admin_role()}")


def downgrade() -> None:
    op.drop_index("ix_scalping_validation_lookup", table_name=_TABLE)
    op.drop_index("ux_scalping_validation_hash", table_name=_TABLE)
    op.drop_table(_TABLE)
