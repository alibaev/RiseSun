"""obis_reference_entries: полный каталог объектов по 3 моделям DTZY217

Revision ID: d4f6b8c1e3a9
Revises: c8a1e5f2d4b7
Create Date: 2026-09-10 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f6b8c1e3a9'
down_revision: Union[str, Sequence[str], None] = 'c8a1e5f2d4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'obis_reference_entries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('meter_model', sa.String(length=32), nullable=False),
        sa.Column('class_name', sa.String(length=64), nullable=False),
        sa.Column('class_id', sa.Integer(), nullable=True),
        sa.Column('logical_name', sa.String(length=32), nullable=False),
        sa.Column('description', sa.String(length=512), nullable=True),
        sa.Column('version', sa.Integer(), nullable=True),
        sa.Column('scaler', sa.Integer(), nullable=True),
        sa.Column('unit', sa.Integer(), nullable=True),
        sa.UniqueConstraint('meter_model', 'logical_name', name='uq_obis_reference_entry'),
    )
    op.create_index('ix_obis_reference_entries_meter_model', 'obis_reference_entries', ['meter_model'])
    op.create_index('ix_obis_reference_entries_logical_name', 'obis_reference_entries', ['logical_name'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_obis_reference_entries_logical_name', table_name='obis_reference_entries')
    op.drop_index('ix_obis_reference_entries_meter_model', table_name='obis_reference_entries')
    op.drop_table('obis_reference_entries')
