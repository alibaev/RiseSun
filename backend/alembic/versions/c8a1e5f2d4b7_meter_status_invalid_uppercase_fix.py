"""meter_status: добавить 'INVALID' (исправление регистра)

Revision ID: c8a1e5f2d4b7
Revises: b3f4a1d29e7c
Create Date: 2026-09-08

Предыдущая миграция (b3f4a1d29e7c) добавила в тип 'invalid' строчными
буквами — SQLAlchemy Enum(MeterStatus, name="meter_status") на самом
деле хранит ИМЯ члена Python-enum (INSTALLED, ACTIVE), а не .value,
поэтому чтение строки со status='invalid' падало с LookupError сразу
после первого деплоя. ADD VALUE нельзя использовать в той же
транзакции, где значение добавлено — отсюда отдельная миграция, а не
правка предыдущей (та уже применена на этом окружении).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8a1e5f2d4b7'
down_revision: Union[str, Sequence[str], None] = 'b3f4a1d29e7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE meter_status ADD VALUE IF NOT EXISTS 'INVALID'")


def downgrade() -> None:
    pass
