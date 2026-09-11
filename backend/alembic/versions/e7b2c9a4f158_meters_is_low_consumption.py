"""meters: is_low_consumption (малое потребление)

Revision ID: e7b2c9a4f158
Revises: d4f6b8c1e3a9
Create Date: 2026-09-11 03:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b2c9a4f158'
down_revision: Union[str, Sequence[str], None] = 'd4f6b8c1e3a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'meters',
        sa.Column('is_low_consumption', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('meters', 'is_low_consumption')
