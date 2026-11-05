"""WP-SC.4: Lưu trữ Strategy Card bất biến, có phiên bản và tenant-scoped (QUANTSMART).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-05 11:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "strategy_cards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("card_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_strategy_cards_content_hash_hex",
        ),
        sa.CheckConstraint(
            "length(card_id) > 0 AND length(version) > 0 AND length(name) > 0",
            name="ck_strategy_cards_identifiers_present",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(card_payload) = 'object'",
            name="ck_strategy_cards_payload_object",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_cards")),
        sa.UniqueConstraint(
            "workspace_id",
            "card_id",
            "version",
            name="uq_strategy_cards_workspace_card_version",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "content_hash",
            name="uq_strategy_cards_workspace_content_hash",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_strategy_cards_workspace_id"),
    )
    op.create_index(
        "ix_strategy_cards_workspace_active",
        "strategy_cards",
        ["workspace_id", "is_active"],
    )
    op.create_index(
        "ix_strategy_cards_workspace_card_id",
        "strategy_cards",
        ["workspace_id", "card_id"],
    )
    op.create_index(
        "ix_strategy_cards_content_hash",
        "strategy_cards",
        ["content_hash"],
    )

    op.execute('ALTER TABLE "strategy_cards" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "strategy_cards" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "strategy_cards" '
        f"TO {ADMIN} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE strategy_cards FROM PUBLIC, {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE strategy_cards TO {ADMIN}")


def downgrade() -> None:
    op.execute('DROP POLICY workspace_isolation ON "strategy_cards"')
    op.execute('ALTER TABLE "strategy_cards" DISABLE ROW LEVEL SECURITY')
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE strategy_cards FROM PUBLIC, {ADMIN}")
    op.drop_index("ix_strategy_cards_content_hash", table_name="strategy_cards")
    op.drop_index("ix_strategy_cards_workspace_card_id", table_name="strategy_cards")
    op.drop_index("ix_strategy_cards_workspace_active", table_name="strategy_cards")
    op.drop_table("strategy_cards")
