"""eudb export runs/items (ежедневный экспорт профиля 1 в ЕЭБД)

Revision ID: 07c9d271d656
Revises: 2ef55454e76f
Create Date: 2026-09-14 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07c9d271d656'
down_revision: Union[str, Sequence[str], None] = '2ef55454e76f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'eudb_export_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'status',
            sa.Enum('RUNNING', 'SUCCEEDED', 'PARTIAL_FAILURE', 'FAILED', name='eudb_export_run_status'),
            nullable=False,
            server_default='RUNNING',
        ),
        sa.Column('meters_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('meters_succeeded', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('meters_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('triggered_manually', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
    )

    op.create_table(
        'eudb_export_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('eudb_export_runs.id'), nullable=False),
        sa.Column('meter_id', sa.Integer(), sa.ForeignKey('meters.id'), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('rows_exported', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_eudb_export_items_run_id', 'eudb_export_items', ['run_id'])
    op.create_index('ix_eudb_export_items_meter_id', 'eudb_export_items', ['meter_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_eudb_export_items_meter_id', table_name='eudb_export_items')
    op.drop_index('ix_eudb_export_items_run_id', table_name='eudb_export_items')
    op.drop_table('eudb_export_items')
    op.drop_table('eudb_export_runs')
    sa.Enum(name='eudb_export_run_status').drop(op.get_bind(), checkfirst=True)
