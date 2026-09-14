"""meter_status: invalid убран, чистка мусорных записей

Revision ID: a5c7d9f1b3e2
Revises: 07c9d271d656
Create Date: 2026-09-14

По прямому указанию пользователя (2026-09-14) — статус
MeterStatus.INVALID и вкладка «Некорректные данные» в UI убраны:
счётчик с повреждённым при call-home обнаружении серийным номером
(буквы вместо цифр) теперь отклоняется Gateway'ем ещё на этапе
опознания (см. CallHomePool._identify, callhome.py) и вообще не
доходит до Backend, поэтому статус для них больше не нужен.

На момент миграции в БД уже было 15 таких записей (созданы до фикса
в Gateway, все is_active=false, серийники — мусор от старого бага).
После удаления MeterStatus.INVALID из Python-enum (models.py) чтение
этих строк роняет SQLAlchemy с LookupError — оставлять их нельзя.
История этих счётчиков — 761 запись в jobs (только failed-попытки
чтения мусорного адреса) и 12 в eudb_export_items (неуспешные попытки
экспорта) — не несёт аудиторской ценности, удаляется вместе с ними по
прямому указанию пользователя. Остальные таблицы с FK на meters
(event_log, meter_status_snapshots, tamper_log, meter_readings,
parameter_write_history, load_profile_data, notifications,
disconnect_batch_items) на момент миграции для этих 15 счётчиков пусты
— ожидаемо, счётчики ни разу не были активны.

Postgres не позволяет удалить значение из enum-типа (тот же случай,
что и в b3f4a1d29e7c/f8712d78dd15) — значение 'INVALID' остаётся в
типе meter_status на уровне БД, но раз строк с этим значением больше
нет, это не мешает работе приложения.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a5c7d9f1b3e2'
down_revision: Union[str, Sequence[str], None] = '07c9d271d656'
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
