"""merge dev and wp06-followthrough heads

Revision ID: ab26dde001df
Revises: c8e1a2b4d9f0, f0a2c4e6b8e0
Create Date: 2026-11-25 10:58:30.281735
"""

from collections.abc import Sequence

revision: str = "ab26dde001df"
down_revision: str | None = ("c8e1a2b4d9f0", "f0a2c4e6b8e0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
