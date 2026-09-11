"""meters: res_name (РЭС/объект по call-home порту)

Revision ID: a3c6f091b2d4
Revises: e7b2c9a4f158
Create Date: 2026-09-11 08:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c6f091b2d4'
down_revision: Union[str, Sequence[str], None] = 'e7b2c9a4f158'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('meters', sa.Column('res_name', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('meters', 'res_name')
