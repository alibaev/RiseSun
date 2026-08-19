"""Права доступа по ролям (ТЗ п. 4.2.1, Приложение Б TABLE 0).

Роли не образуют строгую линейную иерархию: «Наблюдатель» видит те же
данные, что и «Оператор», но, в отличие от него, не может запускать
операции чтения — поэтому права заданы явными наборами по роли, а не
сравнением уровней.
"""

from __future__ import annotations

import enum

from ..models import UserRole


class Permission(str, enum.Enum):
    VIEW_METERS = "view_meters"
    TRIGGER_READ = "trigger_read"
    WRITE_PARAMETER = "write_parameter"  # Этап 2, модель заведена заранее
    MANAGE_METERS = "manage_meters"
    MANAGE_USERS = "manage_users"
    MANAGE_GATEWAYS = "manage_gateways"
    VIEW_AUDIT_LOG = "view_audit_log"
    # Этап 4 (ТЗ Приложение Б TABLE 0): «Инженер» лишь ПРИМЕНЯЕТ схемы
    # (в том числе массово — уже покрыто WRITE_PARAMETER), а вот
    # создание/редактирование/удаление схем и расписаний автоопроса —
    # explicitly в перечне прав «Администратора», не «Инженера».
    MANAGE_PARAMETER_SCHEMES = "manage_parameter_schemes"
    MANAGE_SCHEDULED_JOBS = "manage_scheduled_jobs"


_ROLE_PERMISSIONS: dict[UserRole, set[Permission]] = {
    UserRole.OBSERVER: {Permission.VIEW_METERS},
    UserRole.OPERATOR: {Permission.VIEW_METERS, Permission.TRIGGER_READ},
    UserRole.ENGINEER: {
        Permission.VIEW_METERS,
        Permission.TRIGGER_READ,
        Permission.WRITE_PARAMETER,
    },
    UserRole.ADMIN: {
        Permission.VIEW_METERS,
        Permission.TRIGGER_READ,
        Permission.WRITE_PARAMETER,
        Permission.MANAGE_METERS,
        Permission.MANAGE_USERS,
        Permission.VIEW_AUDIT_LOG,
        Permission.MANAGE_PARAMETER_SCHEMES,
        Permission.MANAGE_SCHEDULED_JOBS,
    },
    UserRole.SUPER_ADMIN: {
        Permission.VIEW_METERS,
        Permission.TRIGGER_READ,
        Permission.WRITE_PARAMETER,
        Permission.MANAGE_METERS,
        Permission.MANAGE_USERS,
        Permission.VIEW_AUDIT_LOG,
        Permission.MANAGE_GATEWAYS,
        Permission.MANAGE_PARAMETER_SCHEMES,
        Permission.MANAGE_SCHEDULED_JOBS,
    },
}


def role_has_permission(role: UserRole, permission: Permission) -> bool:
    return permission in _ROLE_PERMISSIONS.get(role, set())
