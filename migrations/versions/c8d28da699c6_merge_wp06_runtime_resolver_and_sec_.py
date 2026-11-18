"""merge wp06 runtime resolver and sec heads

Revision ID: c8d28da699c6
Revises: 778263f66bad, e9a1c3f5b7d2
Create Date: 2026-11-18 17:22:58.505705
"""

from collections.abc import Sequence

revision: str = "c8d28da699c6"
down_revision: str | None = ("778263f66bad", "e9a1c3f5b7d2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
