"""merge wp06 runtime resolver and dev heads

Revision ID: 778263f66bad
Revises: a9c7e5f3d1b2, e1f3a5b7c9d1
Create Date: 2026-11-18 16:19:34.785668
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '778263f66bad'
down_revision: Union[str, None] = ('a9c7e5f3d1b2', 'e1f3a5b7c9d1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
