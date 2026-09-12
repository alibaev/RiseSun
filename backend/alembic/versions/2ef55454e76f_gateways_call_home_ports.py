"""gateways_call_home_ports

Revision ID: 2ef55454e76f
Revises: a3c6f091b2d4
Create Date: 2026-09-12 12:37:25.848486

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '2ef55454e76f'
down_revision: Union[str, Sequence[str], None] = 'a3c6f091b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("gateways", sa.Column("call_home_ports", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("gateways", "call_home_ports")
