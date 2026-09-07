"""meters: rated_current_amps (токовый класс, Imax)

Revision ID: a1c4e9f2b6d3
Revises: f8712d78dd15
Create Date: 2026-09-07 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4e9f2b6d3'
down_revision: Union[str, Sequence[str], None] = 'f8712d78dd15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('meters', sa.Column('rated_current_amps', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('meters', 'rated_current_amps')
