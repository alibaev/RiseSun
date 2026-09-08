"""meter_status: invalid (некорректные данные)

Revision ID: b3f4a1d29e7c
Revises: 7cce92c9a67d
Create Date: 2026-09-08

Счётчики с повреждённым при call-home обнаружении серийным номером
(буквы вместо цифр — addressing.py не может вычислить физический
адрес, чтение падает с ADDRESSING_ERROR на каждой попытке) переводятся
в отдельный статус вместо ACTIVE/INSTALLED — вкладка «Некорректные
данные» в UI (см. MeterStatus.INVALID, models.py).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3f4a1d29e7c'
down_revision: Union[str, Sequence[str], None] = '7cce92c9a67d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy Enum(MeterStatus, name="meter_status") хранит в БД ИМЯ
    # члена Python-enum (INSTALLED, ACTIVE — заглавными), а не .value
    # ("installed"/"active", как можно ошибочно предположить по строкам
    # схемы) — поэтому новое значение тоже должно быть заглавным,
    # иначе SQLAlchemy падает с LookupError при чтении строки (найдено
    # 2026-09-08 сразу после первого деплоя этой миграции).
    op.execute("ALTER TYPE meter_status ADD VALUE IF NOT EXISTS 'INVALID'")


def downgrade() -> None:
    # Postgres не поддерживает удаление значения enum — откат недоступен,
    # тот же подход, что и в f8712d78dd15 (meter_status: installed/active).
    pass
