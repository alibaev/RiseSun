"""meter_status invalid: чистка новых мусорных записей после Gateway-фикса

Revision ID: c9c50b32f184
Revises: a5c7d9f1b3e2
Create Date: 2026-09-17 12:45:31.304941

Миграция a5c7d9f1b3e2 (2026-09-14) удалила 15 счётчиков со статусом
INVALID и убрала это значение из Python-enum MeterStatus, но сама
проблема на call-home была устранена в Gateway (CallHomePool._identify,
callhome.py) отдельным изменением, которое доехало до прод-инстанса
позже. В промежутке (2026-09-16) Gateway успел завести ещё 6 счётчиков
с тем же мусорным статусом INVALID и поломанными серийниками — с тех
пор чтение /meters роняло backend с LookupError, потому что в
Python-enum значения INVALID уже нет.

Как и в a5c7d9f1b3e2: все 6 записей с is_active=false, без единой
связанной строки в jobs/eudb_export_items/event_log/meter_status_snapshots/
tamper_log/meter_readings/parameter_write_history/load_profile_data/
notifications/disconnect_batch_items (проверено перед миграцией) —
удаляются без потери истории.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9c50b32f184'
down_revision: Union[str, Sequence[str], None] = 'a5c7d9f1b3e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM jobs WHERE meter_id IN (SELECT id FROM meters WHERE status = 'INVALID')"
    )
    op.execute(
        "DELETE FROM eudb_export_items WHERE meter_id IN "
        "(SELECT id FROM meters WHERE status = 'INVALID')"
    )
    op.execute("DELETE FROM meters WHERE status = 'INVALID'")


def downgrade() -> None:
    # Удалённые мусорные записи не восстанавливаются — тот же принцип,
    # что и у прочих необратимых enum-миграций в этом проекте.
    pass
