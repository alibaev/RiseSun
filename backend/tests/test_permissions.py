import os

os.environ.setdefault("MMWS_DATABASE_URL", "postgresql+asyncpg://mmws:mmws@localhost:5432/mmws_test")

from app.core.permissions import Permission, role_has_permission
from app.models import UserRole


def test_observer_can_view_but_not_trigger_read():
    # ТЗ TABLE 0: «Наблюдатель — только просмотр, без права запуска
    # каких-либо операций записи или чтения».
    assert role_has_permission(UserRole.OBSERVER, Permission.VIEW_METERS) is True
    assert role_has_permission(UserRole.OBSERVER, Permission.TRIGGER_READ) is False


def test_operator_can_trigger_read_but_not_write():
    assert role_has_permission(UserRole.OPERATOR, Permission.TRIGGER_READ) is True
    assert role_has_permission(UserRole.OPERATOR, Permission.WRITE_PARAMETER) is False


def test_engineer_can_write_but_not_manage_gateways():
    assert role_has_permission(UserRole.ENGINEER, Permission.WRITE_PARAMETER) is True
    assert role_has_permission(UserRole.ENGINEER, Permission.MANAGE_GATEWAYS) is False


def test_admin_cannot_manage_gateways():
    # ТЗ п. 4.1.1: регистрация/подтверждение Gateway — исключительно
    # Супер-администратор, даже «Администратор» этого не может.
    assert role_has_permission(UserRole.ADMIN, Permission.MANAGE_GATEWAYS) is False
    assert role_has_permission(UserRole.ADMIN, Permission.MANAGE_USERS) is True


def test_only_super_admin_can_manage_gateways():
    assert role_has_permission(UserRole.SUPER_ADMIN, Permission.MANAGE_GATEWAYS) is True
