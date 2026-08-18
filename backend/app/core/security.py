"""Пароли пользователей — bcrypt (ТЗ п. 4.5). Секреты счётчиков (пароль
доступа, AES-ключ канала) — Fernet-шифрование в покое, отдельно от
пользовательских паролей (Promt_MMWS.md, раздел 3, принцип 4; раздел 5,
гвард «пароли пользователей системы хешируются... это отдельно от
шифрования секретов счётчиков, не подменяет его»).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from jose import jwt
from passlib.context import CryptContext

from ..config import settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_fernet = Fernet(settings.secret_encryption_key.encode())


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd_context.verify(password, password_hash)


def encrypt_secret(plaintext: bytes) -> bytes:
    return _fernet.encrypt(plaintext)


def decrypt_secret(ciphertext: bytes) -> bytes:
    return _fernet.decrypt(ciphertext)


def create_access_token(*, subject: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_token_ttl_minutes)
    payload = {"sub": subject, "role": role, "type": "access", "exp": expire}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(*, subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_ttl_days)
    payload = {"sub": subject, "type": "refresh", "exp": expire}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
